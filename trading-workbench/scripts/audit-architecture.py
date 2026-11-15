"""Read-only architecture audit reproductions: temporary ledgers and fake exchange only.

Assertions confirm an observed weakness; PASS does not mean the weakness is fixed.
"""
import json
import sys
import threading
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'service' / 'tests_execution'))
from support import ServiceCase, approve


def emit(name, **facts):
    print(json.dumps({'finding': name, **facts}, ensure_ascii=False), flush=True)


def pause_race():
    case = ServiceCase(with_fake=True)
    try:
        with case.client() as client:
            approved = approve(client, cycle_id='arch-pause-race')
            service = case.service
            checked, release = threading.Event(), threading.Event()
            original = service.risk.check
            outcome = {}

            def gate(*args, **kwargs):
                verdict = original(*args, **kwargs)
                if verdict.approved:
                    checked.set()
                    if not release.wait(8):
                        raise RuntimeError('audit barrier timeout')
                return verdict

            service.risk.check = gate

            def submit():
                try:
                    outcome['result'] = service.submit({'approval_token': approved['approval']['token']})
                except Exception as exc:
                    outcome['error'] = type(exc).__name__ + ': ' + str(exc)

            thread = threading.Thread(target=submit, daemon=True)
            thread.start()
            assert checked.wait(8), 'P2 did not reach an approved gate'
            orders_before = len(case.fake.orders)
            service.control({'state': 'pause', 'confirm': 'PAUSE', 'reason': 'offline architecture audit'})
            release.set()
            thread.join(12)
            assert not thread.is_alive(), 'audit worker did not stop'
            assert 'result' in outcome, outcome
            orders_after = len(case.fake.orders)
            assert orders_after > orders_before, outcome
            assert service.state.control().state == 'pause'
            emit('pause_commit_race', control='pause', fake_orders_before=orders_before,
                 fake_orders_after=orders_after, entry_status=outcome['result'].get('status'))
    finally:
        case.close()


def fee_sign():
    case = ServiceCase(with_fake=False)
    try:
        case.service.ledger.recent_close_fills = Mock(return_value=[
            {'client_order_id': 'close-fee-proof', 'realized_pnl': '-0.01',
             'commission': '0.10', 'commission_asset': 'USDT', 'trade_time_ms': 1000},
        ])
        actual = case.service._merged_close_results()[0][1]
        expected = Decimal('-0.01') - Decimal('0.10')
        assert actual == Decimal('0.09') and expected == Decimal('-0.11')
        emit('loss_streak_fee_sign', computed_net=str(actual), expected_net=str(expected),
             losing_close_classified_as_win=actual >= 0)
    finally:
        case.close()


def stale_reconcile_audit():
    case = ServiceCase(with_fake=False)
    try:
        case.service.reconcile = Mock(return_value={'exchange_reachable': False, 'stale': True,
                                                    'protection_ok': True, 'positions': 1})
        result = case.service.operator_command('reconcile', {'request_id': 'arch-stale-reconcile'})
        row = case.service.ledger.operations()[0]
        assert row['status'] == 'succeeded' and result['exchange_reachable'] is False
        emit('stale_reconcile_audit', exchange_reachable=False, stale=True, audit_status=row['status'])
    finally:
        case.close()


if __name__ == '__main__':
    pause_race()
    fee_sign()
    stale_reconcile_audit()
