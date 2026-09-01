"""交易服务：决策循环 + 运行控制 + 纸面执行，供 CLI 与 Web 共用。"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase
from audit.jsonl_logger import JsonlLogger
from agents.models import RiskVerdict, TradeDecision
from core.agent_recorder import AgentRecorder
from core.config_loader import Settings
from core.control import ControlState
from core.graph import build_graph
from llm.factory import build_llm_for_agent
from exchange.factory import create_broker
from core.capital import CapitalManager
from core.channels import ChannelManager
from data.market_universe import MarketUniverse
from data.market_query import get_live_prices
from core.intel_cache import IntelCache
from core.quotas import TradeQuotaManager
from llm.base import LLMClient
from llm.mock_client import MockLLMClient


def build_execution_feedback(state: dict[str, Any], decision: Any,
                             channel_status: dict[str, Any],
                             broker_state: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造上一轮执行反馈（Web 与 CLI 双路径共用，避免口径漂移）。"""
    if broker_state is None:
        broker_state = {}
    risk_verdict = state.get("risk_verdict")
    strategy_decision = state.get("strategy_decision")
    ai_decision = state.get("ai_decision")
    return {
        "ts": time.time(),
        "decision": decision.model_dump() if decision else None,
        "risk_verdict": risk_verdict.model_dump() if risk_verdict else None,
        "execution_receipts": state.get("execution") or [],
        "strategy_decision": strategy_decision.model_dump() if strategy_decision else None,
        "ai_decision": ai_decision.model_dump() if ai_decision else None,
        "tickets": state.get("tickets") or [],
        "positions": broker_state.get("positions", []),
        "channel_state": channel_status,
    }


class TradingService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = AuditDatabase(settings.db_file)
        self.logger = JsonlLogger(settings.logs_dir)
        self.recorder = AgentRecorder(self.db, self.logger, settings.artifacts_dir)
        self.broker = create_broker(settings, self.db)
        self.paper_broker = self.broker  # 兼容旧名称
        if getattr(self.broker, "mode", "") in ("testnet", "demo") and hasattr(self.broker, "sync_positions"):
            try:
                self.broker.sync_positions()
            except Exception:
                pass
        self.quota = TradeQuotaManager(self.db, settings.raw_settings.get("quotas", {}))
        intel_ttl = float(settings.raw_settings.get("intel_cache", {}).get("ttl_seconds", 3600))
        self.intel_cache = IntelCache(ttl_seconds=intel_ttl)
        cap_cfg = settings.raw_settings.get("capital", {})
        self.capital = CapitalManager(
            state_file=settings.project_root / cap_cfg.get("state_file", "data/capital_state.json"),
            broker=self.broker,
            db=self.db,
            initial_equity=float(cap_cfg.get("initial_equity", 200)),
            bankruptcy_threshold=float(cap_cfg.get("bankruptcy_threshold", 1.0)),
            reset_on_bankruptcy=bool(cap_cfg.get("reset_on_bankruptcy", True)),
            # 硬性保证金熔断上限（最后一道防线）：必须高于风控常规档(0.80)，
            # 否则会误杀强信心档仓位；取 strong_max_margin_usage(0.95) 是有意为之。
            max_margin_usage=float(settings.raw_risk.get(
                "strong_max_margin_usage",
                settings.raw_risk.get("max_margin_usage", 0.80),
            )),
        )
        ch_cfg = settings.raw_settings.get("channels", {}) or {}
        mu_cfg = settings.raw_settings.get("market_universe", {}) or {}
        self.universe = MarketUniverse(
            state_file=settings.project_root / str(mu_cfg.get("state_file", "data/market_universe_state.json")),
            core_symbols=mu_cfg.get("core_symbols") or [],
            quick_core_top=mu_cfg.get("quick_core_top", 3),
            quick_anomaly_top=mu_cfg.get("quick_anomaly_top", 2),
            full_top_n=mu_cfg.get("full_top_n", 20),
            full_anomaly_n=mu_cfg.get("full_anomaly_n", 5),
            short_move_pct=mu_cfg.get("short_move_pct", 3.0),
            full_scan_interval_minutes=mu_cfg.get("full_scan_interval_minutes", 60),
            db=self.db,
        )
        self.channels = ChannelManager(
            state_file=settings.project_root / str(ch_cfg.get("state_file", "data/channel_state.json")),
            broker=self.broker,
            db=self.db,
            strategy_weight=float(ch_cfg.get("strategy_weight", 0.6)),
            ai_weight=float(ch_cfg.get("ai_weight", 0.4)),
            channel_max_margin_usage=float(ch_cfg.get("channel_max_margin_usage", 0.80)),
            ai_daily_loss_stop=float(ch_cfg.get("ai_daily_loss_stop", 0.30)),
        )
        self.llm, self.model = build_llm_for_agent(settings, "ai_trader")
        self.agent_llms: dict[str, tuple[LLMClient, str]] = {}
        for name in ["market_agent", "macro_agent", "onchain_agent"]:
            client, m = build_llm_for_agent(settings, name)
            if not isinstance(client, MockLLMClient):
                self.agent_llms[name] = (client, m)
        self.control = ControlState(settings.project_root / "data/control_state.json")
        self.execution_feedback: dict[str, Any] = {}
        self._latest: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cycle_lock = threading.Lock()
        # 保护单/挂单对账的独立轻量锁：与慢循环的 _cycle_lock 分开，
        # 避免 LLM 长决策（最长 600s）期间 SL/TP 补挂与限价单到期撤单全部停摆。
        self._protection_lock = threading.Lock()
        self._run_thread: threading.Thread | None = None
        self._purge_old_logs()

    def _purge_old_logs(self, keep_days: int = 7) -> None:
        """启动时清理超过 keep_days 的按日 JSONL 日志目录（data/logs/YYYY-MM-DD/）。"""
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
            logs_root = Path(self.settings.logs_dir)
            if not logs_root.exists():
                return
            for d in logs_root.iterdir():
                if d.is_dir() and d.name[:10] < cutoff and len(d.name) >= 10 and d.name[:10].replace("-", "").isdigit():
                    for f in d.iterdir():
                        try:
                            f.unlink()
                        except Exception:
                            pass
                    try:
                        d.rmdir()
                    except Exception:
                        pass
        except Exception:
            pass

    def _signal_scanner_config(self) -> dict[str, Any]:
        cfg = dict(self.settings.raw_settings.get("signal_scanner", {}) or {})
        cfg["signal_state_file"] = str(
            self.settings.project_root / str(cfg.get("signal_state_file", "data/signal_scanner_state.json"))
        )
        mu = getattr(self, "universe", None)
        cfg["scan_pool"] = mu.get_scan_pool() if mu is not None else None
        cfg["universal_cards"] = bool(self.settings.raw_settings.get("signal_scanner", {}).get("universal_cards", True))
        return cfg

    def run_once(self) -> dict[str, Any]:
        if not self._cycle_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "cycle_running"}
        try:
            return self._run_once_inner()
        finally:
            self._cycle_lock.release()

    def refresh_after_reset(self) -> None:
        """手动重置资金后，实时刷新通道/会话权益到 _latest，页面立即显示正确值。

        否则 /api/channels 会一直返回重置前的旧缓存，直到下个慢循环。
        """
        self._refresh_latest_channels()

    def _refresh_latest_channels(self, state: dict[str, Any] | None = None,
                                 prices: dict[str, float] | None = None) -> None:
        """用当前会话权益/价格刷新 _latest 中的通道与资金快照。

        快循环每 30 秒调用一次，让页面通道数值接近实时；重置后也调用以立即生效。
        """
        try:
            if state is None:
                state = self.broker.state if isinstance(self.broker.state, dict) else {}
            if prices is None:
                symbols = [str(p.get("symbol")) for p in state.get("positions", []) if p.get("symbol")]
                prices = get_live_prices(symbols) if symbols else {}
            session_equity = self.capital.session_equity()
            channel_status = self.channels.sync(prices, session_equity=session_equity, read_only=True)
            with self._lock:
                if self._latest:
                    self._latest["capital_state"] = {
                        "session_equity": session_equity,
                        "initial_equity": self.capital.initial_equity,
                        "baseline": self.capital.baseline,
                        "last_reset": self.capital.last_reset,
                    }
                    self._latest["channel_state"] = channel_status
                    self._latest["paper_state"] = dict(state)
        except Exception:
            pass

    def run_fast_once(self) -> dict[str, Any]:
        """快循环：无 LLM。只处理挂单、保护单、来源台账对账。

        保护单/挂单对账用独立轻量锁 _protection_lock，与慢循环的 _cycle_lock
        分离：LLM 决策（最长 600s）期间，SL/TP 补挂与限价单到期撤单照常运行。
        """
        if not self._protection_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "protection_sync_running"}
        try:
            state = self.broker.state if isinstance(self.broker.state, dict) else {}
            symbols = [str(p.get("symbol")) for p in state.get("positions", [])]
            prices = get_live_prices(symbols) if symbols else {}
            decision = TradeDecision(decision="no_trade", channel="merged")
            verdict = RiskVerdict(verdict="approved", reasons=["fast cycle"], adjusted_intents=[])
            receipts = []
            try:
                receipts = self.broker.execute(decision, verdict, prices,
                                               self.settings.raw_risk, self.db)
            except Exception as e:
                self.db.log_risk_event(event_type="fast_cycle_error", severity="warn",
                                       details=f"{type(e).__name__}: {e}", action_taken="continue")
            # sync_positions 只在慢循环里做，避免 30 秒一次写入大量重复 OPEN 流水
            if hasattr(self.broker, "sync_protections"):
                try:
                    self.broker.sync_protections()
                except Exception as e:
                    self.db.log_risk_event(event_type="fast_cycle_error", severity="warn",
                                           details=f"sync_protections: {type(e).__name__}: {e}",
                                           action_taken="continue")
            # 快循环也刷新通道/会话权益快照，让页面数值接近实时（慢循环 10 分钟太长）
            self._refresh_latest_channels(state, prices)
            return {"ts": time.time(), "receipts": receipts, "prices": prices}
        finally:
            self._protection_lock.release()

    def _build_feedback(self, state: dict[str, Any], decision: Any,
                        channel_status: dict[str, Any],
                        broker_state: dict[str, Any] | None = None) -> dict[str, Any]:
        return build_execution_feedback(state, decision, channel_status, broker_state)

    def request_run(self) -> bool:
        if self._run_thread and self._run_thread.is_alive():
            return False
        self._run_thread = threading.Thread(target=self.run_once, daemon=True)
        self._run_thread.start()
        return True

    def cycle_running(self) -> bool:
        return bool(self._run_thread and self._run_thread.is_alive())

    def _run_once_inner(self) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        capital_result = self.capital.sync()
        equity_available = bool(capital_result.get("equity_available", True))
        session_equity = self.capital.session_equity()
        if hasattr(self.broker, "sync_positions"):
            try:
                self.broker.sync_positions()
            except Exception as e:
                self.db.log_risk_event(event_type="futures_sync_error", severity="warn",
                                       details=f"{type(e).__name__}: {e}", action_taken="skip")
        allow_new = bool(self.control.allow_new_positions and equity_available)
        # 先同步双通道资金状态（60/40 日切重算、AI 熔断、通道保证金上限）
        broker_state = self.broker.state if isinstance(self.broker.state, dict) else {}
        position_symbols = [str(p.get("symbol")) for p in broker_state.get("positions", [])]
        pre_prices = get_live_prices(position_symbols) if position_symbols else {}
        channel_status = self.channels.sync(pre_prices, session_equity=self.capital.session_equity())
        risk_config = dict(self.settings.raw_risk)
        ch_cfg = self.settings.raw_settings.get("channels", {}) or {}
        risk_config["channel_max_margin_usage"] = float(ch_cfg.get("channel_max_margin_usage", 0.80))
        risk_config["ai_max_leverage"] = int(ch_cfg.get("ai_max_leverage", 20))
        risk_config["ai_strong_max_leverage"] = int(ch_cfg.get("ai_strong_max_leverage", 25))
        risk_config["channel_status"] = channel_status
        # 全量白名单：过滤非法 symbol（垃圾数据），只保留标准 USDT 永续格式
        all_symbols = list(getattr(self.broker, "_symbol_filters", {}).keys()) or []
        risk_config["supported_symbols"] = [
            s for s in all_symbols
            if re.fullmatch(r"[0-9A-Z]+USDT", str(s))
        ] or None
        previous_feedback = dict(self.execution_feedback or {})
        graph = build_graph(
            llm=self.llm,
            model=self.model,
            db=self.db,
            logger=self.logger,
            recorder=self.recorder,
            risk_config=risk_config,
            knowledge_dir=self.settings.knowledge_dir,
            market_top_n=int(self.settings.raw_settings.get("symbol_universe", {}).get("top_n", 40)),
            market_analyze_top=int(self.settings.raw_settings.get("symbol_universe", {}).get("analyze_top", 10)),
            # 核心池白名单：市场情报只从这里选标的，杜绝美股/商品代币化合约混入
            market_core_symbols=(list(self.settings.raw_settings.get("market_universe", {}).get("core_symbols") or [])
                                 or None),
            tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip() or None,
            etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip() or None,
            cache_dir=self.settings.project_root / "data/cache",
            enable_live_data=True,
            intel_cache=self.intel_cache,
            intel_cache_ttl=self.intel_cache.default_ttl_seconds,
            broker=self.broker,
            allow_new_positions=allow_new,
            agent_llms=self.agent_llms,
            portfolio_state=self.capital.portfolio(),
            available_cash=session_equity if equity_available else None,
            quota_manager=self.quota,
            signal_scanner_config=self._signal_scanner_config(),
            execution_feedback=previous_feedback,
            control=self.control,
            query_budget=int((self.settings.raw_settings.get("decision_cycle", {})
                              or {}).get("query_budget", 8)),
        )
        state = graph.invoke({"run_id": run_id, "decision_id": decision_id})
        decision = state.get("decision")
        if decision is not None:
            self.db.log_trade_decision(
                decision_id=decision_id, run_id=run_id, decision=decision.decision,
                reasoning=decision.reasoning, raw_json=decision.model_dump_json(),
                channel=str(getattr(decision, "channel", "ai") or "ai"),
            )
        result = {
            "run_id": run_id,
            "decision": decision,
            "risk_verdict": state.get("risk_verdict"),
            "market": state.get("market"),
            "macro": state.get("macro"),
            "onchain": state.get("onchain"),
            "strategy": state.get("strategy"),
            "tickets": state.get("tickets"),
            "strategy_decision": state.get("strategy_decision"),
            "ai_decision": state.get("ai_decision"),
            "execution": state.get("execution"),
            "paper_state": dict(broker_state),
            "capital_state": {
                "session_equity": session_equity,
                "initial_equity": self.capital.initial_equity,
                "baseline": self.capital.baseline,
                "last_reset": self.capital.last_reset,
            },
            "control_state": self.control.state,
            "quota_state": self.quota.status(),
            "channel_state": channel_status,
            "intel_cache": self.intel_cache.stats(),
            "ts": time.time(),
        }
        self.execution_feedback = self._build_feedback(state, decision, channel_status, broker_state)
        with self._lock:
            self._latest = result
        return result

    def latest(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._latest)

    def set_control(self, new_state: str) -> dict[str, Any]:
        if not self.control.set_state(new_state):
            return {"ok": False, "error": "invalid state"}
        receipts = []
        if new_state == "emergency":
            prices = {}
            with self._lock:
                market = self._latest.get("market")
                if market is not None:
                    prices = dict(market.last_prices)
            # 循环重试直到无持仓或达到上限：平仓耗时数秒期间新平仓单可能
            # 未全部成交，只平一轮快照会留下残余仓位继续暴露风险。
            max_rounds = 5
            for _ in range(max_rounds):
                receipts = self.broker.close_all(prices, self.db)
                state = self.broker.state if isinstance(self.broker.state, dict) else {}
                if not [p for p in (state.get("positions") or []) if abs(float(p.get("qty") or 0)) > 0]:
                    break
            self.db.log_risk_event(event_type="manual_emergency_close", severity="critical",
                                   details=f"人工触发紧急平仓，平仓单数={len(receipts)}",
                                   action_taken="close_all")
        return {"ok": True, "state": new_state, "receipts": receipts}

    def start_background(self, interval_minutes: float) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, args=(interval_minutes,), daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop_event.set()

    def _loop(self, interval_minutes: float) -> None:
        fast_interval = float(self.settings.raw_settings.get("decision_cycle", {}).get("fast_interval_seconds", 30))
        next_slow = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_slow:
                self._run_thread = threading.current_thread()
                try:
                    self.run_once()
                except Exception as e:
                    self.db.log_risk_event(event_type="cycle_error", severity="error",
                                           details=f"{type(e).__name__}: {e}", action_taken="skip cycle")
                finally:
                    if self._run_thread is threading.current_thread():
                        self._run_thread = None
                next_slow = time.monotonic() + interval_minutes * 60
            else:
                try:
                    self.run_fast_once()
                except Exception as e:
                    self.db.log_risk_event(event_type="fast_cycle_error", severity="warn",
                                           details=f"{type(e).__name__}: {e}", action_taken="skip fast cycle")
            # 每日自动复盘：检查很轻（未到点/已生成即返回），挂在主循环里即可
            try:
                self._maybe_auto_review()
            except Exception:
                pass
            wait = max(1.0, min(fast_interval, max(1.0, next_slow - time.monotonic())))
            self._stop_event.wait(wait)

    def _maybe_auto_review(self, now: datetime | None = None) -> list[str]:
        """到点自动生成复盘报告。

        触发条件：UTC 时间已过 review.time_utc，且该日报告尚未生成（查 daily_reviews 表，
        重启也不会重复跑）。最多回填最近 7 天的缺口。返回本次生成的日期列表。
        """
        cfg = self.settings.raw_settings.get("review", {}) or {}
        if not bool(cfg.get("auto", True)):
            return []
        try:
            hh, mm = str(cfg.get("time_utc", "00:05")).split(":")
            cutoff_h, cutoff_m = int(hh), int(mm)
        except Exception:
            cutoff_h, cutoff_m = 0, 5
        now = now or datetime.now(timezone.utc)
        if (now.hour, now.minute) < (cutoff_h, cutoff_m):
            return []

        # 先找缺口（最近 7 天），没有就直接返回，不构建 LLM
        missing: list[str] = []
        for offset in range(1, 8):
            date = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
            if not self.db.fetch_one("SELECT review_date FROM daily_reviews WHERE review_date=?", (date,)):
                missing.append(date)
        if not missing:
            return []

        from agents.review import run_review_agent
        llm = None
        model = None
        if bool(cfg.get("use_llm", True)):
            try:
                client, m = build_llm_for_agent(self.settings, "review_agent")
                if not isinstance(client, MockLLMClient):
                    llm, model = client, m
            except Exception:
                llm = None
        generated: list[str] = []
        for date in missing:
            try:
                run_review_agent(self.db, self.settings.reviews_dir, review_date=date,
                                 llm=llm, model=model, logger=self.logger)
                generated.append(date)
            except Exception as e:
                self.db.log_risk_event(event_type="auto_review_error", severity="warn",
                                       details=f"{date}: {type(e).__name__}: {e}", action_taken="skip")
        if generated:
            self.db.log_risk_event(event_type="auto_review_done", severity="info",
                                   details=f"自动复盘已生成：{', '.join(generated)}",
                                   action_taken="none")
        return generated

    def close(self) -> None:
        self.stop_background()
        if hasattr(self.broker, "close"):
            try:
                self.broker.close()
            except Exception:
                pass
        self.db.close()
