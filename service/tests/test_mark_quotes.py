import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench_service.market.config import MarketSettings
from workbench_service.market.service import MarketService, UnknownSymbol
from workbench_service.market.transport import MarketUpstreamError


class Transport:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.offset = 0
        self.batch_mode = None  # None | "list" | "dict_symbol"

    def get_json(self, path, params, **kwargs):
        self.calls.append((path, params))
        if self.fail:
            raise MarketUpstreamError(503, "UNAVAILABLE", "offline")
        ts = int(time.time() * 1000) + self.offset
        if not params and self.batch_mode == "list":
            return [
                {
                    "symbol": sym,
                    "markPrice": str(100 + idx),
                    "lastFundingRate": "0.0002",
                    "time": ts,
                }
                for idx, sym in enumerate(
                    ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT")
                )
            ]
        if not params and self.batch_mode == "dict_symbol":
            return {"symbol": "BTCUSDT", "markPrice": "101", "time": ts}
        sym = (params or {}).get("symbol")
        base = 200 if sym == "ETHUSDT" else 100
        return {"symbol": sym, "markPrice": str(base), "lastFundingRate": "0.0001", "time": ts}


class MarksTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.transport = Transport()
        self.service = MarketService(
            MarketSettings(
                base_url="https://demo-fapi.binance.com",
                data_dir=Path(self.directory.name),
            ),
            transport=self.transport,
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_coalesces_readers_and_never_fetches_depth(self):
        with ThreadPoolExecutor(8) as pool:
            rows = list(
                pool.map(
                    lambda _: self.service.mark_snapshot(["BTCUSDT", "ETHUSDT"]),
                    range(8),
                )
            )
        self.assertEqual(len(self.transport.calls), 2)
        self.assertTrue(all(p[0] == "/fapi/v1/premiumIndex" for p in self.transport.calls))
        btc = rows[0]["prices"]["BTCUSDT"]
        self.assertFalse(btc["stale"])
        self.assertIsNotNone(btc["age_ms"])
        self.assertGreaterEqual(btc["age_ms"], 0)

    def test_failure_retains_evidence_time_and_backs_off(self):
        before = self.service.mark_snapshot(["BTCUSDT"])["prices"]["BTCUSDT"]
        self.transport.fail = True
        with patch("workbench_service.market.service.C.PRICE_TTL_S_CACHE", 0):
            after = self.service.mark_snapshot(["BTCUSDT"])["prices"]["BTCUSDT"]
            self.service.mark_snapshot(["BTCUSDT"])
        self.assertTrue(after["stale"])
        self.assertEqual(after["as_of_ms"], before["as_of_ms"])
        self.assertEqual(after["mark"], before["mark"])
        self.assertEqual(len(self.transport.calls), 2)

    def test_first_failure_unknown_and_unsupported_symbol(self):
        self.transport.fail = True
        q = self.service.mark_snapshot(["BTCUSDT"])["prices"]["BTCUSDT"]
        self.assertIsNone(q["mark"])
        self.assertIsNone(q["age_ms"])
        self.assertTrue(q["stale"])
        with self.assertRaises(UnknownSymbol):
            self.service.mark_snapshot(["EVIL"])
        self.assertEqual(len(self.transport.calls), 1)

    def test_older_and_future_quotes_do_not_replace_evidence(self):
        first = self.service.mark_snapshot(["BTCUSDT"])["prices"]["BTCUSDT"]
        self.transport.offset = -60000
        with patch("workbench_service.market.service.C.PRICE_TTL_S_CACHE", 0):
            q = self.service.mark_snapshot(["BTCUSDT"])["prices"]["BTCUSDT"]
        self.assertEqual(q["as_of_ms"], first["as_of_ms"])
        self.assertTrue(q["stale"])
        self.service._mark_retry.clear()
        self.transport.offset = 60000
        with patch("workbench_service.market.service.C.PRICE_TTL_S_CACHE", 0):
            q = self.service.mark_snapshot(["BTCUSDT"])["prices"]["BTCUSDT"]
        self.assertEqual(q["as_of_ms"], first["as_of_ms"])

    def test_calibrated_server_clock_prevents_drift_false_positive(self):
        # Simulate local PC clock lagging server by 8s: server_time_ms is +8000ms ahead.
        local_ms = int(time.time() * 1000)
        server_ms = local_ms + 8_000
        self.service._clock = {
            "server_time_ms": server_ms,
            "local_time_ms": local_ms,
            "offset_ms": 8_000,
            "rtt_ms": 10,
            "clock_trusted": False,
        }
        self.service._clock_mono = time.monotonic()
        # Upstream quote timestamp is server_ms - 400ms (+7600ms ahead of raw local time!)
        self.transport.offset = 7_600
        snap = self.service.mark_snapshot(["BTCUSDT"])
        btc = snap["prices"]["BTCUSDT"]
        self.assertFalse(btc["stale"])
        self.assertIsNone(btc["error"])
        self.assertIsNotNone(btc["age_ms"])
        self.assertLessEqual(btc["age_ms"], 2000)

    def test_batch_fetch_for_five_or_more_symbols_and_dict_fallback(self):
        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT"]
        self.transport.batch_mode = "list"
        snap = self.service.mark_snapshot(symbols)
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0], ("/fapi/v1/premiumIndex", {}))
        self.assertEqual(snap["prices"]["BTCUSDT"]["mark"], 100.0)
        self.assertEqual(snap["prices"]["BNBUSDT"]["mark"], 105.0)
        self.assertEqual(snap["prices"]["BTCUSDT"]["funding_rate"], 0.0002)
        self.assertTrue(all(not snap["prices"][s]["stale"] for s in symbols))

        # Dict fallback when fake transport returns a single-symbol dict on empty params:
        self.service._marks.clear()
        self.transport.calls.clear()
        self.transport.batch_mode = "dict_symbol"
        snap2 = self.service.mark_snapshot(symbols)
        # 1 batch attempt (populates BTCUSDT) + 5 individual fallbacks = 6 calls
        self.assertEqual(len(self.transport.calls), 6)
        self.assertEqual(snap2["prices"]["BTCUSDT"]["mark"], 101.0)
        self.assertEqual(snap2["prices"]["ETHUSDT"]["mark"], 200.0)


if __name__ == "__main__":
    unittest.main()
