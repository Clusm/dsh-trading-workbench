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
from core.db_backup import DatabaseBackupManager
from data.market_universe import MarketUniverse
from data.market_query import get_live_prices
from core.intel_cache import IntelCache
from core.instance_lock import InstanceLock
from core.quotas import TradeQuotaManager
from core.risk_guard import AccountRiskGuard
from llm.base import LLMClient
from llm.mock_client import MockLLMClient
from signal_engine import resolve_strategy_lab_src
from intel import ReadOnlyIntelService


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
    """聚合交易依赖并管理快慢循环、控制面和资源生命周期。

    每个项目目录只允许一个实例。慢循环持有 ``_cycle_lock`` 执行情报、决策与下单；
    快循环使用独立 ``_protection_lock``，确保 LLM 阻塞时仍可完成保护单和订单对账。
    ``_lock`` 只保护内存快照，不能在持有它时进行网络 I/O。关闭时必须先停止后台线程，
    再关闭流、Broker、数据库，最后释放实例锁。
    """
    def __init__(self, settings: Settings):
        self.settings = settings
        # 单实例是资金安全约束而非便利功能：两个进程会各自持有旧内存状态，
        # 在不同时间覆写基线/通道台账，且可能重复提交订单。
        self._instance_lock = InstanceLock(settings.project_root / "data" / "trading_service.lock")
        self._instance_lock.acquire()
        self.db = AuditDatabase(settings.db_file)
        # 新实例已成功取得单实例锁，说明上一次“等待在途决策”的关闭过程已经结束；
        # 原告警保留在事件流水中，但不应继续显示为当前未恢复事故。
        self.db.resolve_risk_incidents_by_type("service_shutdown_deferred")
        backup_cfg = settings.raw_settings.get("database_backup", {}) or {}
        self.db_backup = DatabaseBackupManager(
            self.db,
            settings.project_root / str(backup_cfg.get("directory", "data/backups")),
            enabled=bool(backup_cfg.get("enabled", True)),
            interval_hours=float(backup_cfg.get("interval_hours", 24)),
            keep_days=int(backup_cfg.get("keep_days", 14)),
            retry_hours=float(backup_cfg.get("retry_hours", 1)),
        )
        self.logger = JsonlLogger(settings.logs_dir)
        self.recorder = AgentRecorder(self.db, self.logger, settings.artifacts_dir)
        self.broker = create_broker(settings, self.db)
        self.paper_broker = self.broker  # 兼容旧名称
        # 不在构造阶段同步仓位：外部平仓补记会回查多笔 userTrades，网络波动时
        # 可阻塞数分钟，导致 Uvicorn 尚未监听、资金页面无法打开。后台慢循环会
        # 在服务启动后立刻执行同一同步，既不丢对账也不阻塞控制台首屏。
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
        stability_cfg = settings.raw_settings.get("stability", {}) or {}
        self.risk_guard = AccountRiskGuard(
            state_file=settings.project_root / "data" / "account_risk_state.json",
            daily_loss_stop=float(settings.raw_risk.get("daily_loss_stop", 0.15)),
            max_drawdown=float(settings.raw_risk.get("max_drawdown_from_peak", 0.30)),
            mode=str(stability_cfg.get("risk_guard_mode", "audit_only")),
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
        self.intel_sidecar = ReadOnlyIntelService(
            db=self.db,
            config=self.settings.raw_settings.get("web3_intel", {}) or {},
            symbols=list(mu_cfg.get("core_symbols") or []),
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
        if self.control.recovery_required:
            self.db.log_risk_event(
                event_type="control_state_corrupt", severity="critical",
                details=f"控制状态文件无法校验：{self.control.load_error}",
                action_taken="fail closed to close_only; require explicit operator state")
        if self.risk_guard.recovery_required:
            # 风险水位不可证明时，无论配置是 audit_only 还是 enforce 都不能开新仓。
            # close_only 不会平掉现有仓位，保护/减仓能力继续保留。
            if self.control.state == "normal":
                self.control.set_state("close_only")
            self.db.log_risk_event(
                event_type="account_risk_state_corrupt", severity="critical",
                details=f"账户风险状态文件无法校验：{self.risk_guard.load_error}",
                action_taken="set close_only; require verified recovery")
        self.execution_feedback: dict[str, Any] = {}
        self._latest: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._intel_thread: threading.Thread | None = None
        self._cycle_lock = threading.Lock()
        # 保护单/挂单对账的独立轻量锁：与慢循环的 _cycle_lock 分开，
        # 避免 LLM 长决策（最长 600s）期间 SL/TP 补挂与限价单到期撤单全部停摆。
        self._protection_lock = threading.Lock()
        self._run_thread: threading.Thread | None = None
        self._run_thread_lock = threading.Lock()
        self._startup_safety: dict[str, Any] = {
            "ready": False, "reason": "尚未完成仓位/保护单核验", "checked_at": None,
        }
        self._account_risk_status: dict[str, Any] = {}
        self._started_at = time.time()
        self._last_fast_cycle_at: float | None = None
        self._last_fast_cycle_error: str | None = None
        self._last_fast_cycle_duration_ms: float | None = None
        self._last_slow_cycle_started_at: float | None = None
        self._last_slow_cycle_at: float | None = None
        self._last_slow_cycle_error: str | None = None
        self._last_slow_cycle_duration_ms: float | None = None
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
        cfg["strategy_lab_src"] = resolve_strategy_lab_src(
            self.settings.project_root, cfg.get("strategy_lab_src")
        )
        cfg["signal_state_file"] = str(
            self.settings.project_root / str(cfg.get("signal_state_file", "data/signal_scanner_state.json"))
        )
        mu = getattr(self, "universe", None)
        cfg["scan_pool"] = mu.get_scan_pool() if mu is not None else None
        cfg["universal_cards"] = bool(self.settings.raw_settings.get("signal_scanner", {}).get("universal_cards", True))
        return cfg

    def run_once(self) -> dict[str, Any]:
        """同步运行一轮慢决策；已有慢循环时返回 skipped，绝不并发执行两轮。"""
        if not self._cycle_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "cycle_running"}
        started = time.perf_counter()
        with self._lock:
            self._last_slow_cycle_started_at = time.time()
        try:
            result = self._run_once_inner()
            with self._lock:
                self._last_slow_cycle_at = time.time()
                self._last_slow_cycle_error = None
            return result
        except Exception as exc:
            with self._lock:
                self._last_slow_cycle_error = type(exc).__name__
            raise
        finally:
            with self._lock:
                self._last_slow_cycle_duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
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
        started = time.perf_counter()
        try:
            # 慢决策仍在进行时，也要持续检查账户级止损；一旦触线，执行节点
            # 提交前读取到 close_only 后会丢弃尚未发出的开仓 intent。
            session_equity = self.capital.session_equity()
            equity_available = not bool(getattr(self.capital, "_equity_stale", True))
            account_risk = self._sync_account_risk(session_equity, equity_available)
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
            result = {"ts": time.time(), "receipts": receipts, "prices": prices,
                      "account_risk": account_risk}
            with self._lock:
                self._last_fast_cycle_at = result["ts"]
                self._last_fast_cycle_error = None
            return result
        except Exception as exc:
            with self._lock:
                self._last_fast_cycle_error = type(exc).__name__
            raise
        finally:
            with self._lock:
                self._last_fast_cycle_duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
            self._protection_lock.release()

    def _build_feedback(self, state: dict[str, Any], decision: Any,
                        channel_status: dict[str, Any],
                        broker_state: dict[str, Any] | None = None) -> dict[str, Any]:
        return build_execution_feedback(state, decision, channel_status, broker_state)

    def request_run(self) -> bool:
        """请求后台执行一轮慢决策；已有请求运行时返回 False。"""
        # Web手动触发与调度线程可能同时到达，检查和赋值必须原子；
        # 否则两个线程都可能越过 is_alive() 并各自启动一次慢决策。
        with self._run_thread_lock:
            if self._run_thread and self._run_thread.is_alive():
                return False
            worker = threading.Thread(target=self._run_once_background, daemon=True)
            self._run_thread = worker
            worker.start()
        return True

    def _run_once_background(self) -> None:
        try:
            self.run_once()
        except Exception as e:
            self.db.log_risk_event(event_type="cycle_error", severity="error",
                                   details=f"{type(e).__name__}: {e}", action_taken="skip cycle")
        finally:
            with self._run_thread_lock:
                if self._run_thread is threading.current_thread():
                    self._run_thread = None

    def cycle_running(self) -> bool:
        """返回手动或定时慢循环是否仍在执行。"""
        with self._run_thread_lock:
            return bool(self._run_thread and self._run_thread.is_alive())

    def runtime_status(self) -> dict[str, Any]:
        """返回线程循环的脱敏运行状态，供健康接口读取。"""
        with self._lock:
            return {
                "last_fast_cycle_at": self._last_fast_cycle_at,
                "last_fast_cycle_error": self._last_fast_cycle_error,
                "last_fast_cycle_duration_ms": self._last_fast_cycle_duration_ms,
                "last_slow_cycle_started_at": self._last_slow_cycle_started_at,
                "last_slow_cycle_at": self._last_slow_cycle_at,
                "last_slow_cycle_error": self._last_slow_cycle_error,
                "last_slow_cycle_duration_ms": self._last_slow_cycle_duration_ms,
                "started_at": self._started_at,
            }

    def _run_once_inner(self) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        capital_result = self.capital.sync()
        equity_available = bool(capital_result.get("equity_available", True))
        session_equity = self.capital.session_equity()
        startup_safety = self._verify_startup_safety()
        account_risk = self._sync_account_risk(session_equity, equity_available)
        try:
            order_submission_ready = (
                self.broker.order_submission_ready()
                if hasattr(self.broker, "order_submission_ready") else True)
        except Exception:
            order_submission_ready = False
        allow_new = bool(
            self.control.allow_new_positions
            and equity_available
            and account_risk.get("allow_new", False)
            and startup_safety.get("ready", False)
            and order_submission_ready
        )
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
        market_brief = state.get("market")
        try:
            self.intel_sidecar.record_baseline_shadow(
                run_id=run_id, decision=decision,
                prices=dict(getattr(market_brief, "last_prices", {}) or {}),
            )
        except Exception as exc:
            self.intel_sidecar.record_internal_error("baseline_shadow", exc)
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
            "account_risk": account_risk,
            "startup_safety": startup_safety,
            "quota_state": self.quota.status(),
            "channel_state": channel_status,
            "intel_cache": self.intel_cache.stats(),
            "web3_intel": self.intel_sidecar.snapshot(),
            "ts": time.time(),
        }
        self.execution_feedback = self._build_feedback(state, decision, channel_status, broker_state)
        with self._lock:
            self._latest = result
        return result

    def _verify_startup_safety(self) -> dict[str, Any]:
        """开仓前核验交易所仓位与保护单；未知状态默认禁止开仓。"""
        now = datetime.now(timezone.utc).isoformat()
        try:
            if hasattr(self.broker, "sync_positions"):
                self.broker.sync_positions()
            if hasattr(self.broker, "sync_protections"):
                self.broker.sync_protections()
            if hasattr(self.broker, "protection_coverage"):
                coverage = self.broker.protection_coverage()
                if not coverage.get("ok", False):
                    result = {"ready": False, "reason": str(coverage.get("reason") or "保护单核验失败"),
                              "checked_at": now, "coverage": coverage}
                    self._startup_safety = result
                    self.db.log_risk_event(event_type="startup_safety_block", severity="critical",
                                           details=result["reason"], action_taken="block new opens")
                    return result
            result = {"ready": True, "reason": "", "checked_at": now}
            resolver = getattr(self.db, "resolve_risk_incidents_by_type", None)
            if callable(resolver):
                resolver("startup_safety_block")
        except Exception as e:
            result = {"ready": False, "reason": f"{type(e).__name__}: {e}", "checked_at": now}
            self.db.log_risk_event(event_type="startup_safety_block", severity="critical",
                                   details=result["reason"], action_taken="block new opens")
        self._startup_safety = result
        return result

    def _sync_account_risk(self, session_equity: float, equity_available: bool) -> dict[str, Any]:
        """更新账户级闸门；首次触发时将控制面持久化为 close_only。"""
        account_risk = self.risk_guard.sync(
            session_equity,
            equity_available=equity_available,
            anchor_id=self.capital.anchor_id,
        )
        with self._lock:
            self._account_risk_status = dict(account_risk)
        if ((account_risk.get("latched") or account_risk.get("recovery_required"))
                and account_risk.get("enforced")
                and self.control.state == "normal"):
            self.control.set_state("close_only")
            self.db.log_risk_event(
                event_type="account_risk_stop", severity="critical",
                details=str(account_risk.get("reason") or "账户级风险阈值触发"),
                action_taken="set close_only; block new opens",
            )
        return account_risk

    def latest(self) -> dict[str, Any]:
        """返回最近完整服务快照的浅拷贝，供 Dashboard 只读展示。"""
        with self._lock:
            return dict(self._latest)

    def set_control(self, new_state: str) -> dict[str, Any]:
        """更新控制状态；emergency 会在状态落盘后执行多轮紧急平仓。"""
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
        """启动快慢循环和只读情报线程；重复调用不会创建第二组线程。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, args=(interval_minutes,), daemon=True)
        self._thread.start()
        if self.intel_sidecar.enabled and not (self._intel_thread and self._intel_thread.is_alive()):
            self._intel_thread = threading.Thread(
                target=self._intel_loop, name="readonly-intel-sidecar", daemon=True)
            self._intel_thread.start()

    def _intel_loop(self) -> None:
        """独立只读采集循环；失败不会传播到交易调度线程。"""
        while not self._stop_event.is_set():
            snapshot = self.intel_sidecar.maybe_refresh(force=True)
            with self._lock:
                if self._latest:
                    self._latest["web3_intel"] = snapshot
            self._stop_event.wait(self.intel_sidecar.refresh_seconds)

    def stop_background(self, wait_seconds: float = 5.0) -> bool:
        """停止调度并等待在途慢决策；返回是否已安全静默。

        不能在慢决策仍可能访问 Broker/DB 时先关闭共享资源。超时后由调用方
        保留资源和实例锁，进程退出时交给操作系统回收，避免产生半关闭竞态。
        """
        self._stop_event.set()
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        scheduler = self._thread
        if scheduler and scheduler is not threading.current_thread() and scheduler.is_alive():
            scheduler.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._run_thread_lock:
            worker = self._run_thread
        if worker and worker is not threading.current_thread() and worker.is_alive():
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        scheduler_alive = bool(scheduler and scheduler.is_alive())
        # 生命周期单测和极早期构造失败路径可能没有创建该属性。
        intel_worker = getattr(self, "_intel_thread", None)
        if intel_worker and intel_worker is not threading.current_thread() and intel_worker.is_alive():
            intel_worker.join(timeout=max(0.0, deadline - time.monotonic()))
        intel_alive = bool(intel_worker and intel_worker.is_alive())
        worker_alive = bool(worker and worker.is_alive())
        return not scheduler_alive and not worker_alive and not intel_alive

    def _loop(self, interval_minutes: float) -> None:
        fast_interval = float(self.settings.raw_settings.get("decision_cycle", {}).get("fast_interval_seconds", 30))
        next_slow = time.monotonic()
        while not self._stop_event.is_set():
            # SQLite在线备份包含WAL内容；频率门禁在manager内部，循环检查成本近似为零。
            self.db_backup.maybe_backup()
            now = time.monotonic()
            if now >= next_slow:
                # 慢决策放到独立线程，调度线程继续运行快循环，避免长 LLM 调用
                # 使保护单同步、限价单处理和账户熔断停摆数分钟。
                self.request_run()
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
        """幂等关闭线程、流、Broker、数据库及实例锁。"""
        if not self.stop_background():
            try:
                self.db.log_risk_event(
                    event_type="service_shutdown_deferred", severity="warn",
                    details="在途决策未在5秒内结束，保留Broker/DB/实例锁直到进程退出",
                    action_taken="defer resource close",
                )
            except Exception:
                pass
            return
        if hasattr(self.broker, "close"):
            try:
                self.broker.close()
            except Exception:
                pass
        try:
            self.db.close()
        finally:
            self._instance_lock.release()
