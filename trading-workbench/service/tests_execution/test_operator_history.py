"""Operator history and portfolio arithmetic, isolated temporary ledgers only."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ServiceCase
from workbench_service.ledger.store import Ledger
from workbench_service.ledger.schema import LEDGER_SCHEMA_VERSION
from workbench_service.errors import ServiceError


class OperatorHistoryTests(unittest.TestCase):
    def setUp(self):
        self.case = ServiceCase(with_fake=False)
        self.client = self.case.client()
        self.client.__enter__()
        self.service = self.case.service

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_control_is_persisted_and_duplicate_id_never_reexecutes(self):
        body = dict(state="pause", confirm="PAUSE", reason="人工暂停", request_id="req-pause-test")
        response = self.client.post('/api/control', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['operation_status'], 'succeeded')
        duplicate = self.client.post('/api/control', json={**body, 'state': 'normal', 'confirm': 'NORMAL'})
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(self.service.state.control().state, 'pause')
        self.service.ledger.close()
        reopened = Ledger(self.case.data_dir)
        try:
            rows = reopened.operations()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['request_id'], body['request_id'])
            self.assertEqual(rows[0]['status'], 'succeeded')
        finally:
            reopened.close()

    def test_wrong_confirmation_does_not_change_state_and_records_failure(self):
        response = self.client.post('/api/control', json=dict(state='emergency', confirm='yes', reason='测试', request_id='req-bad-confirm'))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.service.state.control().state, 'normal')
        self.assertEqual(self.service.ledger.operations()[0]['status'], 'failed')

    def test_resume_respects_latched_hard_risk(self):
        self.service.state.sync(__import__('decimal').Decimal('5000'), equity_available=True)
        row = self.service.ledger.load_risk_state()
        row['daily_loss_latched'] = 1
        self.service.ledger.save_risk_state(row)
        self.service.state.set_control('pause', 'test')
        response = self.client.post('/api/control', json=dict(state='normal', confirm='NORMAL', reason='恢复', request_id='req-locked-resume'))
        self.assertEqual(response.status_code, 423)
        self.assertEqual(self.service.state.control().state, 'pause')
        self.assertEqual(self.service.ledger.operations()[0]['error_code'], 'E_CONTROL_LOCKED')

    def test_emergency_without_proven_flatten_is_partial(self):
        response = self.client.post('/api/control', json=dict(state='emergency', confirm='EMERGENCY', reason='测试', request_id='req-partial-emergency'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['control']['state'], 'emergency')
        self.assertEqual(response.json()['operation_status'], 'partial')
        self.assertEqual(self.service.ledger.operations()[0]['status'], 'partial')

    def test_uncertain_close_is_not_retried_and_secrets_are_redacted(self):
        self.service.close = Mock(side_effect=TimeoutError('unknown remote result'))
        body = dict(symbol='BTCUSDT', request_id='req-timeout-close', reason='token=secret-value')
        with self.assertRaises(TimeoutError):
            self.service.operator_command('close', body)
        with self.assertRaises(ServiceError):
            self.service.operator_command('close', body)
        self.assertEqual(self.service.close.call_count, 1)
        row = self.service.ledger.operations()[0]
        self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('secret-value', row['reason'])

    def test_history_get_does_not_execute_any_command(self):
        self.service.close = Mock()
        response = self.client.get('/api/operations')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['operations'], [])
        self.service.close.assert_not_called()

    def test_fill_unconfirmed_is_unknown_in_history(self):
        self.service.close = Mock(side_effect=ServiceError(202, 'E_FILL_UNCONFIRMED', 'exchange result unknown'))
        with self.assertRaises(ServiceError):
            self.service.operator_command('close', {'symbol': 'BTCUSDT', 'request_id': 'req-unconfirmed'})
        self.assertEqual(self.service.ledger.operations()[0]['status'], 'unknown')

    def test_partial_repair_records_per_symbol_receipt(self):
        self.service.repair = Mock(return_value={'ok': False, 'repaired': [{'symbol': 'BTCUSDT', 'status': 'UNKNOWN', 'ok': False}]})
        result = self.service.operator_command('repair', {'request_id': 'req-partial-repair'})
        self.assertEqual(result['operation_status'], 'partial')
        row = self.service.ledger.operations()[0]
        self.assertEqual(row['status'], 'partial')
        self.assertEqual(row['summary']['outcomes'][0]['symbol'], 'BTCUSDT')

    def test_legacy_commission_schema_migrates_without_losing_fills(self):
        ledger = self.service.ledger
        ledger.insert_fill(dict(fill_id='legacy', client_order_id='local', symbol='BTCUSDT', side='SELL', price='100', qty='1', realized_pnl='1', commission='0.01'))
        ledger.execute('ALTER TABLE fills DROP COLUMN commission_asset')
        ledger.close()
        rows = ledger.list_fills()
        self.assertEqual(rows[0]['fill_id'], 'legacy')
        self.assertIsNone(rows[0]['commission_asset'])
        # 版本号随 LEDGER_SCHEMA_VERSION 前进（v3 加了资金流水/回填游标）；
        # 该用例的真实目的是“旧 fills 行不丢”，不要在这里写死历史版本号。
        self.assertEqual(ledger.schema_version(), LEDGER_SCHEMA_VERSION)

    def test_shanghai_day_uses_all_fills_and_decimal_fees(self):
        now = datetime(2026, 11, 11, 1, tzinfo=timezone.utc)
        start = int(datetime(2026, 11, 10, 16, tzinfo=timezone.utc).timestamp() * 1000)
        ledger = self.service.ledger
        for index in range(601):
            ledger.insert_fill(dict(fill_id=f'f{index}', client_order_id='local', symbol='BTCUSDT', side='SELL', price='100', qty='1', realized_pnl='0.1', commission='0.01', commission_asset='USDT', trade_time_ms=start + index))
        ledger.insert_fill(dict(fill_id='outside', client_order_id='local', symbol='BTCUSDT', side='SELL', price='100', qty='1', realized_pnl='999', commission='0', commission_asset='USDT', trade_time_ms=start - 1))
        ledger.insert_equity_snapshot(dict(ts_bucket='2026-11-10T16:03:00Z', equity_exchange='5000', equity_session='5000'))
        p = ledger.local_day_performance(now=now)
        self.assertEqual(p['date'], '2026-11-11')
        self.assertEqual(p['fill_count'], 601)
        self.assertEqual(p['realized_pnl'], '60.1')
        self.assertEqual(p['commissions'], {'USDT': '6.01'})
        self.assertEqual(p['net_realized_pnl'], '54.09')
        self.assertEqual(p['baseline_at'], '2026-11-10T16:03:00Z')
        self.assertIsNone(p['net_pnl'])

    def test_mixed_or_missing_fee_assets_never_fabricate_usdt_net(self):
        now = datetime.now(timezone.utc)
        ledger = self.service.ledger
        for index, asset in enumerate(['BNB', None]):
            ledger.insert_fill(dict(fill_id=f'mixed{index}', client_order_id='local', symbol='BTCUSDT', side='SELL', price='100', qty='1', realized_pnl='1', commission='0.01', commission_asset=asset, trade_time_ms=int(now.timestamp()*1000)))
        p = ledger.local_day_performance(now=now)
        self.assertEqual(p['commissions'], {'BNB': '0.01'})
        self.assertEqual(p['unknown_commission_count'], 1)
        self.assertIsNone(p['net_realized_pnl'])


if __name__ == '__main__':
    unittest.main()
