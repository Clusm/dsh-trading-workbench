"""L03 离线回归：Binance 端点登记、动态成本和用途优先级。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.binance_request_policy import (  # noqa: E402
    UnknownBinanceEndpoint,
    policy_for,
    registered_endpoints,
)
from data import rate_limit  # noqa: E402
from data.binance_transport import request_json  # noqa: E402


def main() -> None:
    assert ("GET", "/fapi/v1/userTrades") in registered_endpoints()
    assert policy_for("GET", "/fapi/v1/ticker/24hr", {}, purpose="market_scan").ip_weight == 40
    assert policy_for("GET", "/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"}).ip_weight == 1
    assert policy_for("GET", "/fapi/v1/klines", {"limit": 600}).ip_weight == 5
    assert policy_for("GET", "/futures/data/takerlongshortRatio",
                      purpose="intel").endpoint_window_costs == {"futures_data_5m": 1}
    protect = policy_for("POST", "/fapi/v1/algoOrder", purpose="protect")
    assert protect.priority == "critical" and protect.order_costs["order_count_10s"] == 1
    assert policy_for("GET", "/fapi/v1/openOrders", purpose="order_reconcile").priority == "critical"
    try:
        policy_for("GET", "/fapi/v1/not_registered")
    except UnknownBinanceEndpoint:
        pass
    else:
        raise AssertionError("unknown endpoint must fail closed")

    class Client:
        calls = 0

        def get(self, *_: object, **__: object) -> object:
            self.calls += 1
            raise AssertionError("coordination failure must stop before HTTP")

    with tempfile.TemporaryDirectory(prefix="mta-policy-") as temp_dir:
        bad_state = Path(temp_dir) / "bad.json"
        bad_state.write_text("{broken", encoding="utf-8")
        rate_limit._reset_for_test()
        rate_limit.configure_rate_limit_state(bad_state)
        assert rate_limit.rate_limit_status()["coordination_available"] is False
        try:
            request_json(Client(), "/fapi/v1/time", priority="background")
        except rate_limit.RateLimitCoordinationUnavailable:
            pass
        else:
            raise AssertionError("ordinary request must fail closed when coordination is broken")
    rate_limit._reset_for_test()
    print(f"PASS request policy: registered={len(registered_endpoints())}")


if __name__ == "__main__":
    main()
