import sys, tempfile, time, unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench_service.market.service import MarketService, UnknownSymbol
from workbench_service.market.config import MarketSettings
from workbench_service.market.transport import MarketUpstreamError

class Transport:
    def __init__(self): self.calls=[]; self.fail=False; self.offset=0
    def get_json(self, path, params, **kwargs):
        self.calls.append((path, params))
        if self.fail: raise MarketUpstreamError(503, 'UNAVAILABLE', 'offline')
        return {'markPrice':'100', 'time': int(time.time()*1000)+self.offset}

class MarksTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.transport=Transport()
        self.service=MarketService(MarketSettings(base_url='https://demo-fapi.binance.com', data_dir=Path(self.directory.name)),transport=self.transport)
    def tearDown(self): self.directory.cleanup()
    def test_coalesces_readers_and_never_fetches_depth(self):
        with ThreadPoolExecutor(8) as pool:
            rows=list(pool.map(lambda _:self.service.mark_snapshot(['BTCUSDT','ETHUSDT']),range(8)))
        self.assertEqual(len(self.transport.calls),2)
        self.assertTrue(all(p[0]=='/fapi/v1/premiumIndex' for p in self.transport.calls))
        self.assertFalse(rows[0]['prices']['BTCUSDT']['stale'])
    def test_failure_retains_evidence_time_and_backs_off(self):
        before=self.service.mark_snapshot(['BTCUSDT'])['prices']['BTCUSDT']
        self.transport.fail=True
        with patch('workbench_service.market.service.C.PRICE_TTL_S_CACHE',0):
            after=self.service.mark_snapshot(['BTCUSDT'])['prices']['BTCUSDT']
            self.service.mark_snapshot(['BTCUSDT'])
        self.assertTrue(after['stale'])
        self.assertEqual(after['as_of_ms'],before['as_of_ms'])
        self.assertEqual(after['mark'],before['mark'])
        self.assertEqual(len(self.transport.calls),2)
    def test_first_failure_unknown_and_unsupported_symbol(self):
        self.transport.fail=True
        q=self.service.mark_snapshot(['BTCUSDT'])['prices']['BTCUSDT']
        self.assertIsNone(q['mark']); self.assertTrue(q['stale'])
        with self.assertRaises(UnknownSymbol): self.service.mark_snapshot(['EVIL'])
        self.assertEqual(len(self.transport.calls),1)
    def test_older_and_future_quotes_do_not_replace_evidence(self):
        first=self.service.mark_snapshot(['BTCUSDT'])['prices']['BTCUSDT']
        self.transport.offset=-60000
        with patch('workbench_service.market.service.C.PRICE_TTL_S_CACHE',0):
            q=self.service.mark_snapshot(['BTCUSDT'])['prices']['BTCUSDT']
        self.assertEqual(q['as_of_ms'],first['as_of_ms']); self.assertTrue(q['stale'])
        self.service._mark_retry.clear(); self.transport.offset=60000
        with patch('workbench_service.market.service.C.PRICE_TTL_S_CACHE',0):
            q=self.service.mark_snapshot(['BTCUSDT'])['prices']['BTCUSDT']
        self.assertEqual(q['as_of_ms'],first['as_of_ms'])

if __name__=='__main__': unittest.main()
