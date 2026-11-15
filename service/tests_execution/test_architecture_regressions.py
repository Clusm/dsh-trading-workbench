"""Regression tests for the 2026-11-11 architecture audit P0/P1 items.

These replace the old "confirm the defect" audit assertions with assertions that
the defect cannot happen. All tests use a temporary ledger and fake exchange;
no real credentials, network order, or live control state is touched.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServiceCase, approve, evaluate, submit  # noqa: E402

from workbench_service.errors import ServiceError  # noqa: E402
from workbench_service.execution.lease import ExecutionLeaseStore, LeaseError  # noqa: E402


class ArchitectureRegressionTests(unittest.TestCase):
    def test_cross_process_lease_is_exclusive_and_generation_fenced(self) -> None:
        """A01/A02: two Service instances share SQLite, not memory."""
        case = ServiceCase(with_fake=True)
        try:
            now = [1_700_000_000.0]
            first = ExecutionLeaseStore(case.service.ledger, clock=lambda: now[0])
            second = ExecutionLeaseStore(case.service.ledger, clock=lambda: now[0])
            lease = first.acquire("run-A", owner="host-A", ttl_s=10)
            self.assertEqual(first.validate(lease["lease_id"])["run_id"], "run-A")
            with self.assertRaises(LeaseError) as held:
                second.acquire("run-B", owner="host-B", ttl_s=10)
            self.assertEqual(held.exception.code, "E_LEASE_HELD")
            self.assertFalse(second.revoke_run("other-run"))
            self.assertTrue(second.revoke_run("run-A"))
            second.revoke_all("operator-pause")
            with self.assertRaises(LeaseError) as revoked:
                first.validate(lease["lease_id"])
            self.assertIn(revoked.exception.code, {"E_LEASE_REVOKED", "E_LEASE_STALE"})
            now[0] += 20
            with self.assertRaises(LeaseError) as stale:
                first.validate(lease["lease_id"])
            self.assertIn(stale.exception.code, {"E_LEASE_EXPIRED", "E_LEASE_STALE", "E_LEASE_REVOKED"})
        finally:
            case.close()

    def test_lease_api_is_visible_to_two_http_callers(self) -> None:
        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                first = client.post(
                    "/api/execution/lease/acquire",
                    json={"run_id": "http-run-A", "owner": "host-A"},
                )
                self.assertEqual(first.status_code, 200, first.text)
                lease = first.json()
                second = client.post(
                    "/api/execution/lease/acquire",
                    json={"run_id": "http-run-B", "owner": "host-B"},
                )
                self.assertEqual(second.status_code, 409, second.text)
                self.assertEqual(second.json()["error"]["code"], "E_LEASE_HELD")
                released = client.post(
                    "/api/execution/lease/release",
                    json={"lease_id": lease["lease_id"]},
                )
                self.assertEqual(released.status_code, 200, released.text)
        finally:
            case.close()

    def test_revoked_lease_cannot_reach_p2(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            lease = service.leases.acquire("run-revoked", owner="host")
            service.control({"state": "pause", "confirm": "PAUSE", "reason": "revoke test"})
            with self.assertRaises(ServiceError) as raised:
                service.submit({"approval_token": "x", "lease_id": lease["lease_id"]})
            self.assertIn(raised.exception.code, {"E_LEASE_REVOKED", "E_LEASE_STALE"})
            self.assertEqual(len(case.fake.orders), 0)
        finally:
            case.close()

    def test_require_lease_rejects_unleased_p2(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            service.settings = dataclasses.replace(
                service.settings, require_execution_lease=True
            )
            with self.assertRaises(ServiceError) as raised:
                service.submit({"approval_token": "not-a-token"})
            self.assertEqual(raised.exception.code, "E_LEASE_REQUIRED")
            self.assertEqual(len(case.fake.orders), 0)
        finally:
            case.close()

    def test_pause_cannot_cross_an_approved_p2_submission(self) -> None:
        """A01: pause and P2 share one account mutation boundary."""
        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                approved = approve(client, cycle_id="a01-pause-lock")
                service = case.service
                checked, release = threading.Event(), threading.Event()
                outcome: dict[str, object] = {}
                original = service.risk.check

                def gate(*args, **kwargs):
                    verdict = original(*args, **kwargs)
                    if verdict.approved:
                        checked.set()
                        if not release.wait(8):
                            raise RuntimeError("A01 test barrier timeout")
                    return verdict

                service.risk.check = gate

                def submit():
                    try:
                        outcome["result"] = service.submit(
                            {"approval_token": approved["approval"]["token"]}
                        )
                    except Exception as exc:  # noqa: BLE001 - asserted below
                        outcome["error"] = f"{type(exc).__name__}: {exc}"

                submit_thread = threading.Thread(target=submit, daemon=True)
                submit_thread.start()
                self.assertTrue(checked.wait(8), "P2 never reached an approved gate")
                before = len(case.fake.orders)

                pause_thread = threading.Thread(
                    target=lambda: service.control(
                        {"state": "pause", "confirm": "PAUSE", "reason": "A01 regression"}
                    ),
                    daemon=True,
                )
                pause_thread.start()
                time.sleep(0.2)
                self.assertTrue(
                    pause_thread.is_alive(),
                    "control returned while P2 was between approval and submission",
                )

                release.set()
                submit_thread.join(8)
                pause_thread.join(8)
                self.assertFalse(submit_thread.is_alive(), "P2 thread did not settle")
                self.assertFalse(pause_thread.is_alive(), "control thread did not settle")
                self.assertIn("result", outcome, str(outcome))
                self.assertEqual(len(case.fake.orders), before + 1)
                self.assertEqual(service.state.control().state, "pause")
                after_pause = len(case.fake.orders)
                time.sleep(0.1)
                self.assertEqual(len(case.fake.orders), after_pause, "order appeared after pause returned")

                # A fresh approval under pause must be rejected without an order.
                rejected = evaluate(client, cycle_id="a01-pause-recheck")
                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertEqual(rejected.json()["error"]["code"], "E_CONTROL_LOCKED")
                self.assertEqual(len(case.fake.orders), after_pause)
        finally:
            case.close()

    def test_stale_reconcile_is_not_recorded_as_succeeded(self) -> None:
        """A08: empty diffs + stale venue must not become a success receipt."""
        case = ServiceCase(with_fake=False)
        try:
            service = case.service
            service.reconcile = mock.Mock(
                return_value={
                    "exchange_reachable": False,
                    "stale": True,
                    "adopted": [],
                    "closed_external": [],
                    "protection_missing": [],
                    "outbox": {"checked": 2, "resolved": 0},
                    "protection_ok": True,
                }
            )
            result = service.operator_command(
                "reconcile", {"request_id": "a08-stale-reconcile"}
            )
            row = service.ledger.operations()[0]
            self.assertNotEqual(row["status"], "succeeded")
            self.assertEqual(result["operation_status"], "partial")
            self.assertEqual(row["summary"]["exchange_reachable"], False)
            self.assertEqual(row["summary"]["unresolved_queries"], 2)
        finally:
            case.close()

    def test_reconcile_backfills_an_external_protection_fill_idempotently(self) -> None:
        """A03: a fill only visible on the exchange reaches the local ledger."""
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            case.fake.trades[999] = [
                {
                    "id": 9001,
                    "orderId": 999,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "price": "68100.0",
                    "qty": "0.01",
                    "realizedPnl": "5.00",
                    "commission": "0.05",
                    "commissionAsset": "USDT",
                    "time": 1_700_000_000_000,
                }
            ]
            first = service.broker.sync_fills(["BTCUSDT"])
            self.assertEqual(first["inserted"], 1)
            second = service.broker.sync_fills(["BTCUSDT"])
            self.assertEqual(second["inserted"], 0, "fill_id must be idempotent")
            self.assertEqual(second["cursors"]["BTCUSDT"], 9001)
            self.assertEqual(service.ledger.get_meta("fill_cursor:BTCUSDT"), "9001")
            fills = service.ledger.list_fills(limit=10, symbol="BTCUSDT")
            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0]["client_order_id"], "external-999")
            order = service.ledger.get_order("external-999")
            self.assertIsNotNone(order)
            self.assertEqual(order["purpose"], "EXIT")
            closes = service.ledger.recent_close_fills(limit=10)
            self.assertEqual(len(closes), 1)
            self.assertEqual(Decimal(closes[0]["realized_pnl"]), Decimal("5.00"))
        finally:
            case.close()

    def test_fill_sync_uses_configured_symbols_when_no_position_exists(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            service.broker.fill_symbols = ["SOLUSDT"]
            service.broker.client.user_trades = mock.Mock(return_value=[])
            result = service.broker.sync_fills()
            self.assertEqual(result["symbols"], 1)
            service.broker.client.user_trades.assert_called_with(
                "SOLUSDT", from_id=None, limit=1000
            )
        finally:
            case.close()

    def test_supervisor_survives_a_failed_cycle(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            service.reconcile = mock.Mock(side_effect=RuntimeError("supervisor cycle failed"))
            self.assertTrue(service.start_monitor(interval_s=0.05))
            time.sleep(0.2)
            self.assertIsNotNone(service._monitor_thread)
            self.assertTrue(service._monitor_thread.is_alive())
            service.stop_monitor()
            self.assertIsNone(service._monitor_thread)
        finally:
            case.close()

    def test_time_stop_is_opt_in_and_closes_only_expired_positions(self) -> None:
        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                approved = approve(client, cycle_id="time-stop-open")
                response = submit(client, approved)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertTrue(case.fake.positions)
                service = case.service
                service._enforce_time_stop()
                self.assertTrue(case.fake.positions, "default hours=0 must not close")
                service.settings = dataclasses.replace(
                    service.settings, auto_time_stop_hours=1.0
                )
                service.ledger.execute(
                    "UPDATE positions SET opened_at=?",
                    ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),),
                )
                service._enforce_time_stop()
                self.assertEqual(case.fake.positions, {})
        finally:
            case.close()

    def test_fill_sync_paginates_and_recovers_a_stale_cursor(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            first_page = [
                {
                    "id": i, "orderId": 10_000 + i, "symbol": "BTCUSDT", "side": "SELL",
                    "price": "68000", "qty": "0.001", "realizedPnl": "0.1",
                    "commission": "0.001", "commissionAsset": "USDT", "time": i,
                }
                for i in range(1, 1001)
            ]
            second_page = [{
                "id": 1001, "orderId": 11_001, "symbol": "BTCUSDT", "side": "SELL",
                "price": "68000", "qty": "0.001", "realizedPnl": "0.1",
                "commission": "0.001", "commissionAsset": "USDT", "time": 1001,
            }]
            service.broker.client.user_trades = mock.Mock(side_effect=[first_page, second_page])
            result = service.broker.sync_fills(["BTCUSDT"])
            self.assertEqual(result["pages"], 2)
            self.assertEqual(result["inserted"], 1001)
            self.assertEqual(service.ledger.get_meta("fill_cursor:BTCUSDT"), "1001")

            # An invalid fromId must clear the cursor and recover from the
            # recent page, not wedge the sync forever.
            service.ledger.set_meta("fill_cursor:BTCUSDT", "100")
            service.broker.client.user_trades = mock.Mock(
                side_effect=[RuntimeError("invalid fromId"), second_page]
            )
            recovered = service.broker.sync_fills(["BTCUSDT"])
            self.assertIn("BTCUSDT", recovered["recovered"])
            self.assertEqual(recovered["inserted"], 0, "the recovered row is already idempotent")
        finally:
            case.close()

    def test_health_reports_supervisor_started_at_lifespan(self) -> None:
        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                body = client.get("/health").json()
            self.assertIn(body.get("supervisor"), {"running", "read_only"})
        finally:
            case.close()

    def test_supervisor_runs_independently_and_can_stop(self) -> None:
        """A06: reconcile is not only driven by an API read."""
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            called = threading.Event()
            service.reconcile = mock.Mock(
                side_effect=lambda: (called.set(), {"exchange_reachable": True})[1]
            )
            self.assertTrue(service.start_monitor(interval_s=0.05))
            self.assertTrue(called.wait(2), "supervisor did not run without a read request")
            service.stop_monitor()
            self.assertIsNone(service._monitor_thread)
            self.assertEqual(len(case.fake.orders), 0, "supervisor must never submit orders")
        finally:
            case.close()

    def test_close_fee_is_a_cost_and_unknown_fee_currency_is_not_a_win(self) -> None:
        """A04: net = realized - commission; non-quote fee is unknown, not profit."""
        case = ServiceCase(with_fake=False)
        try:
            service = case.service
            service.ledger.recent_close_fills = mock.Mock(
                return_value=[
                    {
                        "client_order_id": "close-a04-usdt",
                        "realized_pnl": "-0.01",
                        "commission": "0.10",
                        "commission_asset": "USDT",
                        "trade_time_ms": 1000,
                    },
                    {
                        "client_order_id": "close-a04-bnb",
                        "realized_pnl": "1.00",
                        "commission": "0.02",
                        "commission_asset": "BNB",
                        "trade_time_ms": 2000,
                    },
                ]
            )
            merged = dict(
                (cid, net) for cid, net, _ts in service._merged_close_results()
            )
            self.assertEqual(merged["close-a04-usdt"], Decimal("-0.11"))
            self.assertIsNone(merged["close-a04-bnb"])
        finally:
            case.close()


# ======================================================================
# T03 · §8·B：停止语义、提交前硬刷新、重启恢复
# ======================================================================

class ExecutionClosureRegressionTests(unittest.TestCase):
    def test_pause_keeps_position_and_protection_but_revokes_the_lease(self) -> None:
        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                approved = approve(client, cycle_id="t03-pause-open")
                self.assertEqual(submit(client, approved).status_code, 201)
                lease = case.service.leases.acquire("run-t03-pause", owner="host")
                posts_before = len(case.fake.calls_for("/fapi/v1/order", "POST"))
                body = client.post(
                    "/api/control",
                    json={
                        "state": "pause",
                        "confirm": "PAUSE",
                        "reason": "普通停止不平仓",
                        "request_id": "req-t03-pause",
                    },
                ).json()
            self.assertEqual(body["control"]["state"], "pause")
            self.assertIsNone(body["flattened"], "pause 不得平仓")
            self.assertTrue(body["protection_orders_untouched"])
            self.assertEqual(len(case.fake.positions), 1, "pause 不平仓")
            self.assertEqual(len(case.fake._open_algos()), 2, "pause 不撤保护单")
            with self.assertRaises(LeaseError):
                case.service.leases.validate(lease["lease_id"])
            self.assertEqual(
                len(case.fake.calls_for("/fapi/v1/order", "POST")), posts_before
            )
            blocked = evaluate(client, cycle_id="t03-pause-recheck")
            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertEqual(blocked.json()["error"]["code"], "E_CONTROL_LOCKED")
        finally:
            case.close()

    def test_pre_submit_refresh_bypasses_the_two_second_snapshot_cache(self) -> None:
        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                approved = approve(client, cycle_id="t03-refresh")
                cached = Decimal(case.service.broker._cache.account["availableBalance"])
                self.assertGreater(cached, Decimal("1000"))
                case.fake.available_override = Decimal("50")
                response = submit(client, approved)
            self.assertEqual(response.status_code, 201, response.text)
            body = response.json()
            self.assertEqual(
                Decimal(body["pre_submit_refresh"]["available_margin"]), Decimal("50")
            )
            self.assertEqual(Decimal(body["qty_requested"]), Decimal("0.004"))
            self.assertEqual(
                case.fake.positions["BTCUSDT"]["qty"], Decimal("0.004")
            )
        finally:
            case.close()

    def test_refresh_failure_leaves_no_submitting_outbox_row(self) -> None:
        """刷新发生在 outbox prepare 之前：失败时不得留下 SUBMITTING 孤儿行。"""

        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                approved = approve(client, cycle_id="t03-refresh-fail")
                case.fake.fail_account = 10
                response = submit(client, approved)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(
                response.json()["error"]["code"], "E_EXECUTION_REFRESH_FAILED"
            )
            counts = case.service.outbox.counts()
            self.assertEqual(counts.get("submitting", 0), 0, counts)
            self.assertEqual(counts.get("unknown", 0), 0, counts)
            self.assertEqual(case.service.ledger.list_orders(limit=10), [])
            self.assertEqual(len(case.fake.calls_for("/fapi/v1/order", "POST")), 0)
        finally:
            case.close()

    def test_restart_recovers_pending_rows_before_position_and_protection_check(self) -> None:
        from support import make_service

        case = ServiceCase(with_fake=True)
        client = case.client()
        try:
            with client:
                client.get("/api/status")
                ledger = case.service.ledger
                cid = "wb-20261110T200500Z-dead04"
                created_at = "2026-11-10T20:05:00Z"
                ledger.outbox_prepare(
                    {
                        "client_order_id": cid,
                        "business_key": "ENTRY:BTCUSDT:BOTH",
                        "endpoint": "/fapi/v1/order",
                        "request": {"symbol": "BTCUSDT"},
                        "status": "SUBMITTING",
                    }
                )
                ledger.execute(
                    "UPDATE order_outbox SET status='UNKNOWN', created_at=?, "
                    "updated_at=?, unknown_since=? WHERE client_order_id=?",
                    (created_at, created_at, created_at, cid),
                )
                ledger.insert_order(
                    {
                        "client_order_id": cid,
                        "purpose": "ENTRY",
                        "symbol": "BTCUSDT",
                        "side": "BUY",
                        "order_type": "MARKET",
                        "qty_requested": "0.02",
                        "status": "UNKNOWN",
                        "cycle_id": "cyc-20261110T200500Z-dead04",
                        "created_at": created_at,
                        "updated_at": created_at,
                    }
                )
                case.service.ledger.close()
                service = make_service(case.settings, case.fake, clock=case.clock)
                startup = service.ensure_started()
            self.assertTrue(startup["recovery"]["converged"], startup["recovery"])
            self.assertIn("protection", startup)
            self.assertFalse(startup["new_entry_blocked"])
            self.assertEqual(service.ledger.get_order(cid)["status"], "CANCELED")
            self.assertEqual(service.outbox.get(cid)["status"], "CANCELED")
            service.ledger.close()
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
