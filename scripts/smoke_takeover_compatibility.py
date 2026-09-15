"""离线验证实盘接管闸门；不加载 dotenv、不连接 Binance。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.service import TradingService  # noqa: E402
from exchange.binance_futures import (  # noqa: E402
    BinanceFuturesBroker,
    assess_takeover_compatibility,
)


one_way = assess_takeover_compatibility(
    {"dualSidePosition": False},
    [{"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "0.01"}],
    [],
    [{"symbol": "BTCUSDT", "reduceOnly": True, "orderType": "STOP_MARKET"}],
)
assert one_way["ok"] is True
assert one_way["position_mode"] == "one_way"

hedge = assess_takeover_compatibility(
    {"dualSidePosition": True},
    [
        {"symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.01"},
        {"symbol": "BTCUSDT", "positionSide": "SHORT", "positionAmt": "-0.02"},
    ], [], [],
)
assert hedge["ok"] is False
assert hedge["position_mode_supported"] is False

hedge_single_side = assess_takeover_compatibility(
    {"dualSidePosition": True},
    [
        {"symbol": "ZECUSDT", "positionSide": "SHORT", "positionAmt": "-0.012"},
        {"symbol": "TRUMPUSDT", "positionSide": "LONG", "positionAmt": "50.63"},
    ], [], [],
)
assert hedge_single_side["ok"] is True
assert hedge_single_side["position_mode"] == "hedge"

external_entry = assess_takeover_compatibility(
    {"dualSidePosition": "false"}, [],
    [{"symbol": "ETHUSDT", "side": "BUY", "type": "LIMIT",
      "reduceOnly": False, "closePosition": False, "clientOrderId": "external-1"}],
    [],
)
assert external_entry["ok"] is False
assert len(external_entry["unmanaged_entry_orders"]) == 1

local_and_exit = assess_takeover_compatibility(
    {"dualSidePosition": False}, [],
    [
        {"symbol": "ETHUSDT", "reduceOnly": False, "clientOrderId": "mta-local"},
        {"symbol": "BTCUSDT", "reduceOnly": "true", "clientOrderId": "external-exit"},
    ],
    [{"symbol": "SOLUSDT", "closePosition": "true", "clientAlgoId": "external-stop"}],
    local_entry_client_ids={"mta-local"},
)
assert local_and_exit["ok"] is True

# 官方 Hedge Mode 语义：普通订单必须带 positionSide 且不得带 reduceOnly；
# 条件保护单使用 closePosition=true，不发送 quantity/reduceOnly。
broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
broker._hedge_mode = True
broker.environment = "live"
broker.db = None
broker.order_outbox = None
broker._format_price = lambda _symbol, value: str(value)
broker._log_order = lambda **_kwargs: None
captured: list[dict[str, object]] = []
broker._submit_algo_order = lambda params, *, purpose: (
    captured.append(dict(params)) or {"algoId": 1}
)
mode_params = broker._standard_order_mode_params("SHORT", reduce=True)
assert mode_params == {"positionSide": "SHORT"}
broker._place_algo_protection(
    "TRUMPUSDT", "SELL", "50.63", "STOP_MARKET", 1.9, "sl", "STOP")
assert captured[0]["positionSide"] == "LONG"
assert captured[0]["closePosition"] == "true"
assert "quantity" not in captured[0]
assert "reduceOnly" not in captured[0]


class _Db:
    def __init__(self) -> None:
        self.events: list[str] = []

    def log_risk_event_once_while_open(self, *, event_type: str, **_: object) -> None:
        self.events.append(event_type)


class _HedgeBroker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def takeover_compatibility(self) -> dict[str, object]:
        self.calls.append("compatibility")
        return {"ok": False, "position_mode_supported": False,
                "reason": "当前执行器尚不支持 Hedge Mode 实盘接管"}

    def sync_positions(self) -> None:
        self.calls.append("sync_positions")

    def sync_protections(self) -> None:
        self.calls.append("sync_protections")


service = object.__new__(TradingService)
service.broker = _HedgeBroker()
service.db = _Db()
service._startup_safety = {}
blocked = TradingService._verify_startup_safety(service)
assert blocked["ready"] is False
assert service.broker.calls == ["compatibility"], service.broker.calls
assert service.db.events == ["startup_takeover_block"]

print("SMOKE TAKEOVER COMPATIBILITY PASS")
