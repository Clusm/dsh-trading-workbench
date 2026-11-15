"""Demo 执行 Broker：账户快照、市价开仓 + 强制 SL/TP、平仓、保护修复、对账。

对应 docs/03 §5（订单生命周期）、§6（对账）、§9（失败路径）的最小实现：
- POST 前 Outbox 落库；结果不确定时**只查询不重发**；
- 开仓成功后必须确认 SL/TP 覆盖，确证失败则立即市价平仓（绝不裸仓）；
- 查询失败（``UNKNOWN``）不平仓、不重挂，只标记并让后续请求被 R23/R17 拦下；
- 仅调用 ``demo-fapi.binance.com``（由 ``DemoBinanceClient`` 强制）。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..ledger.numbers import dec_text, to_decimal
from ..ledger.store import Ledger, utc_now
from ..risk.config import RiskLimits
from ..risk.models import OrderPlan
from .client import DemoBinanceClient
from .errors import DemoApiError, ExecutionConfigError, ExecutionRejected, OutboxUncertain
from .filters import (
    SymbolFilters,
    format_qty,
    load_exchange_info,
    stop_trigger,
    take_profit_trigger,
)
from .outbox import NOT_FOUND_REASON, OrderOutbox, TERMINAL_STATUSES, status_rank
from .protection import (
    ProtectionCoverage,
    ProtectionFailurePolicy,
    ProtectionManager,
    ProtectionResult,
    coverage_from_rows,
)

__all__ = ["DemoBroker", "BrokerSnapshot", "NormalizedPosition", "derive_client_order_id"]

logger = logging.getLogger("workbench.broker")

_CACHE_TTL_S = 2.0
_TERMINAL = TERMINAL_STATUSES | {"EXPIRED_IN_MATCH"}


@dataclass(frozen=True, slots=True)
class NormalizedPosition:
    symbol: str
    position_side: str
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal | None = None
    upnl: Decimal | None = None
    leverage: int | None = None
    margin: Decimal | None = None
    margin_mode: str | None = None
    liq_price: Decimal | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "side": self.position_side,
            "qty": dec_text(self.qty),
            "entry_price": dec_text(self.entry_price),
            "mark_price": None if self.mark_price is None else dec_text(self.mark_price),
            "upnl": None if self.upnl is None else dec_text(self.upnl),
            "leverage": self.leverage,
            "margin": None if self.margin is None else dec_text(self.margin),
            "margin_mode": self.margin_mode,
            "liq_price": None if self.liq_price is None else dec_text(self.liq_price),
        }


@dataclass(slots=True)
class BrokerSnapshot:
    account: dict[str, Any] = field(default_factory=dict)
    positions: list[NormalizedPosition] = field(default_factory=list)
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    algo_orders: list[dict[str, Any]] = field(default_factory=list)
    protection: dict[str, dict[str, Any]] = field(default_factory=dict)
    position_mode: str = "UNKNOWN"
    fetched_at: float = 0.0
    fetched_at_iso: str = ""
    error: str | None = None
    stale: bool = False
    #: T20 §2.2：最近一次**真正**从交易所取到数据的时间。交易所不可达时界面必须
    #: 显示“最后成功时间”，而不是把失败时间伪装成数据时间。
    last_success_at: str | None = None

    @property
    def equity_exchange(self) -> Decimal | None:
        return to_decimal(self.account.get("totalMarginBalance"))

    @property
    def wallet_balance(self) -> Decimal | None:
        return to_decimal(self.account.get("totalWalletBalance"))

    @property
    def available_balance(self) -> Decimal | None:
        return to_decimal(self.account.get("availableBalance"))

    @property
    def unrealized_pnl(self) -> Decimal | None:
        return to_decimal(self.account.get("totalUnrealizedProfit"))

    def protection_ok(self) -> bool | None:
        if self.error:
            return None
        if not self.positions:
            return True
        statuses = [self.protection.get(pos.symbol, {}).get("status") for pos in self.positions]
        if any(status == "UNKNOWN" for status in statuses):
            return None
        return all(status == "OK" for status in statuses)

    def protection_missing(self) -> list[str]:
        return [
            pos.symbol
            for pos in self.positions
            if self.protection.get(pos.symbol, {}).get("status") not in (None, "OK")
        ]


def derive_client_order_id(cycle_id: str, symbol: str, action: str, seq: int = 0) -> str:
    """确定性 clientOrderId：``wb-{cycle18}-{sym6}-{act1}-{seq}``（≤36 字符）。

    M1.4d：必须取 cycle_id 的**末尾**（时间戳 + 随机后缀），不能取前 8 个字符。
    cycle_id 形如 ``cyc-20261110T161044Z-abc123``，前 8 个字符整日固定为
    ``cyc20261``，会让同一天所有轮次派生同一个 clientOrderId；后续轮次的 P2
    因此命中 outbox 幂等分支，静默回放上一笔订单（界面上“成交”，实际没下单）。
    """

    cycle_compact = re.sub(r"[^A-Za-z0-9]", "", str(cycle_id or ""))
    cycle = cycle_compact[-18:] if cycle_compact else f"m{uuid.uuid4().hex[:7]}"
    base = re.sub(r"[^A-Za-z0-9]", "", symbol.upper().replace("USDT", ""))[:6] or "SYM"
    action_code = (action or "O").upper()[:1]
    return f"wb-{cycle}-{base}-{action_code}-{int(seq)}"


class DemoBroker:
    def __init__(
        self,
        client: DemoBinanceClient,
        ledger: Ledger,
        outbox: OrderOutbox,
        limits: RiskLimits,
        *,
        environment: str = "demo",
        poll_attempts: int = 5,
        poll_interval_s: float = 0.15,
        unknown_query_attempts: int = 2,
        unknown_query_interval_s: float = 0.1,
        protection_policy: ProtectionFailurePolicy | None = None,
        not_found_confirmations: int = 2,
        #: 快照缓存 TTL（秒）；测试可设 0 以强制每次真读。
        cache_ttl_s: float = _CACHE_TTL_S,
        #: T20：一次只读快照的总预算（秒）；0 表示不设总预算。
        snapshot_budget_s: float = 4.0,
        #: T20：读取失败后的冷却（秒）；冷却期内不再打上游。
        failure_cooldown_s: float = 15.0,
        clock=time.time,
    ) -> None:
        self.client = client
        self.ledger = ledger
        self.outbox = outbox
        self.limits = limits
        self.environment = environment
        self.fill_symbols: list[str] = []
        self.protection_policy = protection_policy or ProtectionFailurePolicy()
        self.protection = ProtectionManager(
            client, outbox, policy=self.protection_policy, clock=clock
        )
        #: 一次“查不到”可能是撮合尚未可见；需要几次独立查询事实才收敛。
        self._not_found_confirmations = max(1, int(not_found_confirmations))
        self._poll_attempts = max(1, int(poll_attempts))
        self._poll_interval_s = max(0.0, float(poll_interval_s))
        self._unknown_query_attempts = max(1, int(unknown_query_attempts))
        self._unknown_query_interval_s = max(0.0, float(unknown_query_interval_s))
        self._clock = clock
        self._filters: dict[str, SymbolFilters] = {}
        self._filters_loaded_at = 0.0
        self._position_mode: str | None = None
        self._cache: BrokerSnapshot | None = None
        self._last_good: BrokerSnapshot | None = None
        # T20：失败冷却（避免每个请求都真打上游打满超时）+ 最近失败事实（可读）。
        self._cache_ttl_s = max(0.0, float(cache_ttl_s))
        self._snapshot_budget_s = max(0.0, float(snapshot_budget_s))
        self._failure_cooldown_s = max(0.0, float(failure_cooldown_s))
        self._unavailable_until = 0.0
        self._last_failure: dict[str, Any] | None = None

    # ------------------------------------------------------------------ 基础设施
    def sync_time(self) -> int | None:
        return self.client.sync_time()

    def load_filters(self, *, force: bool = False) -> dict[str, SymbolFilters]:
        now = self._clock()
        if not force and self._filters and now - self._filters_loaded_at < 3600:
            return self._filters
        self._filters = load_exchange_info(self.client)
        self._filters_loaded_at = now
        return self._filters

    def filters_for(self, symbol: str) -> SymbolFilters:
        symbol = symbol.upper()
        filters = self._filters.get(symbol)
        if filters is None:
            self.load_filters(force=True)
            filters = self._filters.get(symbol)
        if filters is None:
            raise ExecutionConfigError(f"symbol filters unavailable for {symbol}")
        return filters

    def position_mode(self, *, force: bool = False) -> str:
        if self._position_mode is None or force:
            mode = self.client.position_mode()
            if mode is None:
                raise ExecutionRejected(
                    "E_POSITION_MODE_UNPROVEN",
                    "cannot prove account position mode",
                    status_code=503,
                )
            self._position_mode = mode
        return self._position_mode

    # ------------------------------------------------------------------ 读取
    def account(self, *, force: bool = False) -> dict[str, Any]:
        snapshot = self.snapshot(force=force)
        return snapshot.account

    def positions(self, *, force: bool = False) -> list[NormalizedPosition]:
        snapshot = self.snapshot(force=force)
        return snapshot.positions

    def snapshot(self, *, force: bool = False, cooldown: bool = True) -> BrokerSnapshot:
        """当前账户事实；读路径的有界超时 + 失败冷却都在这里（T20）。

        - 正常：2s 内复用缓存，否则真读（总时长受 ``snapshot_budget_s`` 约束）；
        - 失败：记下冷却窗口，窗口内**不再打上游**，直接给出 ``stale=True`` 的
          最近成功副本（有）或明确的不可用（无）；
        - ``force=True`` 会跳过缓存与冷却：下单前的硬刷新必须看到当下事实，
          宁可失败也不能拿旧数据冒充现在；
        - ``cooldown=False`` 只给**交易评估**路径用（P1/P2）：那里不能拿旧快照
          放行，必须在有界预算内重试一次并失败关闭；只读展示路径保持默认
          ``cooldown=True``，故障期间不反复打上游。
        """

        now = self._clock()
        if not force and self._cache is not None and now - self._cache.fetched_at < self._cache_ttl_s:
            return self._cache
        if not force and cooldown and now < self._unavailable_until:
            return self._cooldown_snapshot(now)
        try:
            if self._snapshot_budget_s > 0:
                with self.client.read_budget(self._snapshot_budget_s):
                    snapshot = self._fetch_snapshot()
            else:
                snapshot = self._fetch_snapshot()
        except Exception as exc:  # noqa: BLE001 - 读取失败保留最近有效快照
            self._note_failure(now, exc)
            if self._last_good is not None:
                stale = self._stale_from_last_good(now, exc)
                self._cache = stale
                return stale
            raise ExecutionRejected(
                "E_EXCHANGE_UNAVAILABLE",
                f"demo exchange unreachable: {type(exc).__name__}",
                status_code=502,
                details={
                    "error": type(exc).__name__,
                    "last_success_at": self.last_success_at,
                    "cooldown_until": self.cooldown_until,
                    "read_budget_s": self._snapshot_budget_s,
                },
            ) from exc
        self._unavailable_until = 0.0
        self._last_failure = None
        self._cache = snapshot
        self._last_good = snapshot
        return snapshot

    @property
    def last_success_at(self) -> str | None:
        return self._last_good.fetched_at_iso if self._last_good is not None else None

    @property
    def cooldown_until(self) -> str | None:
        if self._unavailable_until <= self._clock():
            return None
        return datetime.fromtimestamp(self._unavailable_until, tz=timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")

    def read_health(self) -> dict[str, Any]:
        """只读取数健康（不触发上游）；供 ``/api/status`` 等端点解释降级原因。"""

        now = self._clock()
        failure = self._last_failure or {}
        return {
            "last_success_at": self.last_success_at,
            "cooldown_until": self.cooldown_until,
            "cooldown_remaining_s": round(max(0.0, self._unavailable_until - now), 1),
            "last_error": failure.get("error"),
            "last_failure_at": failure.get("at"),
            "read_budget_s": self._snapshot_budget_s,
            "failure_cooldown_s": self._failure_cooldown_s,
            "read_timeout_s": getattr(self.client, "read_timeout_s", None),
            "write_timeout_s": getattr(self.client, "write_timeout_s", None),
        }

    def _note_failure(self, now: float, exc: BaseException) -> None:
        self._unavailable_until = now + self._failure_cooldown_s
        self._last_failure = {
            "at": utc_now(),
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "cooldown_until": self.cooldown_until,
        }
        # 失败时作废 2s 缓存：否则同一请求内的后续读取还会拿“上次成功”的快照，
        # 让降级状态看起来像正常。
        self._cache = None

    def _stale_from_last_good(self, now: float, exc: BaseException) -> BrokerSnapshot:
        good = self._last_good
        assert good is not None
        return BrokerSnapshot(
            account=dict(good.account),
            positions=list(good.positions),
            open_orders=list(good.open_orders),
            algo_orders=list(good.algo_orders),
            protection=dict(good.protection),
            position_mode=good.position_mode,
            fetched_at=now,
            fetched_at_iso=utc_now(),
            error=f"{type(exc).__name__}: {exc}"[:200],
            stale=True,
            last_success_at=good.fetched_at_iso,
        )

    def _cooldown_snapshot(self, now: float) -> BrokerSnapshot:
        """冷却期内不复用失败结果，也不再打上游。"""

        if self._last_good is not None:
            stale = self._stale_from_last_good(
                now,
                DemoApiError(
                    "exchange in failure cooldown",
                    path="/",
                    kind="timeout",
                ),
            )
            self._cache = stale
            return stale
        failure = (self._last_failure or {}).get("error") or "exchange unreachable"
        raise ExecutionRejected(
            "E_EXCHANGE_UNAVAILABLE",
            f"demo exchange unreachable (cooldown): {failure}"[:200],
            status_code=502,
            details={
                "error": "failure_cooldown",
                "last_success_at": self.last_success_at,
                "cooldown_until": self.cooldown_until,
                "read_budget_s": self._snapshot_budget_s,
            },
        )

    def _fetch_snapshot(self) -> BrokerSnapshot:
        account = self.client.account()
        raw_positions = self.client.position_risk()
        open_orders = self.client.open_orders()
        algo_rows = self.client.open_algo_orders()
        mode = self._position_mode or self.client.position_mode() or "UNKNOWN"
        self._position_mode = mode

        positions: list[NormalizedPosition] = []
        for row in raw_positions:
            normalized = _normalize_position(row)
            if normalized is not None:
                positions.append(normalized)

        algo_normalized = [ProtectionManager._normalize(row) for row in algo_rows]
        protection: dict[str, dict[str, Any]] = {}
        for pos in positions:
            coverage = coverage_from_rows(
                pos.symbol, algo_normalized, position_side=pos.position_side
            )
            protection[pos.symbol] = coverage.as_dict()
            protection[pos.symbol]["status"] = coverage.status

        stamp = utc_now()
        return BrokerSnapshot(
            account=account,
            positions=positions,
            open_orders=open_orders,
            algo_orders=algo_normalized,
            protection=protection,
            position_mode=mode,
            fetched_at=self._clock(),
            fetched_at_iso=stamp,
            # 成功取到的事实时间；失败副本用它回答“最后成功是什么时候”。
            last_success_at=stamp,
        )

    def equity(self) -> Decimal | None:
        try:
            return self.snapshot().equity_exchange
        except Exception:  # noqa: BLE001 - 调用方按"不可用"处理
            return None

    def position(self, symbol: str, *, force: bool = True) -> NormalizedPosition | None:
        for pos in self.positions(force=force):
            if pos.symbol == symbol.upper():
                return pos
        return None

    # ------------------------------------------------------------------ 开仓
    def open_market(
        self,
        plan: OrderPlan,
        qty: Decimal,
        *,
        cycle_id: str,
        trace_id: str | None = None,
        time_stop: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = plan.symbol.upper()
        filters = self.filters_for(symbol)
        if filters.status != "TRADING":
            raise ExecutionRejected("E_RISK_SYMBOL", f"{symbol} status {filters.status}")
        mode = self.position_mode()

        qty_text = format_qty(qty, filters.step_size)
        if to_decimal(qty_text) is None or Decimal(qty_text) <= 0:
            raise ExecutionRejected("E_RISK_BELOW_MIN", f"qty {qty} below stepSize")

        self._prepare_symbol(symbol, plan.leverage, mode)

        cid = derive_client_order_id(cycle_id, symbol, "O", 0)
        side = "BUY" if plan.side == "LONG" else "SELL"
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty_text,
            "newClientOrderId": cid,
        }
        if mode == "HEDGE":
            params["positionSide"] = plan.position_side
        else:
            params["reduceOnly"] = "false"

        prepared = self.outbox.prepare(
            client_order_id=cid,
            purpose="ENTRY",
            symbol=symbol,
            side=side,
            position_side=plan.position_side if mode == "HEDGE" else None,
            order_type="MARKET",
            endpoint="/fapi/v1/order",
            request=params,
            qty=qty_text,
        )
        if prepared.duplicate:
            existing = self.ledger.get_order(cid)
            logger.warning("duplicate open skipped cid=%s", cid)
            return {
                "client_order_id": cid,
                "intent_id": cycle_id,
                "business_key": prepared.business_key,
                "status": (existing or {}).get("status", "UNKNOWN"),
                "idempotent_replay": True,
                "duplicate": True,
                "qty_requested": qty_text,
                "qty_filled": (existing or {}).get("qty_filled", "0"),
                "avg_price": (existing or {}).get("avg_price"),
                "protection": {"status": "UNKNOWN"},
                "position": None,
            }

        response, recovered = self._submit_with_recovery(
            cid, symbol, params, kind="entry", trace_id=trace_id, cycle_id=cycle_id
        )
        fills = self._wait_and_persist(
            cid, symbol, side, plan, response, qty_text, recovered, cycle_id=cycle_id
        )
        filled_qty = to_decimal(fills.get("qty_filled"), Decimal("0")) or Decimal("0")
        fill_proven = bool(fills.get("fill_proven"))
        terminal = str(fills.get("status") or "").upper() in _TERMINAL

        # ---- 成交/持仓证明（H03）：零成交必须由订单终态证明；有成交必须有仓位 ----
        position: NormalizedPosition | None = None
        position_error: Exception | None = None
        try:
            position = self._read_entry_position(symbol)
        except Exception as exc:  # noqa: BLE001 - 读取失败必须与“零成交”区分
            position_error = exc
        if position is None and terminal and fill_proven and filled_qty > 0:
            # 撮合与 positionRisk 之间可能有毫秒级延迟：再读一次
            try:
                position = self._read_entry_position(symbol)
                position_error = None
            except Exception as exc:  # noqa: BLE001
                position_error = exc

        protection_payload: dict[str, Any] = {"status": "NOT_APPLICABLE"}
        emergency: dict[str, Any] | None = None
        protection_close_failed = False
        proof = "position_snapshot"

        if position is None:
            if terminal and fill_proven and filled_qty == 0:
                # 订单终态 + executedQty=0：零成交已由订单查询证明
                self._refresh_positions_after_trade(symbol, protection_payload)
                return {
                    "client_order_id": cid,
                    "exchange_order_id": fills.get("exchange_order_id"),
                    "status": fills.get("status"),
                    "qty_requested": qty_text,
                    "qty_filled": "0",
                    "avg_price": fills.get("avg_price"),
                    "fill_unconfirmed": False,
                    "recovered_from_unknown": recovered,
                    "protection": protection_payload,
                    "position": None,
                    "emergency_close": None,
                    "zero_fill": True,
                    "proof": "order_terminal_zero_fill",
                    "protection_close_failed": False,
                    "idempotent_replay": False,
                }
            # 成交或持仓无法证明：先尝试平掉“可能存在的仓位”，平不掉则 UNKNOWN 阻断
            emergency = self._safe_emergency_flatten(
                symbol,
                filled_qty,
                reason="position_unproven",
                cycle_id=f"{cycle_id}-unproven",
                trace_id=trace_id,
            )
            self.ledger.log_risk_event(
                event_type="position_unproven",
                severity="critical",
                rule_id="R23",
                action="emergency_close" if emergency.get("closed") else "unknown_block",
                details={
                    "symbol": symbol,
                    "filled_qty": str(filled_qty),
                    "order_status": fills.get("status"),
                    "read_error": (
                        None
                        if position_error is None
                        else f"{type(position_error).__name__}: {position_error}"
                    ),
                    "emergency": emergency,
                },
                trace_id=trace_id,
            )
            if emergency.get("closed"):
                self._refresh_positions_after_trade(symbol, {"status": "UNKNOWN"})
                return {
                    "client_order_id": cid,
                    "exchange_order_id": fills.get("exchange_order_id"),
                    "status": fills.get("status"),
                    "qty_requested": qty_text,
                    "qty_filled": fills.get("qty_filled", "0"),
                    "avg_price": fills.get("avg_price"),
                    "fill_unconfirmed": True,
                    "recovered_from_unknown": recovered,
                    "protection": {"status": "UNKNOWN"},
                    "position": None,
                    "emergency_close": emergency,
                    "zero_fill": False,
                    "proof": "emergency_closed_after_unproven",
                    "protection_close_failed": False,
                    "idempotent_replay": False,
                }
            current = self.outbox.get(cid)
            if current is None or str(current.get("status")).upper() not in _TERMINAL:
                self.outbox.mark_unknown(cid, RuntimeError("position unproven"))
                raise OutboxUncertain(
                    cid,
                    "entry fill/position unproven and emergency close failed; query only, never repost",
                    details={
                        "client_order_id": cid,
                        "reason": "position_unproven",
                        "emergency": emergency,
                    },
                )
            # 订单已终态（不可回退为 UNKNOWN），用结构化标记让服务层切 close_only。
            return {
                "client_order_id": cid,
                "exchange_order_id": fills.get("exchange_order_id"),
                "status": fills.get("status"),
                "qty_requested": qty_text,
                "qty_filled": fills.get("qty_filled", "0"),
                "avg_price": fills.get("avg_price"),
                "fill_unconfirmed": True,
                "recovered_from_unknown": recovered,
                "protection": {"status": "UNKNOWN"},
                "position": None,
                "emergency_close": emergency,
                "zero_fill": False,
                "proof": "position_unproven",
                "position_unproven": True,
                "protection_close_failed": False,
                "idempotent_replay": False,
            }

        sl_text = stop_trigger(position.entry_price, plan.sl, plan.side, filters.tick_size)
        tp_text = take_profit_trigger(position.entry_price, plan.tp, plan.side, filters.tick_size)
        result = self.protection.ensure(
            symbol=symbol,
            position_side=position.position_side,
            sl_trigger=sl_text,
            tp_trigger=tp_text,
            retry=self.limits.protection_place_retry,
            position_mode=mode,
        )
        protection_payload = result.as_dict()
        self._record_protection_orders(symbol, position, result, cycle_id=cycle_id)

        if result.status == "UNKNOWN":
            # 查询失败不是“没有保护”：不平仓、不重挂，只告警并让 R23 阻断
            self.ledger.log_risk_event(
                event_type="protection_unknown",
                severity="critical",
                rule_id="R23",
                action="alert_only_no_flat_close",
                details={"symbol": symbol, "error": result.coverage.error},
                trace_id=trace_id,
            )
        elif not result.ok:
            # H02：MISMATCH/PARTIAL/MISSING/DUPLICATE/PROTECTION_FAILED 一律紧急平仓
            self.ledger.log_risk_event(
                event_type="protection_incomplete",
                severity="critical",
                rule_id="R23",
                action="emergency_close",
                details={"symbol": symbol, "status": result.status, "result": result.as_dict()},
                trace_id=trace_id,
            )
            emergency = self._safe_emergency_flatten(
                symbol,
                position.qty,
                reason=f"protection_{result.status.lower()}",
                cycle_id=f"{cycle_id}-protfix",
                trace_id=trace_id,
            )
            if not emergency.get("closed"):
                protection_close_failed = True
                self._mark_position_unprotected(symbol, "PROTECTION_FAILED")
                self.ledger.log_risk_event(
                    event_type="protection_close_failed",
                    severity="critical",
                    rule_id="R23",
                    action="control_blocked",
                    details={
                        "symbol": symbol,
                        "protection_status": result.status,
                        "emergency": emergency,
                    },
                    trace_id=trace_id,
                )

        self._refresh_positions_after_trade(symbol, protection_payload)

        # T03 §8·B6: 期限与来源计划版本随仓位持久化；重启后按原期限判断。
        # 必须在仓位行写入之后执行（否则 UPDATE 命中 0 行）。
        if time_stop:
            self.ledger.set_position_time_stop(
                symbol,
                position.position_side,
                opened_at=time_stop.get("opened_at"),
                deadline_at=time_stop.get("deadline_at"),
                source=time_stop.get("source"),
            )

        return {
            "client_order_id": cid,
            "exchange_order_id": fills.get("exchange_order_id"),
            "status": fills.get("status"),
            "qty_requested": qty_text,
            "qty_filled": fills.get("qty_filled", "0"),
            "avg_price": fills.get("avg_price"),
            "fill_unconfirmed": fills.get("fill_unconfirmed", False),
            "recovered_from_unknown": recovered,
            "protection": protection_payload,
            "position": position.as_dict(),
            "emergency_close": emergency,
            "zero_fill": False,
            "proof": proof,
            "protection_close_failed": protection_close_failed,
            "protection_policy": self.protection_policy.as_dict(),
            "idempotent_replay": False,
            "intent_id": cycle_id,
            "business_key": prepared.business_key,
            "time_stop": time_stop,
        }

    def _prepare_symbol(self, symbol: str, leverage: int, mode: str) -> None:
        try:
            self.client.set_margin_type(symbol, "ISOLATED")
        except DemoApiError as exc:
            # 保证金模式无法确认/设置时必须 fail-closed：cross 与 isolated 的
            # 风险口径不同（docs/03 §3.2），不能带着未知模式开仓。
            self.ledger.log_risk_event(
                event_type="margin_mode_unconfirmed",
                severity="critical",
                rule_id="R04",
                action="skip order",
                details={"symbol": symbol, "error": exc.as_dict()},
            )
            raise ExecutionRejected(
                "E_MARGIN_MODE_UNCONFIRMED",
                f"cannot confirm ISOLATED margin mode on {symbol}: {exc.message}",
                status_code=422,
                details={"binance_code": exc.code, "mode": mode},
            ) from exc
        try:
            self.client.set_leverage(symbol, leverage)
        except DemoApiError as exc:
            self.ledger.log_risk_event(
                event_type="leverage_set_failed",
                severity="critical",
                rule_id="R04",
                action="skip order",
                details={"symbol": symbol, "leverage": leverage, "error": exc.as_dict()},
            )
            raise ExecutionRejected(
                "E_LEVERAGE_SET",
                f"cannot set leverage {leverage}x on {symbol}",
                details={"binance_code": exc.code},
            ) from exc

    # ------------------------------------------------------------------ 内部读取
    def _read_entry_position(self, symbol: str) -> NormalizedPosition | None:
        """读开仓后的真实仓位；读取失败抛异常（不能当成“零成交”）。"""

        snapshot = self.snapshot(force=True)
        if snapshot.error:
            raise RuntimeError(f"position snapshot unavailable: {snapshot.error}")
        for position in snapshot.positions:
            if position.symbol == symbol.upper():
                return position
        return None

    def _safe_emergency_flatten(
        self,
        symbol: str,
        qty: Decimal,
        *,
        reason: str,
        cycle_id: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        """尽力平仓；失败只返回结构化错误（由调用方决定控制态）。"""

        try:
            return self.close_market(
                symbol,
                qty=qty if qty > 0 else None,
                cycle_id=cycle_id,
                reason=reason,
                trace_id=trace_id,
            )
        except Exception as exc:  # noqa: BLE001 - 紧急平仓任何异常都不能再抛
            self.ledger.log_risk_event(
                event_type="emergency_close_failed",
                severity="critical",
                rule_id="R23",
                action="mark_unprotected",
                details={
                    "symbol": symbol,
                    "qty": dec_text(qty),
                    "reason": reason,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                trace_id=trace_id,
            )
            return {
                "symbol": symbol,
                "closed": False,
                "status": "ERROR",
                "reason": reason,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _mark_position_unprotected(self, symbol: str, status: str) -> None:
        for row in self.ledger.list_positions():
            if row["symbol"] != symbol.upper():
                continue
            self.ledger.upsert_position(
                {**row, "ledger_id": self.ledger.ledger_id, "protection_status": status}
            )

    # ------------------------------------------------------------------ 市场事实
    def mmr_for(self, symbol: str, leverage: int | None = None) -> Decimal | None:
        """逐 symbol 维持保证金率；未从 leverageBracket 取得时返回 None（R11 fail-closed）。

        ``leverage`` 给定时返回“允许该杠杆的最小档位”的 mmr；不给时退回全档最大值
        （仅用于诊断/兼容调用）。
        """

        try:
            filters = self.filters_for(symbol)
        except Exception:  # noqa: BLE001 - 过滤器缺失 = MMR 未证明
            return None
        if leverage is None:
            return filters.maint_margin_ratio
        return filters.mmr_for_leverage(leverage)

    def market_facts(self, symbol: str) -> dict[str, Any]:
        """服务端实时行情事实（P1 唯一事实来源，拒绝请求体覆盖）。"""

        try:
            premium = self.client.premium_index(symbol)
            book = self.client.book_ticker(symbol)
            klines = self.client.klines(symbol, interval="1m", limit=2)
        except DemoApiError as exc:
            raise ExecutionRejected(
                "E_MARKET_UNAVAILABLE",
                f"server market facts unavailable for {symbol}: {exc.message}",
                status_code=503,
                details=exc.as_dict(),
            ) from exc

        mark = to_decimal(premium.get("markPrice"))
        if mark is None or mark <= 0:
            raise ExecutionRejected(
                "E_MARKET_UNAVAILABLE",
                f"markPrice missing for {symbol}",
                status_code=503,
            )
        index_price = to_decimal(premium.get("indexPrice"))
        deviation = None
        if index_price is not None and index_price > 0:
            deviation = abs((mark - index_price) / index_price)
        spread_bps = None
        bid = to_decimal(book.get("bidPrice"))
        ask = to_decimal(book.get("askPrice"))
        if bid is not None and ask is not None and bid > 0 and ask > bid:
            mid = (bid + ask) / 2
            if mid > 0:
                spread_bps = ((ask - bid) / mid) * Decimal("10000")
        mark_move = None
        if len(klines) >= 2:
            previous_close = to_decimal(klines[-2].get("close"))
            if previous_close is not None and previous_close > 0:
                mark_move = abs((mark - previous_close) / previous_close)

        return {
            "mark": dec_text(mark),
            "index_price": None if index_price is None else dec_text(index_price),
            "snapshot_age_ms": 0,
            "mark_move_60s_pct": None if mark_move is None else dec_text(mark_move),
            "mark_index_deviation_pct": None if deviation is None else dec_text(deviation),
            "spread_bps": None if spread_bps is None else dec_text(spread_bps),
        }

    def _submit_with_recovery(
        self,
        cid: str,
        symbol: str,
        params: dict[str, Any],
        *,
        kind: str,
        trace_id: str | None,
        cycle_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        try:
            response = self.client.submit_order(params)
            self.outbox.acknowledge(cid, response)
            return response, False
        except DemoApiError as exc:
            if exc.definitely_rejected:
                self.outbox.reject(cid, exc)
                self._insert_order_record(
                    cid,
                    symbol=symbol,
                    side=str(params.get("side")),
                    order_type=str(params.get("type")),
                    qty=params.get("quantity"),
                    status="REJECTED",
                    binance_code=exc.code,
                    error_msg=exc.message,
                    raw=exc.as_dict(),
                    purpose="ENTRY" if kind == "entry" else "EXIT",
                    cycle_id=cycle_id,
                )
                raise ExecutionRejected(
                    "E_EXCHANGE_REJECT",
                    f"binance rejected {kind}: {exc.code} {exc.message}",
                    status_code=422,
                    details={"binance_code": exc.code, "kind": kind},
                ) from exc

            self.outbox.mark_unknown(cid, exc)
            self.ledger.log_risk_event(
                event_type="order_unknown",
                severity="critical",
                rule_id="R17",
                action="query_only_no_repost",
                details={"client_order_id": cid, "symbol": symbol, "error": exc.as_dict()},
                trace_id=trace_id,
            )
            recovered = self._recover_unknown_order(cid, symbol)
            if recovered is not None:
                self.outbox.acknowledge(cid, recovered)
                return recovered, True

            self._insert_order_record(
                cid,
                symbol=symbol,
                side=str(params.get("side")),
                order_type=str(params.get("type")),
                qty=params.get("quantity"),
                status="UNKNOWN",
                raw={"error": exc.as_dict()},
                purpose="ENTRY" if kind == "entry" else "EXIT",
                cycle_id=cycle_id,
            )
            raise OutboxUncertain(
                cid,
                f"{kind} result unknown; query only, never repost",
                details={"client_order_id": cid, "error": exc.as_dict()},
            ) from exc

    def _recover_unknown_order(self, cid: str, symbol: str) -> dict[str, Any] | None:
        for _ in range(self._unknown_query_attempts):
            try:
                data = self.client.query_order(symbol, cid)
            except DemoApiError as exc:
                if exc.code == -2013:
                    return None
                if self._unknown_query_interval_s:
                    time.sleep(self._unknown_query_interval_s)
                continue
            if data.get("orderId") is not None:
                return data
        return None

    # ------------------------------------------------------------------ 平仓
    def close_market(
        self,
        symbol: str,
        *,
        qty: Decimal | None = None,
        cycle_id: str | None = None,
        reason: str = "manual",
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        position = self.position(symbol, force=True)
        if position is None:
            return {
                "symbol": symbol,
                "status": "NO_POSITION",
                "closed": False,
                "reason": reason,
            }
        filters = self.filters_for(symbol)
        remaining = position.qty if qty is None else min(Decimal(qty), position.qty)
        if remaining <= 0:
            return {"symbol": symbol, "status": "NO_POSITION", "closed": False, "reason": reason}

        mode = self.position_mode()
        close_side = "SELL" if position.position_side == "LONG" else "BUY"
        cycle = cycle_id or f"close-{uuid.uuid4().hex[:6]}"
        cid = derive_client_order_id(cycle, symbol, "C", 0)
        qty_text = format_qty(remaining, filters.step_size)
        if Decimal(qty_text) <= 0:
            raise ExecutionRejected("E_RISK_BELOW_MIN", "close qty below stepSize")

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": qty_text,
            "newClientOrderId": cid,
        }
        if mode == "HEDGE":
            params["positionSide"] = position.position_side
        else:
            params["reduceOnly"] = "true"

        prepared = self.outbox.prepare(
            client_order_id=cid,
            purpose="EXIT",
            symbol=symbol,
            side=close_side,
            position_side=position.position_side if mode == "HEDGE" else None,
            order_type="MARKET",
            endpoint="/fapi/v1/order",
            request=params,
            qty=qty_text,
        )
        if prepared.duplicate:
            existing = self.ledger.get_order(cid)
            return {
                "symbol": symbol,
                "client_order_id": cid,
                "status": (existing or {}).get("status", "UNKNOWN"),
                "closed": False,
                "idempotent_replay": True,
            }

        response, recovered = self._submit_with_recovery(
            cid, symbol, params, kind="exit", trace_id=trace_id, cycle_id=cycle
        )
        fills = self._wait_and_persist(
            cid,
            symbol,
            close_side,
            None,
            response,
            qty_text,
            recovered,
            purpose="EXIT",
            cycle_id=cycle,
        )

        after = self.position(symbol, force=True)
        order_count = 1
        if after is not None and not fills.get("fill_unconfirmed") and after.qty > 0:
            leftover = after.qty
            retry_text = format_qty(leftover, filters.step_size)
            if Decimal(retry_text) > 0:
                retry_cid = derive_client_order_id(cycle, symbol, "C", 1)
                retry_params = dict(params)
                retry_params["quantity"] = retry_text
                retry_params["newClientOrderId"] = retry_cid
                try:
                    self.outbox.prepare(
                        client_order_id=retry_cid,
                        purpose="EXIT",
                        symbol=symbol,
                        side=close_side,
                        position_side=position.position_side if mode == "HEDGE" else None,
                        order_type="MARKET",
                        endpoint="/fapi/v1/order",
                        request=retry_params,
                        qty=retry_text,
                    )
                    retry_response, _ = self._submit_with_recovery(
                        retry_cid,
                        symbol,
                        retry_params,
                        kind="exit",
                        trace_id=trace_id,
                        cycle_id=cycle,
                    )
                    self._wait_and_persist(
                        retry_cid,
                        symbol,
                        close_side,
                        None,
                        retry_response,
                        retry_text,
                        False,
                        purpose="EXIT",
                        cycle_id=cycle,
                    )
                    order_count += 1
                except Exception as exc:  # noqa: BLE001 - 重试失败保留仓位给下一轮
                    logger.warning("close retry failed symbol=%s error=%s", symbol, exc)

        after = self.position(symbol, force=True)
        flat = after is None or after.qty <= 0
        canceled = 0
        if flat:
            canceled = self._cancel_symbol_protections(symbol)
            self.ledger.delete_position(symbol, position.position_side)
            self.ledger.insert_position_event(
                symbol=symbol,
                position_side=position.position_side,
                kind="CLOSE",
                note=f"closed reason={reason}",
            )
        else:
            self._refresh_positions_after_trade(symbol, {})

        pnl = fills.get("pnl")
        return {
            "symbol": symbol,
            "client_order_id": cid,
            "status": fills.get("status"),
            "closed": flat,
            "qty": qty_text,
            "avg_price": fills.get("avg_price"),
            "pnl": None if pnl is None else dec_text(pnl),
            "order_count": order_count,
            "fill_unconfirmed": fills.get("fill_unconfirmed", False),
            "protection_canceled": canceled,
            "reason": reason,
        }

    # ------------------------------------------------------------------ 保护修复
    def _ledger_position(self, symbol: str, position_side: str) -> dict[str, Any] | None:
        for row in self.ledger.list_positions():
            if (
                str(row.get("symbol") or "").upper() == symbol.upper()
                and str(row.get("position_side") or "").upper() == position_side.upper()
            ):
                return row
        return None

    def _fallback_triggers(self, position: NormalizedPosition) -> tuple[str, str]:
        filters = self.filters_for(position.symbol)
        mark = position.mark_price or position.entry_price
        if position.position_side == "LONG":
            sl_price = mark * (Decimal(1) - Decimal(str(self.limits.fallback_sl_pct)))
            tp_price = mark * (Decimal(1) + Decimal(str(self.limits.fallback_tp_pct)))
        else:
            sl_price = mark * (Decimal(1) + Decimal(str(self.limits.fallback_sl_pct)))
            tp_price = mark * (Decimal(1) - Decimal(str(self.limits.fallback_tp_pct)))
        return (
            stop_trigger(mark, sl_price, position.position_side, filters.tick_size),
            take_profit_trigger(mark, tp_price, position.position_side, filters.tick_size),
        )

    def _ensure_symbol_protection(
        self,
        position: NormalizedPosition,
        snapshot: BrokerSnapshot,
        *,
        reduce_on_failure: bool,
    ) -> dict[str, Any]:
        """核对/补挂一个仓位的保护，并按策略处置“确认建不起来”的情况。

        - 查询失败（UNKNOWN）→ 只阻断新风险，不平仓、不重挂（不把未知当缺失）；
        - 已确认失败 + 系统仓位 → 按配置的风险减少策略市价减仓并记录命令结果；
        - 外部仓位 → 不自动套用系统策略，只阻断新开仓并记录“未受保护”事实。
        """

        sl_text, tp_text = self._fallback_triggers(position)
        result = self.protection.ensure(
            symbol=position.symbol,
            position_side=position.position_side,
            sl_trigger=sl_text,
            tp_trigger=tp_text,
            retry=self.limits.protection_place_retry,
            position_mode=snapshot.position_mode,
            policy=self.protection_policy,
            verify_triggers=False,
        )
        self._record_protection_orders(position.symbol, position, result)
        self._update_position_protection(
            position.symbol,
            result.coverage.as_dict()["status"],
            sl=result.coverage.sl,
            tp=result.coverage.tp,
        )
        payload: dict[str, Any] = {
            "symbol": position.symbol,
            "position_side": position.position_side,
            "qty": dec_text(position.qty),
            **result.as_dict(),
            "disposition": "none",
        }
        if result.ok:
            return payload
        if result.status == "UNKNOWN" or not result.confirmed_failure:
            self.ledger.log_risk_event(
                event_type="protection_unproven",
                severity="critical",
                rule_id="R23",
                action="block_new_entries_no_flat_close",
                details={
                    "symbol": position.symbol,
                    "status": result.status,
                    "result": result.as_dict(),
                },
            )
            payload["disposition"] = "block_new_entries"
            return payload

        ledger_row = self._ledger_position(position.symbol, position.position_side) or {}
        origin = str(ledger_row.get("origin") or "OPENED_BY_WORKBENCH").upper()
        policy = self.protection_policy
        if origin == "EXTERNAL" or not reduce_on_failure or policy.on_failure == "block_new_risk":
            reason = (
                "external_position_not_managed"
                if origin == "EXTERNAL"
                else "policy_block_new_risk"
            )
            self.ledger.log_risk_event(
                event_type="protection_unrecoverable",
                severity="critical",
                rule_id="R23",
                action="block_new_entries_position_unprotected",
                details={
                    "symbol": position.symbol,
                    "origin": origin,
                    "reason": reason,
                    "policy": policy.as_dict(),
                    "result": result.as_dict(),
                },
            )
            self._mark_position_unprotected(position.symbol, "PROTECTION_FAILED")
            payload["disposition"] = "block_new_entries"
            payload["protection_failure_reason"] = reason
            return payload

        # 系统管理仓位：风险减少策略 = 市价减仓。命令标识由仓位身份派生，
        # 同一仓位重复触发不会重复平仓（operation_history 主键即幂等边界）。
        command_id = _stable_command_id(
            "protreduce",
            position.symbol,
            position.position_side,
            ledger_row.get("opened_at") or "",
        )
        payload["command_id"] = command_id
        if not self.ledger.operation_start(
            command_id, "reduce_position", position.symbol, "protection_unrecoverable"
        ):
            self.ledger.log_risk_event(
                event_type="protection_reduce_already_recorded",
                severity="warn",
                rule_id="R23",
                action="no_repeat_reduce",
                details={"symbol": position.symbol, "command_id": command_id},
            )
            payload["disposition"] = "reduce_already_recorded"
            return payload
        try:
            closed = self.close_market(
                position.symbol,
                qty=position.qty,
                cycle_id=command_id,
                reason="protection_unrecoverable",
            )
        except Exception as exc:  # noqa: BLE001 - 平仓失败只记录，由控制态阻断
            self.ledger.operation_finish(command_id, "unknown", code="E_REDUCE_UNKNOWN")
            self._mark_position_unprotected(position.symbol, "PROTECTION_FAILED")
            self.ledger.log_risk_event(
                event_type="protection_reduce_failed",
                severity="critical",
                rule_id="R23",
                action="control_close_only_position_unprotected",
                details={
                    "symbol": position.symbol,
                    "command_id": command_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            payload["disposition"] = "reduce_unknown"
            payload["reduce_error"] = f"{type(exc).__name__}: {exc}"
            return payload
        flat = bool(closed.get("closed"))
        self.ledger.operation_finish(
            command_id,
            "succeeded" if flat else "partial",
            summary={
                "symbol": position.symbol,
                "closed": flat,
                "policy": policy.as_dict(),
                "reason": "protection_unrecoverable",
            },
        )
        if not flat:
            self._mark_position_unprotected(position.symbol, "PROTECTION_FAILED")
        self.ledger.log_risk_event(
            event_type="protection_reduce_position",
            severity="critical",
            rule_id="R23",
            action="reduce_position" if flat else "reduce_position_incomplete",
            details={
                "symbol": position.symbol,
                "command_id": command_id,
                "policy": policy.as_dict(),
                "closed": flat,
            },
        )
        payload["disposition"] = "reduce_position" if flat else "reduce_position_incomplete"
        payload["closed"] = flat
        return payload

    def repair_protection(
        self, symbols: list[str] | None = None, *, reduce_on_failure: bool | None = None
    ) -> dict[str, Any]:
        snapshot = self.snapshot(force=True)
        targets = {pos.symbol for pos in snapshot.positions}
        if symbols:
            targets &= {symbol.upper() for symbol in symbols}
        reduce = (
            self.protection_policy.on_failure == "reduce_position"
            if reduce_on_failure is None
            else bool(reduce_on_failure)
        )
        results: list[dict[str, Any]] = []
        for position in snapshot.positions:
            if position.symbol not in targets:
                continue
            results.append(
                self._ensure_symbol_protection(
                    position, snapshot, reduce_on_failure=reduce
                )
            )
        if results:
            # 修复后强制刷新快照，避免 /api/status 命中修复前的 2s 缓存。
            # 只刷新成功项：失败项的“未受保护/未证明”事实必须保留，不能被
            # 一次迟到的快照覆盖成 OK 或 MISSING。
            ok_symbols = {item["symbol"] for item in results if item.get("ok")}
            snapshot = self.snapshot(force=True)
            for position in snapshot.positions:
                if position.symbol not in ok_symbols:
                    continue
                entry = snapshot.protection.get(position.symbol, {}) or {}
                self._update_position_protection(
                    position.symbol,
                    entry.get("status", "UNKNOWN"),
                    sl=entry.get("sl"),
                    tp=entry.get("tp"),
                )
        return {
            "ok": all(item.get("ok") for item in results),
            "repaired": results,
            "checked": len(results),
            "policy": self.protection_policy.as_dict(),
        }

    def enforce_protection(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """监督周期入口：核对并补挂保护，失败时按已配置策略减少风险。"""

        return self.repair_protection(symbols, reduce_on_failure=True)


    def _cancel_symbol_protections(self, symbol: str) -> int:
        try:
            rows = self.protection.list_open(symbol)
        except Exception:  # noqa: BLE001 - 查询失败下轮重试
            return 0
        canceled = 0
        for row in rows:
            algo_id = row.get("algo_id") or ""
            if algo_id and self.protection.cancel(algo_id):
                canceled += 1
        return canceled

    # ------------------------------------------------------------------ 查询收敛
    def _query_order_record(self, order: dict[str, Any]) -> dict[str, Any] | None:
        """按原 client_order_id 查询交易所事实；明确 -2013 返回 None。"""

        cid = str(order.get("client_order_id") or "")
        if not cid:
            return None
        purpose = str(order.get("purpose") or "").upper()
        if purpose == "PROTECTION":
            try:
                data = self.client.query_algo_order(cid)
            except DemoApiError as exc:
                if exc.code == -2013:
                    return None
                raise
            return data if data.get("algoId") is not None else None
        symbol = str(order.get("symbol") or "")
        try:
            data = self.client.query_order(symbol, cid)
        except DemoApiError as exc:
            if exc.code == -2013:
                return None
            raise
        return data if data.get("orderId") is not None else None

    def _persist_query_fact(
        self, cid: str, order: dict[str, Any], data: dict[str, Any], *, is_algo: bool
    ) -> str:
        """把查询到的交易所事实写回 outbox + orders（终态只由这个事实决定）。"""

        status = self.outbox.status_from_response(data)
        self.outbox.acknowledge(cid, data)
        avg_price = None if is_algo else to_decimal(data.get("avgPrice") or data.get("price"))
        if avg_price is not None and avg_price <= 0:
            avg_price = None
        self._insert_order_record(
            cid,
            symbol=str(order.get("symbol") or data.get("symbol") or ""),
            side=str(order.get("side") or data.get("side") or ""),
            order_type=str(order.get("order_type") or data.get("type") or ""),
            qty=order.get("qty_requested") or data.get("origQty") or "0",
            status=status,
            purpose=str(order.get("purpose") or ("PROTECTION" if is_algo else "ENTRY")),
            exchange_order_id=str(
                data.get("algoId") if is_algo else data.get("orderId")
            ) or order.get("exchange_order_id"),
            qty_filled=data.get("executedQty") or ("0" if is_algo else order.get("qty_filled") or "0"),
            avg_price=None if avg_price is None else dec_text(avg_price),
            raw=data,
            position_side=order.get("position_side"),
            cycle_id=order.get("cycle_id"),
        )
        return status

    def _converge_order_not_found(
        self, order: dict[str, Any], *, reason: str = NOT_FOUND_REASON, note: bool = True
    ) -> bool:
        """交易所双重事实（不在 open 列表 + 按原标识查不到）→ 明确终态。

        收敛依据是可解释的交易所事实，不是“重试次数用尽”；历史行因此能说明
        “为什么它不在交易所却仍是 NEW”。
        """

        cid = str(order.get("client_order_id") or "")
        if not cid:
            return False
        if note:
            self.outbox.note_query(cid, found=False, error=reason)
        self.outbox.converge_not_found(cid, reason=reason, confirmations_required=1)
        row = self.outbox.get(cid)
        # 幂等：outbox 已经是 CANCELED 时同样把 orders 镜像收敛到同一终态。
        if row is None or str(row.get("status") or "").upper() != "CANCELED":
            return False
        self._insert_order_record(
            cid,
            symbol=str(order.get("symbol") or ""),
            side=str(order.get("side") or ""),
            order_type=str(order.get("order_type") or ""),
            qty=order.get("qty_requested") or "0",
            status="CANCELED",
            purpose=str(order.get("purpose") or "ENTRY"),
            exchange_order_id=order.get("exchange_order_id"),
            qty_filled=order.get("qty_filled") or "0",
            avg_price=order.get("avg_price"),
            error_msg=reason,
            raw={
                "convergence": reason,
                "source": "exchange_not_found",
                "previous_status": order.get("status"),
            },
            position_side=order.get("position_side"),
            cycle_id=order.get("cycle_id"),
        )
        return True

    def converge_stale_orders(
        self, *, snapshot: BrokerSnapshot | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """按交易所事实收敛“工作态永停”的订单（T01 基线 G4）。

        历史保护单在 orders/order_outbox 停在 NEW，而交易所 openOrders/openAlgoOrders
        没有它、按原 client_order_id 查询返回 -2013。规则：只有同时满足
        (1) 不在交易所 open 列表、(2) 按原标识查询被明确回答“无此单”，
        才写入 CANCELED + canceled_not_found_at_exchange；查询失败保持原状态。
        """

        open_lists_available = True
        if snapshot is None:
            try:
                snapshot = self.snapshot(force=True)
            except ExecutionRejected:
                # 拿不到 open 列表时仍可按原标识逐个查询；查询本身是权威事实。
                snapshot = None
                open_lists_available = False
        open_order_cids = (
            {str(row.get("clientOrderId") or "") for row in snapshot.open_orders}
            if snapshot is not None
            else set()
        )
        open_algo_cids = (
            {str(row.get("client_algo_id") or "") for row in snapshot.algo_orders}
            if snapshot is not None
            else set()
        )
        open_algo_ids = (
            {str(row.get("algo_id") or "") for row in snapshot.algo_orders}
            if snapshot is not None
            else set()
        )
        ttl_s = max(30.0, float(getattr(self.limits, "unknown_resolve_ttl_ms", 120_000)) / 1000.0)
        now = self._clock()
        checked = 0
        errors = 0
        converged: list[dict[str, Any]] = []
        for order in self.ledger.list_active_orders(limit=limit):
            cid = str(order.get("client_order_id") or "")
            if not cid or str(order.get("status") or "").upper() not in ACTIVE_ORDER_STATUSES:
                continue
            age = _age_seconds(order.get("created_at"), now)
            if age is None or age < ttl_s:
                continue
            is_algo = str(order.get("purpose") or "").upper() == "PROTECTION"
            if is_algo and (
                cid in open_algo_cids
                or str(order.get("exchange_order_id") or "") in open_algo_ids
            ):
                continue
            if not is_algo and cid in open_order_cids:
                continue
            checked += 1
            try:
                data = self._query_order_record(order)
            except DemoApiError as exc:
                # 查询失败不是“没有这张单”：保持工作态，下一轮再查。
                errors += 1
                logger.warning(
                    "converge_query_failed cid=%s error=%s", cid, exc.message
                )
                continue
            if data is not None:
                self._persist_query_fact(cid, order, data, is_algo=is_algo)
                continue
            if self._converge_order_not_found(order):
                converged.append(
                    {
                        "client_order_id": cid,
                        "purpose": str(order.get("purpose") or ""),
                        "symbol": str(order.get("symbol") or ""),
                        "previous_status": str(order.get("status") or ""),
                        "status": "CANCELED",
                        "reason": NOT_FOUND_REASON,
                        "age_s": round(age, 1),
                    }
                )
                self.ledger.log_risk_event(
                    event_type="order_converged_not_found",
                    severity="warn",
                    rule_id="R17",
                    action="terminal_canceled",
                    details={
                        "client_order_id": cid,
                        "symbol": order.get("symbol"),
                        "purpose": order.get("purpose"),
                        "previous_status": order.get("status"),
                        "reason": NOT_FOUND_REASON,
                    },
                )
        return {
            "checked": checked,
            "converged": converged,
            "errors": errors,
            "ttl_s": ttl_s,
            "open_lists_available": open_lists_available,
        }

    def query_order_state(
        self, client_order_id: str, *, trace_id: str | None = None
    ) -> dict[str, Any]:
        """断线后按原标识查询收敛（只查不重发）。"""

        cid = str(client_order_id or "").strip()
        if not cid:
            raise ExecutionRejected("E_VALIDATION", "client_order_id is required")
        order = self.ledger.get_order(cid)
        outbox_row = self.outbox.get(cid)
        if order is None and outbox_row is None:
            raise ExecutionRejected(
                "E_ORDER_NOT_FOUND",
                f"no local record for {cid}",
                status_code=404,
            )
        record = order or {
            "client_order_id": cid,
            "symbol": _symbol_from_business_key(outbox_row),
            "purpose": "PROTECTION"
            if str((outbox_row or {}).get("endpoint") or "").endswith("/algoOrder")
            else "ENTRY",
            "status": str((outbox_row or {}).get("status") or "UNKNOWN"),
            "created_at": str((outbox_row or {}).get("created_at") or ""),
            "exchange_order_id": None,
            "side": str((outbox_row or {}).get("side") or ""),
            "order_type": "",
            "qty_requested": "0",
            "qty_filled": "0",
            "avg_price": None,
            "position_side": None,
            "cycle_id": None,
        }
        current = str(record.get("status") or "").upper()
        is_algo = str(record.get("purpose") or "").upper() == "PROTECTION"
        try:
            data = self._query_order_record(record)
        except DemoApiError as exc:
            return {
                "client_order_id": cid,
                "status": current,
                "pending_confirmation": True,
                "converged": False,
                "query_error": exc.as_dict(),
                "reason": "exchange_query_failed",
            }
        if data is not None:
            status = self._persist_query_fact(cid, record, data, is_algo=is_algo)
            return {
                "client_order_id": cid,
                "status": status,
                "pending_confirmation": status.upper() in {"SUBMITTING", "UNKNOWN"},
                "converged": True,
                "converged_by": "exchange_order_query",
                "exchange_order_id": record.get("exchange_order_id"),
            }
        confirmations = self.outbox.note_query(cid, found=False, error=NOT_FOUND_REASON)
        age = _age_seconds(record.get("created_at"), self._clock())
        ttl_s = max(30.0, float(getattr(self.limits, "unknown_resolve_ttl_ms", 120_000)) / 1000.0)
        required = 1 if age is not None and age >= ttl_s else self._not_found_confirmations
        self.outbox.converge_not_found(
            cid, reason=NOT_FOUND_REASON, confirmations_required=required
        )
        confirmed = current in {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED"}
        converged = self.outbox.get(cid)
        converged_status = str((converged or {}).get("status") or current).upper()
        if confirmed and confirmations >= required:
            self._converge_order_not_found(record, note=False)
            converged_status = "CANCELED"
        return {
            "client_order_id": cid,
            "status": converged_status,
            "pending_confirmation": converged_status in {"SUBMITTING", "UNKNOWN"},
            "converged": converged_status == "CANCELED",
            "converged_by": "exchange_not_found" if converged_status == "CANCELED" else None,
            "reason": NOT_FOUND_REASON,
            "query_confirmations": confirmations,
            "required_confirmations": required,
            "exchange_order_id": record.get("exchange_order_id"),
        }

    # ------------------------------------------------------------------ 对账
    def reconcile(self) -> dict[str, Any]:
        """对账入口：整轮上游读取共用一份读预算（T20）。

        对账内部会串行读账户/挂单/成交/未知单；不设总预算时“3s × N”仍会把
        这一轮拖到十几秒，监督循环因此错过下一个节拍。
        """

        if self._snapshot_budget_s > 0:
            with self.client.read_budget(self._snapshot_budget_s):
                return self._reconcile_once()
        return self._reconcile_once()

    def _reconcile_once(self) -> dict[str, Any]:
        snapshot = self.snapshot(force=True)
        stale_result = self._resolve_stale_submitting()
        unknown_result = self.outbox.reconcile(self._query_unknown_row)
        # G4: 工作态永停的历史单按交易所事实收敛（不在 open 列表 + 查不到）。
        converged = self.converge_stale_orders(snapshot=snapshot)

        exchange_keys = {(pos.symbol, pos.position_side) for pos in snapshot.positions}
        ledger_rows = {
            (row["symbol"], row["position_side"]): row
            for row in self.ledger.list_positions()
        }

        adopted: list[str] = []
        closed_external: list[str] = []
        incremental_fills: list[str] = []
        for position in snapshot.positions:
            key = (position.symbol, position.position_side)
            previous = ledger_rows.get(key)
            if previous is None:
                adopted.append(position.symbol)
                self.ledger.insert_position_event(
                    symbol=position.symbol,
                    position_side=position.position_side,
                    kind="ADOPT",
                    after=position.as_dict(),
                    note="exchange position without local record",
                )
            else:
                previous_qty = to_decimal(previous.get("qty"), Decimal("0")) or Decimal("0")
                if position.qty != previous_qty:
                    # 部分成交后继续成交（或外部增减仓）：必须重新核对保护覆盖。
                    incremental_fills.append(position.symbol)
            entry = snapshot.protection.get(position.symbol, {}) or {}
            coverage_status = entry.get("status", "UNKNOWN")
            record = {
                **position.as_dict(),
                "ledger_id": self.ledger.ledger_id,
                "protection_status": coverage_status,
                # 建仓身份事实不因对账刷新而改写；外部仓位永远保持 EXTERNAL。
                "origin": (previous or {}).get("origin") or "EXTERNAL",
            }
            # M1.4e: backfill the live triggers so the ledger never reports a
            # protected position as having no SL/TP.
            if coverage_status == "OK":
                if entry.get("sl") is not None:
                    record["sl_price"] = entry["sl"]
                if entry.get("tp") is not None:
                    record["tp_price"] = entry["tp"]
            self.ledger.upsert_position(record)
        for symbol, side in set(ledger_rows) - exchange_keys:
            self.ledger.delete_position(symbol, side)
            closed_external.append(symbol)
            self.ledger.insert_position_event(
                symbol=symbol,
                position_side=side,
                kind="CLOSE_EXTERNAL",
                note="position no longer exists on exchange",
            )

        if incremental_fills:
            self.ledger.log_risk_event(
                event_type="protection_recheck_after_incremental_fill",
                severity="warn",
                rule_id="R23",
                action="recheck_coverage",
                details={
                    "symbols": incremental_fills,
                    "protection": {
                        symbol: snapshot.protection.get(symbol, {})
                        for symbol in incremental_fills
                    },
                },
            )

        protection_missing = snapshot.protection_missing()
        if protection_missing:
            self.ledger.log_risk_event(
                event_type="protection_missing",
                severity="critical",
                rule_id="R23",
                action="block_new_entries_no_flat_close",
                details={"symbols": protection_missing},
            )

        fills_sync = self.sync_fills()
        return {
            "at": utc_now(),
            "exchange_reachable": snapshot.error is None,
            "stale": snapshot.stale,
            "outbox": unknown_result,
            "stale_submitting": stale_result,
            "converged_orders": converged,
            "positions": len(snapshot.positions),
            "adopted": adopted,
            "closed_external": closed_external,
            "incremental_fills": incremental_fills,
            "protection_missing": protection_missing,
            "protection_ok": snapshot.protection_ok(),
            "fills_sync": fills_sync,
        }


    # ------------------------------------------------------------------ §8·C2 资金流水
    def sync_income(
        self, *, start_ms: int, end_ms: int, complete_window: bool = True
    ) -> dict[str, Any]:
        """把交易所 income 流水落进 capital_flows（资金费/划转/返佣分开归类）。

        幂等：按交易所 income id 去重，重复回填不产生重复行。
        只有明确拿到窗口内的完整结果才把 income 流标为 complete——
        否则资金费仍是未知，不能按 0 计入策略盈亏。
        """

        if self.client is None:
            return {"ok": False, "reason": "no_client", "inserted": 0, "rows": 0, "complete": False}
        rows = self.client.income(start_ms=start_ms, end_ms=end_ms)
        mapped: list[dict[str, Any]] = []
        for row in rows:
            try:
                ref_id = str(row.get("tranId") or row.get("id") or "").strip()
                at_ms = int(row.get("time") or 0)
                amount = row.get("income")
                if not ref_id or not at_ms or amount in (None, ""):
                    continue
                income_type = str(row.get("incomeType") or "OTHER").upper()
                if income_type == "FUNDING_FEE":
                    kind = "FUNDING_FEE"
                elif income_type in {"TRANSFER", "INTERNAL_TRANSFER", "CROSS_COLLATERAL_TRANSFER"}:
                    kind = "TRANSFER_IN" if to_decimal(amount, Decimal("0")) >= 0 else "TRANSFER_OUT"
                elif income_type in {"COMMISSION", "INSURANCE_CLEAR"}:
                    kind = "COMMISSION"
                elif income_type == "REALIZED_PNL":
                    # 已实现盈亏以成交事实为准；income 里的同项只留档，不参与归集。
                    kind = "REALIZED_PNL"
                else:
                    kind = "OTHER"
                mapped.append(
                    {
                        "ref_id": ref_id,
                        "at_ms": at_ms,
                        "kind": kind,
                        "asset": str(row.get("asset") or "USDT"),
                        "amount": amount,
                        "income_type": income_type,
                        "symbol": row.get("symbol") or None,
                        "raw": row,
                    }
                )
            except (TypeError, ValueError):
                continue
        inserted = self.ledger.upsert_capital_flows(mapped)
        if complete_window:
            self.ledger.set_backfill_state(
                "income",
                cursor_ms=end_ms,
                covered_from_ms=start_ms,
                covered_to_ms=end_ms,
                complete=True,
            )
        return {
            "ok": True,
            "rows": len(rows),
            "mapped": len(mapped),
            "inserted": inserted,
            "complete": complete_window,
            "window": {"start_ms": start_ms, "end_ms": end_ms},
        }
    def sync_fills(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Backfill exchange fills with a persistent per-symbol cursor (A03).

        Known symbols come from configured ``fill_symbols``, current positions
        and recent local orders. Pages are pulled with ``fromId``; a stale/
        invalid cursor is cleared and retried from the recent page. This is
        still not an all-symbol discovery mechanism: the exchange requires a
        symbol for userTrades, so arbitrary external symbols need explicit
        configuration. ``complete`` therefore remains false.
        """

        if self.client is None:
            return {
                "symbols": 0, "inserted": 0, "errors": 0, "pages": 0,
                "recovered": [], "truncated": [], "complete": False,
            }
        if symbols is None:
            symbols = list(getattr(self, "fill_symbols", []) or [])
            symbols += [row["symbol"] for row in self.ledger.list_positions()]
            symbols += [row["symbol"] for row in self.ledger.list_orders(limit=500)]
        unique = sorted({str(symbol).upper() for symbol in symbols if symbol})
        order_map: dict[str, str] = {}
        for row in self.ledger.list_orders(limit=500):
            exchange_id = row.get("exchange_order_id")
            if exchange_id not in (None, ""):
                order_map[str(exchange_id)] = str(row["client_order_id"])
        inserted = 0
        errors = 0
        pages = 0
        recovered: list[str] = []
        truncated: list[str] = []
        cursors: dict[str, int] = {}
        max_pages = 20
        page_size = 1000
        for symbol in unique:
            cursor_key = f"fill_cursor:{symbol}"
            raw_cursor = self.ledger.get_meta(cursor_key)
            cursor = int(raw_cursor) if raw_cursor and str(raw_cursor).isdigit() else None
            from_id = (cursor + 1) if cursor is not None else None
            max_seen = cursor
            page = 0
            while page < max_pages:
                try:
                    rows = self.client.user_trades(
                        symbol,
                        from_id=from_id,
                        limit=page_size,
                    )
                except Exception:  # noqa: BLE001 - cursor recovery / next cycle
                    if cursor is not None and page == 0:
                        # Exchange rejected an old fromId: clear it and re-scan
                        # the exchange's recent page exactly once.
                        self.ledger.set_meta(cursor_key, "")
                        cursor = None
                        from_id = None
                        max_seen = None
                        if symbol not in recovered:
                            recovered.append(symbol)
                        continue
                    errors += 1
                    break
                page += 1
                pages += 1
                for row in rows:
                    trade_id = row.get("id") or row.get("tradeId")
                    order_id = row.get("orderId")
                    if trade_id is None or order_id in (None, ""):
                        continue
                    if str(trade_id).isdigit():
                        max_seen = max(max_seen or 0, int(trade_id))
                    cid = order_map.get(str(order_id))
                    realized = to_decimal(row.get("realizedPnl"))
                    try:
                        if cid is None:
                            cid = f"external-{order_id}"
                            if self.ledger.get_order(cid) is None:
                                self._insert_order_record(
                                    cid,
                                    symbol=symbol,
                                    side=str(row.get("side") or ""),
                                    order_type="MARKET",
                                    qty=row.get("qty") or 0,
                                    status="FILLED",
                                    purpose="EXIT" if realized not in (None, Decimal("0")) else "ENTRY",
                                    exchange_order_id=order_id,
                                    qty_filled=row.get("qty") or 0,
                                    avg_price=row.get("price"),
                                    raw=row,
                                )
                        if self.ledger.insert_fill(
                            {
                                "fill_id": str(trade_id),
                                "client_order_id": cid,
                                "symbol": symbol,
                                "side": str(row.get("side") or "").upper(),
                                "price": row.get("price") or 0,
                                "qty": row.get("qty") or 0,
                                "realized_pnl": row.get("realizedPnl"),
                                "commission": row.get("commission"),
                                "commission_asset": row.get("commissionAsset"),
                                "trade_time_ms": row.get("time") or 0,
                                "source": "reconcile",
                            }
                        ):
                            inserted += 1
                    except Exception:  # noqa: BLE001 - malformed row
                        errors += 1
                if len(rows) < page_size:
                    break
                if max_seen is None:
                    break
                next_from = max_seen + 1
                if from_id is not None and next_from <= from_id:
                    break
                from_id = next_from
            else:
                truncated.append(symbol)
            if max_seen is not None and max_seen > (cursor or 0):
                self.ledger.set_meta(cursor_key, str(max_seen))
                cursors[symbol] = max_seen
            elif cursor is not None:
                cursors[symbol] = cursor
        return {
            "symbols": len(unique),
            "inserted": inserted,
            "errors": errors,
            "pages": pages,
            "cursors": cursors,
            "recovered": recovered,
            "truncated": truncated,
            # Explicitly not a complete account history: configured symbols,
            # bounded pages, and no backward historical scan before first cursor.
            "complete": False,
        }

    def _resolve_stale_submitting(self) -> dict[str, Any]:
        """Resolve SUBMITTING rows left behind by a crash or an aborted request.

        Query-only: if the exchange knows the client id we acknowledge it;
        otherwise the exchange explicitly answered "no such order" and we write
        the explicit terminal CANCELED + canceled_not_found_at_exchange. The
        convergence reason is an exchange fact, never a retry counter.
        """

        checked = resolved = 0
        not_found = 0
        for row in self.outbox.stale_submitting(older_than_s=120):
            checked += 1
            cid = str(row.get("client_order_id") or "")
            if not cid:
                continue
            try:
                data = self._query_unknown_row(row)
            except Exception:  # noqa: BLE001 - 查询失败保持原状态，下轮再试
                continue
            if data:
                self._persist_query_fact(
                    cid,
                    {
                        "client_order_id": cid,
                        "symbol": _symbol_from_business_key(row),
                        "purpose": "PROTECTION"
                        if str(row.get("endpoint") or "").endswith("/algoOrder")
                        else "ENTRY",
                        "side": "",
                        "order_type": "",
                        "qty_requested": "0",
                        "status": str(row.get("status") or ""),
                        "created_at": str(row.get("created_at") or ""),
                    },
                    data,
                    is_algo=str(row.get("endpoint") or "").endswith("/algoOrder"),
                )
            else:
                self.outbox.note_query(cid, found=False, error=NOT_FOUND_REASON)
                self.outbox.converge_not_found(
                    cid, reason=NOT_FOUND_REASON, confirmations_required=1
                )
                order = self.ledger.get_order(cid)
                if order is not None:
                    self._converge_order_not_found(order, note=False)
                not_found += 1
            resolved += 1
        return {"checked": checked, "resolved": resolved, "not_found": not_found}
    def _query_unknown_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        endpoint = str(row.get("endpoint") or "")
        cid = str(row["client_order_id"])
        try:
            if endpoint.endswith("/algoOrder"):
                data = self.client.query_algo_order(cid)
                return data if data.get("algoId") is not None else None
            symbol = str(row.get("symbol") or row.get("business_key", "").split(":")[1])
            data = self.client.query_order(symbol, cid)
            return data if data.get("orderId") is not None else None
        except DemoApiError as exc:
            if exc.code == -2013:
                return None
            raise

    # ------------------------------------------------------------------ 持久化
    def _wait_and_persist(
        self,
        cid: str,
        symbol: str,
        side: str,
        plan: OrderPlan | None,
        response: dict[str, Any],
        qty_text: str,
        recovered: bool,
        *,
        purpose: str = "ENTRY",
        cycle_id: str | None = None,
    ) -> dict[str, Any]:
        data = dict(response)
        attempts = self._poll_attempts
        for _ in range(attempts):
            status = str(data.get("status") or "")
            if status in _TERMINAL:
                break
            if self._poll_interval_s:
                time.sleep(self._poll_interval_s)
            try:
                data = self.client.query_order(symbol, cid)
            except DemoApiError:
                break

        status = str(data.get("status") or ("UNKNOWN" if not recovered else "ACKNOWLEDGED")).upper()
        if status == "EXPIRED_IN_MATCH":
            status = "EXPIRED"
        filled = to_decimal(data.get("executedQty"), Decimal("0")) or Decimal("0")
        avg_price = to_decimal(data.get("avgPrice") or data.get("price"))
        if avg_price is not None and avg_price <= 0:
            avg_price = None
        confirmed = status in {"FILLED", "PARTIALLY_FILLED"} and avg_price is not None

        self._insert_order_record(
            cid,
            symbol=symbol,
            side=side,
            order_type="MARKET",
            qty=qty_text,
            status=status if status else "ACKNOWLEDGED",
            exchange_order_id=str(data.get("orderId") or "") or None,
            qty_filled=filled,
            avg_price=None if avg_price is None else dec_text(avg_price),
            raw=data,
            purpose=purpose,
            position_side=None if plan is None else plan.position_side,
            cycle_id=cycle_id,
        )
        if data.get("orderId") is not None:
            self._record_fills(symbol, data.get("orderId"), cid, side)
        if status in _TERMINAL:
            self.outbox.resolve(cid, status)

        terminal = status in _TERMINAL
        fill_proven = terminal and (filled == 0 or avg_price is not None)
        return {
            "status": status,
            "qty_filled": dec_text(filled),
            "avg_price": None if avg_price is None else dec_text(avg_price),
            "exchange_order_id": str(data.get("orderId") or "") or None,
            "fill_unconfirmed": not confirmed,
            "fill_proven": fill_proven,
            "pnl": _sum_realized_pnl(self.ledger, cid),
        }

    def _insert_order_record(
        self,
        cid: str,
        *,
        symbol: str,
        side: str,
        order_type: str,
        qty: Any,
        status: str,
        purpose: str = "ENTRY",
        exchange_order_id: str | None = None,
        qty_filled: Any = "0",
        avg_price: str | None = None,
        binance_code: int | None = None,
        error_msg: str | None = None,
        raw: Any = None,
        position_side: str | None = None,
        cycle_id: str | None = None,
    ) -> None:
        record = {
            "client_order_id": cid,
            "cycle_id": cycle_id,
            "exchange_order_id": exchange_order_id,
            "kind": "STANDARD",
            "purpose": purpose,
            "symbol": symbol.upper(),
            "side": side.upper(),
            "position_side": position_side,
            "order_type": order_type.upper(),
            "qty_requested": dec_text(qty, "0"),
            "qty_filled": dec_text(qty_filled, "0"),
            "avg_price": avg_price,
            "status": status,
            "binance_code": binance_code,
            "error_msg": error_msg,
            "raw_json": raw,
            "cycle_id": cycle_id,
        }
        existing = self.ledger.get_order(cid)
        if existing is None:
            try:
                self.ledger.insert_order(record)
                return
            except Exception:  # noqa: BLE001 - 并发重复：退化为更新
                existing = self.ledger.get_order(cid)
        payload = {
            "status": status,
            "qty_filled": record["qty_filled"],
            "avg_price": avg_price,
            "exchange_order_id": exchange_order_id,
            "binance_code": binance_code,
            "error_msg": error_msg,
            "raw_json": raw,
            "cycle_id": cycle_id,
        }
        if existing is not None:
            current = str(existing.get("status") or "").upper()
            new_status = str(status or "").upper()
            if current in _TERMINAL and new_status != current:
                # 终态不可回退：迟到的查询响应只更新成交量/价格等字段
                payload["status"] = current
            elif current and status_rank(new_status) < status_rank(current):
                payload["status"] = current
            self.ledger.update_order(cid, **payload)

    def _record_fills(self, symbol: str, order_id: Any, cid: str, side: str) -> None:
        try:
            rows = self.client.user_trades(symbol, order_id)
        except Exception:  # noqa: BLE001 - 成交流水失败不影响主流程
            return
        for row in rows:
            trade_id = row.get("id") or row.get("tradeId")
            if trade_id is None:
                continue
            self.ledger.insert_fill(
                {
                    "fill_id": str(trade_id),
                    "client_order_id": cid,
                    "symbol": symbol,
                    "side": str(row.get("side") or side).upper(),
                    "price": row.get("price") or 0,
                    "qty": row.get("qty") or 0,
                    "realized_pnl": row.get("realizedPnl"),
                    "commission": row.get("commission"),
                    "commission_asset": row.get("commissionAsset"),
                    "trade_time_ms": row.get("time") or 0,
                }
            )

    def _inherit_cycle_id(self, symbol: str) -> str | None:
        """保护单/平仓单继承开仓轮次：归因断链的根因是它们没带 cycle_id。

        规则：同一标的最近一笔带 cycle_id 的 ENTRY 订单就是它的来源轮次。
        查不到就返回 None（不猜、不编造轮次 ID）。
        """

        try:
            row = self.ledger.query_one(
                "SELECT cycle_id FROM orders WHERE symbol=? AND purpose='ENTRY' "
                "AND cycle_id IS NOT NULL AND cycle_id<>'' ORDER BY created_at DESC LIMIT 1",
                (symbol.upper(),),
            )
        except Exception:  # noqa: BLE001 - 归因是辅助信息，不能影响保护本身
            return None
        return str(row["cycle_id"]) if row and row.get("cycle_id") else None

    def _record_protection_orders(
        self,
        symbol: str,
        position: NormalizedPosition,
        result: Any,
        *,
        cycle_id: str | None = None,
    ) -> None:
        if not cycle_id:
            cycle_id = self._inherit_cycle_id(symbol)
        try:
            rows = self.protection.list_open(symbol)
        except Exception:  # noqa: BLE001 - 记录失败不影响保护本身
            return
        coverage = result.coverage
        for row in rows:
            if row["order_type"] not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
                continue
            cid = row.get("client_algo_id") or f"algo-{row.get('algo_id')}"
            qty_value = row.get("quantity")
            if qty_value in (None, "") or to_decimal(qty_value) in (None, Decimal("0")):
                qty_value = position.qty
            self._insert_order_record(
                cid,
                symbol=symbol,
                side=row.get("side") or "",
                order_type=row["order_type"],
                qty=qty_value,
                status="NEW",
                purpose="PROTECTION",
                exchange_order_id=row.get("algo_id"),
                raw=row,
                position_side=position.position_side,
                cycle_id=cycle_id,
            )
        del coverage

    def _refresh_positions_after_trade(
        self, symbol: str, protection_payload: dict[str, Any]
    ) -> None:
        snapshot = self.snapshot(force=True)
        status = protection_payload.get("status", "UNKNOWN")
        # M1.4e: keep the real triggers on the ledger row. The exchange holds
        # them, but without this the cockpit fallback and the post-trade review
        # report "no SL/TP" for a position that is in fact protected.
        coverage = protection_payload.get("coverage") or {}
        for position in snapshot.positions:
            if position.symbol != symbol:
                continue
            entry = snapshot.protection.get(position.symbol, {}) or {}
            coverage_status = (
                status
                if status in {"OK", "MISSING", "PARTIAL", "DUPLICATE", "UNKNOWN"}
                else entry.get("status", "UNKNOWN")
            )
            sl = coverage.get("sl") if coverage.get("sl") is not None else entry.get("sl")
            tp = coverage.get("tp") if coverage.get("tp") is not None else entry.get("tp")
            record = {
                **position.as_dict(),
                "ledger_id": self.ledger.ledger_id,
                "protection_status": coverage_status,
                "origin": "OPENED_BY_WORKBENCH",
            }
            if coverage_status == "OK":
                if sl is not None:
                    record["sl_price"] = sl
                if tp is not None:
                    record["tp_price"] = tp
            self.ledger.upsert_position(record)

    def _update_position_protection(
        self,
        symbol: str,
        status: str,
        *,
        sl: Any = None,
        tp: Any = None,
    ) -> None:
        """Update protection status and (M1.4e) persist the live SL/TP triggers.

        The exchange knows the real triggers, but the ledger row used to keep
        ``sl_price``/``tp_price`` NULL, so the cockpit fallback path and the
        post-trade review showed "no protection levels" for a position that was
        in fact protected.
        """

        rows = [row for row in self.ledger.list_positions() if row["symbol"] == symbol]
        for row in rows:
            self.ledger.upsert_position(
                {
                    **row,
                    "ledger_id": self.ledger.ledger_id,
                    "protection_status": status,
                    "sl_price": sl if sl is not None else row.get("sl_price"),
                    "tp_price": tp if tp is not None else row.get("tp_price"),
                }
            )


# ---------------------------------------------------------------------- helpers
ACTIVE_ORDER_STATUSES = frozenset(
    {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED"}
)


def _age_seconds(created_at: Any, now: float) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, float(now) - stamp.timestamp())


def _stable_command_id(prefix: str, *parts: Any) -> str:
    """稳定命令标识：同一仓位身份重复触发得到同一个 request_id（幂等边界）。"""

    raw = ":".join(str(part or "") for part in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _symbol_from_business_key(row: dict[str, Any] | None) -> str:
    key = str((row or {}).get("business_key") or "")
    parts = key.split(":")
    if len(parts) >= 2:
        return parts[1].upper()
    return str((row or {}).get("symbol") or "")


def _normalize_position(row: dict[str, Any]) -> NormalizedPosition | None:
    amount = to_decimal(row.get("positionAmt"), Decimal("0")) or Decimal("0")
    if abs(amount) <= Decimal("1e-12"):
        return None
    explicit = str(row.get("positionSide") or "").upper()
    side = explicit if explicit in {"LONG", "SHORT"} else ("LONG" if amount > 0 else "SHORT")
    entry = to_decimal(row.get("entryPrice"), Decimal("0")) or Decimal("0")
    mark = to_decimal(row.get("markPrice"))
    notional = to_decimal(row.get("notional"))
    leverage = _int_or_none(row.get("leverage"))
    margin = None
    for key in ("isolatedMargin", "isolatedWallet", "positionInitialMargin"):
        candidate = to_decimal(row.get(key))
        if candidate is not None and candidate > 0:
            margin = candidate
            break
    if margin is None and notional is not None and leverage:
        margin = abs(notional) / Decimal(max(1, leverage))
    return NormalizedPosition(
        symbol=str(row.get("symbol") or "").upper(),
        position_side=side,
        qty=abs(amount),
        entry_price=entry,
        mark_price=mark,
        upnl=to_decimal(row.get("unRealizedProfit")),
        leverage=leverage,
        margin=margin,
        margin_mode=str(row.get("marginType") or "").upper() or None,
        liq_price=to_decimal(row.get("liquidationPrice")),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sum_realized_pnl(ledger: Ledger, client_order_id: str) -> Decimal | None:
    rows = ledger.query(
        "SELECT realized_pnl FROM fills WHERE client_order_id=?", (client_order_id,)
    )
    if not rows:
        return None
    total = Decimal("0")
    for row in rows:
        value = to_decimal(row.get("realized_pnl"))
        if value is not None:
            total += value
    return total
