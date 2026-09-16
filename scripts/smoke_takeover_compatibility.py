"""离线验证实盘接管闸门；不加载 dotenv、不连接 Binance。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.service import TradingService  # noqa: E402
from core.release_manifest import build_release_manifest  # noqa: E402
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


class _CompatibleLiveBroker:
    environment = "live"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def takeover_compatibility(self) -> dict[str, object]:
        self.calls.append("compatibility")
        return {"ok": True, "position_mode_supported": True, "reason": ""}

    def sync_positions(self) -> None:
        self.calls.append("sync_positions")

    def sync_protections(self) -> None:
        self.calls.append("sync_protections")

    def protection_coverage(self) -> dict[str, object]:
        self.calls.append("protection_coverage")
        return {"ok": True}


class _MarginCap:
    @staticmethod
    def margin_cap_status() -> dict[str, object]:
        return {
            "evaluated": True,
            "breached": True,
            "equity": 6.45,
            "total_margin": 8.36,
            "limit": 6.13,
            "max_margin_usage": 0.95,
            "reason": "存量总保证金 8.36U 超过风险档上限 6.13U",
        }


service2 = object.__new__(TradingService)
service2.broker = _CompatibleLiveBroker()
service2.capital = _MarginCap()
service2.db = _Db()
service2.release_manifest = {"egress_id": "test-egress"}
service2._startup_safety = {}
blocked2 = TradingService._verify_startup_safety(service2)
assert blocked2["ready"] is False
assert "超过风险档上限" in str(blocked2["reason"])
assert service2.broker.calls == [
    "compatibility", "sync_positions", "sync_protections", "protection_coverage",
]
assert blocked2["takeover"]["margin_cap"]["breached"] is True
assert service2.db.events == ["startup_takeover_block"]

# live 可继续同步持仓与保护，但出口身份未知时必须阻止新增仓位，避免把不同公网出口的
# 418/429 观察证据混在同一个发布批次里。
service3 = object.__new__(TradingService)
service3.broker = _CompatibleLiveBroker()
service3.capital = _MarginCap()
service3.db = _Db()
service3.release_manifest = {"egress_id": "unknown"}
service3._startup_safety = {}
blocked3 = TradingService._verify_startup_safety(service3)
assert blocked3["ready"] is False
assert "MTA_EGRESS_ID" in str(blocked3["reason"])
assert service3.broker.calls == [
    "compatibility", "sync_positions", "sync_protections", "protection_coverage",
]
assert service3.db.events == ["startup_takeover_block"]


class _CycleCapital:
    @staticmethod
    def session_equity() -> float:
        return 6.21


class _CycleChannels:
    def __init__(self) -> None:
        self.read_only_values: list[bool] = []

    def sync(self, _prices: dict[str, float], *, session_equity: float,
             read_only: bool) -> dict[str, object]:
        assert session_equity == 6.21
        self.read_only_values.append(read_only)
        return {"read_only": read_only}


# 接管阻断期只读计算通道状态，不能触发 close_source；核验恢复后保持原自动风控行为。
service4 = object.__new__(TradingService)
service4.capital = _CycleCapital()
service4.channels = _CycleChannels()
blocked_channels = TradingService._sync_channels_for_cycle(
    service4, {"BTCUSDT": 50000.0}, {"ready": False})
ready_channels = TradingService._sync_channels_for_cycle(
    service4, {"BTCUSDT": 50000.0}, {"ready": True})
assert blocked_channels["read_only"] is True
assert ready_channels["read_only"] is False
assert service4.channels.read_only_values == [True, False]

# 出口身份属于网络稳定性观察窗口的一部分；更换出口必须自动生成新发布批次，
# 不能把两个公网出口的 418/429 证据拼接在同一 release_id 下。
with tempfile.TemporaryDirectory(prefix="mta-release-egress-") as tmp:
    release_root = Path(tmp)
    first_release = build_release_manifest(
        release_root, environment="live", egress_id="162.248.75.128")
    second_release = build_release_manifest(
        release_root, environment="live", egress_id="203.0.113.8")
assert first_release.release_id != second_release.release_id

print("SMOKE TAKEOVER COMPATIBILITY PASS")
