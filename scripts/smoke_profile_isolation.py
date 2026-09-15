"""双账户状态、模式及零策略资金的离线隔离回归。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.config_loader import Settings  # noqa: E402
from core.control import ControlState  # noqa: E402
from core.instance_lock import InstanceLock  # noqa: E402
from exchange.factory import create_broker  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        raw = {"channels": {"strategy_weight": 0.6, "ai_weight": 0.4},
               "profiles": {"live": {"channels": {"strategy_weight": 0, "ai_weight": 1}}}}
        demo = Settings(root, raw, {}, {}, "demo")
        live = Settings(root, raw, {}, {}, "live")
        demo.ensure_dirs()
        live.ensure_dirs()
        assert demo.db_file == root / "data/agent_trader.sqlite"
        assert live.db_file == root / "data/live/agent_trader.sqlite"
        assert demo.rate_limit_state == live.rate_limit_state
        assert demo.console_port == 8000 and live.console_port == 8001
        assert demo.raw_settings["channels"]["ai_weight"] == 0.4
        assert live.raw_settings["channels"]["ai_weight"] == 1
        for state in ("capital_state.json", "channel_state.json", "futures_source_ledger.json",
                      "release_manifest.json", "account_risk_state.json", "cache/klines"):
            assert demo.runtime_path("data/" + state) != live.runtime_path("data/" + state)
        left = ControlState(demo.runtime_path("data/control_state.json"))
        right = ControlState(live.runtime_path("data/control_state.json"))
        left.set_state("close_only")
        assert right.state == "normal"
        right.set_state("emergency")
        assert left.state == "close_only"
        locks = [InstanceLock(s.runtime_path("data/trading_service.lock")) for s in (demo, live)]
        for lock in locks:
            lock.acquire()
        try:
            duplicate = InstanceLock(demo.runtime_path("data/trading_service.lock"))
            try:
                duplicate.acquire()
            except RuntimeError:
                pass
            else:
                raise AssertionError("同一 profile 未拒绝重复启动")
        finally:
            for lock in locks:
                lock.release()
        for invalid in ("../escape.json", "data/../escape.json", "data/live/capital_state.json"):
            try:
                live.runtime_path(invalid)
            except ValueError:
                pass
            else:
                raise AssertionError(invalid)
        with patch.dict(os.environ, {"EXECUTION_MODE": "demo", "BINANCE_LIVE_API_KEY": "fake",
                                     "BINANCE_LIVE_API_SECRET": "fake"}), \
                patch("exchange.factory.BinanceFuturesBroker") as broker:
            create_broker(live, MagicMock())
            args = broker.call_args.kwargs
            assert args["environment"] == "live"
            assert args["base_url"] == "https://fapi.binance.com"
            assert args["ledger_file"] == live.runtime_path("data/futures_source_ledger.json")
        live.raw_settings["execution"] = {"allow_paper_fallback": True}
        with patch.dict(os.environ, {"BINANCE_LIVE_API_KEY": "", "BINANCE_LIVE_API_SECRET": ""}):
            try:
                create_broker(live, MagicMock())
            except RuntimeError:
                pass
            else:
                raise AssertionError("显式实盘不能回退 paper")
    print("PROFILE ISOLATION OK")


if __name__ == "__main__":
    main()
