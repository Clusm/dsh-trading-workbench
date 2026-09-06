"""Binance USD-M私有流事件的无副作用标准化。"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any


SUPPORTED_USER_EVENTS = {
    "ORDER_TRADE_UPDATE",
    "ACCOUNT_UPDATE",
    "MARGIN_CALL",
    "ACCOUNT_CONFIG_UPDATE",
    "listenKeyExpired",
}


@dataclass(frozen=True)
class ExchangeEvent:
    """标准化后的不可变交易所事件，保留原始 payload 供审计与重放。"""
    event_key: str
    event_type: str
    event_time_ms: int | None
    transaction_time_ms: int | None
    source: str
    symbol: str | None
    client_order_id: str | None
    exchange_order_id: str | None
    trade_id: str | None
    payload: dict[str, Any]
    supported: bool


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_user_event(raw: dict[str, Any], *, source: str = "user_stream") -> ExchangeEvent:
    """解析组合流或原始私有流帧，并在落库前移除listenKey。"""
    if not isinstance(raw, dict):
        raise TypeError("用户流事件必须是JSON object")
    body = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    payload = copy.deepcopy(body)
    if "listenKey" in payload:
        payload["listenKey"] = "<redacted>"

    event_type = str(payload.get("e") or "UNKNOWN")
    order = payload.get("o") if isinstance(payload.get("o"), dict) else {}
    account = payload.get("a") if isinstance(payload.get("a"), dict) else {}
    positions = account.get("P") if isinstance(account.get("P"), list) else []
    margin_positions = payload.get("p") if isinstance(payload.get("p"), list) else []

    symbol = _text(order.get("s"))
    if symbol is None and positions and isinstance(positions[0], dict):
        symbol = _text(positions[0].get("s"))
    if symbol is None and margin_positions and isinstance(margin_positions[0], dict):
        symbol = _text(margin_positions[0].get("s"))

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    event_time = _integer(payload.get("E"))
    event_key = f"{event_type}:{event_time or 0}:{digest}"
    return ExchangeEvent(
        event_key=event_key,
        event_type=event_type,
        event_time_ms=event_time,
        transaction_time_ms=_integer(payload.get("T")),
        source=str(source or "user_stream"),
        symbol=symbol.upper() if symbol else None,
        client_order_id=_text(order.get("c")),
        exchange_order_id=_text(order.get("i")),
        trade_id=_text(order.get("t")),
        payload=payload,
        supported=event_type in SUPPORTED_USER_EVENTS,
    )
