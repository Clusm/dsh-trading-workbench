"""Shared signal ticket validation and stable entry identity."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any

from core.strategy_funnel import (
    DETAIL_ENTRY_ZONE_INVERTED,
    DETAIL_TICKET_EXPIRED,
    DETAIL_TICKET_SHAPE_INVALID,
)

#: 允许的周期集合（判定与展示共用同一处，避免两处口径漂移）。
VALID_TIMEFRAMES: frozenset[str] = frozenset({"30m", "1h", "2h", "4h", "8h", "1d"})

#: ``ticket_skip_detail`` 的三类形状/时效细分码（登记表在 U08 漏斗模块里）。
TICKET_DETAIL_SHAPE_INVALID = DETAIL_TICKET_SHAPE_INVALID
TICKET_DETAIL_EXPIRED = DETAIL_TICKET_EXPIRED
TICKET_DETAIL_ENTRY_ZONE_INVERTED = DETAIL_ENTRY_ZONE_INVERTED


def ticket_order_id(ticket_id: str) -> str:
    return "mta-t-" + hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:24]


def ticket_skip_detail(ticket: Any, now: datetime | None = None) -> str | None:
    """返回这张 ticket **为什么不可用**（U08 细分码）；可用时返回 ``None``。

    这是 :func:`valid_signal_ticket` 的**唯一**判定实现：旧的 ``bool`` 接口保留为
    它的薄包装，因此扫描器（发射侧）、执行器（消费侧）与漏斗（解释侧）看到的是
    同一份真值表，不会出现"扫描说有效、执行说过期"两套口径。

    判定顺序：过期 → 形状 → 入场区几何。原因码只用于**解释**，**不**改变真假
    （``detail is None`` 与 ``valid_signal_ticket(...) is True`` 逐例等价）。
    """
    try:
        expires = datetime.fromisoformat(str(ticket["expires_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return TICKET_DETAIL_SHAPE_INVALID
    if expires.tzinfo is None or expires <= (now or datetime.now(timezone.utc)):
        return TICKET_DETAIL_EXPIRED
    try:
        if any(not isinstance(ticket.get(key), str) or not ticket[key].strip()
               for key in ("ticket_id", "signal_id", "strategy_id", "symbol", "timeframe")):
            return TICKET_DETAIL_SHAPE_INVALID
        if ticket["timeframe"] not in VALID_TIMEFRAMES:
            return TICKET_DETAIL_SHAPE_INVALID
        if ticket.get("side") not in {"LONG", "SHORT"}:
            return TICKET_DETAIL_SHAPE_INVALID
        for key in ("signal_price", "entry_zone_low", "entry_zone_high",
                    "stop_loss_pct", "take_profit_pct", "time_stop_hours"):
            value = float(ticket[key])
            if not math.isfinite(value) or value <= 0:
                return TICKET_DETAIL_SHAPE_INVALID
        if not float(ticket["entry_zone_low"]) <= float(ticket["entry_zone_high"]):
            return TICKET_DETAIL_ENTRY_ZONE_INVERTED
    except (KeyError, TypeError, ValueError, OverflowError):
        return TICKET_DETAIL_SHAPE_INVALID
    return None


def valid_signal_ticket(ticket: dict[str, Any], now: datetime | None = None) -> bool:
    """``ticket_skip_detail`` 的薄包装（同一份真值表，不是第二套判定）。"""
    return ticket_skip_detail(ticket, now) is None
