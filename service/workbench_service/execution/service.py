"""执行服务：把账本、风控、Demo Broker 组装成 API 可用的门面。

职责边界（docs/03 §7.1）：
- P1（``/api/risk/evaluate``）：构造确定性上下文 → 规则 → 一次性 approval_token；
- P2（``/api/execution/order``）：校验令牌 → 用**实时事实**复检 → 下单；
- 只读：账户/仓位/订单/成交/权益曲线（交易所优先，账本兜底并标注 stale）；
- 启动对账是**只读**的：不自动下单、不自动挂保护（M1.2 约束）。
"""

from __future__ import annotations

import functools
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import ServiceError
from ..strategies import StrategyStore
from ..strategies.locking import execution_guard
from ..strategies.execution import validate_entry, reserve_signal, record_result, restore_position_metadata
from ..ledger.numbers import dec_text, to_decimal
from ..ledger.store import Ledger, bucket_iso, utc_now
from ..risk.approval import ApprovalError, ApprovalManager
from ..risk.config import RiskLimits
from ..risk.models import OrderPlan, PlanValidationError, PositionView, RiskContext, SymbolFiltersView
from ..risk.rules import BLOCKING_CODES
from ..risk.service import RiskService
from ..risk.state import RiskStateError, RiskStateStore
from .broker import BrokerSnapshot, DemoBroker, NormalizedPosition
from .client import DemoBinanceClient
from .credentials import DemoCredentials, load_demo_credentials, load_live_credentials
from .errors import DemoApiError, ExecutionConfigError, ExecutionRejected, OutboxUncertain
from .lease import ExecutionLeaseStore, LeaseError
from .outbox import ACTIVE_STATUSES, NOT_FOUND_REASON, TERMINAL_STATUSES, OrderOutbox
from .protection import ProtectionFailurePolicy

__all__ = ["ExecutionService", "build_execution_service"]

logger = logging.getLogger("workbench.execution")

_SERVICE_DIR = Path(__file__).resolve().parents[2]


def _serialized(method):
    """Serialize account-mutating commands behind one re-entrant lock.

    A01: control-state changes and P2 order submission must share one boundary.
    Without it, pause can succeed while an already-approved P2 request is still
    between its last control read and ``open_market``.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._exec_lock:
            with execution_guard(self.ledger.path):
                return method(self, *args, **kwargs)

    return wrapper


@dataclass(frozen=True, slots=True)
class _EquityInfo:
    session: Decimal | None
    exchange: Decimal | None
    available: bool
    source: str
    age_ms: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session": None if self.session is None else dec_text(self.session),
            "exchange": None if self.exchange is None else dec_text(self.exchange),
            "available": self.available,
            "source": self.source,
            "age_ms": self.age_ms,
        }


class ExecutionService:
    def __init__(
        self,
        settings: Settings,
        ledger: Ledger,
        *,
        limits: RiskLimits,
        state: RiskStateStore,
        approvals: ApprovalManager,
        risk: RiskService,
        outbox: OrderOutbox,
        client: DemoBinanceClient | None = None,
        broker: DemoBroker | None = None,
        credentials: DemoCredentials | None = None,
        clock=time.time,
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.limits = limits
        self.state = state
        self.approvals = approvals
        self.risk = risk
        self.outbox = outbox
        self.client = client
        self.broker = broker
        self.credentials = credentials
        self._clock = clock
        self.leases = ExecutionLeaseStore(ledger, clock=self._clock)
        self.strategies = StrategyStore(ledger.path)
        self._started = False
        self._start_lock = threading.Lock()
        # A01: one account-level mutation boundary for submit/close/repair/control.
        self._exec_lock = threading.RLock()
        # A06: independent read-only supervision loop (started by the service
        # assembly, stopped by app shutdown). It never submits orders.
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._monitor_interval_s = 30.0
        self._last_reconcile_at = 0.0
        self._last_reconcile: dict[str, Any] | None = None
        self._reconcile_interval_s = 30.0
        self.startup: dict[str, Any] = {"state": "not_started"}
        # T03 §8·B4: 保护失败处置策略（有限回查 → 已配置的风险减少动作）。
        self.protection_policy: ProtectionFailurePolicy = (
            getattr(broker, "protection_policy", None) or ProtectionFailurePolicy()
        )
        # T03 §8·B5: 最近一次启动/恢复的未决项收敛结果（对外可解释）。
        self._recovery: dict[str, Any] = {"state": "not_started", "converged": None}
        # T18 §8·C2：income（资金费/划转）自动回填。以前只能靠操作员手动
        # POST /api/sync-income，没人点就一直 funding_known=false，策略净盈亏只能
        # 显示 null。这里把回填挂到监督循环上：最小间隔 + 单次窗口 + 游标推进，
        # 无人值守也能收敛，且只读交易所（GET /fapi/v1/income），不产生下单路径。
        self._income_sync_enabled = bool(getattr(settings, "income_sync_enabled", True))
        self._income_sync_interval_s = max(
            1.0, float(getattr(settings, "income_sync_interval_s", 900.0) or 900.0)
        )
        self._income_sync_min_interval_s = max(
            0.0, float(getattr(settings, "income_sync_min_interval_s", 60.0) or 60.0)
        )
        self._income_sync_window_s = max(
            3600.0, float(getattr(settings, "income_sync_window_s", 604800.0) or 604800.0)
        )
        self._income_sync_overlap_s = max(
            0.0, float(getattr(settings, "income_sync_overlap_s", 60.0) or 0.0)
        )
        self._income_sync_lock = threading.Lock()
        # 最近一次**尝试**（含失败）用于节流；最近一次**成功**用于对外展示。
        self._last_income_sync_at: float | None = None
        self._last_income_sync_ok_at: float | None = None
        self._last_income_sync: dict[str, Any] | None = None
        self._last_income_sync_error: str | None = None
        # T20：启动对账失败（多半是交易所不可达）后按节奏重试，而不是“一次失败
        # 永久 error”，也不是每个请求都重来一遍。
        self._startup_retry_at = 0.0
        self._startup_retry_s = max(
            5.0, float(getattr(settings, "exchange_failure_cooldown_s", 15.0) or 15.0)
        )

    # ------------------------------------------------------------------ 启动
    @property
    def configured(self) -> bool:
        return self.broker is not None

    def ensure_started(self) -> dict[str, Any]:
        """首次请求时执行一次只读启动对账（不自动下单）。"""

        with self._start_lock:
            if self._started and self.startup.get("state") == "error":
                # 上一轮启动对账失败（交易所不可达等）：冷却期内直接复用失败结论，
                # 不每次请求都重打一轮上游；到点后再试一次。
                if self._clock() < self._startup_retry_at:
                    return self.startup
            if self._started or self.broker is None:
                if not self._started:
                    self.startup = {
                        "state": "read_only",
                        "at": utc_now(),
                        "reason": "E_DEMO_CREDENTIALS_MISSING",
                    }
                    self._started = True
                return self.startup
            started_at = utc_now()
            try:
                offset = self.client.sync_time() if self.client else None
                filters = self.broker.load_filters(force=True)
                # T03 §8·B5：先恢复 SUBMITTING/UNKNOWN 等未决项（只查询不重发），
                # 再核对仓位与保护；未收敛前禁止受影响账户的新开仓。
                recovery = self._recover_pending()
                reconcile = self.broker.reconcile()
                self._last_reconcile_at = self._clock()
                self._last_reconcile = reconcile
                protection = self._inspect_protection(reconcile)
                converged = bool(recovery.get("converged"))
                protected = protection.get("ok")
                self.startup = {
                    "state": "ok" if converged and protected is not False else "recovering",
                    "at": started_at,
                    "time_offset_ms": offset,
                    "symbols": len(filters),
                    "reconcile": reconcile,
                    "recovery": recovery,
                    "protection": protection,
                    "read_only": True,
                    "new_entry_blocked": (not converged) or protected is False,
                }
                self.ledger.log_risk_event(
                    event_type="startup_reconcile",
                    severity="info" if converged else "warn",
                    action="recover_pending_then_verify_positions",
                    details={
                        "symbols": len(filters),
                        "positions": reconcile.get("positions"),
                        "protection_missing": reconcile.get("protection_missing"),
                        "recovery_converged": converged,
                        "recovery_unresolved": recovery.get("unresolved"),
                        "protection_ok": protected,
                    },
                )

            except Exception as exc:  # noqa: BLE001 - 启动失败不阻断只读接口
                self.startup = {
                    "state": "error",
                    "at": started_at,
                    "error": f"{type(exc).__name__}: {exc}",
                    "retry_at_s": self._startup_retry_s,
                    "read_only": True,
                }
                self._startup_retry_at = self._clock() + self._startup_retry_s
                logger.warning("startup_reconcile_failed error=%s", type(exc).__name__)
            self._started = True
            return self.startup

    # ------------------------------------------------------------------ 启动恢复
    def _recover_pending(self) -> dict[str, Any]:
        """启动/恢复入口：只查询不重发地收敛未决项（T03 §8·B5）。

        顺序：UNKNOWN → 按原 client_order_id 查询；陈旧的 SUBMITTING → 查询后
        写入明确终态；orders 表中工作态永停的行 → 用交易所 open 列表 + 原标识
        查询两个事实收敛。查询失败保持原状态并继续阻断新开仓。
        """

        if self.broker is None or self.outbox is None:
            return {
                "at": utc_now(),
                "state": "read_only",
                "before": {},
                "after": {},
                "converged": True,
                "unresolved": 0,
                "pending": [],
            }
        before = self.outbox.counts()
        unknown_query = self.outbox.reconcile(self.broker._query_unknown_row)
        stale_submitting = self.broker._resolve_stale_submitting()
        convergence = self.broker.converge_stale_orders()
        after = self.outbox.counts()
        pending = [
            {
                "client_order_id": row.get("client_order_id"),
                "business_key": row.get("business_key"),
                "status": row.get("status"),
                "display": _pending_display(row.get("status")),
                "created_at": row.get("created_at"),
            }
            for row in self.outbox.active(50)
        ]
        unresolved = int(after.get("unknown", 0)) + int(after.get("submitting", 0))
        result = {
            "at": utc_now(),
            "before": before,
            "after": after,
            "unknown_query": unknown_query,
            "stale_submitting": stale_submitting,
            "converged_orders": convergence.get("converged", []),
            "convergence_checked": convergence.get("checked", 0),
            "pending": pending,
            "unresolved": unresolved,
            "converged": unresolved == 0,
        }
        self._recovery = result
        self.ledger.log_risk_event(
            event_type="pending_recovery",
            severity="info" if result["converged"] else "warn",
            rule_id="R17",
            action="recover_pending_query_only",
            details={
                "before": before,
                "after": after,
                "unresolved": unresolved,
                "converged": result["converged"],
                "converged_orders": result["converged_orders"],
            },
        )
        return result

    def _inspect_protection(self, reconcile: dict[str, Any] | None = None) -> dict[str, Any]:
        """核对交易所确认的实际仓位与保护覆盖（只读，不挂单）。"""

        if self.broker is None:
            return {"ok": None, "reason": "credentials_missing", "positions": 0, "missing": []}
        try:
            snapshot = self.broker.snapshot()
        except Exception as exc:  # noqa: BLE001 - 读不到就按“未证明”处理
            return {
                "ok": None,
                "reason": "exchange_unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "positions": 0,
                "missing": [],
            }
        ok = snapshot.protection_ok()
        missing = snapshot.protection_missing()
        result = {
            "ok": ok,
            "positions": len(snapshot.positions),
            "missing": missing,
            "detail": snapshot.protection,
            "stale": bool(snapshot.stale),
            "reconcile_protection_ok": None
            if reconcile is None
            else reconcile.get("protection_ok"),
        }
        if ok is False:
            self.ledger.log_risk_event(
                event_type="startup_protection_missing",
                severity="critical",
                rule_id="R23",
                action="block_new_entries_no_flat_close",
                details={"missing": missing},
            )
        return result

    def recover_pending(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """运维入口：手动触发一次未决项收敛（只查询、不重发）。"""

        self._require_broker()
        result = self._recover_pending()
        result.update(
            {
                "venue": self.settings.venue,
                "demo_only": True,
                "ledger_id": self.ledger.ledger_id,
                "request_id": str((payload or {}).get("request_id") or ""),
            }
        )
        return result

    def _enforce_protection(self) -> dict[str, Any]:
        """监督周期：核对/补挂保护；确认失败时按策略减少风险并记录。"""

        if self.broker is None:
            return {"ok": None, "checked": 0, "repaired": []}
        try:
            result = self.broker.enforce_protection()
        except Exception as exc:  # noqa: BLE001 - 监督周期必须存活
            logger.warning(
                "protection_enforce_failed error=%s", type(exc).__name__
            )
            return {"ok": False, "checked": 0, "repaired": [], "error": type(exc).__name__}
        blocking = [
            item
            for item in result.get("repaired", [])
            if str(item.get("disposition") or "none")
            in {"reduce_unknown", "reduce_position_incomplete"}
        ]
        if blocking:
            # 系统仓位确认无保护且减仓未完成：切 close_only（只允许减仓）。
            # 外部仓位/策略 block_new_risk 只靠 readiness + R23 阻断新开仓，
            # 一旦保护恢复即可自动解除，不需要人工改控制态。
            state = "close_only"
            self.state.set_control(
                state,
                "protection unrecoverable: "
                + ",".join(str(item.get("symbol")) for item in blocking)[:160],
                updated_by="risk_engine",
            )
            self.leases.revoke_all("control:close_only")
        return result

    # ------------------------------------------------------------------ 监督 worker

    def start_monitor(self, interval_s: float | None = None) -> bool:
        """Start a read-only reconcile/loss-streak loop (A06).

        This is deliberately not a trading worker: it never calls P2, close,
        repair or control. It is idempotent and can be stopped deterministically
        from the app lifespan.
        """

        if self.broker is None:
            return False
        if interval_s is not None:
            self._monitor_interval_s = max(1.0, float(interval_s))
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return False
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="workbench-supervisor",
            daemon=True,
        )
        self._monitor_thread.start()
        return True

    def stop_monitor(self, timeout_s: float = 2.0) -> None:
        thread = self._monitor_thread
        if thread is None:
            return
        self._monitor_stop.set()
        if thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_s)))
        self._monitor_thread = None

    def _monitor_loop(self) -> None:
        while not self._monitor_stop.wait(self._monitor_interval_s):
            try:
                with self._exec_lock:
                    if self.broker is None:
                        return
                    # 账户/权益/连亏刷新 + 仓位/outbox 对账；随后按交易所确认的
                    # 实际仓位核对保护覆盖（缺失则补挂，确认失败按策略减仓），
                    # 最后处理 time_stop 到期。监督循环本身绝不下新开仓单。
                    self._heartbeat()
                    self.reconcile()
                    self._enforce_protection()
                    self._enforce_time_stop()
            except Exception as exc:  # noqa: BLE001 - supervisor must survive a failed cycle
                logger.warning("supervisor_cycle_failed error=%s", type(exc).__name__)
            # T18 §8·C2：income 回填在账户锁**之外**执行——交易所 GET 慢或失败都不会
            # 拖住、也不会阻断同一轮的权益快照与保护核对（心跳已先落库）。
            self._income_sync_tick()

    def _enforce_time_stop(self) -> None:
        """Opt-in position management for system-managed positions (T03 §8·B6).

        - 建仓时把 opened_at / deadline_at / time_stop 来源计划版本持久化；
          重启后按**原期限**判断，不因配置变化或进程重启重新计时；
        - 外部仓位（origin=EXTERNAL）不自动套用系统策略；
        - 到期处置用由仓位身份派生的稳定命令标识，同一仓位只处置一次，
          不逐周期重复平仓。
        """

        if self.broker is None:
            return
        restore_position_metadata(self.strategies)
        hours = float(getattr(self.settings, "auto_time_stop_hours", 0) or 0)
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        for row in self.ledger.list_positions():
            symbol = str(row.get("symbol") or "")
            side = str(row.get("position_side") or "")
            origin = str(row.get("origin") or "OPENED_BY_WORKBENCH").upper()
            if origin == "EXTERNAL":
                # 外部仓位不自动套用系统策略。
                continue
            deadline = _parse_dt(row.get("deadline_at"))
            if deadline is None:
                if ";strategy:" in str(row.get("time_stop_source") or ""):
                    continue  # Confirmed strategy explicitly has no time stop.
                if hours <= 0:
                    continue
                opened = _parse_dt(row.get("opened_at"))
                if opened is None:
                    continue
                deadline = opened + timedelta(hours=hours)
                source = f"legacy_derive:{hours}h@plan"
                self.ledger.set_position_time_stop(
                    symbol,
                    side,
                    opened_at=row.get("opened_at"),
                    deadline_at=_iso_utc(deadline),
                    source=source,
                )
            if now < deadline:
                continue
            request_id = _stable_time_stop_id(
                symbol, side, row.get("opened_at") or row.get("deadline_at") or ""
            )
            if not self.ledger.operation_start(
                request_id, "time_stop_close", symbol, "time_stop"
            ):
                # 同一仓位身份已处置过：不逐周期重复平仓。
                continue
            summary = {
                "symbol": symbol,
                "deadline_at": row.get("deadline_at"),
                "time_stop_source": row.get("time_stop_source"),
            }
            try:
                result = self.close(
                    {"symbol": symbol, "qty": row.get("qty"), "reason": "time_stop"},
                    trace_id="supervisor-time-stop",
                )
            except ServiceError as exc:
                uncertain = exc.status_code == 202 or exc.code in {
                    "E_FILL_UNCONFIRMED",
                    "E_ORDER_UNKNOWN",
                }
                self.ledger.operation_finish(
                    request_id, "unknown" if uncertain else "failed", code=exc.code, summary=summary
                )
                logger.warning(
                    "time_stop_close_failed symbol=%s code=%s", symbol, exc.code
                )
                continue
            except Exception as exc:  # noqa: BLE001 - 未知结果不重复平仓
                self.ledger.operation_finish(
                    request_id, "unknown", code="E_OPERATION_RESULT_UNKNOWN", summary=summary
                )
                logger.warning(
                    "time_stop_close_unknown symbol=%s error=%s", symbol, type(exc).__name__
                )
                continue
            closed = bool(result.get("closed"))
            self.ledger.operation_finish(
                request_id, "succeeded" if closed else "partial", summary={**summary, "closed": closed}
            )
            self.ledger.log_risk_event(
                event_type="time_stop_close",
                severity="warn",
                rule_id="TIME_STOP",
                action="close_stable_command",
                details={
                    "symbol": symbol,
                    "command_id": request_id,
                    "closed": closed,
                    "deadline_at": row.get("deadline_at"),
                    "time_stop_source": row.get("time_stop_source"),
                },
            )


    # ------------------------------------------------------------------ 权益
    def _baseline(self, exchange_equity: Decimal) -> Decimal:
        stored = self.ledger.get_meta("equity_baseline")
        value = to_decimal(stored)
        if value is not None:
            return value
        baseline = max(Decimal("0"), exchange_equity - Decimal(str(self.limits.initial_equity)))
        self.ledger.set_meta("equity_baseline", dec_text(baseline))
        self.ledger.set_meta("equity_baseline_at", utc_now())
        self.ledger.set_meta("equity_anchor_id", self.state.anchor_id)
        return baseline

    def _equity_info(self, snapshot: BrokerSnapshot | None) -> _EquityInfo:
        if snapshot is not None and snapshot.error is None and snapshot.equity_exchange is not None:
            exchange = snapshot.equity_exchange
            baseline = self._baseline(exchange)
            age_ms = int(max(0.0, self._clock() - snapshot.fetched_at) * 1000)
            return _EquityInfo(
                session=exchange - baseline,
                exchange=exchange,
                available=True,
                source="exchange",
                age_ms=age_ms,
            )
        last = self.ledger.latest_equity()
        if last is not None:
            return _EquityInfo(
                session=to_decimal(last.get("equity_session")),
                exchange=to_decimal(last.get("equity_exchange")),
                available=False,
                source="ledger",
                age_ms=None,
            )
        return _EquityInfo(None, None, False, "none", None)

    def _persist_equity(self, snapshot: BrokerSnapshot, equity: _EquityInfo) -> None:
        if equity.exchange is None or equity.session is None:
            return
        risk_state = self.state.snapshot()
        self.ledger.insert_equity_snapshot(
            {
                "ledger_id": self.ledger.ledger_id,
                "ts_bucket": bucket_iso(30),
                "equity_exchange": equity.exchange,
                "equity_session": equity.session,
                "wallet_balance": snapshot.wallet_balance,
                "available": snapshot.available_balance,
                "upnl": snapshot.unrealized_pnl,
                "margin_used": sum(
                    (position.margin or Decimal("0")) for position in snapshot.positions
                ),
                "day_start_equity": risk_state.get("day_start_equity"),
                "peak_equity": risk_state.get("peak_equity"),
                "daily_loss_ratio": risk_state.get("daily_loss_ratio"),
                "drawdown_ratio": risk_state.get("drawdown_ratio"),
                "source": "loop",
            }
        )

    # ------------------------------------------------------------------ 只读取数
    def _snapshot_readonly(
        self, *, cooldown: bool = True
    ) -> tuple[BrokerSnapshot | None, dict[str, Any]]:
        """三处只读端点唯一的取数路径（T20 §2.2）。

        取数失败**不抛**：交易所不可达是“数据未知”，不是 5xx。返回的 meta 一次
        说清 source / stale / last_success_at / unknown，界面据此显示“最后成功时间”，
        既不转圈，也不回退成 0 或“一切正常”。
        """

        if self.broker is None:
            meta = self._read_meta(None)
            meta["unknown"].append(
                "E_DEMO_CREDENTIALS_MISSING：未配置 Demo 凭据，无法读取交易所事实"
            )
            return None, meta
        snapshot: BrokerSnapshot | None = None
        try:
            snapshot = self.broker.snapshot(cooldown=cooldown)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - 读取失败给出 stale 状态
            logger.warning("snapshot_failed error=%s", type(exc).__name__)
            snapshot = None
        return snapshot, self._read_meta(snapshot)

    def _read_meta(self, snapshot: BrokerSnapshot | None) -> dict[str, Any]:
        """把一次快照翻译成「从哪来 / 新不新 / 上次成功何时 / 为什么未知」。

        纯函数（只读 health，不触发上游）：status/positions 复用同一份 meta，
        避免三个端点各说一套。
        """

        meta: dict[str, Any] = {
            "source": "none",
            "stale": True,
            "last_success_at": None,
            "cooldown_until": None,
            "cooldown_remaining_s": 0.0,
            "read_timeout_s": getattr(self.settings, "read_timeout_s", None),
            "read_budget_s": getattr(self.settings, "exchange_read_budget_s", None),
            "unknown": [],
        }
        health: dict[str, Any] = {}
        if self.broker is not None:
            reader = getattr(self.broker, "read_health", None)
            if callable(reader):
                try:
                    health = reader()
                except Exception:  # noqa: BLE001 - 健康信息缺失不影响只读接口
                    health = {}
        meta["last_success_at"] = health.get("last_success_at")
        meta["cooldown_until"] = health.get("cooldown_until")
        meta["cooldown_remaining_s"] = health.get("cooldown_remaining_s") or 0.0
        meta["exchange_read"] = health
        if snapshot is not None and snapshot.error is None:
            meta["source"] = "exchange"
            meta["stale"] = bool(snapshot.stale)
            meta["last_success_at"] = snapshot.last_success_at or meta["last_success_at"]
            if meta["stale"]:
                meta["unknown"].append(
                    "EXCHANGE_STALE：本次数据来自交易所缓存，不是最新事实"
                )
            return meta
        if snapshot is not None:
            meta["last_success_at"] = snapshot.last_success_at or meta["last_success_at"]
            meta["unknown"].append(
                f"E_EXCHANGE_UNAVAILABLE：读取交易所失败（{snapshot.error}），"
                "显示最后一次成功的数据"
            )
        else:
            meta["unknown"].append("E_EXCHANGE_UNAVAILABLE：交易所不可达，当前没有可用快照")
        if meta["cooldown_until"]:
            meta["unknown"].append(
                f"E_EXCHANGE_COOLDOWN：{meta['cooldown_until']} 前不再请求交易所（失败冷却）"
            )
        return meta

    # ------------------------------------------------------------------ 心跳
    def _heartbeat(
        self, *, read_path: bool = False
    ) -> tuple[BrokerSnapshot | None, _EquityInfo, dict[str, Any], dict[str, Any]]:
        """刷新账户/权益/水位。

        ``read_path=True`` 表示这次调用服务于只读展示端点：尊重失败冷却，故障期间
        不再反复打上游；交易评估（P1/P2）保持 ``read_path=False``，在有界预算内
        重试一次并失败关闭——绝不能拿旧快照放行。
        """

        # T20：整轮“启动对账 + 取快照”共用一份读预算——冷启动遇到死上游时，
        # 端点也不会被“启动预检 + 快照”两段超时叠加拖成十几秒。
        budget = float(getattr(self.settings, "exchange_read_budget_s", 0.0) or 0.0)
        if self.client is not None and budget > 0 and hasattr(self.client, "read_budget"):
            with self.client.read_budget(budget):
                return self._heartbeat_once(read_path=read_path)
        return self._heartbeat_once(read_path=read_path)

    def _heartbeat_once(
        self, *, read_path: bool
    ) -> tuple[BrokerSnapshot | None, _EquityInfo, dict[str, Any], dict[str, Any]]:
        self.ensure_started()
        snapshot, _read_meta = self._snapshot_readonly(cooldown=read_path)
        equity = self._equity_info(snapshot)
        # 先按成交流水刷新连亏/冷却，再 sync 水位，保证返回的 risk_state 已是最新。
        self._refresh_loss_streak()
        risk_state = self.state.sync(
            equity.session if equity.available else None, equity_available=equity.available
        )
        if snapshot is not None and snapshot.error is None and equity.available:
            self._persist_equity(snapshot, equity)
        control = self.state.control()
        return snapshot, equity, risk_state, control.as_dict()

    # ------------------------------------------------------------------ 连亏/冷却
    def _merged_close_results(self) -> list[tuple[str, Decimal | None, int]]:
        """把同一 ``client_order_id`` 的分批成交归并成一笔平仓结果。

        返回 ``[(client_order_id, net_pnl, last_fill_ms), ...]``，按时间倒序。
        ``net_pnl`` 已合并**报价币**手续费；非报价币手续费无法在本账本内换算，
        该笔返回 ``None``（连亏扫描遇到未知净额时停止，不把亏损误判为盈利）。
        只有 ``purpose='EXIT'`` 的成交参与连亏。
        """

        quote_assets = {"", "USDT", "USDC", "BUSD"}
        merged: dict[str, dict[str, Any]] = {}
        for row in self.ledger.recent_close_fills(limit=200):
            client_order_id = str(row.get("client_order_id") or "")
            if not client_order_id:
                continue
            item = merged.setdefault(
                client_order_id,
                {"net": Decimal("0"), "ts": 0, "unconverted": set()},
            )
            realized = to_decimal(row.get("realized_pnl"), Decimal("0")) or Decimal("0")
            commission = to_decimal(row.get("commission"), Decimal("0")) or Decimal("0")
            commission_asset = str(row.get("commission_asset") or "").upper()
            # A04: commission is a cost, never income.
            if commission_asset in quote_assets:
                item["net"] += realized - commission
            else:
                item["net"] += realized
                item["unconverted"].add(commission_asset)
            item["ts"] = max(int(item["ts"]), int(row.get("trade_time_ms") or 0))
        ordered = sorted(merged.items(), key=lambda pair: pair[1]["ts"], reverse=True)
        return [
            (cid, None if item["unconverted"] else item["net"], item["ts"])
            for cid, item in ordered
        ]

    def _refresh_loss_streak(self) -> None:
        """维护 R20 连亏/冷却状态。

        规则（docs/03 §6 R20）：
        - 按 ``client_order_id`` 归并分批成交后，从最近一笔平仓往回数连续亏损笔数；
        - ``cooldown_until`` 以**最后一笔亏损平仓时间**为锚点一次性设置为
          ``last_loss_at + consecutive_loss_cooldown_hours``，轮询/心跳**不会续期**；
        - 只在 streak 变化时写入；到期后清除 ``cooldown_until`` 并保留
          ``consecutive_losses``（下一次盈利平仓才把 streak 归零）。
        """

        results = self._merged_close_results()
        streak = 0
        last_loss_ms: int | None = None
        for _cid, net, ts in results:  # 时间倒序
            if net is None:
                # 手续费币种无法换算：不要用“已实现盈亏”冒充净盈亏，
                # 更不能把可能有成本的平仓判成盈利。
                logger.warning("close_fill_commission_unconverted; loss streak scan stopped")
                break
            if net < 0:
                streak += 1
                if last_loss_ms is None:
                    last_loss_ms = ts
            else:
                break

        state = self.ledger.load_risk_state() or {}
        current_streak = int(state.get("consecutive_losses") or 0)
        current_until = state.get("cooldown_until")
        now_ms = int(self._clock() * 1000)

        if streak != current_streak:
            until_iso = None
            if (
                self.limits.enable_loss_streak
                and self.limits.consecutive_losses > 0
                and streak >= self.limits.consecutive_losses
                and last_loss_ms is not None
            ):
                until_ms = int(last_loss_ms) + int(
                    Decimal(str(self.limits.consecutive_loss_cooldown_hours))
                    * Decimal(3600_000)
                )
                if until_ms > now_ms:
                    until_iso = (
                        datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc)
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z")
                    )
                # 锚点已在过去（例如导入历史成交）：直接视为已到期，不写冷却。
            self.state.set_cooldown(until_iso, consecutive_losses=streak)
            return

        # streak 未变化：只做一次到期清理，绝不再延长 cooldown。
        if current_until:
            until_ms = _parse_iso_ms(str(current_until))
            if until_ms is not None and now_ms >= until_ms:
                self.state.set_cooldown(None, consecutive_losses=streak)

    # ------------------------------------------------------------------ 只读
    def _maybe_periodic_reconcile(self, *, exchange_ok: bool = True) -> None:
        """周期对账的最小实现：status 轮询驱动，≥30s 一次，只读、幂等、失败不抛。

        T20：``exchange_ok=False``（本轮没取到交易所事实）时直接跳过，不叠加
        新的上游请求——否则一个故障轮次会在 30s 节拍上再花掉一份读预算。
        """

        if self.broker is None or not self._started:
            return
        if not exchange_ok:
            self._last_reconcile = {
                "at": utc_now(),
                "skipped": "E_EXCHANGE_UNAVAILABLE",
                "note": "交易所不可达，本轮跳过对账；不叠加新的上游请求",
            }
            return
        now = self._clock()
        if now - self._last_reconcile_at < self._reconcile_interval_s:
            return
        self._last_reconcile_at = now
        try:
            self._last_reconcile = self.broker.reconcile()
        except Exception as exc:  # noqa: BLE001 - 周期对账失败不影响只读响应
            self._last_reconcile = {
                "at": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
            }

    def status(self) -> dict[str, Any]:
        snapshot, equity, risk_state, control = self._heartbeat(read_path=True)
        read_meta = self._read_meta(snapshot)
        # 交易所不可达时不再叠加一轮上游往返：本轮对账直接跳过并说明原因。
        self._maybe_periodic_reconcile(exchange_ok=read_meta["source"] == "exchange")
        positions = self._positions_from(snapshot)
        outbox_counts = self.outbox.counts() if self.outbox else {}
        blockers = self._blocking_reasons(snapshot, equity, risk_state, control, outbox_counts)
        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "base_url": self.settings.base_url,
            "ledger_id": self.ledger.ledger_id,
            "ledger_schema_version": self.ledger.schema_version(),
            "schema_version": self.settings.schema_version,
            "credentials_configured": self.configured,
            "credentials": (
                self.credentials.as_public_dict()
                if self.credentials is not None
                else {"configured": False}
            ),
            "exchange": {
                # T20：一次说清来源/新鲜度/最后成功时间/为什么未知，界面不再靠猜。
                "source": read_meta["source"],
                "reachable": read_meta["source"] == "exchange",
                "stale": read_meta["stale"],
                "last_success_at": read_meta["last_success_at"],
                "cooldown_until": read_meta["cooldown_until"],
                "cooldown_remaining_s": read_meta["cooldown_remaining_s"],
                "read_timeout_s": read_meta["read_timeout_s"],
                "read_budget_s": read_meta["read_budget_s"],
                "unknown": read_meta["unknown"],
                "fetched_at": snapshot.fetched_at_iso if snapshot else None,
                "position_mode": snapshot.position_mode if snapshot else "UNKNOWN",
                "error": None if snapshot is None else snapshot.error,
            },
            "account": self._account_payload(snapshot, equity),
            "equity": equity.as_dict(),
            "positions": positions,
            "protection": {
                "ok": None if snapshot is None else snapshot.protection_ok(),
                "positions": len(positions),
                "missing": [] if snapshot is None else snapshot.protection_missing(),
                "detail": {} if snapshot is None else snapshot.protection,
            },
            "open_orders": {
                "count": 0 if snapshot is None else len(snapshot.open_orders),
                "items": [] if snapshot is None else snapshot.open_orders[:20],
            },
            "outbox": {
                **outbox_counts,
                "unknown_rows": self.outbox.unknown(5) if self.outbox else [],
                # 提交中/结果未知只能显示“待确认”，不得显示成确定失败。
                "pending": _annotate_pending(
                    self.outbox.pending_confirmation(20) if self.outbox else []
                ),
                "pending_confirmation_count": int(outbox_counts.get("unknown", 0))
                + int(outbox_counts.get("submitting", 0)),
            },
            "recovery": self._recovery,
            # T18 §8·C2：资金费自动回填的可读状态（关闭/节流/失败都显式可见）。
            "income_sync": self.income_sync_status(),
            "risk": risk_state,
            "performance": self.performance_payload(equity),
            "control": control,
            "startup": self.startup,
            "reconcile": self._last_reconcile,
            "readiness": {"new_entry_allowed": not blockers, "reasons": blockers},
            "checked_at": utc_now(),
        }

    def positions_payload(self) -> dict[str, Any]:
        snapshot, _equity, _risk, _control = self._heartbeat(read_path=True)
        read_meta = self._read_meta(snapshot)
        positions = self._positions_from(snapshot)
        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            # 只在真拿到交易所事实时才说 exchange；否则沿用账本并标注 stale。
            "source": "exchange" if read_meta["source"] == "exchange" else "ledger",
            "stale": read_meta["stale"],
            "last_success_at": read_meta["last_success_at"],
            "unknown": read_meta["unknown"],
            "positions": positions,
            "count": len(positions),
        }

    def orders_payload(self, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
        now = self._clock()
        rows = [
            _annotate_order(row, now)
            for row in self.ledger.list_orders_with_outbox(status=status, limit=limit)
        ]
        pending = [row for row in rows if row.get("pending_confirmation")]
        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "orders": rows,
            "count": len(rows),
            "pending_confirmation_count": len(pending),
            "pending_confirmation_ids": [row.get("client_order_id") for row in pending],
        }

    def fills_payload(self, *, limit: int = 100) -> dict[str, Any]:
        rows = self.ledger.list_fills(limit=limit)
        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "fills": rows,
            "count": len(rows),
        }

    def equity_curve_payload(self, *, range_s: int = 86400, bucket_s: int = 60) -> dict[str, Any]:
        since = (
            datetime.now(timezone.utc) - timedelta(seconds=max(60, int(range_s)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        rows = self.ledger.list_equity_curve(since=since)
        points = _downsample(rows, max(10, int(bucket_s)))
        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "range_s": range_s,
            "bucket_s": bucket_s,
            "points": points,
            "count": len(points),
        }

    # ---------------------------------------------------- §8·C2 资金费自动回填（T18）
    def income_sync_settings(self) -> dict[str, Any]:
        """T18：自动回填参数（默认值可读，便于诊断“为什么还没收敛”）。"""

        return {
            "enabled": self._income_sync_enabled,
            "interval_s": self._income_sync_interval_s,
            "min_interval_s": self._income_sync_min_interval_s,
            "window_s": self._income_sync_window_s,
            "overlap_s": self._income_sync_overlap_s,
        }

    def income_sync_status(self) -> dict[str, Any]:
        """资金费回填的可读状态：为何已知/未知、上次成败、下次到期。

        “关闭开关”不是静默：这里给出 enabled=false、原因码与仍然可用的手动入口。
        """

        backfill = self.ledger.backfill_report("income")
        last = self._last_income_sync or {}
        last_attempt = self._last_income_sync_at
        next_due = (
            _ms_to_iso(int((last_attempt + self._income_sync_interval_s) * 1000))
            if self._income_sync_enabled and last_attempt is not None
            else None
        )
        last_error = self._last_income_sync_error or backfill.get("last_error")
        if not self._income_sync_enabled:
            note = (
                "自动回填已关闭（WORKBENCH_INCOME_SYNC=0）：资金费只能手动 "
                "POST /api/sync-income 触发，funding_known 会保持未知。"
            )
        elif last_error:
            note = f"上次自动回填失败，将按间隔重试：{last_error}"
        elif not backfill.get("complete"):
            note = "资金费尚未回填完成：策略净盈亏保持未知，不会按 0 计入。"
        else:
            note = None
        return {
            **self.income_sync_settings(),
            "last_status": last.get("status"),
            "last_attempt_at": _ms_to_iso(
                None if last_attempt is None else int(last_attempt * 1000)
            ),
            "last_success_at": _ms_to_iso(
                None if self._last_income_sync_ok_at is None else int(self._last_income_sync_ok_at * 1000)
            ),
            "next_due_at": next_due,
            "last_error": last_error,
            "last_result": last or None,
            "backfill": backfill,
            "funding_known": bool(backfill.get("complete")),
            "manual_endpoint": "POST /api/sync-income",
            "note": note,
        }

    def maybe_sync_income(self, *, force: bool = False) -> dict[str, Any]:
        """服务端自身触发的 income 回填（T18 §8·C2）。

        - 只读交易所（``GET /fapi/v1/income``），只写本地 capital_flows/backfill_state；
        - 有最小间隔（``interval_s``；``force`` 也受 ``min_interval_s`` 约束），
          不会因为每个 HTTP 请求就多打一次交易所；
        - 只拉游标之后的未覆盖区间，游标只进不退，重复回填按 tranId 幂等；
        - 失败只写 ``backfill_state.last_error``，不抛给调用方、不改控制态、
          不影响同轮的权益快照与保护核对。
        """

        now = self._clock()
        base: dict[str, Any] = {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "at": utc_now(),
            "read_only_exchange": True,
            **self.income_sync_settings(),
        }
        if not self._income_sync_enabled and not force:
            result = {
                **base,
                "status": "disabled",
                "ok": False,
                "skipped": True,
                "reason": "income_sync_disabled",
                "note": (
                    "自动回填已关闭（WORKBENCH_INCOME_SYNC=0）；资金费只能手动 "
                    "POST /api/sync-income 触发。"
                ),
            }
            self._last_income_sync = result
            return result
        if self.broker is None:
            return {
                **base,
                "status": "unconfigured",
                "ok": False,
                "skipped": True,
                "reason": "no_broker",
            }
        if not self._income_sync_lock.acquire(blocking=False):
            return {
                **base,
                "status": "busy",
                "ok": False,
                "skipped": True,
                "reason": "already_running",
            }
        try:
            last_attempt = self._last_income_sync_at
            min_gap = (
                self._income_sync_min_interval_s if force else self._income_sync_interval_s
            )
            if last_attempt is not None and min_gap > 0 and (now - last_attempt) < min_gap:
                return {
                    **base,
                    "status": "throttled",
                    "ok": False,
                    "skipped": True,
                    "reason": "interval_not_due",
                    "retry_after_s": round(min_gap - (now - last_attempt), 1),
                    "next_due_at": _ms_to_iso(int((last_attempt + min_gap) * 1000)),
                }
            start_ms, end_ms = self._income_sync_window(now)
            # 先记“尝试”：失败也算一次，避免失败时被每个心跳反复重试打交易所。
            self._last_income_sync_at = now
            try:
                synced = self.broker.sync_income(start_ms=start_ms, end_ms=end_ms)
            except Exception as exc:  # noqa: BLE001 - 回填失败不得冒泡到监督循环/接口
                message = f"{type(exc).__name__}: {exc}"[:200]
                self._last_income_sync_error = f"income_sync_failed: {message}"
                self.ledger.set_backfill_state("income", last_error=self._last_income_sync_error)
                result = {
                    **base,
                    "status": "failed",
                    "ok": False,
                    "reason": "income_sync_failed",
                    "error": message,
                    "window": {"start_ms": start_ms, "end_ms": end_ms},
                }
                self._last_income_sync = result
                logger.warning("income_sync_failed error=%s", type(exc).__name__)
                return result
            if not synced.get("ok"):
                reason = str(synced.get("reason") or "income_sync_unavailable")
                self._last_income_sync_error = f"income_sync_unavailable: {reason}"
                self.ledger.set_backfill_state("income", last_error=self._last_income_sync_error)
                result = {
                    **base,
                    "status": "unavailable",
                    "ok": False,
                    "reason": reason,
                    "window": {"start_ms": start_ms, "end_ms": end_ms},
                }
                self._last_income_sync = result
                logger.warning("income_sync_unavailable reason=%s", reason)
                return result
            self._last_income_sync_ok_at = now
            self._last_income_sync_error = None
            backfill = self.ledger.backfill_report("income")
            result = {
                **base,
                "status": "synced",
                "ok": True,
                "rows": synced.get("rows"),
                "mapped": synced.get("mapped"),
                "inserted": synced.get("inserted"),
                "window": synced.get("window") or {"start_ms": start_ms, "end_ms": end_ms},
                "cursor": backfill.get("cursor"),
                "covered_from": backfill.get("covered_from"),
                "complete": bool(backfill.get("complete")),
                "funding_known": bool(backfill.get("complete")),
                "next_due_at": _ms_to_iso(
                    int((now + self._income_sync_interval_s) * 1000)
                ),
            }
            self._last_income_sync = result
            logger.info(
                "income_sync_ok inserted=%s window=%s..%s",
                result["inserted"],
                start_ms,
                end_ms,
            )
            return result
        finally:
            self._income_sync_lock.release()

    def _income_sync_window(self, now: float) -> tuple[int, int]:
        """单次窗口：从游标（减重叠）起，不超过 window_s，也不超过现在。

        游标落后很远时按页推进（每轮推进一页），而不是一次拉穿后把 covered_from
        说成一段其实没拉过的历史。
        """

        now_ms = int(now * 1000)
        window_ms = int(self._income_sync_window_s * 1000)
        overlap_ms = int(self._income_sync_overlap_s * 1000)
        state = self.ledger.backfill_state("income") or {}
        cursor = state.get("cursor_ms")
        start_ms = (int(cursor) - overlap_ms) if cursor is not None else (now_ms - window_ms)
        end_ms = min(start_ms + window_ms, now_ms)
        if end_ms <= start_ms:
            # 时钟回拨/游标超前：退化为最近一小段，绝不让窗口反向。
            start_ms = max(0, now_ms - max(overlap_ms, 1000))
            end_ms = now_ms
        return start_ms, end_ms

    def _income_sync_tick(self) -> None:
        """监督循环的 income 节拍；任何失败只记日志，不影响账户/保护循环。"""

        try:
            self.maybe_sync_income()
        except Exception as exc:  # noqa: BLE001 - 监督循环必须存活
            logger.warning("income_sync_tick_failed error=%s", type(exc).__name__)

    def sync_capital_flows(self, *, range_s: int = 7 * 24 * 3600) -> dict[str, Any]:
        """§8·C2：拉取交易所 income 流水（资金费/划转）并落库。

        只读交易所（GET /fapi/v1/income），只写本地 capital_flows；不下单、不改控制态。
        手动触发不受自动回填的节流限制，但成功后会更新“最近一次成功”，
        避免紧接着的自动节拍再打一次交易所。
        """

        self._require_broker()
        now_ms = int(self._clock() * 1000)
        start_ms = now_ms - max(3600, int(range_s)) * 1000
        # 与自动节拍共用一把锁：操作员手动点一次时，不会和后台 tick 同时打
        # 交易所（两路并发只会让节流状态互相覆盖）。
        with self._income_sync_lock:
            result = self.broker.sync_income(start_ms=start_ms, end_ms=now_ms)
            if result.get("ok"):
                now = self._clock()
                self._last_income_sync_at = now
                self._last_income_sync_ok_at = now
                self._last_income_sync_error = None
                self._last_income_sync = {
                    "status": "synced",
                    "ok": True,
                    "trigger": "manual",
                    "at": utc_now(),
                    "inserted": result.get("inserted"),
                    "complete": bool(result.get("complete")),
                    "window": result.get("window"),
                }
        return {
            **result,
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
        }
    def pnl_payload(self, *, range_s: int = 86400) -> dict[str, Any]:
        """§8·C：资金口径总表——四个权益口径 + 策略盈亏四本账 + 资金对账。

        只读：不落权益快照、不改风控水位（对账与展示都不应改变账本事实）。
        unknown 一律显式返回，不折叠成 0。
        """

        from ..ledger.numbers import to_decimal as _to_decimal

        now_ms = int(self._clock() * 1000)
        start_ms = now_ms - max(300, int(range_s)) * 1000
        report = self.ledger.strategy_pnl(start_ms=start_ms, end_ms=now_ms)

        snapshot, read_meta = self._snapshot_readonly()

        calibers: dict[str, Any] = {
            "wallet_balance": None,
            "unrealized_pnl": None,
            "margin_equity": None,
            "available_balance": None,
            "source": "none",
            "stale": True,
            "last_success_at": read_meta["last_success_at"],
            "age_ms": None,
            "unknown": list(read_meta["unknown"]),
        }
        if snapshot is not None and snapshot.error is None and snapshot.equity_exchange is not None:
            calibers.update(
                {
                    "wallet_balance": dec_text(snapshot.wallet_balance)
                    if snapshot.wallet_balance is not None
                    else None,
                    "unrealized_pnl": dec_text(snapshot.unrealized_pnl)
                    if snapshot.unrealized_pnl is not None
                    else None,
                    "margin_equity": dec_text(snapshot.equity_exchange),
                    "available_balance": dec_text(snapshot.available_balance)
                    if snapshot.available_balance is not None
                    else None,
                    "source": "exchange",
                    "stale": False,
                    "age_ms": max(0, int((self._clock() - snapshot.fetched_at) * 1000)),
                }
            )
        else:
            last = self.ledger.latest_equity()
            if last is not None:
                calibers.update(
                    {
                        "wallet_balance": last.get("wallet_balance"),
                        "unrealized_pnl": last.get("upnl"),
                        "margin_equity": last.get("equity_exchange"),
                        "available_balance": last.get("available"),
                        "source": "ledger",
                        "stale": True,
                        "age_ms": None,
                        "unknown": calibers["unknown"]
                        + ["EQUITY_FROM_LEDGER：交易所不可达，权益取自本地最后一条快照"],
                    }
                )

        # 同一时点或可解释窗口：用窗口内首末权益快照算交易所权益变动。
        since_iso = (
            datetime.now(timezone.utc) - timedelta(seconds=max(300, int(range_s)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        curve = self.ledger.list_equity_curve(since=since_iso, limit=5000)
        exchange_delta = None
        window_note = "窗口内权益快照不足两个，无法计算权益变动"
        if len(curve) >= 2:
            first = _to_decimal(curve[0].get("equity_exchange"))
            last = _to_decimal(curve[-1].get("equity_exchange"))
            if first is not None and last is not None:
                exchange_delta = last - first
                window_note = (
                    f"{curve[0].get('ts_bucket')} → {curve[-1].get('ts_bucket')}"
                    f"（{len(curve)} 个快照）"
                )

        reconcile = self.ledger.reconcile_funds(
            start_ms=start_ms,
            end_ms=now_ms,
            exchange_delta=exchange_delta,
            tolerance=self.settings.pnl_reconcile_tolerance,
        )
        reconcile["window_note"] = window_note

        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "range_s": range_s,
            "calibers": calibers,
            # T20：只读取数元信息与 calibers 并列，界面不必从多个字段拼“是否新鲜”。
            "read": {
                "source": read_meta["source"],
                "stale": read_meta["stale"],
                "last_success_at": read_meta["last_success_at"],
                "cooldown_until": read_meta["cooldown_until"],
                "read_timeout_s": read_meta["read_timeout_s"],
                "unknown": read_meta["unknown"],
            },
            "caliber_note": (
                "wallet_balance=交易所钱包余额；margin_equity=保证金权益（含未实现盈亏）；"
                "unrealized_pnl=未实现盈亏；strategy_net_pnl=本地策略净盈亏（不含划转）"
            ),
            "strategy": report,
            "reconcile": reconcile,
            "schema": self.ledger.schema_report(),
        }
    # ------------------------------------------------------------------ P1
    # ------------------------------------------------------------------ execution lease
    def _assert_lease(self, lease_id: str | None) -> None:
        """Validate a lease when provided, or when the deployment requires one.

        Direct read-only P1 calls in tests/dev remain possible without a lease;
        every Host run acquires one and sends it to P1/P2. `WORKBENCH_REQUIRE_
        EXECUTION_LEASE=on` closes the no-lease bypass for real deployments.
        """

        if not self.settings.require_execution_lease and not lease_id:
            return
        try:
            self.leases.validate(lease_id)
        except LeaseError as exc:
            raise ServiceError(exc.status, exc.code, exc.message, details=exc.details) from exc

    def acquire_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            lease = self.leases.acquire(
                str(payload.get("run_id") or ""),
                owner=str(payload.get("owner") or "host"),
                ttl_s=float(payload.get("ttl_s") or 600),
            )
        except LeaseError as exc:
            raise ServiceError(exc.status, exc.code, exc.message, details=exc.details) from exc
        return {**lease, "ok": True}

    def renew_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            lease = self.leases.renew(
                str(payload.get("lease_id") or ""),
                ttl_s=float(payload.get("ttl_s") or 600),
            )
        except LeaseError as exc:
            raise ServiceError(exc.status, exc.code, exc.message, details=exc.details) from exc
        return {**lease, "ok": True}

    def release_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        released = self.leases.revoke(str(payload.get("lease_id") or ""), "host_release")
        return {"ok": True, "released": released}

    def revoke_lease_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        revoked = self.leases.revoke_run(str(payload.get("run_id") or ""), "stale_run")
        return {"ok": True, "revoked": revoked}

    @_serialized
    def evaluate(self, payload: dict[str, Any], *, trace_id: str | None = None) -> dict[str, Any]:
        self._assert_lease(payload.get("lease_id"))
        cycle_id = str(payload.get("cycle_id") or f"m{uuid.uuid4().hex[:8]}")
        try:
            plan = OrderPlan.from_payload(payload.get("plan") or {}, cycle_id=cycle_id)
        except PlanValidationError as exc:
            raise ServiceError(422, "E_VALIDATION", exc.message, details=exc.details) from exc

        self.ensure_started()
        snapshot, equity, risk_state, control = self._heartbeat()
        # 证据年龄只从服务端 md-evidence/1 快照（DB 引用 + 文件 hash/时间）派生；
        # 请求体 evidence_age_ms 一律忽略。行情事实同样只来自服务端。
        # 无凭据时不取行情/证据，让规则层先以 R25/E_EQUITY_UNAVAILABLE 拒绝（只读模式可解释）。
        if self.broker is not None:
            market_facts = self._server_market_facts(plan.symbol)
            evidence = self._server_evidence(cycle_id)
            evidence_age_ms: int | None = evidence["age_ms"]
        else:
            market_facts = {}
            evidence = None
            evidence_age_ms = None
        context = self._risk_context(
            snapshot=snapshot,
            equity=equity,
            risk_state=risk_state,
            control=control,
            market=market_facts,
            evidence_age_ms=evidence_age_ms,
            symbol=plan.symbol,
            leverage=plan.leverage,
        )
        plan, strategy_context = validate_entry(self.strategies, payload.get("strategy_context"), plan, market_facts, now_ms=int(self._clock() * 1000), compile_plan=True)
        if strategy_context and snapshot and snapshot.positions:
            raise ServiceError(409, "E_STRATEGY_POSITION_LIMIT", "已有仓位，不能新增策略开仓")
        filters = self._filters_view(plan.symbol)
        verdict, approval = self.risk.evaluate(
            plan,
            context,
            filters=filters,
            trace_id=trace_id,
            approval_context={
                "market": market_facts,
                "evidence": evidence,
                "evidence_age_ms": evidence_age_ms,
                "rules_version": self.risk.rules_version(),
                "strategy_context": strategy_context,
                "strategy_mode_revision": self.strategies.config()["revision"],
            },
        )
        if not verdict.approved:
            code = BLOCKING_CODES.get(verdict.blocking_rule or "", "E_RISK_REJECTED")
            raise ServiceError(
                422,
                code,
                verdict.blocked_reason or "risk rejected",
                retryable=False,
                details={
                    "rule": verdict.blocking_rule,
                    "violations": [item.as_dict() for item in verdict.violations],
                    "verdict": verdict.as_dict(),
                    "cycle_id": cycle_id,
                },
            )
        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "cycle_id": cycle_id,
            "plan": plan.normalized_dict(),
            "verdict": verdict.as_dict(),
            "approval": approval,
            "context": {
                "equity_session": dec_text(equity.session) if equity.session is not None else None,
                "equity_age_ms": equity.age_ms,
                "control_state": control.get("state"),
                "open_positions": len(context.open_positions),
                "mark": market_facts.get("mark"),
                "snapshot_age_ms": market_facts.get("snapshot_age_ms"),
                "market_source": "exchange",
                "evidence_source": "md-evidence/1" if evidence else None,
                "evidence_id": None if evidence is None else evidence["evidence_id"],
                "evidence_age_ms": evidence_age_ms,
                "evidence_sha256": None if evidence is None else evidence["sha256"],
                "evidence_produced_at_ms": (
                    None if evidence is None else evidence["produced_at_ms"]
                ),
                "trades_today": context.trades_today,
                "trades_symbol_today": context.trades_symbol_today,
                "mmr": dec_text(context.mmr),
            },
            "checked_at": utc_now(),
        }

    # ------------------------------------------------------------------ P2
    @_serialized
    def submit(self, payload: dict[str, Any], *, trace_id: str | None = None) -> dict[str, Any]:
        lease_id = payload.get("lease_id")
        self._assert_lease(lease_id)
        if self.broker is None or self.client is None:
            raise ServiceError(
                503,
                "E_DEMO_CREDENTIALS_MISSING",
                "Demo API key/secret not configured; signed execution disabled",
                details={"env": ["WORKBENCH_DEMO_API_KEY", "WORKBENCH_DEMO_API_SECRET"]},
            )
        token = payload.get("approval_token")
        provided_plan = None
        if payload.get("plan") is not None:
            cycle_hint = payload.get("cycle_id")
            try:
                provided_plan = OrderPlan.from_payload(
                    payload["plan"], cycle_id=str(cycle_hint) if cycle_hint else None
                )
            except PlanValidationError as exc:
                raise ServiceError(422, "E_VALIDATION", exc.message, details=exc.details) from exc

        try:
            approval_payload = self.approvals.consume(token, plan=provided_plan)
        except ApprovalError as exc:
            raise ServiceError(
                exc.status_code, exc.code, exc.message, details=exc.details
            ) from exc

        try:
            plan = self.approvals.plan_from_payload(approval_payload)
        except ApprovalError as exc:
            raise ServiceError(exc.status_code, exc.code, exc.message, details=exc.details) from exc
        approved_qty = to_decimal(approval_payload.get("approved_qty"), Decimal("0")) or Decimal("0")
        strategy_context = approval_payload.get("strategy_context")
        if payload.get("strategy_context") != strategy_context:
            raise ServiceError(409, "E_STRATEGY_PLAN_MISMATCH", "P2 策略上下文与批准不一致")

        # T20：规则版本校验必须**两侧都成立**。旧写法 `if expected_rules and ...` 在
        # 令牌未携带 rules_version 时直接短路跳过——只要绕过 risk 侧（或将来新增入口）
        # 就能避开这条校验。现在缺失与不符一律拒绝，执行侧不再依赖 risk 侧补位。
        expected_rules = approval_payload.get("rules_version")
        current_rules = self.risk.rules_version()
        if not expected_rules or str(expected_rules) != current_rules:
            raise ServiceError(
                409,
                "E_APPROVAL_RULES_CHANGED",
                "approval token is missing the current risk rules version; re-run P1",
                details={
                    "approved": expected_rules,
                    "current": current_rules,
                    "reason": (
                        "rules_version_missing" if not expected_rules else "rules_version_mismatch"
                    ),
                },
            )

        self.ensure_started()
        snapshot, equity, risk_state, control = self._heartbeat()
        # P2 只使用 token 内的服务端事实并强制老化；请求体 market/evidence 一律忽略。
        market_payload, evidence_age_ms = self._approval_facts(approval_payload)
        context = self._risk_context(
            snapshot=snapshot,
            equity=equity,
            risk_state=risk_state,
            control=control,
            market=market_payload,
            evidence_age_ms=evidence_age_ms,
            symbol=plan.symbol,
            leverage=plan.leverage,
        )
        filters = self._filters_view(plan.symbol)
        verdict = self.risk.check(plan, context, filters=filters, trace_id=trace_id)
        if not verdict.approved:
            code = BLOCKING_CODES.get(verdict.blocking_rule or "", "E_RISK_REJECTED")
            raise ServiceError(
                422,
                code,
                verdict.blocked_reason or "P2 re-check rejected",
                details={
                    "stage": "P2",
                    "rule": verdict.blocking_rule,
                    "violations": [item.as_dict() for item in verdict.violations],
                    "client_order_id": None,
                },
            )
        qty = min(approved_qty, verdict.approved_qty)
        if qty <= 0:
            raise ServiceError(
                422, "E_RISK_BELOW_MIN", "P2 approved qty is zero", details={"stage": "P2"}
            )

        # T03/A4：真正提交前硬刷新价格与可用余额（force=True 绕过 broker 的 2s
        # 快照缓存），并用刷新后的事实重跑限额与 sizing；刷新失败即阻断新开仓。
        refresh = self._pre_submit_refresh(plan, evidence_age_ms=evidence_age_ms)
        snapshot = refresh["snapshot"]
        equity = refresh["equity"]
        risk_state = refresh["risk_state"]
        control = refresh["control"]
        context = refresh["context"]
        verdict = self.risk.check(plan, context, filters=filters, trace_id=trace_id)
        if not verdict.approved:
            code = BLOCKING_CODES.get(verdict.blocking_rule or "", "E_RISK_REJECTED")
            raise ServiceError(
                422,
                code,
                f"pre-submit refresh rejected: {verdict.blocked_reason or 'risk rejected'}",
                details={
                    "stage": "P2_REFRESH",
                    "rule": verdict.blocking_rule,
                    "violations": [item.as_dict() for item in verdict.violations],
                    "client_order_id": None,
                    "refreshed_mark": refresh.get("market", {}).get("mark"),
                },
            )
        qty = min(approved_qty, verdict.approved_qty)
        if qty <= 0:
            raise ServiceError(
                422,
                "E_RISK_BELOW_MIN",
                "pre-submit refresh produced zero approved qty",
                details={"stage": "P2_REFRESH"},
            )
        # Re-validate immediately before the irreversible exchange call: a
        # pause/revoke may have happened while P1/P2 facts were being checked.
        self._assert_lease(lease_id)


        plan, strategy_context = validate_entry(self.strategies, strategy_context, plan, refresh.get("market", {}), now_ms=int(self._clock() * 1000), expected_mode_revision=approval_payload.get("strategy_mode_revision"))
        if strategy_context and snapshot and snapshot.positions:
            raise ServiceError(409, "E_STRATEGY_POSITION_LIMIT", "已有仓位，不能新增策略开仓")
        time_stop = self._time_stop_request(plan, strategy_context=strategy_context)
        reserve_signal(self.strategies, strategy_context, plan, time_stop=time_stop)
        try:
            result = self.broker.open_market(
                plan,
                qty,
                cycle_id=plan.cycle_id or uuid.uuid4().hex[:8],
                trace_id=trace_id,
                time_stop=time_stop,
            )
            record_result(self.strategies, strategy_context, plan, result)
        except OutboxUncertain as exc:
            raise ServiceError(
                202,
                "E_FILL_UNCONFIRMED",
                exc.message,
                retryable=True,
                details={
                    "client_order_id": exc.client_order_id,
                    # 提交中/结果未知：对外显示“待确认”，不是确定失败。
                    "display": "PENDING_CONFIRMATION",
                    "pending_confirmation": True,
                    **exc.details,
                },
            ) from exc
        except ExecutionRejected as exc:
            raise ServiceError(exc.status_code, exc.code, exc.message, details=exc.details) from exc
        except DemoApiError as exc:
            raise ServiceError(
                502,
                "E_EXCHANGE_UNAVAILABLE" if exc.kind == "unknown" else "E_EXCHANGE_REJECT",
                exc.message,
                retryable=exc.kind == "unknown",
                details=exc.as_dict(),
            ) from exc
        except ExecutionConfigError as exc:
            raise ServiceError(503, "E_NOT_READY", str(exc)) from exc

        if result.get("position_unproven"):
            # 成交已发生但仓位/成交无法证明，且紧急平仓失败：阻断所有新开仓。
            self.state.set_control(
                "close_only",
                "entry filled but position unprovable",
                updated_by="risk_engine",
            )
            raise ServiceError(
                503,
                "E_POSITION_UNPROVEN",
                "entry filled but position/emergency close could not be proven; new entries blocked",
                retryable=False,
                details={
                    "client_order_id": result.get("client_order_id"),
                    "emergency_close": result.get("emergency_close"),
                },
            )

        if result.get("protection_close_failed"):
            # 保护不完整且紧急平仓失败：切 close_only，阻断后续 P1（R22）。
            self.state.set_control(
                "close_only",
                "protection incomplete and emergency close failed",
                updated_by="risk_engine",
            )
            raise ServiceError(
                503,
                "E_PROTECTION_CLOSE_FAILED",
                "protection incomplete and emergency close failed; new entries blocked",
                retryable=False,
                details={
                    "client_order_id": result.get("client_order_id"),
                    "protection": result.get("protection"),
                    "emergency_close": result.get("emergency_close"),
                },
            )

        status_code = 200 if result.get("idempotent_replay") else 201
        result.update(
            {
                "venue": self.settings.venue,
                "demo_only": True,
                "ledger_id": self.ledger.ledger_id,
                "approval_jti": approval_payload.get("jti"),
                "http_status": status_code,
                "intent_id": plan.cycle_id,
                "display": _pending_display(result.get("status")),
                "pending_confirmation": str(result.get("status") or "").upper()
                in _PENDING_STATUSES,
                "pre_submit_refresh": {
                    "mark": refresh.get("market", {}).get("mark"),
                    "available_margin": dec_text(refresh["snapshot"].available_balance)
                    if refresh["snapshot"].available_balance is not None
                    else None,
                    "equity_source": refresh["equity"].source,
                    "equity_age_ms": refresh["equity"].age_ms,
                    "snapshot_age_ms": refresh.get("market", {}).get("snapshot_age_ms"),
                    "rechecked": True,
                },
                "time_stop": time_stop,
            }
        )
        return result

    # ------------------------------------------------------------------ 平仓/修复/控制
    @_serialized
    def close(self, payload: dict[str, Any], *, trace_id: str | None = None) -> dict[str, Any]:
        self._require_broker()
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ServiceError(422, "E_VALIDATION", "symbol is required")
        qty = to_decimal(payload.get("qty"))
        self.ensure_started()
        try:
            result = self.broker.close_market(
                symbol,
                qty=qty,
                cycle_id=payload.get("cycle_id"),
                reason=str(payload.get("reason") or "manual"),
                trace_id=trace_id,
            )
        except OutboxUncertain as exc:
            raise ServiceError(
                202,
                "E_FILL_UNCONFIRMED",
                exc.message,
                retryable=True,
                details={"client_order_id": exc.client_order_id},
            ) from exc
        except ExecutionRejected as exc:
            raise ServiceError(exc.status_code, exc.code, exc.message, details=exc.details) from exc
        # 平仓后立即按已实现盈亏刷新连亏/冷却（R20 状态始终维护）
        self._refresh_loss_streak()
        result.update({"venue": self.settings.venue, "demo_only": True, "ledger_id": self.ledger.ledger_id})
        return result

    @_serialized
    def repair(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_broker()
        symbols = payload.get("symbols")
        if isinstance(symbols, str):
            symbols = [symbols]
        result = self.broker.repair_protection(symbols=symbols if isinstance(symbols, list) else None)
        result.update({"venue": self.settings.venue, "demo_only": True, "ledger_id": self.ledger.ledger_id})
        return result

    def performance_payload(self, equity: _EquityInfo) -> dict[str, Any]:
        performance = self.ledger.local_day_performance()
        baseline = to_decimal(performance.get("baseline_equity"))
        performance["equity_change"] = (
            dec_text(equity.session - baseline)
            if equity.available and equity.session is not None and baseline is not None else None
        )
        performance["equity_change_pct"] = (
            dec_text((equity.session - baseline) / baseline * 100)
            if equity.available and equity.session is not None and baseline and baseline > 0 else None
        )
        return performance

    @_serialized
    def operator_command(self, action: str, payload: dict[str, Any], *, trace_id: str | None = None) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or f"op-{uuid.uuid4().hex}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
            raise ServiceError(422, "E_REQUEST_ID_INVALID", "invalid request_id")
        reason = str(payload.get("reason") or "").strip()
        if len(reason) > 300:
            raise ServiceError(422, "E_VALIDATION", "reason must be at most 300 characters")
        public_reason = re.sub(r"(?i)(api[_-]?key|secret|password|token|authorization)\s*[:=]\s*\S+", r"\1=[redacted]", reason)
        target = str(payload.get("state") if action == "control" else payload.get("symbol") or ",".join(payload.get("symbols") or []) or "all")[:160]
        if not self.ledger.operation_start(request_id, action, target, public_reason):
            raise ServiceError(409, "E_OPERATION_EXISTS", "request already recorded; inspect operation history before retrying", details={"request_id": request_id})
        try:
            if action == "close":
                result = self.close(payload, trace_id=trace_id)
            elif action == "repair":
                result = self.repair(payload)
            elif action == "control":
                result = self.control(payload)
            elif action == "recover_orders":
                result = self.recover_pending(payload)
            elif action == "reconcile":
                result = self.reconcile()
            else:
                raise ServiceError(422, "E_VALIDATION", "unknown operator action")
        except ServiceError as exc:
            uncertain = exc.status_code == 202 or exc.code in {"E_FILL_UNCONFIRMED", "E_ORDER_UNKNOWN"}
            self.ledger.operation_finish(request_id, "unknown" if uncertain else "failed", code=exc.code)
            raise
        except Exception:
            self.ledger.operation_finish(request_id, "unknown", code="E_OPERATION_RESULT_UNKNOWN")
            raise
        partial = result.get("ok") is False
        if action == "control" and (result.get("control") or {}).get("state") == "emergency":
            partial = not (result.get("flattened") or {}).get("ok", False)
        if action == "recover_orders" and not result.get("converged", True):
            partial = True
        if action == "reconcile":
            # A08: an unreachable/stale venue or unresolved outbox queries is
            # not a successful reconciliation, even when all diff arrays are empty.
            outbox = result.get("outbox") or {}
            checked = int(outbox.get("checked") or 0)
            resolved = int(outbox.get("resolved") or 0)
            unresolved = max(0, checked - resolved)
            if result.get("exchange_reachable") is False or result.get("stale") is True or unresolved > 0:
                partial = True
        outcomes = (result.get("flattened") or {}).get("results") if action == "control" else result.get("repaired")
        summary = {
            "control_state": (result.get("control") or {}).get("state"), "partial": partial,
            "recovery_unresolved": result.get("unresolved"),
            "recovery_converged": result.get("converged"),
            "converged_orders": result.get("converged_orders"),
            "exchange_reachable": result.get("exchange_reachable"),
            "stale": result.get("stale"),
            "unresolved_queries": max(0, int((result.get("outbox") or {}).get("checked") or 0) - int((result.get("outbox") or {}).get("resolved") or 0)) if action == "reconcile" else None,
            "result_status": result.get("status"), "position_closed": result.get("closed"),
            "outcomes": [
                {"symbol": item.get("symbol"), "closed": item.get("closed"), "status": item.get("status"),
                 "ok": item.get("ok"), "error_code": item.get("code") or ("E_ACTION_FAILED" if item.get("error") else None)}
                for item in (outcomes or []) if isinstance(item, dict)
            ],
        }
        self.ledger.operation_finish(request_id, "partial" if partial else "succeeded", summary=summary)
        return {**result, "request_id": request_id, "operation_status": "partial" if partial else "succeeded"}

    @_serialized
    def control(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = str(payload.get("state") or "").strip().lower()
        confirm = str(payload.get("confirm") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if confirm != target.upper():
            raise ServiceError(
                422,
                "E_CONTROL_CONFIRM_REQUIRED",
                "confirm must equal the state in uppercase",
                details={"expected": target.upper()},
            )
        try:
            control = self.state.set_control(target, reason, updated_by="operator")
        except RiskStateError as exc:
            status = 423 if exc.code == "E_CONTROL_LOCKED" else 422
            raise ServiceError(status, exc.code, exc.message, details=exc.details) from exc
        # Any control change invalidates every in-flight execution lease,
        # including one held by another Host process.
        revoked_generation = self.leases.revoke_all(f"control:{target}")
        flattened: dict[str, Any] | None = None
        protection_untouched = True
        recovery: dict[str, Any] | None = None
        if target == "emergency" and self.broker is not None:
            # 只有 emergency 才会平仓；pause 复用“停止自动执行”的语义。
            flattened = self._flatten_all()
            protection_untouched = False
        elif target == "normal" and self.broker is not None:
            # 控制停止后的恢复：先收敛未决项，再核对仓位与保护。
            recovery = self._recover_pending()
            self._last_reconcile = self.broker.reconcile()
            protection = self._inspect_protection(self._last_reconcile)
            recovery["protection"] = protection
        self.ledger.log_risk_event(
            event_type="control_pause_semantics" if target == "pause" else "control_state_applied",
            severity="warn" if target != "normal" else "info",
            rule_id="R22",
            action="revoke_lease_block_new_entries",
            details={
                "state": target,
                "lease_generation": revoked_generation,
                "flattened": flattened,
                "protection_orders_canceled": not protection_untouched,
                "recovery_converged": None if recovery is None else recovery.get("converged"),
            },
        )
        return {
            "venue": self.settings.venue,
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "control": control.as_dict(),
            "flattened": flattened,
            "lease_generation": revoked_generation,
            "protection_orders_untouched": protection_untouched,
            "recovery": recovery,
        }


    def _flatten_all(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        try:
            positions = self.broker.positions(force=True)
        except Exception as exc:  # noqa: BLE001 - 无法读取仓位时只记录
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "results": []}
        for position in positions:
            try:
                results.append(
                    self.broker.close_market(
                        position.symbol,
                        qty=position.qty,
                        cycle_id=f"emergency-{uuid.uuid4().hex[:6]}",
                        reason="control_emergency",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 逐个仓位尽力而为
                results.append(
                    {
                        "symbol": position.symbol,
                        "status": "ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return {"ok": all(item.get("closed") for item in results), "results": results}

    def reconcile(self) -> dict[str, Any]:
        self._require_broker()
        result = self.broker.reconcile()
        result.update({"venue": self.settings.venue, "demo_only": True, "ledger_id": self.ledger.ledger_id})
        return result

    def envelope(self) -> dict[str, Any]:
        snapshot, equity, risk_state, control = self._heartbeat()
        outbox_counts = self.outbox.counts() if self.outbox else {}
        blockers = self._blocking_reasons(snapshot, equity, risk_state, control, outbox_counts)
        positions = self._positions_from(snapshot)
        return self.risk.envelope(
            {
                "control": self.state.control(),
                "risk_state": risk_state,
                "equity": equity.as_dict(),
                "account": {
                    "credentials_configured": self.configured,
                    "reachable": snapshot is not None and snapshot.error is None,
                },
                "positions": {
                    "count": len(positions),
                    "protection_ok": None if snapshot is None else snapshot.protection_ok(),
                    "missing": [] if snapshot is None else snapshot.protection_missing(),
                },
                "outbox": outbox_counts,
                "blocking_reasons": blockers,
                "checked_at": utc_now(),
            }
        )

    def query_order(self, client_order_id: str) -> dict[str, Any]:
        """断线后按原 client_order_id 查询收敛（只查询、不重发）。"""

        self._require_broker()
        try:
            result = self.broker.query_order_state(client_order_id)
        except ExecutionRejected as exc:
            raise ServiceError(
                exc.status_code, exc.code, exc.message, details=exc.details
            ) from exc
        result.update(
            {
                "venue": self.settings.venue,
                "demo_only": True,
                "ledger_id": self.ledger.ledger_id,
                "display": _pending_display(result.get("status")),
                "checked_at": utc_now(),
            }
        )
        return result

    # ------------------------------------------------------------------ 内部
    def _pre_submit_refresh(
        self, plan: OrderPlan, *, evidence_age_ms: Any
    ) -> dict[str, Any]:
        """提交前硬刷新：强制重读账户/仓位与即时行情，再让规则重算一次。

        - 账户/余额用 ``snapshot(force=True)``：不接受 broker 的 2s 缓存；
        - 价格用公共行情端点即时取（mark/bid/ask/klines），snapshot_age_ms 归零；
        - 证据年龄不刷新（决策证据必须继续老化）；
        - 刷新失败即阻断本次新开仓（不拿旧数据当提交依据）。
        """

        if self.broker is None:
            raise ServiceError(
                503,
                "E_DEMO_CREDENTIALS_MISSING",
                "pre-submit refresh requires Demo credentials",
                details={"stage": "P2_REFRESH"},
            )
        try:
            snapshot = self.broker.snapshot(force=True)
        except Exception as exc:  # noqa: BLE001 - 刷新失败阻断新开仓
            raise ServiceError(
                503,
                "E_EXECUTION_REFRESH_FAILED",
                "pre-submit account refresh failed; new entry blocked",
                retryable=True,
                details={
                    "stage": "P2_REFRESH",
                    "reason": "snapshot_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            ) from exc
        if snapshot.error is not None:
            raise ServiceError(
                503,
                "E_EXECUTION_REFRESH_FAILED",
                "pre-submit account refresh failed; new entry blocked",
                retryable=True,
                details={
                    "stage": "P2_REFRESH",
                    "reason": "exchange_unavailable",
                    "error": snapshot.error,
                },
            )
        equity = self._equity_info(snapshot)
        if not equity.available or equity.session is None or equity.age_ms is None:
            raise ServiceError(
                503,
                "E_EQUITY_UNAVAILABLE",
                "fresh equity unavailable; new entry blocked",
                retryable=True,
                details={"stage": "P2_REFRESH", "reason": "equity_unavailable"},
            )
        # 行情即时刷新（公共端点每次真实请求，不复用 P1 快照）。
        market = dict(self._server_market_facts(plan.symbol))
        market["snapshot_age_ms"] = 0
        risk_state = self.state.sync(equity.session, equity_available=True)
        control = self.state.control().as_dict()
        context = self._risk_context(
            snapshot=snapshot,
            equity=equity,
            risk_state=risk_state,
            control=control,
            market=market,
            evidence_age_ms=evidence_age_ms,
            symbol=plan.symbol,
            leverage=plan.leverage,
        )
        return {
            "snapshot": snapshot,
            "equity": equity,
            "risk_state": risk_state,
            "control": control,
            "context": context,
            "market": market,
        }

    def _time_stop_request(self, plan: OrderPlan, *, strategy_context=None) -> dict[str, Any] | None:
        """建仓时确定 time_stop 期限与来源计划版本（随仓位持久化）。"""

        hours = float(getattr(self.settings, "auto_time_stop_hours", 0) or 0)
        if strategy_context:
            version = self.strategies.version(strategy_context["strategy_id"], strategy_context["version"])
            hours = float(version["rules"]["exit"].get("time_stop_hours", 0))
        if hours <= 0 and not strategy_context:
            return None
        opened = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        deadline = opened + timedelta(hours=hours)
        source = (
            f"plan:{plan.cycle_id or 'unknown'};"
            f"rules:{self.risk.rules_version()};hours:{hours}"
        )
        if strategy_context:
            source += f";strategy:{strategy_context['strategy_id']};version:{strategy_context['version']}"
        return {
            "opened_at": _iso_utc(opened),
            "deadline_at": _iso_utc(deadline) if hours > 0 else None,
            "source": source,
            "hours": hours,
        }

    def _approval_facts(self, approval_payload: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        """P2 事实：只用 P1 token 内的服务端快照，并按经过时间强制老化。

        请求体里的 ``market`` / ``evidence_age_ms`` 一律忽略（伪造行情无法绕过
        R14/R15/R08–R11）；超过 ``entry_snapshot_max_age_ms`` 会被 R14 拒绝，
        调用方必须重新走 P1。
        """

        facts = dict(approval_payload.get("market") or {})
        if not facts or not facts.get("mark"):
            raise ServiceError(
                409,
                "E_APPROVAL_INVALID",
                "approval token is missing server market facts",
                details={"reason": "market_facts_missing"},
            )
        evidence = dict(approval_payload.get("evidence") or {})
        if not evidence.get("evidence_id") or not evidence.get("sha256"):
            raise ServiceError(
                409,
                "E_APPROVAL_INVALID",
                "approval token is missing server evidence facts",
                details={"reason": "evidence_facts_missing"},
            )
        if not str(evidence.get("schema_version") or "").startswith("md-evidence/1"):
            raise ServiceError(
                409,
                "E_APPROVAL_INVALID",
                "approval token evidence schema is not md-evidence/1",
                details={"reason": "evidence_schema_unsupported"},
            )
        base_evidence = approval_payload.get("evidence_age_ms")
        if base_evidence is None or int(base_evidence) != int(evidence.get("age_ms") or -1):
            raise ServiceError(
                409,
                "E_APPROVAL_INVALID",
                "approval token evidence age is inconsistent with evidence facts",
                details={"reason": "evidence_age_mismatch"},
            )
        now_ms = int(self._clock() * 1000)
        context_ts = approval_payload.get("context_ts_ms")
        elapsed_ms = max(0, now_ms - int(context_ts)) if context_ts else 0
        facts["snapshot_age_ms"] = elapsed_ms
        facts["evidence"] = evidence
        return facts, int(base_evidence) + elapsed_ms

    def _server_evidence(self, cycle_id: str) -> dict[str, Any]:
        """从服务端 md-evidence/1 快照派生证据年龄（DB 引用 + 文件 hash/时间）。

        请求体 ``evidence_age_ms`` 完全不参与：没有快照、hash 不符、schema 不符
        或时间被篡改都 fail-closed（``E_EVIDENCE_UNAVAILABLE``）。
        """

        # 复用账本连接（同一 WAL 库）；只读 SELECT，不新建连接，避免 Windows 只读 WAL 问题。
        try:
            row = self.ledger.query_one(
                "SELECT schema_version, produced_at_ms, path, sha256, bytes "
                "FROM evidence_snapshots WHERE cycle_id=?",
                (cycle_id,),
            )
        except sqlite3.Error as exc:
            if "no such table" in str(exc).lower():
                # 全新数据目录尚未生成过证据：等价于“没有服务端证据”，按 422 fail-closed。
                row = None
            else:
                raise ServiceError(
                    503,
                    "E_EVIDENCE_UNAVAILABLE",
                    "evidence store unavailable",
                    details={
                        "reason": "evidence_table_unavailable",
                        "error": type(exc).__name__,
                    },
                ) from exc
        if row is None:
            raise ServiceError(
                422,
                "E_EVIDENCE_UNAVAILABLE",
                f"no md-evidence/1 snapshot for cycle {cycle_id}; build evidence before P1",
                details={"cycle_id": cycle_id, "expected_schema": "md-evidence/1"},
            )

        schema_version = str(row.get("schema_version") or "")
        produced_at_ms = int(row.get("produced_at_ms") or 0)
        relative_path = str(row.get("path") or "")
        expected_sha = str(row.get("sha256") or "")
        expected_bytes = int(row.get("bytes") or 0)
        if not schema_version.startswith("md-evidence/1"):
            raise ServiceError(
                422,
                "E_EVIDENCE_UNVERIFIED",
                "evidence schema is not md-evidence/1",
                details={"cycle_id": cycle_id, "schema_version": schema_version},
            )

        data_root = self.settings.data_dir.resolve()
        target = (self.settings.data_dir / relative_path).resolve()
        if data_root not in target.parents or not target.is_file():
            raise ServiceError(
                422,
                "E_EVIDENCE_UNVERIFIED",
                "evidence file is missing or outside the data directory",
                details={"cycle_id": cycle_id, "path": relative_path},
            )
        try:
            before = target.stat()
            if before.st_size != expected_bytes:
                raise ServiceError(
                    422,
                    "E_EVIDENCE_UNVERIFIED",
                    "evidence file size does not match the DB reference",
                    details={
                        "cycle_id": cycle_id,
                        "path": relative_path,
                        "bytes": before.st_size,
                        "expected_bytes": expected_bytes,
                    },
                )
            raw = target.read_text(encoding="utf-8")
            after = target.stat()
            if after.st_size != expected_bytes or after.st_size != before.st_size:
                raise ServiceError(
                    422,
                    "E_EVIDENCE_UNVERIFIED",
                    "evidence file changed while being read",
                    details={"cycle_id": cycle_id, "path": relative_path},
                )
            payload = json.loads(raw)
        except ServiceError:
            raise
        except (OSError, ValueError) as exc:
            raise ServiceError(
                422,
                "E_EVIDENCE_UNVERIFIED",
                "evidence file is not valid JSON",
                details={"cycle_id": cycle_id, "error": type(exc).__name__},
            ) from exc
        from ..market.evidence import verify_hash  # 延迟导入，避免执行层依赖 market 初始化

        provenance = payload.get("provenance") if isinstance(payload, dict) else None
        actual_sha = str((provenance or {}).get("snapshot_hash") or "")
        if (
            not isinstance(payload, dict)
            or not verify_hash(payload)
            or actual_sha != expected_sha
        ):
            raise ServiceError(
                422,
                "E_EVIDENCE_UNVERIFIED",
                "evidence file hash does not match the DB reference",
                details={"cycle_id": cycle_id, "path": relative_path},
            )
        if str(payload.get("schema_version") or "") != schema_version:
            raise ServiceError(
                422,
                "E_EVIDENCE_UNVERIFIED",
                "evidence schema mismatch between file and DB reference",
                details={"cycle_id": cycle_id},
            )
        if int(payload.get("produced_at_ms") or 0) != produced_at_ms:
            raise ServiceError(
                422,
                "E_EVIDENCE_UNVERIFIED",
                "evidence produced_at_ms mismatch between file and DB reference",
                details={"cycle_id": cycle_id},
            )
        now_ms = int(self._clock() * 1000)
        if produced_at_ms > now_ms + 1000:
            raise ServiceError(
                422,
                "E_EVIDENCE_UNVERIFIED",
                "evidence timestamp is in the future",
                details={"cycle_id": cycle_id, "produced_at_ms": produced_at_ms},
            )
        return {
            "evidence_id": cycle_id,
            "schema_version": schema_version,
            "produced_at_ms": produced_at_ms,
            "age_ms": max(0, now_ms - produced_at_ms),
            "sha256": expected_sha,
            "bytes": expected_bytes,
            "path": relative_path,
        }

    def _server_market_facts(self, symbol: str) -> dict[str, Any]:
        """P1 的服务端事实来源；拿不到就 fail-closed，绝不回退到请求体。"""

        if self.broker is None:
            raise ServiceError(
                503,
                "E_DEMO_CREDENTIALS_MISSING",
                "server market facts require Demo credentials",
                details={"env": ["WORKBENCH_DEMO_API_KEY", "WORKBENCH_DEMO_API_SECRET"]},
            )
        try:
            return self.broker.market_facts(symbol)
        except ExecutionRejected as exc:
            raise ServiceError(exc.status_code, exc.code, exc.message, details=exc.details) from exc

    def _require_broker(self) -> None:
        if self.broker is None or self.client is None:
            raise ServiceError(
                503,
                "E_DEMO_CREDENTIALS_MISSING",
                "Demo API key/secret not configured; signed execution disabled",
                details={"env": ["WORKBENCH_DEMO_API_KEY", "WORKBENCH_DEMO_API_SECRET"]},
            )
        self.ensure_started()

    def _filters_view(self, symbol: str) -> SymbolFiltersView | None:
        if self.broker is None:
            return None
        try:
            return self.broker.filters_for(symbol).to_view()
        except Exception:  # noqa: BLE001 - 过滤器不可用时规则按最小信息评估
            return None

    def _positions_from(self, snapshot: BrokerSnapshot | None) -> list[dict[str, Any]]:
        if snapshot is not None and snapshot.error is None:
            return [
                {
                    **position.as_dict(),
                    "protection": snapshot.protection.get(position.symbol, {}),
                }
                for position in snapshot.positions
            ]
        rows = []
        for row in self.ledger.list_positions():
            rows.append(
                {
                    "symbol": row["symbol"],
                    "side": row["position_side"],
                    "position_side": row["position_side"],
                    "qty": row["qty"],
                    "entry_price": row["entry_price"],
                    "mark_price": row.get("mark_price"),
                    "upnl": row.get("upnl"),
                    "leverage": row.get("leverage"),
                    "margin": row.get("margin"),
                    "margin_mode": row.get("margin_mode"),
                    "liq_price": row.get("liq_price"),
                    "protection": {"status": row.get("protection_status")},
                    "origin": row.get("origin"),
                    "stale": True,
                }
            )
        return rows

    def _account_payload(
        self, snapshot: BrokerSnapshot | None, equity: _EquityInfo
    ) -> dict[str, Any] | None:
        if snapshot is None or snapshot.error is not None:
            return None
        account = snapshot.account
        return {
            "total_margin_balance": dec_text(account.get("totalMarginBalance")),
            "total_wallet_balance": dec_text(account.get("totalWalletBalance")),
            "available_balance": dec_text(account.get("availableBalance")),
            "total_unrealized_profit": dec_text(account.get("totalUnrealizedProfit")),
            "total_maint_margin": dec_text(account.get("totalMaintMargin")),
            "total_initial_margin": dec_text(account.get("totalInitialMargin")),
            "equity_session": equity.as_dict()["session"],
            "stale": bool(snapshot.stale),
        }

    def _blocking_reasons(
        self,
        snapshot: BrokerSnapshot | None,
        equity: _EquityInfo,
        risk_state: dict[str, Any],
        control: dict[str, Any],
        outbox_counts: dict[str, int],
    ) -> list[str]:
        reasons: list[str] = []
        if not self.configured:
            reasons.append("E_DEMO_CREDENTIALS_MISSING")
        if snapshot is None or snapshot.error is not None:
            reasons.append("E_EXCHANGE_UNAVAILABLE")
        else:
            if snapshot.stale:
                reasons.append("EXCHANGE_STALE")
            if snapshot.protection_ok() is False:
                reasons.append("PROTECTION_MISSING")
            elif snapshot.protection_ok() is None:
                # 保护覆盖未证明：未收敛前同样禁止新开仓。
                reasons.append("PROTECTION_UNPROVEN")
        if not equity.available:
            reasons.append("EQUITY_UNAVAILABLE")
        if control.get("state") != "normal":
            reasons.append("CONTROL_NOT_NORMAL")
        if risk_state.get("daily_loss_latched"):
            reasons.append("DAILY_LOSS_LATCHED")
        if risk_state.get("drawdown_latched"):
            reasons.append("DRAWDOWN_LATCHED")
        if outbox_counts.get("unknown", 0) > 0:
            reasons.append("ORDER_OUTBOX_UNKNOWN")
        if outbox_counts.get("submitting", 0) > 0:
            reasons.append("ORDER_OUTBOX_SUBMITTING")
        return list(dict.fromkeys(reasons))

    def _risk_context(
        self,
        *,
        snapshot: BrokerSnapshot | None,
        equity: _EquityInfo,
        risk_state: dict[str, Any],
        control: dict[str, Any],
        market: dict[str, Any],
        evidence_age_ms: Any,
        symbol: str | None = None,
        leverage: int | None = None,
    ) -> RiskContext:
        positions: list[PositionView] = []
        used_margin = Decimal("0")
        if snapshot is not None and snapshot.error is None:
            for position in snapshot.positions:
                used_margin += position.margin or Decimal("0")
                positions.append(
                    PositionView(
                        symbol=position.symbol,
                        position_side=position.position_side,
                        qty=position.qty,
                        margin=position.margin,
                        origin="OPENED_BY_WORKBENCH",
                    )
                )
        else:
            for row in self.ledger.list_positions():
                qty = to_decimal(row.get("qty"), Decimal("0")) or Decimal("0")
                used_margin += to_decimal(row.get("margin"), Decimal("0")) or Decimal("0")
                positions.append(
                    PositionView(
                        symbol=row["symbol"],
                        position_side=row["position_side"],
                        qty=qty,
                        margin=to_decimal(row.get("margin")),
                        origin=str(row.get("origin") or "OPENED_BY_WORKBENCH"),
                    )
                )

        unknown_orders = int(self.outbox.counts().get("unknown", 0)) if self.outbox else 0
        submitting = int(self.outbox.counts().get("submitting", 0)) if self.outbox else 0
        pending_rows = (
            self.ledger.query(
                "SELECT business_key FROM order_outbox WHERE status IN "
                "('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED') "
                "AND business_key LIKE 'ENTRY:%'"
            )
            if self.outbox
            else []
        )
        day_start = (
            datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        trades_today = self.ledger.count_entries_since(day_start)
        trades_symbol_today = (
            self.ledger.count_entries_since(day_start, symbol=symbol) if symbol else 0
        )
        maint_ratio = None
        if snapshot is not None and snapshot.error is None:
            maint = to_decimal(snapshot.account.get("totalMaintMargin"))
            balance = snapshot.equity_exchange
            if maint is not None and balance is not None and balance > 0:
                maint_ratio = maint / balance

        available_margin: Decimal | None = None
        if snapshot is not None and snapshot.error is None:
            available_margin = snapshot.available_balance
            if available_margin is None:
                # 账户接口缺 availableBalance：按最保守的 0 处理（R07 拒绝），
                # 绝不用“权益×比例”冒充可用保证金。
                available_margin = Decimal("0")
        # MMR 未证明时保持 None，由 R11 fail-closed（不得用 1% 兜底放宽）。
        # 按目标杠杆选档位：全档最大值（1x 档常见 50%）会把任何 lev≥2 的计划判死。
        mmr = (
            self.broker.mmr_for(symbol, leverage)
            if self.broker is not None and symbol
            else None
        )

        base = RiskContext(
            equity_session=equity.session,
            equity_age_ms=equity.age_ms,
            used_margin=used_margin,
            available_margin=available_margin,
            mmr=mmr,
            open_positions=tuple(positions),
            day_start_equity=to_decimal(risk_state.get("day_start_equity")),
            peak_equity=to_decimal(risk_state.get("peak_equity")),
            daily_loss_latched=bool(risk_state.get("daily_loss_latched")),
            drawdown_latched=bool(risk_state.get("drawdown_latched")),
            control_state=str(control.get("state") or "normal"),
            position_mode=snapshot.position_mode if snapshot else "UNKNOWN",
            maint_margin_ratio=maint_ratio,
            unknown_orders=unknown_orders,
            submitting_orders=submitting,
            protection_ok=(
                None if snapshot is None or snapshot.error is not None else snapshot.protection_ok()
            ),
            protection_missing=(
                () if snapshot is None else tuple(snapshot.protection_missing())
            ),
            trades_today=trades_today,
            trades_symbol_today=trades_symbol_today,
            pending_business_keys=tuple(
                str(row["business_key"]) for row in pending_rows if row.get("business_key")
            ),
            cooldown_active=self.state.cooldown_active(),
        )
        return RiskContext.from_payload(
            {"market": market, "evidence_age_ms": evidence_age_ms}, base=base
        )


# ---------------------------------------------------------------------- 工厂
def build_execution_service(
    settings: Settings,
    *,
    client: DemoBinanceClient | None = None,
    ledger: Ledger | None = None,
    credentials: DemoCredentials | None = None,
    limits: RiskLimits | None = None,
    broker_options: dict[str, Any] | None = None,
    use_env_credentials: bool = True,
    clock=time.time,
) -> ExecutionService:
    """组装服务；``client`` 为空时尝试从 env/``.env.local`` 读取 Demo 凭据。"""

    ledger = ledger or Ledger(settings.data_dir)
    limits = limits or RiskLimits.from_env()
    # T20：读路径的有界超时/预算/冷却默认取配置，测试可显式覆盖。
    resolved_broker_options = dict(broker_options or {})
    resolved_broker_options.setdefault(
        "snapshot_budget_s", settings.exchange_read_budget_s
    )
    resolved_broker_options.setdefault(
        "failure_cooldown_s", settings.exchange_failure_cooldown_s
    )
    state = RiskStateStore(ledger, limits)
    approvals = ApprovalManager(ledger, ttl_s=limits.approval_ttl_s, clock=clock)
    risk = RiskService(limits, ledger, state, approvals)
    outbox = OrderOutbox(ledger)

    resolved_credentials = credentials
    if client is None and resolved_credentials is None and use_env_credentials:
        # §4.2：先把账本与环境身份绑定（错目录/错身份直接拒绝，不静默改标签）。
        ledger.bind_venue(settings.venue)

        # §6 M2：凭据按环境取。Live 缺凭据时返回 None（不再装配客户端），
        # 绝不回退到 Demo 凭据——环境串味比“暂时不能交易”危险得多。
        # 凭据位置与 data_dir 绑定：settings 写入 <data_dir>/credentials/{demo,live}.env，
        # 包内 .env.local / config/live.env 只作旧安装兜底（见 docs/bundle-DISTRIBUTION.md §4.3）。
        resolved_credentials = (
            load_live_credentials(_SERVICE_DIR, data_dir=settings.data_dir)
            if settings.venue == "live"
            else load_demo_credentials(_SERVICE_DIR, data_dir=settings.data_dir)
        )
    if client is None and resolved_credentials is not None:
        # §4.2：账本还要绑定到具体的凭据指纹——换 key 不换账本会被拒绝，
        # 但指纹只证明“同一把 key”，所以同时记 account_identity_verified=false。
        ledger.bind_account(
            settings.venue,
            resolved_credentials.fingerprint(),
            source=resolved_credentials.source,
        )
        client = DemoBinanceClient(
            settings.base_url,
            resolved_credentials,
            read_timeout_s=settings.read_timeout_s,
            # §6 M2：把该环境的受控 host 传给客户端守卫；不传就会永远只认 demo host，
            # 实盘启动时客户端会拒绝自己（这是 live 路径的必要一环）。
            allowed_host=settings.venue_host,
        )
    broker = (
        DemoBroker(client, ledger, outbox, limits, **resolved_broker_options)
        if client is not None
        else None
    )
    if broker is not None:
        broker.fill_symbols = list(settings.fill_symbols)
    return ExecutionService(
        settings,
        ledger,
        limits=limits,
        state=state,
        approvals=approvals,
        risk=risk,
        outbox=outbox,
        client=client,
        broker=broker,
        credentials=resolved_credentials,
        clock=clock,
    )


# ---------------------------------------------------------------------- helpers
_PENDING_STATUSES = frozenset({"SUBMITTING", "UNKNOWN"})
_WORKING_STATUSES = frozenset({"ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED"})


def _pending_display(status: Any) -> str:
    """对外展示口径：提交中/结果未知只能显示“待确认”，不是失败。"""

    value = str(status or "").upper()
    if value in _PENDING_STATUSES:
        return "PENDING_CONFIRMATION"
    if value in _WORKING_STATUSES:
        return "WORKING"
    if value == "FILLED":
        return "FILLED"
    if value in {"CANCELED", "EXPIRED", "REJECTED"}:
        return "TERMINAL"
    return "UNKNOWN"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stable_time_stop_id(symbol: str, position_side: str, anchor: str) -> str:
    import hashlib

    raw = f"time_stop:{symbol.upper()}:{position_side.upper()}:{anchor}"
    return f"timestop-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _row_age_s(created_at: Any, now: float) -> float | None:
    stamp = _parse_dt(created_at)
    if stamp is None:
        return None
    return round(max(0.0, float(now) - stamp.timestamp()), 1)


def _annotate_order(row: dict[str, Any], now: float) -> dict[str, Any]:
    status = str(row.get("status") or "").upper()
    annotated = dict(row)
    annotated.update(
        {
            "business_key": row.get("outbox_business_key"),
            "intent_id": row.get("cycle_id"),
            "display": _pending_display(status),
            "pending_confirmation": status in _PENDING_STATUSES,
            "working": status in _WORKING_STATUSES,
            "terminal": status in TERMINAL_STATUSES,
            "age_s": _row_age_s(row.get("created_at"), now),
            "outbox_status": row.get("outbox_status"),
            # 收敛原因必须可解释：为什么交易所没有它却曾停在 NEW。
            "convergence_reason": (
                str(row.get("error_msg") or "")
                if status == "CANCELED" and str(row.get("error_msg") or "").startswith("canceled_")
                else None
            ),
        }
    )
    return annotated


def _annotate_pending(
    rows: list[dict[str, Any]], *, now: float | None = None
) -> list[dict[str, Any]]:
    moment = time.time() if now is None else float(now)
    annotated: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(
            {
                "display": _pending_display(row.get("status")),
                "pending_confirmation": True,
                "age_s": _row_age_s(row.get("created_at"), moment),
            }
        )
        annotated.append(item)
    return annotated


def _ms_to_iso(value: int | None) -> str | None:
    """epoch 毫秒 → 秒级 UTC ISO；None 保持 None（未知不伪造成时间）。"""

    if value is None:
        return None
    return _iso_utc(datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc))


def _parse_iso_ms(value: str) -> int | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _downsample(rows: list[dict[str, Any]], bucket_s: int) -> list[dict[str, Any]]:
    if not rows:
        return []
    bucket_s = max(1, int(bucket_s))
    points: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row["ts_bucket"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        epoch = int(stamp.timestamp())
        bucket = epoch - (epoch % bucket_s)
        points[bucket] = {
            "ts": datetime.fromtimestamp(bucket, tz=timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "equity_session": row.get("equity_session"),
            "equity_exchange": row.get("equity_exchange"),
            "available": row.get("available"),
            "upnl": row.get("upnl"),
            "margin_used": row.get("margin_used"),
        }
    return [points[key] for key in sorted(points)]
