"""Shared signal ticket validation and stable entry identity."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any


def ticket_order_id(ticket_id: str) -> str:
    return "mta-t-" + hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:24]


def valid_signal_ticket(ticket: dict[str, Any], now: datetime | None = None) -> bool:
    try:
        expires = datetime.fromisoformat(str(ticket["expires_at"]).replace("Z", "+00:00"))
        if expires.tzinfo is None or expires <= (now or datetime.now(timezone.utc)):
            return False
        if any(not isinstance(ticket.get(key), str) or not ticket[key].strip()
               for key in ("ticket_id", "signal_id", "strategy_id", "symbol", "timeframe")):
            return False
        if ticket["timeframe"] not in {"30m", "1h", "2h", "4h", "8h", "1d"}:
            return False
        if ticket.get("side") not in {"LONG", "SHORT"}:
            return False
        for key in ("signal_price", "entry_zone_low", "entry_zone_high",
                    "stop_loss_pct", "take_profit_pct", "time_stop_hours"):
            value = float(ticket[key])
            if not math.isfinite(value) or value <= 0:
                return False
        return float(ticket["entry_zone_low"]) <= float(ticket["entry_zone_high"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
