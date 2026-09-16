"""交易服务：决策循环 + 运行控制 + 纸面执行，供 CLI 与 Web 共用。"""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase
from audit.jsonl_logger import JsonlLogger
from agents.models import RiskVerdict
from agents import risk
from core.agent_recorder import AgentRecorder
from core.config_loader import Settings
from core.control import ControlState
from core.graph import build_graph
from core.decision_pipeline import authorize_decision, sign_execution
from core.position_guardian import evaluate_positions
from core.trading_scope import scope_from_settings
from core.entry_readiness import ReadinessSnapshot, evaluate_entry_readiness
from llm.factory import build_llm_for_agent
from exchange.factory import create_broker
from core.capital import CapitalManager
from core.channels import ChannelManager
from core.db_backup import DatabaseBackupManager
from data.market_universe import MarketUniverse
from data.market_query import get_live_prices
from data.kline_store import configure_kline_store
from data.rate_limit import (
    configure_rate_limit_budget,
    configure_rate_limit_state,
    rate_limit_status,
)
from core.intel_cache import IntelCache
from core.instance_lock import InstanceLock
from core.quotas import TradeQuotaManager
from core.risk_guard import AccountRiskGuard
from core.release_manifest import build_release_manifest, write_manifest
from llm.base import LLMClient
from llm.mock_client import MockLLMClient
from llm.request_coordinator import coordinator_status
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
        "release_id": state.get("release_id"),
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
        self._instance_lock = InstanceLock(settings.runtime_path("data/trading_service.lock"))
        self._instance_lock.acquire()
        market_data_cfg = settings.raw_settings.get("binance_market_data", {}) or {}
        configure_rate_limit_budget(
            weight_limit_1m=int(market_data_cfg.get("weight_limit_1m", 2400)),
            local_budget_1m=int(market_data_cfg.get("local_weight_budget_1m", 600)),
            background_defer_ratio=float(market_data_cfg.get("background_defer_ratio", 0.75)),
            normal_defer_ratio=float(market_data_cfg.get("normal_defer_ratio", 0.90)),
            data_window_seconds=int(market_data_cfg.get("data_endpoint_window_seconds", 300)),
            data_request_budget_5m=int(market_data_cfg.get("data_endpoint_budget_5m", 800)),
        )
        # Binance 429/418 冷却必须跨服务重启保留；否则重启会立即再次撞被封禁的共享出口。
        configure_rate_limit_state(settings.rate_limit_state)
        configure_kline_store(settings.runtime_path("data/cache/klines"))
        self.db = AuditDatabase(settings.db_file)
        # 新实例已成功取得单实例锁，说明上一次“等待在途决策”的关闭过程已经结束；
        # 原告警保留在事件流水中，但不应继续显示为当前未恢复事故。
        self.db.resolve_risk_incidents_by_type("service_shutdown_deferred")
        backup_cfg = settings.raw_settings.get("database_backup", {}) or {}
        self.db_backup = DatabaseBackupManager(
            self.db,
            settings.runtime_path(str(backup_cfg.get("directory", "data/backups"))),
            enabled=bool(backup_cfg.get("enabled", True)),
            interval_hours=float(backup_cfg.get("interval_hours", 24)),
            keep_days=int(backup_cfg.get("keep_days", 14)),
            retry_hours=float(backup_cfg.get("retry_hours", 1)),
        )
        self.logger = JsonlLogger(settings.logs_dir)
        self.recorder = AgentRecorder(self.db, self.logger, settings.artifacts_dir)
        # 开仓范围必须在实例启动时一次性校验；配置缺失或重复时直接拒绝启动，
        # 不能像旧逻辑一样退回全市场。该对象随后注入 Broker、策略和 AI 三条路径。
        mu_cfg = settings.raw_settings.get("market_universe", {}) or {}
        self.trading_scope = scope_from_settings(settings.raw_settings)
        self.runtime_instance_id = uuid.uuid4().hex
        self.broker = create_broker(settings, self.db)
        self.broker.trading_scope = self.trading_scope
        self.broker.runtime_instance_id = self.runtime_instance_id
        if not getattr(self.broker, "environment", None):
            self.broker.environment = str(getattr(self.broker, "mode", "paper") or "paper")
        broker_environment = str(getattr(self.broker, "environment", "unknown") or "unknown").lower()
        reconciliation_book = getattr(self.broker, "trade_reconciliation", None)
        broker_account_ref = getattr(reconciliation_book, "account_ref", None)
        if not broker_account_ref:
            broker_account_ref = f"{broker_environment}:local"
        self.release_manifest_obj = build_release_manifest(
            settings.project_root,
            environment=broker_environment,
            account_ref=str(broker_account_ref),
            db_file=settings.db_file,
            egress_id=os.getenv("MTA_EGRESS_ID", "unknown"),
            release_id=os.getenv("MTA_RELEASE_ID") or None,
        )
        self.release_manifest = self.release_manifest_obj.to_dict()
        self.release_id = self.release_manifest_obj.release_id
        self.db.set_release_context(
            release_id=self.release_id,
            environment=broker_environment,
            account_ref=self.release_manifest_obj.account_ref,
        )
        self.recorder.release_id = self.release_id
        if reconciliation_book is not None:
            reconciliation_book.release_id = self.release_id
        try:
            write_manifest(settings.runtime_path("data/release_manifest.json"),
                           self.release_manifest_obj)
        except Exception as exc:
            # 清单写失败不改变交易风险档，但必须留下可见事故，发布报告也会因
            # 缺少对应证据而保持 NO_GO，避免“运行着但无法证明版本”的假上线。
            self.db.log_risk_event(
                event_type="release_manifest_write_error", severity="critical",
                details=f"{type(exc).__name__}: {exc}", action_taken="release evidence unavailable",
            )
        self.paper_broker = self.broker  # 兼容旧名称
        # 不在构造阶段同步仓位：外部平仓补记会回查多笔 userTrades，网络波动时
        # 可阻塞数分钟，导致 Uvicorn 尚未监听、资金页面无法打开。后台慢循环会
        # 在服务启动后立刻执行同一同步，既不丢对账也不阻塞控制台首屏。
        self.quota = TradeQuotaManager(self.db, settings.raw_settings.get("quotas", {}))
        intel_ttl = float(settings.raw_settings.get("intel_cache", {}).get("ttl_seconds", 3600))
        self.intel_cache = IntelCache(ttl_seconds=intel_ttl)
        cap_cfg = settings.raw_settings.get("capital", {})
        self.capital = CapitalManager(
            state_file=settings.runtime_path(cap_cfg.get("state_file", "data/capital_state.json")),
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
            # Demo 继续使用配置的 500U 起点；实盘必须以首次接管时的真实权益
            # 建立独立资金锚点，不能把模拟本金当作实盘资产。
            anchor_live_to_actual=settings.profile_name == "live",
        )
        stability_cfg = settings.raw_settings.get("stability", {}) or {}
        self.risk_guard = AccountRiskGuard(
            state_file=settings.runtime_path("data/account_risk_state.json"),
            daily_loss_stop=float(settings.raw_risk.get("daily_loss_stop", 0.15)),
            max_drawdown=float(settings.raw_risk.get("max_drawdown_from_peak", 0.30)),
            mode=str(stability_cfg.get("risk_guard_mode", "audit_only")),
        )
        ch_cfg = settings.raw_settings.get("channels", {}) or {}
        self.universe = MarketUniverse(
            state_file=settings.runtime_path(str(mu_cfg.get("state_file", "data/market_universe_state.json"))),
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
            state_file=settings.runtime_path(str(ch_cfg.get("state_file", "data/channel_state.json"))),
            broker=self.broker,
            db=self.db,
            strategy_weight=float(ch_cfg.get("strategy_weight", 0.6)),
            ai_weight=float(ch_cfg.get("ai_weight", 0.4)),
            channel_max_margin_usage=float(ch_cfg.get("channel_max_margin_usage", 0.80)),
            ai_daily_loss_stop=float(ch_cfg.get("ai_daily_loss_stop", 0.30)),
        )
        if ((self.capital.last_reset or {}).get("reason") == "live_account_anchor"):
            # 新的实盘资金锚点代表新的核算周期。同步重建通道日初基准，避免旧版
            # 错误的 500U 口径或接管前成交继续触发 AI 当日亏损熔断。
            self.channels.reset_day_start(self.capital.session_equity())
        self.llm, self.model = build_llm_for_agent(settings, "ai_trader")
        self.agent_llms: dict[str, tuple[LLMClient, str]] = {}
        for name in ["market_agent", "macro_agent", "onchain_agent"]:
            client, m = build_llm_for_agent(settings, name)
            if not isinstance(client, MockLLMClient):
                self.agent_llms[name] = (client, m)
        control_path = settings.runtime_path("data/control_state.json")
        first_live_start = settings.profile_name == "live" and not control_path.exists()
        self.control = ControlState(control_path)
        if first_live_start:
            # 首次接入先保护和核对已有仓位；运营确认后通过控制台恢复新增仓位。
            self.control.set_state("close_only")
        if self.control.recovery_required:
            self.db.log_risk_event(
                event_type="control_state_corrupt", severity="critical",
                details=f"控制状态文件无法校验：{self.control.load_error}",
                action_taken="fail closed to close_only; require explicit operator state")
        else:
            # 成功加载并校验当前控制文件就是恢复证据；只收口 incident，不改变状态。
            self.db.resolve_risk_incidents_by_type("control_state_corrupt")
        if self.risk_guard.recovery_required:
            # 风险水位不可证明时，无论配置是 audit_only 还是 enforce 都不能开新仓。
            # close_only 不会平掉现有仓位，保护/减仓能力继续保留。
            if self.control.state == "normal":
                self.control.set_state("close_only")
            self.db.log_risk_event(
                event_type="account_risk_state_corrupt", severity="critical",
                details=f"账户风险状态文件无法校验：{self.risk_guard.load_error}",
                action_taken="set close_only; require verified recovery")
        else:
            self.db.resolve_risk_incidents_by_type("account_risk_state_corrupt")
        self.execution_feedback: dict[str, Any] = {}
        self._latest: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._accepting_work = True
        self._closed = False
        self._thread: threading.Thread | None = None
        self._intel_thread: threading.Thread | None = None
        self._review_thread: threading.Thread | None = None
        self._review_queue: queue.Queue[str] = queue.Queue()
        self._review_pending: set[str] = set()
        self._review_lock = threading.Lock()
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
        self._last_release_observation_minute: str | None = None
        self._evidence_thread: threading.Thread | None = None
        self._last_fast_cycle_at: float | None = None
        self._last_fast_cycle_error: str | None = None
        self._last_fast_cycle_duration_ms: float | None = None
        self._last_slow_cycle_started_at: float | None = None
        self._last_slow_cycle_at: float | None = None
        self._last_slow_cycle_error: str | None = None
        self._last_slow_cycle_duration_ms: float | None = None
        # 看板资金/通道快照允许保留上一次有效值，但不能把刷新失败伪装成实时数据。
        # 这些字段仅供健康接口展示，不参与风控阈值或开仓授权。
        self._last_latest_refresh_at: float | None = None
        self._last_latest_refresh_error: str | None = None
        # 先以“未完成核验”初始化；首次慢/快循环会写入真实事实。
        # UI 冷启动可以看到明确原因，不能把空快照误报为可开仓。
        self._entry_readiness_facts: dict[str, Any] = {
            "service_accepting": True,
            "control_state": self.control.state,
            "equity_available": False,
            "account_risk_allow_new": False,
            "startup_safety_ready": False,
            "order_submission_ready": False,
            "market_cooldown_active": None,
            "data_budget_available": False,
            "rate_coordination_available": True,
            "order_mirror_ok": True if self.broker.environment == "paper" else None,
            "protection_mirror_ok": True if self.broker.environment == "paper" else None,
            "unknown_orders": 0,
            "submitting_orders": 0,
            "scope_hash": self.trading_scope.scope_hash,
            "persistence_available": True,
            "environment": self.broker.environment,
            "runtime_instance_id": self.runtime_instance_id,
            "component_versions": {"readiness": 1, "scope": 1},
        }
        self._entry_readiness_snapshot: ReadinessSnapshot = evaluate_entry_readiness(
            self._entry_readiness_facts,
            scope=self.trading_scope,
            runtime_instance_id=self.runtime_instance_id,
            environment=self.broker.environment,
        )
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
            self.settings.runtime_path(str(cfg.get("signal_state_file", "data/signal_scanner_state.json")))
        )
        mu = getattr(self, "universe", None)
        cfg["scan_pool"] = mu.get_scan_pool() if mu is not None else None
        cfg["universal_cards"] = bool(self.settings.raw_settings.get("signal_scanner", {}).get("universal_cards", True))
        cfg["release_id"] = getattr(self, "release_id", "legacy")
        return cfg

    def _fast_cycle_freshness(self, fast_at: float | None, fast_error: str | None) -> str:
        """按 stability 阈值判定快循环时效，与看板 ``dashboard.health`` 同一口径。

        原来的判定只看心跳字段是否存在，快循环停摆十分钟也会写成 healthy —— 发布
        门禁只统计 ``health_state='healthy'`` 的分钟，于是停摆期会被记成有效证据。
        这里改为比较心跳时间：超过 ``stability.fast_cycle_stale_seconds`` 记为
        ``stale``；从未完成且已超出 ``startup_grace_seconds`` 记为 ``missing``。
        """
        if fast_error:
            return "degraded"
        stability_cfg = self.settings.raw_settings.get("stability", {}) or {}
        now = time.time()
        if fast_at is not None:
            limit = max(1.0, float(stability_cfg.get("fast_cycle_stale_seconds", 120)))
            return "healthy" if now - float(fast_at) <= limit else "stale"
        grace = max(1.0, float(stability_cfg.get("startup_grace_seconds", 120)))
        started = float(getattr(self, "_started_at", now))
        # 启动宽限期内尚未完成过快循环属正常启动过程，不作为失健康证据。
        return "unknown" if now - started <= grace else "missing"

    def _record_release_observation(self) -> None:
        """每个 UTC 分钟保存一次运行证据；只读采集内存/状态，不发网络请求。

        健康判定取自真实心跳，因此采样本身准时并不等于证据有效：快循环停摆的分钟
        会记为 degraded 并从覆盖率里剔除，符合“缺失分钟不得记为健康”。
        """
        writer = getattr(self.db, "log_release_observation", None)
        if not callable(writer):
            return
        observed = datetime.now(timezone.utc)
        observed_at = observed.isoformat(timespec="milliseconds")
        observed_minute = observed.isoformat(timespec="minutes")
        if observed_minute == getattr(self, "_last_release_observation_minute", None):
            return
        try:
            rate = rate_limit_status()
            entry = (getattr(getattr(self, "_entry_readiness_snapshot", None), "as_dict", lambda: {})()
                     or {})
            with self._lock:
                fast_error = self._last_fast_cycle_error
                slow_error = self._last_slow_cycle_error
                closed = bool(getattr(self, "_closed", False))
                fast_at = self._last_fast_cycle_at
                slow_at = self._last_slow_cycle_at
            persistence_ok = True
            try:
                self.db.fetch_one("SELECT 1")
            except Exception:
                persistence_ok = False
            fast_state = self._fast_cycle_freshness(fast_at, fast_error)
            components = {
                "service": "stopped" if closed else "healthy",
                "fast_cycle": fast_state,
                "slow_cycle": "degraded" if slow_error else ("healthy" if slow_at else "unknown"),
                "persistence": "healthy" if persistence_ok else "degraded",
            }
            # 与看板时效判定保持同源：宽限期内的 unknown 不算失健康，stale/missing 算。
            health_state = "healthy" if (
                not closed and persistence_ok and not slow_error
                and fast_state in {"healthy", "unknown"}
            ) else "degraded"
            scope = getattr(self, "trading_scope", None)
            scope_hash = getattr(scope, "scope_hash", "unknown")
            scope_symbols = getattr(scope, "allowed_open_symbols", None)
            if callable(scope_symbols):
                try:
                    scope_count = len(scope_symbols(None))
                except Exception:
                    scope_count = None
            else:
                scope_count = None
            inserted = writer(
                observed_at=observed_at,
                observed_minute=observed_minute,
                session_id=str(getattr(self, "runtime_instance_id", "unknown")),
                health_state=health_state,
                component_status=components,
                config_hash=str((getattr(self, "release_manifest", {}) or {}).get("content_hash", "")),
                rate_limit=rate,
                coverage={"scope_hash": scope_hash, "scope_symbol_count": scope_count},
                entry_readiness=entry,
                raw={"runtime_instance_id": getattr(self, "runtime_instance_id", "unknown")},
                release_id=getattr(self, "release_id", "legacy"),
                environment=str(getattr(self.broker, "environment", "unknown") or "unknown"),
                account_ref=str((getattr(self, "release_manifest", {}) or {}).get("account_ref", "unknown")),
            )
            # 即使是同一分钟已存在的幂等行，也不必重复尝试写入。
            if inserted or inserted is False:
                with self._lock:
                    self._last_release_observation_minute = observed_minute
        except Exception as exc:
            try:
                self.db.log_risk_event_once_while_open(
                    event_type="release_observation_error", severity="critical",
                    details=f"{type(exc).__name__}: {exc}",
                    action_taken="release evidence degraded",
                )
            except Exception:
                pass

    def run_once(self) -> dict[str, Any]:
        """同步运行一轮慢决策；已有慢循环时返回 skipped，绝不并发执行两轮。"""
        if not getattr(self, "_accepting_work", True):
            return {"skipped": True, "reason": "service_shutting_down"}
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
                self._last_latest_refresh_at = time.time()
                self._last_latest_refresh_error = None
        except Exception as exc:
            # 保留最后有效展示值，但把失败原因显式暴露给 /api/health。这里不能
            # 静默吞掉异常，否则页面会把旧权益误标为刚刷新。
            with self._lock:
                self._last_latest_refresh_error = type(exc).__name__

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
            guardian = evaluate_positions(
                positions=list(state.get("positions") or []), db=self.db,
                execution_mode=str(getattr(self.broker, "mode", "paper") or "paper"),
            )
            decision = authorize_decision(
                guardian, db=self.db, run_id=f"fast-guardian-{uuid.uuid4()}")
            if decision.intents:
                verdict = risk.evaluate_risk(
                    decision, dict(self.settings.raw_risk), self.db, self.recorder,
                    f"fast-guardian-{uuid.uuid4()}", allow_new_positions=False,
                    prices=prices, available_cash=session_equity, portfolio=state,
                )
                decision = sign_execution(
                    decision, verdict, getattr(self.broker, "_pipeline_secret", None))
            else:
                verdict = RiskVerdict(
                    verdict="approved", reasons=["fast guardian cycle"], adjusted_intents=[])
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
            with self._lock:
                if self._latest:
                    self._latest["guardian_decision"] = guardian
            result = {"ts": time.time(), "receipts": receipts, "prices": prices,
                      "account_risk": account_risk,
                      "guardian_decision": guardian.model_dump()}
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
            if (getattr(self, "_closed", False) or
                    not getattr(self, "_accepting_work", True) or
                    self._stop_event.is_set()):
                return False
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

    def _trade_reconciliation_status(self) -> dict[str, Any]:
        """返回成交生命周期摘要；仅用于观测，不把补账状态改成新的风险档。"""
        book = getattr(self.broker, "trade_reconciliation", None)
        if book is None or not hasattr(book, "db"):
            return {"enabled": False, "state": "disabled"}
        try:
            environment = str(getattr(self.broker, "environment", "unknown") or "unknown")
            return {
                "enabled": True, "state": "active",
                **book.db.trade_reconciliation_status(environment),
            }
        except Exception as exc:
            return {"enabled": True, "state": "degraded", "last_error": type(exc).__name__}

    def runtime_status(self) -> dict[str, Any]:
        """返回线程循环的脱敏运行状态，供健康接口读取。"""
        with self._lock:
            return {
                "accepting_work": bool(getattr(self, "_accepting_work", True)),
                "closed": bool(getattr(self, "_closed", False)),
                "last_fast_cycle_at": self._last_fast_cycle_at,
                "last_fast_cycle_error": self._last_fast_cycle_error,
                "last_fast_cycle_duration_ms": self._last_fast_cycle_duration_ms,
                "last_slow_cycle_started_at": self._last_slow_cycle_started_at,
                "last_slow_cycle_at": self._last_slow_cycle_at,
                "last_slow_cycle_error": self._last_slow_cycle_error,
                "last_slow_cycle_duration_ms": self._last_slow_cycle_duration_ms,
                "last_latest_refresh_at": self._last_latest_refresh_at,
                "last_latest_refresh_error": self._last_latest_refresh_error,
                "started_at": self._started_at,
                "review_queue_size": (
                    self._review_queue.qsize()
                    if isinstance(getattr(self, "_review_queue", None), queue.Queue)
                    else 0
                ),
                "review_worker_alive": bool(
                    getattr(self, "_review_thread", None)
                    and self._review_thread.is_alive()
                ),
                "llm_coordinator": coordinator_status(),
                "trade_reconciliation": self._trade_reconciliation_status(),
                "release_id": getattr(self, "release_id", "legacy"),
                "profile": getattr(getattr(self, "settings", None), "profile_name", None),
                "release_manifest": {
                    key: (getattr(self, "release_manifest", {}) or {}).get(key)
                    for key in ("git_commit", "content_hash", "schema_version",
                                "environment", "account_ref", "egress_id", "threshold_version")
                },
            }

    def _mirror_fact(self, table: str, environment: str,
                     *, protection: bool = False) -> bool | None:
        """读取最近一次镜像对账结果；没有记录时返回 None 而不是假定成功。"""
        try:
            columns = "matched" if protection else "initialized,matched"
            row = self.db.fetch_one(
                f"SELECT {columns} FROM {table} WHERE environment=? "
                "ORDER BY id DESC LIMIT 1", (environment,))
        except Exception:
            return None
        if not row:
            return None
        if protection:
            return bool(row["matched"])
        return bool(row["matched"] or row["initialized"])

    def _collect_entry_readiness_facts(self) -> dict[str, Any]:
        """只读收集当前开仓闸门事实；严禁在这里发交易所网络请求。"""
        environment = str(getattr(self.broker, "environment", "paper") or "paper")
        facts = dict(self._entry_readiness_facts)
        facts.update({
            "service_accepting": bool(getattr(self, "_accepting_work", True)),
            "control_state": self.control.state,
            "scope_hash": self.trading_scope.scope_hash,
            "environment": environment,
            "runtime_instance_id": self.runtime_instance_id,
        })
        market_rate = rate_limit_status()
        data_window = market_rate.get("data_endpoint_window") or {}
        try:
            budget_available = int(data_window.get("requests", 0) or 0) < int(
                data_window.get("budget", 1) or 1)
        except (TypeError, ValueError):
            budget_available = False
        facts["market_cooldown_active"] = bool(market_rate.get("active"))
        facts["data_budget_available"] = budget_available
        facts["rate_coordination_available"] = (
            market_rate.get("coordination_available") is True)
        try:
            facts["order_submission_ready"] = bool(self.broker.order_submission_ready())
        except Exception:
            facts["order_submission_ready"] = False
        outbox = getattr(self.broker, "order_outbox", None)
        if outbox is not None:
            try:
                status = outbox.status()
                facts["unknown_orders"] = int(status.get("unknown_orders", 0) or 0)
                facts["submitting_orders"] = int(status.get("submitting_orders", 0) or 0)
            except Exception:
                facts["unknown_orders"] = 1
                facts["submitting_orders"] = 1
        if environment == "paper":
            # PaperBroker 没有交易所镜像，自己的持久化账本就是已验证事实。
            facts["order_mirror_ok"] = True
            facts["protection_mirror_ok"] = True
        else:
            facts["order_mirror_ok"] = self._mirror_fact(
                "order_mirror_reconciliations", environment)
            facts["protection_mirror_ok"] = self._mirror_fact(
                "protection_reconciliations", environment, protection=True)
        try:
            self.db.fetch_one("SELECT 1")
            facts["persistence_available"] = True
        except Exception:
            facts["persistence_available"] = False
        return facts

    def _refresh_entry_readiness(self, updates: dict[str, Any] | None = None
                                 ) -> ReadinessSnapshot:
        """更新共享事实并生成新快照；调用方应在网络操作之外使用。"""
        facts = self._collect_entry_readiness_facts()
        if updates:
            facts.update(updates)
        snapshot = evaluate_entry_readiness(
            facts,
            scope=self.trading_scope,
            runtime_instance_id=self.runtime_instance_id,
            environment=str(getattr(self.broker, "environment", "paper") or "paper"),
        )
        with self._lock:
            self._entry_readiness_facts = facts
            self._entry_readiness_snapshot = snapshot
        return snapshot

    def entry_readiness_snapshot(self) -> ReadinessSnapshot:
        """执行图使用的轻量复核入口；只读 DB/内存/限流文件，不发交易所请求。"""
        return self._refresh_entry_readiness()

    def entry_readiness(self) -> dict[str, Any]:
        """看板使用的共享开仓就绪判定。"""
        return self.entry_readiness_snapshot().as_dict()

    def _run_once_inner(self) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        decision_id = str(uuid.uuid4())
        # 接管核验必须先于任何可能自动平仓的资金硬保护。存量仓位若不符合当前
        # 风险档，系统先补保护并明确阻断，由运营者决定如何处理，绝不在首屏尚未
        # 就绪时静默 close_all；核验通过后，日常运行仍按原风险档自动执行硬保护。
        startup_safety = self._verify_startup_safety()
        capital_result = self.capital.sync(
            enforce_actions=bool(startup_safety.get("ready", False)))
        equity_available = bool(capital_result.get("equity_available", True))
        session_equity = self.capital.session_equity()
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
        market_rate = rate_limit_status()
        data_window = market_rate.get("data_endpoint_window") or {}
        allow_new_reason = ""
        if market_rate.get("active"):
            allow_new = False
            allow_new_reason = (
                f"公共行情处于 Binance {market_rate.get('last_status') or '限流'} 冷却，"
                "仅允许继续管理已有仓位"
            )
        if (int(data_window.get("requests", 0) or 0)
                >= int(data_window.get("budget", 1) or 1)):
            allow_new = False
            allow_new_reason = "公共 futures/data 五分钟预算已用尽，仅允许继续管理已有仓位"
        # 私有流/REST 对账发现未知订单或保护覆盖不一致时，禁止新增风险敞口；
        # 这些都是数据库中的交易所事实，不改变既有风险档，也不触碰当前仓位。
        environment = str(getattr(self.broker, "environment", "demo") or "demo")
        latest_order = self.db.fetch_one(
            "SELECT initialized,matched FROM order_mirror_reconciliations "
            "WHERE environment=? ORDER BY id DESC LIMIT 1", (environment,))
        if (latest_order and not bool(latest_order["matched"] or latest_order["initialized"])):
            allow_new = False
            allow_new_reason = "订单镜像与交易所挂单不一致，仅允许继续管理已有仓位"
        latest_protection = self.db.fetch_one(
            "SELECT matched FROM protection_reconciliations "
            "WHERE environment=? ORDER BY id DESC LIMIT 1", (environment,))
        if latest_protection and not bool(latest_protection["matched"]):
            allow_new = False
            allow_new_reason = "保护单镜像与持仓不一致，仅允许继续管理已有仓位"
        # 唯一开仓就绪判定：把上面已采集的事实收敛成可复核快照，供看板和
        # 执行节点复用。原因码只阻断 open，保护/退出仍走原有路径。
        try:
            data_budget_available = int(data_window.get("requests", 0) or 0) < int(
                data_window.get("budget", 1) or 1)
        except (TypeError, ValueError):
            data_budget_available = False
        readiness = self._refresh_entry_readiness({
            "service_accepting": bool(getattr(self, "_accepting_work", True)),
            "control_state": self.control.state,
            "equity_available": equity_available,
            "account_risk_allow_new": bool(account_risk.get("allow_new", False)),
            "startup_safety_ready": bool(startup_safety.get("ready", False)),
            "order_submission_ready": bool(order_submission_ready),
            "market_cooldown_active": bool(market_rate.get("active")),
            "data_budget_available": data_budget_available,
            "order_mirror_ok": (True if environment == "paper" else
                                 bool(latest_order["matched"] or latest_order["initialized"])
                                 if latest_order else None),
            "protection_mirror_ok": (True if environment == "paper" else
                                      bool(latest_protection["matched"])
                                      if latest_protection else None),
            "persistence_available": True,
            "scope_hash": self.trading_scope.scope_hash,
        })
        allow_new = readiness.new_entry_allowed
        if readiness.blocking_reasons:
            allow_new_reason = "；".join(readiness.blocking_reasons)
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
        # instrument 集合只表示交易所可识别/可管理的合约；开仓必须再与项目业务
        # TradingScope 求交集。这样白名单外的历史持仓仍可管理，但不能新开。
        all_symbols = list(getattr(self.broker, "_symbol_filters", {}).keys()) or []
        instrument_symbols = [
            s for s in all_symbols
            if re.fullmatch(r"[0-9A-Z]+USDT", str(s))
        ]
        risk_config["supported_symbols"] = instrument_symbols or None
        risk_config["allowed_open_symbols"] = self.trading_scope.allowed_open_symbols(
            instrument_symbols)
        risk_config["trading_scope_hash"] = self.trading_scope.scope_hash
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
            cache_dir=self.settings.runtime_path("data/cache"),
            enable_live_data=True,
            intel_cache=self.intel_cache,
            intel_cache_ttl=self.intel_cache.default_ttl_seconds,
            broker=self.broker,
            allow_new_positions=allow_new,
            allow_new_reason=allow_new_reason,
            agent_llms=self.agent_llms,
            portfolio_state=self.capital.portfolio(),
            available_cash=session_equity if equity_available else None,
            quota_manager=self.quota,
            signal_scanner_config=self._signal_scanner_config(),
            execution_feedback=previous_feedback,
            control=self.control,
            query_budget=int((self.settings.raw_settings.get("decision_cycle", {})
                              or {}).get("query_budget", 8)),
            execution_mode=str(getattr(self.broker, "mode", "paper") or "paper"),
            trading_scope=self.trading_scope,
            entry_readiness_provider=self.entry_readiness_snapshot,
            llm_total_timeout_seconds=float(
                (self.settings.raw_settings.get("llm", {}) or {}).get(
                    "total_timeout_seconds", 240.0)),
            llm_aux_timeout_seconds=float(
                (self.settings.raw_settings.get("llm", {}) or {}).get(
                    "aux_timeout_seconds", 60.0)),
            release_id=getattr(self, "release_id", "legacy"),
        )
        state = graph.invoke({"run_id": run_id, "decision_id": decision_id,
                              "release_id": getattr(self, "release_id", "legacy")})
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
            "guardian_decision": state.get("guardian_decision"),
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
            "entry_readiness": readiness.as_dict(),
            "quota_state": self.quota.status(),
            "channel_state": channel_status,
            "intel_cache": self.intel_cache.stats(),
            "web3_intel": self.intel_sidecar.snapshot(),
            "release_id": getattr(self, "release_id", "legacy"),
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
            compatibility: dict[str, Any] | None = None
            if hasattr(self.broker, "takeover_compatibility"):
                compatibility = self.broker.takeover_compatibility()
                # 当前来源台账不能区分同 symbol 的 LONG/SHORT 双边仓位。必须在
                # sync_positions/sync_protections 产生本地写入、撤单或补单之前停止。
                if not compatibility.get("position_mode_supported", False):
                    result = {
                        "ready": False,
                        "reason": str(compatibility.get("reason") or "账户持仓模式不兼容"),
                        "checked_at": now,
                        "takeover": compatibility,
                    }
                    self._startup_safety = result
                    self.db.log_risk_event_once_while_open(
                        event_type="startup_takeover_block", severity="critical",
                        details=result["reason"],
                        action_taken="block before ledger/protection mutation",
                    )
                    return result
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
            if str(getattr(self.broker, "environment", "paper") or "paper") == "live":
                egress_id = str(
                    (getattr(self, "release_manifest", {}) or {}).get("egress_id") or "unknown"
                ).strip()
                if not egress_id or egress_id.lower() == "unknown":
                    result = {
                        "ready": False,
                        "reason": "实盘发布清单缺少已核验出口标识 MTA_EGRESS_ID",
                        "checked_at": now,
                        "takeover": compatibility,
                    }
                    self._startup_safety = result
                    self.db.log_risk_event_once_while_open(
                        event_type="startup_takeover_block", severity="critical",
                        details=result["reason"],
                        action_taken="protect positions; block new opens until egress is identified",
                    )
                    return result
                margin_cap = self.capital.margin_cap_status()
                if compatibility is not None:
                    compatibility = dict(compatibility)
                    compatibility["margin_cap"] = margin_cap
                if not margin_cap.get("evaluated", False) or margin_cap.get("breached", False):
                    result = {
                        "ready": False,
                        "reason": str(margin_cap.get("reason") or "无法核验实盘总保证金上限"),
                        "checked_at": now,
                        "takeover": compatibility,
                    }
                    self._startup_safety = result
                    self.db.log_risk_event_once_while_open(
                        event_type="startup_takeover_block", severity="critical",
                        details=result["reason"],
                        action_taken="protect positions; block automatic risk close and new opens",
                    )
                    return result
            # 外部增仓挂单不受本进程 close_only 控制。允许上面的现仓保护同步继续，
            # 但绝不能把实例标为可恢复开仓；由运营者核对后撤单或显式纳管。
            if compatibility is not None and not compatibility.get("ok", False):
                result = {
                    "ready": False,
                    "reason": str(compatibility.get("reason") or "存在未纳管增仓挂单"),
                    "checked_at": now,
                    "takeover": compatibility,
                }
                self._startup_safety = result
                self.db.log_risk_event_once_while_open(
                    event_type="startup_takeover_block", severity="critical",
                    details=result["reason"], action_taken="protect positions; block new opens",
                )
                return result
            result = {"ready": True, "reason": "", "checked_at": now}
            resolver = getattr(self.db, "resolve_risk_incidents_by_type", None)
            if callable(resolver):
                resolver("startup_safety_block", "startup_takeover_block")
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
        if getattr(self, "_closed", False):
            return
        if self._thread and self._thread.is_alive():
            return
        with self._run_thread_lock:
            self._accepting_work = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, args=(interval_minutes,), daemon=True)
        self._thread.start()
        # 发布证据采样独立成线程并与分钟边界对齐：挂在交易快循环上会随循环变慢而
        # 整分钟丢样本，使 7 天覆盖率门禁在结构上不可达。
        if not (self._evidence_thread and self._evidence_thread.is_alive()):
            self._evidence_thread = threading.Thread(
                target=self._evidence_loop, name="release-evidence", daemon=True)
            self._evidence_thread.start()
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
        # 先关闭任务入口，再发停止信号；这样停止与 Web 手动触发并发时，
        # 不会在等待期间又创建一轮新的慢决策。
        with self._run_thread_lock:
            self._accepting_work = False
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
        review_worker = getattr(self, "_review_thread", None)
        if (review_worker and review_worker is not threading.current_thread()
                and review_worker.is_alive()):
            review_worker.join(timeout=max(0.0, deadline - time.monotonic()))
        review_alive = bool(review_worker and review_worker.is_alive())
        # 采样线程在 _stop_event 上等待，停止信号已置位，正常情况下立即退出。
        evidence_worker = getattr(self, "_evidence_thread", None)
        if (evidence_worker and evidence_worker is not threading.current_thread()
                and evidence_worker.is_alive()):
            evidence_worker.join(timeout=max(0.0, deadline - time.monotonic()))
        evidence_alive = bool(evidence_worker and evidence_worker.is_alive())
        return (not scheduler_alive and not worker_alive and not intel_alive
                and not review_alive and not evidence_alive)

    @staticmethod
    def _next_evidence_boundary(now: datetime) -> float:
        """返回距离下一个 UTC 分钟边界的秒数，用于把采样对齐到分钟。

        采样对齐是覆盖率门禁成立的前提：原实现挂在交易快循环上，循环一慢就整分钟
        没有采样（实测出现过 123 秒间隔，直接缺掉 16:06 整分钟）。门禁要求 7 天
        ≥99.9%，即 10080 分钟里最多缺 10 分钟，任何分钟级抖动都会超标。
        """
        boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        return max(0.05, (boundary - now).total_seconds())

    def _evidence_loop(self) -> None:
        """独立线程按分钟边界采集发布证据；失败不终止线程。

        与交易快循环解耦，使证据频率不再受单轮快循环耗时影响。采样准时不代表证据
        有效：健康状态仍由 ``_record_release_observation`` 依据真实心跳判定。
        """
        while not self._stop_event.is_set():
            if self._stop_event.wait(self._next_evidence_boundary(datetime.now(timezone.utc))):
                return
            try:
                self._record_release_observation()
            except Exception:
                # 证据采集失败不得终止线程，也不得影响交易循环；单次失败会体现在
                # 缺失分钟上，由覆盖率门禁暴露。
                continue

    def _loop(self, interval_minutes: float) -> None:
        fast_interval = float(self.settings.raw_settings.get("decision_cycle", {}).get("fast_interval_seconds", 30))
        next_slow = time.monotonic()
        while not self._stop_event.is_set():
            # 发布门禁依赖连续、按版本隔离的运行证据；该写入只读内存/状态，
            # 不会触发行情、账户或订单网络请求。主责采样在 _evidence_loop，
            # 这里保留一次作为冗余（同分钟写入由 observed_minute 幂等去重）。
            self._record_release_observation()
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

    def _start_review_worker(self) -> None:
        """启动受管理的复盘worker；复盘不得占用交易调度线程。"""
        if getattr(self, "_stop_event", None) is not None and self._stop_event.is_set():
            return
        with self._review_lock:
            if self._review_thread and self._review_thread.is_alive():
                return
            self._review_thread = threading.Thread(
                target=self._review_worker_loop, name="review-worker", daemon=True,
            )
            self._review_thread.start()

    def _review_worker_loop(self) -> None:
        """串行处理复盘队列；失败只记录审计，不影响快慢交易循环。"""
        while not self._stop_event.is_set():
            try:
                review_date = self._review_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if self._stop_event.is_set():
                    continue
                if self.db.fetch_one(
                    "SELECT review_date FROM daily_reviews WHERE review_date=?",
                    (review_date,),
                ):
                    continue
                cfg = self.settings.raw_settings.get("review", {}) or {}
                llm = None
                model = None
                if bool(cfg.get("use_llm", True)):
                    try:
                        client, model = build_llm_for_agent(self.settings, "review_agent")
                        if not isinstance(client, MockLLMClient):
                            llm = client
                    except Exception:
                        llm = None
                        model = None
                from agents.review import run_review_agent
                run_review_agent(
                    self.db, self.settings.reviews_dir, review_date=review_date,
                    llm=llm, model=model, logger=self.logger,
                    total_timeout_seconds=float(cfg.get("total_timeout_seconds", 120.0)),
                )
                self.db.log_risk_event(
                    event_type="auto_review_done", severity="info",
                    details=f"自动复盘已生成：{review_date}", action_taken="none",
                )
            except Exception as exc:
                self.db.log_risk_event(
                    event_type="auto_review_error", severity="warn",
                    details=f"{review_date}: {type(exc).__name__}: {exc}",
                    action_taken="skip",
                )
            finally:
                with self._review_lock:
                    self._review_pending.discard(review_date)
                self._review_queue.task_done()

    def _run_reviews_inline(self, missing: list[str], cfg: dict[str, Any]) -> list[str]:
        """兼容未完整构造的离线夹具；生产实例始终走队列worker。"""
        from agents.review import run_review_agent
        llm = None
        model = None
        if bool(cfg.get("use_llm", True)):
            try:
                client, model = build_llm_for_agent(self.settings, "review_agent")
                if not isinstance(client, MockLLMClient):
                    llm = client
            except Exception:
                llm = None
                model = None
        generated: list[str] = []
        for date in missing:
            try:
                run_review_agent(
                    self.db, self.settings.reviews_dir, review_date=date,
                    llm=llm, model=model, logger=self.logger,
                    total_timeout_seconds=float(cfg.get("total_timeout_seconds", 120.0)),
                )
                generated.append(date)
            except Exception as exc:
                self.db.log_risk_event(
                    event_type="auto_review_error", severity="warn",
                    details=f"{date}: {type(exc).__name__}: {exc}", action_taken="skip",
                )
        if generated:
            self.db.log_risk_event(
                event_type="auto_review_done", severity="info",
                details=f"自动复盘已生成：{', '.join(generated)}", action_taken="none",
            )
        return generated

    def _maybe_auto_review(self, now: datetime | None = None) -> list[str]:
        """到点发现复盘缺口并入队，返回本次新增的日期列表。"""
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

        # MagicMock/极早期离线夹具没有真实队列；生产TradingService在__init__已初始化。
        if not isinstance(getattr(self, "_review_queue", None), queue.Queue):
            return TradingService._run_reviews_inline(self, missing, cfg)
        queued: list[str] = []
        with self._review_lock:
            for date in missing:
                if date in self._review_pending:
                    continue
                self._review_pending.add(date)
                self._review_queue.put(date)
                queued.append(date)
        if queued:
            self._start_review_worker()
            self.db.log_risk_event(
                event_type="auto_review_queued", severity="info",
                details=f"自动复盘已入队：{', '.join(queued)}", action_taken="queue",
            )
        return queued

    def close(self) -> None:
        """幂等关闭线程、流、Broker、数据库及实例锁。"""
        if getattr(self, "_closed", False):
            return
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
            self._closed = True
