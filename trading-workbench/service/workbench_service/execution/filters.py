"""合约过滤器解析与价格/数量取整。

规则（docs/03 D5/D6、§5.2 步 4）：
- 数量 **向下** 截断到 ``stepSize``（避免 reduceOnly 数量被舍大触发 -2022）；
- 价格按方向安全取整：LONG 的 SL 向下、TP 向上；SHORT 的 SL 向上、TP 向下
  —— 取整只会让触发更难/更晚，不会把保护价推成"立即触发"。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from ..ledger.numbers import ceil_to_step, floor_to_step, format_with_step, to_decimal
from ..risk.models import SymbolFiltersView

__all__ = [
    "SymbolFilters",
    "parse_exchange_info",
    "load_exchange_info",
    "format_qty",
    "format_price",
    "stop_trigger",
    "take_profit_trigger",
]


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str
    status: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    price_precision: int | None = None
    quantity_precision: int | None = None
    exchange_max_leverage: int | None = None
    #: 该 symbol 的维持保证金率（取所有已发布档位的最大值，保守估计）。
    #: ``None`` 表示没有取得逐 symbol 档位 —— R11 必须 fail-closed，不能假设 1%。
    maint_margin_ratio: Decimal | None = None
    #: ``(initialLeverage, maintMarginRatio)`` 档位，按 initialLeverage 升序。
    #:
    #: 不能用 ``maint_margin_ratio``（全档最大值）直接判 R11：Binance 的
    #: leverageBracket 里 1x 档的维持保证金率通常是 50%，把它用到 3x 仓位上会让
    #: ``1/lev − mmr`` 恒为负数，任何 lev≥2 的计划都会被 R11 拒绝。R11 关心的是
    #: 该仓位所在档位的 mmr，因此按目标杠杆选“允许该杠杆的最小档位”。
    mmr_tiers: tuple[tuple[int, Decimal], ...] = ()

    def mmr_for_leverage(self, leverage: int | None) -> Decimal | None:
        """返回允许 ``leverage`` 的最小档位的维持保证金率。"""

        if leverage is None or leverage <= 0:
            return self.maint_margin_ratio
        for tier_leverage, ratio in self.mmr_tiers:
            if tier_leverage >= leverage:
                return ratio
        # 杠杆高于所有已发布档位：按最保守值 fail-closed（调用方通常会先被 R13 拒）。
        return self.maint_margin_ratio

    def to_view(self) -> SymbolFiltersView:
        return SymbolFiltersView(
            symbol=self.symbol,
            step_size=self.step_size,
            tick_size=self.tick_size,
            min_qty=self.min_qty,
            min_notional=self.min_notional,
            max_qty=self.max_qty,
            exchange_max_leverage=self.exchange_max_leverage,
            status=self.status,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "tick_size": str(self.tick_size),
            "step_size": str(self.step_size),
            "min_qty": str(self.min_qty),
            "max_qty": str(self.max_qty),
            "min_notional": str(self.min_notional),
            "price_precision": self.price_precision,
            "quantity_precision": self.quantity_precision,
            "exchange_max_leverage": self.exchange_max_leverage,
        }


def _filters_by_type(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, dict) and item.get("filterType"):
            result[str(item["filterType"])] = item
    return result


def parse_exchange_info(payload: Any) -> dict[str, SymbolFilters]:
    """解析 ``/fapi/v1/exchangeInfo``；只保留 USDT 永续（其余忽略）。"""

    data = payload if isinstance(payload, dict) else {}
    symbols = data.get("symbols")
    if not isinstance(symbols, list):
        return {}
    parsed: dict[str, SymbolFilters] = {}
    for row in symbols:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if not symbol.endswith("USDT"):
            continue
        filters = _filters_by_type(row.get("filters"))
        price_filter = filters.get("PRICE_FILTER") or {}
        lot_size = filters.get("LOT_SIZE") or {}
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        min_notional = filters.get("MIN_NOTIONAL") or {}
        tick_size = to_decimal(price_filter.get("tickSize"), Decimal("0.01")) or Decimal("0.01")
        step_size = to_decimal(lot_size.get("stepSize"), Decimal("0.001")) or Decimal("0.001")
        min_qty = to_decimal(lot_size.get("minQty"), Decimal("0")) or Decimal("0")
        max_qty = (
            to_decimal(market_lot.get("maxQty"))
            or to_decimal(lot_size.get("maxQty"))
            or Decimal("5000")
        )
        min_notional_value = to_decimal(min_notional.get("notional"), Decimal("5")) or Decimal("5")
        parsed[symbol] = SymbolFilters(
            symbol=symbol,
            status=str(row.get("status") or "UNKNOWN").upper(),
            tick_size=tick_size,
            step_size=step_size,
            min_qty=min_qty,
            max_qty=max_qty,
            min_notional=min_notional_value,
            price_precision=_int_or_none(row.get("pricePrecision")),
            quantity_precision=_int_or_none(row.get("quantityPrecision")),
        )
    return parsed


def load_exchange_info(client: Any) -> dict[str, SymbolFilters]:
    """从客户端拉取过滤器 + 杠杆档位。

    档位（leverageBracket）失败时仍返回 exchangeInfo 过滤器，但
    ``maint_margin_ratio`` 保持 ``None``，由 R11 fail-closed（不得用 1% 兜底）。
    """

    payload = client.exchange_info()
    filters = parse_exchange_info(payload)
    try:
        brackets = parse_leverage_brackets(client.leverage_bracket())
    except Exception:  # noqa: BLE001 - 档位失败不阻断启动，R11 用保守 MMR
        brackets = {}
    if not brackets:
        return filters
    merged: dict[str, SymbolFilters] = {}
    for symbol, item in filters.items():
        bracket = brackets.get(symbol)
        if bracket is None:
            merged[symbol] = item
            continue
        merged[symbol] = replace(
            item,
            exchange_max_leverage=bracket.get("max_leverage") or item.exchange_max_leverage,
            maint_margin_ratio=bracket.get("mmr"),
            mmr_tiers=tuple(bracket.get("tiers") or ()),
        )
    return merged


def parse_leverage_brackets(payload: Any) -> dict[str, dict[str, Any]]:
    """解析 ``/fapi/v1/leverageBracket``：最大杠杆 + 最保守维持保证金率。"""

    rows = payload if isinstance(payload, list) else []
    parsed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        brackets = row.get("brackets")
        if not symbol or not isinstance(brackets, list):
            continue
        leverages: list[int] = []
        ratios: list[Decimal] = []
        tiers: list[tuple[int, Decimal]] = []
        for bracket in brackets:
            if not isinstance(bracket, dict):
                continue
            leverage = _int_or_none(bracket.get("initialLeverage"))
            ratio = to_decimal(bracket.get("maintMarginRatio"))
            if leverage and leverage > 0:
                leverages.append(leverage)
                if ratio is not None and ratio > 0:
                    tiers.append((leverage, ratio))
            if ratio is not None and ratio > 0:
                ratios.append(ratio)
        if not leverages:
            continue
        parsed[symbol] = {
            "max_leverage": max(leverages),
            "mmr": max(ratios) if ratios else None,
            "tiers": tuple(sorted(tiers, key=lambda item: item[0])),
        }
    return parsed


# ---------------------------------------------------------------------- 取整
def format_qty(qty: Decimal, step: Decimal) -> str:
    return format_with_step(floor_to_step(qty, step), step)


def format_price(price: Decimal, tick: Decimal, *, rounding: str = "floor") -> str:
    if rounding == "ceil":
        return format_with_step(ceil_to_step(price, tick), tick)
    return format_with_step(floor_to_step(price, tick), tick)


def stop_trigger(entry: Decimal, sl: Decimal, side: str, tick: Decimal) -> str:
    """止损触发价：LONG 向下、SHORT 向上。"""

    del entry
    return format_price(sl, tick, rounding="floor" if side == "LONG" else "ceil")


def take_profit_trigger(entry: Decimal, tp: Decimal, side: str, tick: Decimal) -> str:
    """止盈触发价：LONG 向上、SHORT 向下。"""

    del entry
    return format_price(tp, tick, rounding="ceil" if side == "LONG" else "floor")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
