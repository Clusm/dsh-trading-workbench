"""保护单（Algo STOP_MARKET / TAKE_PROFIT_MARKET）管理。

Demo 实测（docs/03 D1/D2）：``STOP_MARKET`` / ``TAKE_PROFIT_MARKET`` 必须走
``POST /fapi/v1/algoOrder``（``algoType=CONDITIONAL``），普通 ``/fapi/v1/order``
会返回 ``-4120``；Algo 成功响应里的 ``code`` 是字符串 ``"200"``。

安全语义（docs/03 §2.4 I5/I6）：
- 查询失败（异常）返回 ``status=UNKNOWN``，**绝不**当成"没有保护"去重挂/平仓；
- 修复顺序永远"先挂新、后撤旧"：先补缺失的一侧，再清理重复；
- 只有**确证失败**（挂单被明确拒绝且重试仍失败）才让调用方紧急平仓。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ..ledger.numbers import to_decimal
from .client import DemoBinanceClient
from .errors import DemoApiError
from .outbox import OrderOutbox

__all__ = ["ProtectionCoverage", "ProtectionResult", "ProtectionManager"]

logger = logging.getLogger("workbench.protection")

STOP_MARKET = "STOP_MARKET"
TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


@dataclass(frozen=True, slots=True)
class ProtectionCoverage:
    symbol: str
    status: str  # OK | MISSING | PARTIAL | DUPLICATE | UNKNOWN
    stop_count: int = 0
    tp_count: int = 0
    sl: Decimal | None = None
    tp: Decimal | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stop_count": self.stop_count,
            "take_profit_count": self.tp_count,
            "sl": None if self.sl is None else str(self.sl),
            "tp": None if self.tp is None else str(self.tp),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ProtectionResult:
    ok: bool
    status: str
    coverage: ProtectionCoverage
    placed: tuple[str, ...] = ()
    failed_kind: str | None = None
    errors: tuple[str, ...] = ()
    canceled: tuple[str, ...] = field(default=())
    #: True 表示“已按交易所事实确认这一侧保护建不起来”（不是查询失败）。
    confirmed_failure: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "coverage": self.coverage.as_dict(),
            "placed": list(self.placed),
            "failed_kind": self.failed_kind,
            "errors": list(self.errors),
            "canceled": list(self.canceled),
            "confirmed_failure": self.confirmed_failure,
        }


@dataclass(frozen=True, slots=True)
class ProtectionFailurePolicy:
    """T03 §8·B3/B4：保护失败处置策略（有限回查 → 已配置的风险减少动作）。

    recheck_attempts  查询覆盖失败时的独立回查次数（1 表示只查一次）。
    on_failure        reduce_position = 系统仓位按风险减少处理（市价平仓）；
                      block_new_risk = 只阻断新风险并记录仓位未受保护的事实。
    """

    recheck_attempts: int = 2
    recheck_interval_s: float = 0.0
    on_failure: str = "reduce_position"

    def as_dict(self) -> dict[str, Any]:
        return {
            "recheck_attempts": self.recheck_attempts,
            "recheck_interval_s": self.recheck_interval_s,
            "on_failure": self.on_failure,
        }


class ProtectionManager:
    def __init__(
        self,
        client: DemoBinanceClient,
        outbox: OrderOutbox,
        *,
        policy: ProtectionFailurePolicy | None = None,
        clock=time.time,
    ) -> None:
        self.client = client
        self.outbox = outbox
        self.policy = policy or ProtectionFailurePolicy()
        self._clock = clock


    # ------------------------------------------------------------------ 查询
    def list_open(self, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = self.client.open_algo_orders()
        normalized = [self._normalize(row) for row in rows]
        if symbol:
            normalized = [row for row in normalized if row["symbol"] == symbol.upper()]
        return normalized

    @staticmethod
    def _normalize(row: dict[str, Any]) -> dict[str, Any]:
        trigger = row.get("triggerPrice", row.get("stopPrice", row.get("price")))
        return {
            "algo_id": str(row.get("algoId") or row.get("orderId") or ""),
            "client_algo_id": str(row.get("clientAlgoId") or row.get("clientOrderId") or ""),
            "symbol": str(row.get("symbol") or "").upper(),
            "side": str(row.get("side") or "").upper(),
            "position_side": str(row.get("positionSide") or "").upper() or None,
            "order_type": str(row.get("orderType") or row.get("type") or "").upper(),
            "trigger_price": to_decimal(trigger),
            "quantity": to_decimal(row.get("quantity") or row.get("origQty")),
            "close_position": _truthy(row.get("closePosition")),
            "status": str(row.get("algoStatus") or row.get("status") or "").upper(),
        }

    def coverage(
        self, symbol: str, *, position_side: str | None = None
    ) -> ProtectionCoverage:
        symbol = symbol.upper()
        try:
            rows = self.list_open(symbol)
        except DemoApiError as exc:
            return ProtectionCoverage(symbol, "UNKNOWN", error=f"{exc.kind}: {exc.message}")
        except Exception as exc:  # noqa: BLE001 - 任何查询异常都按未证明处理
            return ProtectionCoverage(symbol, "UNKNOWN", error=f"{type(exc).__name__}: {exc}")
        return coverage_from_rows(symbol, rows, position_side=position_side)

    # ------------------------------------------------------------------ 挂单
    def place(
        self,
        *,
        symbol: str,
        close_side: str,
        kind: str,
        trigger: str,
        client_algo_id: str,
        position_side: str | None = None,
        close_position: bool = True,
        qty: str | None = None,
        position_mode: str | None = None,
    ) -> dict[str, Any]:
        kind = kind.upper()
        if kind not in {STOP_MARKET, TAKE_PROFIT_MARKET}:
            raise ValueError(f"unsupported protection kind: {kind}")
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "clientAlgoId": client_algo_id,
            "side": close_side.upper(),
            "type": kind,
            "triggerPrice": trigger,
            "algoType": "CONDITIONAL",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "timeInForce": "GTC",
        }
        # M1.4d: `positionSide` is only legal on a HEDGE (dual-side) account.
        # In one-way mode Binance answers -4061 "Order's position side does not
        # match user's setting", which failed every protection placement and
        # forced an emergency close of each freshly opened position.
        hedge = str(position_mode or "").upper() == "HEDGE"
        if close_position:
            params["closePosition"] = "true"
            if hedge and position_side:
                params["positionSide"] = position_side.upper()
        else:
            if not qty:
                raise ValueError("quantity is required when close_position is false")
            params.update({"quantity": qty, "reduceOnly": "true"})
            if hedge and position_side:
                params["positionSide"] = position_side.upper()
        self.outbox.prepare(
            client_order_id=client_algo_id,
            purpose="PROTECTION",
            symbol=symbol,
            side=close_side.upper(),
            position_side=position_side,
            order_type=kind,
            endpoint="/fapi/v1/algoOrder",
            request=params,
            qty=qty or "0",
            price=trigger,
            reduce_only=not close_position,
            close_position=close_position,
        )
        try:
            response = self.client.submit_algo_order(params)
        except DemoApiError as exc:
            # A definitive rejection must not leave a SUBMITTING outbox row behind:
            # `can_open_new()` would then block every later entry (R17).
            if exc.definitely_rejected:
                self.outbox.reject(client_algo_id, exc)
            else:
                self.outbox.mark_unknown(client_algo_id, exc)
            raise
        self.outbox.acknowledge(client_algo_id, response)
        return response

    def cancel(self, algo_id: str) -> bool:
        try:
            self.client.cancel_algo_order(algo_id)
            return True
        except DemoApiError as exc:
            if exc.code == -2011 or "Unknown order" in str(exc.message):
                return True
            logger.warning("cancel_algo_failed algo_id=%s error=%s", algo_id, exc.message)
            return False

    # ------------------------------------------------------------------ 保证覆盖
    def _coverage_with_recheck(
        self, symbol: str, *, position_side: str | None, attempts: int | None = None
    ) -> ProtectionCoverage:
        """有限次独立回查覆盖；查询失败保持 UNKNOWN（不当成“没有保护”）。"""

        tries = max(1, int(self.policy.recheck_attempts if attempts is None else attempts))
        coverage = self.coverage(symbol, position_side=position_side)
        for _ in range(tries - 1):
            if coverage.status != "UNKNOWN":
                return coverage
            if self.policy.recheck_interval_s > 0:
                time.sleep(self.policy.recheck_interval_s)
            coverage = self.coverage(symbol, position_side=position_side)
        return coverage

    @staticmethod
    def _covers(coverage: ProtectionCoverage, kind: str) -> bool:
        if kind == STOP_MARKET:
            return coverage.stop_count > 0
        return coverage.tp_count > 0

    def _place_verified(
        self,
        *,
        symbol: str,
        close_side: str,
        kind: str,
        trigger: str,
        position_side: str,
        position_mode: str | None,
        retry: int,
    ) -> dict[str, Any]:
        """挂一侧保护，并在每次失败后先按交易所事实核对，再决定是否重发。

        - clientAlgoId 由 symbol+kind+trigger 确定性派生：同一意图重试保留原标识，
          不会因为“换了个 attempt 后缀”在同一侧挂出第二张单；
        - 回执丢失/超时后绝不盲目重发，先查 openAlgoOrders 证明这一侧是否已存在。
        """

        client_algo_id = f"wbp-{kind[:2].lower()}-{_short_id(symbol, trigger)}"
        attempts = 1 + max(0, int(retry))
        last_error = ""
        missing_proven = False
        for attempt in range(attempts):
            try:
                self.place(
                    symbol=symbol,
                    close_side=close_side,
                    kind=kind,
                    trigger=trigger,
                    client_algo_id=client_algo_id,
                    position_side=position_side,
                    position_mode=position_mode,
                )
                return {"ok": True, "proven_by": "receipt", "attempts": attempt + 1}
            except DemoApiError as exc:
                last_error = f"{kind}: {exc.kind}: {exc.message}"
            except Exception as exc:  # noqa: BLE001 - 未分类异常按未证明处理
                last_error = f"{kind}: {type(exc).__name__}: {exc}"
            coverage = self.coverage(symbol, position_side=position_side)
            if self._covers(coverage, kind):
                # 回执丢了但交易所确认已存在：按查询事实收敛 outbox，不重复补挂。
                self.outbox.acknowledge(client_algo_id, {"status": "NEW"})
                return {"ok": True, "proven_by": "query", "attempts": attempt + 1}
            if coverage.status == "UNKNOWN":
                return {
                    "ok": False,
                    "error": last_error or "coverage_unproven",
                    "confirmed_failure": False,
                    "attempts": attempt + 1,
                }
            missing_proven = True
        return {
            "ok": False,
            "error": last_error or "place_failed",
            "confirmed_failure": missing_proven,
            "attempts": attempts,
        }

    def ensure(
        self,
        *,
        symbol: str,
        position_side: str,
        sl_trigger: str,
        tp_trigger: str,
        retry: int = 1,
        position_mode: str | None = None,
        policy: ProtectionFailurePolicy | None = None,
        verify_triggers: bool = True,
    ) -> ProtectionResult:
        active_policy = policy or self.policy
        desired_sl = to_decimal(sl_trigger) or Decimal("0")
        desired_tp = to_decimal(tp_trigger) or Decimal("0")
        close_side = "SELL" if position_side.upper() == "LONG" else "BUY"

        coverage = self._coverage_with_recheck(
            symbol, position_side=position_side, attempts=active_policy.recheck_attempts
        )
        if coverage.status == "UNKNOWN":
            return ProtectionResult(
                False, "UNKNOWN", coverage, errors=(coverage.error or "coverage_unproven",)
            )

        placed: list[str] = []
        errors: list[str] = []
        missing: list[str] = []
        if coverage.stop_count == 0:
            missing.append(STOP_MARKET)
        if coverage.tp_count == 0:
            missing.append(TAKE_PROFIT_MARKET)

        for kind in missing:
            trigger = sl_trigger if kind == STOP_MARKET else tp_trigger
            outcome = self._place_verified(
                symbol=symbol,
                close_side=close_side,
                kind=kind,
                trigger=trigger,
                position_side=position_side,
                position_mode=position_mode,
                retry=retry,
            )
            if outcome["ok"]:
                placed.append(kind)
                continue
            errors.append(str(outcome.get("error") or "place_failed"))
            coverage = self._coverage_with_recheck(
                symbol, position_side=position_side, attempts=active_policy.recheck_attempts
            )
            return ProtectionResult(
                False,
                "PROTECTION_FAILED",
                coverage,
                placed=tuple(placed),
                failed_kind=kind,
                errors=tuple(errors),
                confirmed_failure=bool(outcome.get("confirmed_failure")),
            )

        coverage = self._coverage_with_recheck(symbol, position_side=position_side)
        canceled: list[str] = []
        if coverage.stop_count > 1 or coverage.tp_count > 1:
            # 补挂前已经查过覆盖；这里只清理历史遗留的重复同向保护单。
            canceled = self._dedupe(symbol, desired_sl, desired_tp)
            coverage = self._coverage_with_recheck(symbol, position_side=position_side)

        if verify_triggers:
            ok = (
                coverage.status == "OK"
                and coverage.sl is not None
                and coverage.tp is not None
                and abs(coverage.sl - desired_sl) <= _tolerance(desired_sl)
                and abs(coverage.tp - desired_tp) <= _tolerance(desired_tp)
            )
        else:
            # 修复/监督周期只判“两侧保护是否都在”；已有 SL/TP 的具体价格属于
            # 建仓计划，不能用兜底百分比价格去比较后误判成保护失败。
            ok = coverage.status == "OK"
        if ok:
            return ProtectionResult(
                True, "OK", coverage, placed=tuple(placed), canceled=tuple(canceled)
            )
        status = coverage.status if coverage.status != "OK" else "MISMATCH"
        return ProtectionResult(
            False,
            status,
            coverage,
            placed=tuple(placed),
            failed_kind=None if coverage.status == "OK" else status,
            errors=tuple(errors),
            canceled=tuple(canceled),
            # 覆盖查询成功但价格/单数不符：这是已确认的覆盖失败。
            confirmed_failure=coverage.status in {"MISSING", "PARTIAL", "DUPLICATE", "MISMATCH"},
        )


    def _dedupe(self, symbol: str, desired_sl: Decimal, desired_tp: Decimal) -> list[str]:
        """保留最接近目标价的一 STOP/一 TP，撤销其余（仅在已有新保护后调用）。"""

        canceled: list[str] = []
        try:
            rows = self.list_open(symbol)
        except Exception:  # noqa: BLE001 - 查询失败不撤单
            return canceled
        for order_type, desired in ((STOP_MARKET, desired_sl), (TAKE_PROFIT_MARKET, desired_tp)):
            matching = [row for row in rows if row["order_type"] == order_type]
            if len(matching) <= 1:
                continue
            matching.sort(
                key=lambda row: abs((row["trigger_price"] or Decimal("0")) - desired)
            )
            for row in matching[1:]:
                algo_id = row.get("algo_id") or ""
                if algo_id and self.cancel(algo_id):
                    canceled.append(algo_id)
        return canceled


def _short_id(symbol: str, trigger: str) -> str:
    """确定性保护单标识：同一 symbol/类型/触发价的重试复用同一个 clientAlgoId。"""

    import hashlib

    raw = f"{symbol}:{trigger}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def coverage_from_rows(
    symbol: str,
    rows: list[dict[str, Any]],
    *,
    position_side: str | None = None,
) -> ProtectionCoverage:
    """由已归一化的 Algo 单列表推导**单个 symbol**的覆盖状态。

    - 先按 ``symbol`` 过滤：多 symbol 快照下绝不能拿 ETH 的 STOP/TP 判 BTC 为 OK；
    - HEDGE 账户再按 ``positionSide`` 过滤（``closePosition=true`` 的整仓保护
      也必须落在同一 positionSide 上）；one-way 的区域通常为空/``BOTH``，不过滤。
    """

    symbol = symbol.upper()
    expected_side = (position_side or "").upper()
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("symbol") or "").upper() != symbol:
            continue
        row_side = str(row.get("position_side") or "").upper()
        if expected_side in {"LONG", "SHORT"} and row_side in {"LONG", "SHORT"}:
            if row_side != expected_side:
                continue
        filtered.append(row)

    stops = [row for row in filtered if row.get("order_type") == STOP_MARKET]
    takes = [row for row in filtered if row.get("order_type") == TAKE_PROFIT_MARKET]
    stop_count, tp_count = len(stops), len(takes)
    sl = stops[0].get("trigger_price") if stops else None
    tp = takes[0].get("trigger_price") if takes else None
    if stop_count == 1 and tp_count == 1:
        return ProtectionCoverage(symbol, "OK", stop_count, tp_count, sl, tp)
    if stop_count == 0 and tp_count == 0:
        return ProtectionCoverage(symbol, "MISSING", 0, 0)
    if stop_count == 1 or tp_count == 1:
        return ProtectionCoverage(symbol, "PARTIAL", stop_count, tp_count, sl, tp)
    return ProtectionCoverage(symbol, "DUPLICATE", stop_count, tp_count, sl, tp)


def _tolerance(price: Decimal) -> Decimal:
    """价格比较容差：0.05%（交易所 tick 与格式化误差）。"""

    return max(abs(price) * Decimal("0.0005"), Decimal("0.00000001"))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
