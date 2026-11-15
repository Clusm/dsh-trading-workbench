"""T03 §8·B5/B6 + 停止语义：重启恢复夹具、pause 语义、time_stop 期限持久化。

覆盖验收：
- 启动/控制恢复先收敛 SUBMITTING/UNKNOWN/永停 NEW 等未决项，再核对仓位与保护；
  未收敛前禁止受影响账户新开仓（F1 夹具：同一数据目录关停重建 + G4 复刻行）；
- control(state=pause) 只撤销在途 lease 并阻断新自动执行：不平仓、不撤交易所保护单；
- time_stop 保存 opened_at / 期限 / 来源计划版本，重启后按原期限判断；
  外部仓位不自动套用；到期处置用稳定命令标识，不逐周期重复平仓。
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServiceCase, approve, evaluate, make_app, make_service, submit  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from workbench_service.execution.errors import ExecutionRejected  # noqa: E402
from workbench_service.execution.lease import LeaseError  # noqa: E402


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _seed_stale_protection_rows(case: ServiceCase) -> list[str]:
    """复刻 T01 基线 G4：4 条 2026-11-10 的保护单在 orders/outbox 永停 NEW。"""

    ledger = case.service.ledger
    rows = [
        ("wbp-st-1094bf0d8d082c6fd358", "STOP_MARKET", "1000000282224664", "2026-11-10T19:38:53Z"),
        ("wbp-ta-4357e546eba9a529dc2f", "TAKE_PROFIT_MARKET", "1000000282224667", "2026-11-10T19:38:53Z"),
        ("wbp-st-446f0cf9f2e3e0306a0b", "STOP_MARKET", "1000000282299671", "2026-11-10T21:09:27Z"),
        ("wbp-ta-862f25df05160489235d", "TAKE_PROFIT_MARKET", "1000000282299673", "2026-11-10T21:09:27Z"),
    ]
    for cid, order_type, exchange_id, created_at in rows:
        ledger.insert_order(
            {
                "client_order_id": cid,
                "exchange_order_id": exchange_id,
                "purpose": "PROTECTION",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "position_side": "SHORT",
                "order_type": order_type,
                "qty_requested": "0.037",
                "status": "NEW",
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        ledger.outbox_prepare(
            {
                "client_order_id": cid,
                "business_key": "PROTECTION:BTCUSDT:SHORT",
                "endpoint": "/fapi/v1/algoOrder",
                "request": {"symbol": "BTCUSDT"},
                "status": "NEW",
            }
        )
        ledger.execute(
            "UPDATE order_outbox SET status='NEW', created_at=?, updated_at=? "
            "WHERE client_order_id=?",
            (created_at, created_at, cid),
        )
    return [row[0] for row in rows]


def _seed_stale_submitting(case: ServiceCase, cid: str = "wbp-st-stale-submitting") -> str:
    ledger = case.service.ledger
    created_at = "2026-11-10T20:00:00Z"
    ledger.outbox_prepare(
        {
            "client_order_id": cid,
            "business_key": "PROTECTION:BTCUSDT:SHORT",
            "endpoint": "/fapi/v1/algoOrder",
            "request": {"symbol": "BTCUSDT"},
            "status": "SUBMITTING",
        }
    )
    ledger.execute(
        "UPDATE order_outbox SET created_at=?, updated_at=? WHERE client_order_id=?",
        (created_at, created_at, cid),
    )
    return cid


def _seed_unknown_order(case: ServiceCase, cid: str = "wb-20261110T200500Z-dead01") -> str:
    ledger = case.service.ledger
    created_at = "2026-11-10T20:05:00Z"
    ledger.outbox_prepare(
        {
            "client_order_id": cid,
            "business_key": "ENTRY:BTCUSDT:BOTH",
            "endpoint": "/fapi/v1/order",
            "request": {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET"},
            "status": "SUBMITTING",
        }
    )
    ledger.execute(
        "UPDATE order_outbox SET status='UNKNOWN', created_at=?, updated_at=?, "
        "unknown_since=? WHERE client_order_id=?",
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
            "cycle_id": "cyc-20261110T200500Z-dead01",
            "created_at": created_at,
            "updated_at": created_at,
        }
    )
    return cid


def _reopen(case: ServiceCase, *, settings=None, clock=None):
    """同一数据目录关停重建（F1 夹具）。"""

    settings = settings or case.settings
    case.service.ledger.close()
    service = make_service(settings, case.fake, clock=clock or case.clock)
    case.service = service
    case.app = make_app(settings, service)
    return service


class RestartRecoveryFixtureTests(unittest.TestCase):
    def test_restart_converges_pending_rows_before_position_check(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                client.get("/api/status")  # 首次启动对账
                protection_rows = _seed_stale_protection_rows(case)
                submitting = _seed_stale_submitting(case)
                unknown = _seed_unknown_order(case)
                posts_before = len(case.fake.calls_for("/fapi/v1/order", "POST"))

                service = _reopen(case)
                startup = service.ensure_started()
                recovery = startup["recovery"]
                self.assertTrue(recovery["converged"], recovery)
                self.assertEqual(recovery["unresolved"], 0)
                self.assertGreaterEqual(recovery["before"].get("submitting", 0), 1)
                self.assertGreaterEqual(recovery["before"].get("unknown", 0), 1)
                self.assertIn("protection", startup)
                self.assertFalse(startup["new_entry_blocked"])

                ledger = service.ledger
                for cid in protection_rows + [submitting, unknown]:
                    self.assertEqual(
                        ledger.get_order(cid)["status"]
                        if ledger.get_order(cid)
                        else service.outbox.get(cid)["status"],
                        "CANCELED",
                        cid,
                    )
                for cid in protection_rows + [submitting]:
                    self.assertEqual(service.outbox.get(cid)["status"], "CANCELED")
                # 恢复阶段只查询，绝不下单
                self.assertEqual(
                    len(case.fake.calls_for("/fapi/v1/order", "POST")), posts_before
                )
        finally:
            case.close()

    def test_unconverged_pending_after_restart_blocks_new_entries(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                client.get("/api/status")
                _seed_unknown_order(case, cid="wb-20261110T200500Z-dead02")
                service = _reopen(case)
                service.broker._query_unknown_row = mock.Mock(
                    side_effect=RuntimeError("exchange unreachable")
                )
                service.broker.converge_stale_orders = mock.Mock(
                    return_value={
                        "checked": 0,
                        "converged": [],
                        "errors": 1,
                        "ttl_s": 120.0,
                        "open_lists_available": False,
                    }
                )
                startup = service.ensure_started()
                self.assertFalse(startup["recovery"]["converged"])
                self.assertEqual(startup["state"], "recovering")
                self.assertTrue(startup["new_entry_blocked"])
                self.assertEqual(startup["recovery"]["unresolved"], 1)

                blocked_client = TestClient(case.app)
                with blocked_client:
                    blocked = evaluate(blocked_client, cycle_id="after-restart")
                    status = blocked_client.get("/api/status").json()
                self.assertEqual(blocked.status_code, 422, blocked.text)
                self.assertEqual(blocked.json()["error"]["code"], "E_OUTBOX_PENDING")
                self.assertIn("ORDER_OUTBOX_UNKNOWN", status["readiness"]["reasons"])
        finally:
            case.close()


class PauseSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.fake = self.case.fake
        self.service = self.case.service
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_pause_keeps_position_and_protection_but_revokes_lease(self) -> None:
        response = submit(self.client, approve(self.client, cycle_id="pause-open"))
        self.assertEqual(response.status_code, 201, response.text)
        lease = self.service.leases.acquire("run-pause", owner="host")
        posts_before = len(self.fake.calls_for("/fapi/v1/order", "POST"))

        body = self.client.post(
            "/api/control",
            json={
                "state": "pause",
                "confirm": "PAUSE",
                "reason": "普通停止：不平仓",
                "request_id": "req-pause-semantics",
            },
        ).json()
        self.assertEqual(body["control"]["state"], "pause")
        self.assertIsNone(body["flattened"], "pause 不得触发平仓")
        self.assertTrue(body["protection_orders_untouched"])
        self.assertGreaterEqual(body["lease_generation"], 1)
        self.assertEqual(len(self.fake.positions), 1, "pause 不平仓")
        self.assertEqual(len(self.fake._open_algos()), 2, "pause 不撤交易所保护单")
        with self.assertRaises(LeaseError):
            self.service.leases.validate(lease["lease_id"])

        blocked = evaluate(self.client, cycle_id="after-pause")
        self.assertEqual(blocked.status_code, 422, blocked.text)
        self.assertEqual(blocked.json()["error"]["code"], "E_CONTROL_LOCKED")
        self.assertEqual(
            len(self.fake.calls_for("/fapi/v1/order", "POST")),
            posts_before,
            "pause 后不得再自动执行",
        )

    def test_resume_runs_pending_recovery_before_unblocking(self) -> None:
        _seed_unknown_order(self.case, cid="wb-20261110T200500Z-dead03")
        self.client.post(
            "/api/control",
            json={
                "state": "pause",
                "confirm": "PAUSE",
                "reason": "停",
                "request_id": "req-pause-before-resume",
            },
        )
        body = self.client.post(
            "/api/control",
            json={
                "state": "normal",
                "confirm": "NORMAL",
                "reason": "恢复",
                "request_id": "req-resume-recovery",
            },
        ).json()
        self.assertEqual(body["control"]["state"], "normal")
        self.assertIsNotNone(body["recovery"], body)
        self.assertTrue(body["recovery"]["converged"], body["recovery"])
        self.assertIn("protection", body["recovery"])


class TimeStopLifecycleTests(unittest.TestCase):
    def _case(self, *, hours: float, clock) -> ServiceCase:
        case = ServiceCase(with_fake=True, clock=clock)
        settings = dataclasses.replace(case.settings, auto_time_stop_hours=hours)
        case.settings = settings
        _reopen(case, settings=settings, clock=clock)
        return case

    def test_deadline_and_source_plan_version_are_persisted(self) -> None:
        clock = [1_800_000_000.0]
        case = self._case(hours=2.0, clock=lambda: clock[0])
        client = case.client()
        try:
            with client:
                response = submit(client, approve(client, cycle_id="cyc-time-stop-1"))
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(response.json()["time_stop"]["hours"], 2.0)
                row = case.service.ledger.get_position("BTCUSDT", "LONG")
                opened = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
                deadline = datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00"))
                self.assertAlmostEqual(
                    (deadline - opened).total_seconds(), 2 * 3600, delta=5
                )
                self.assertIn("plan:cyc-time-stop-1", row["time_stop_source"])
                self.assertIn("rules:", row["time_stop_source"])
        finally:
            case.close()

    def test_restart_honours_the_original_deadline(self) -> None:
        clock = [1_800_000_000.0]
        case = self._case(hours=2.0, clock=lambda: clock[0])
        client = case.client()
        try:
            with client:
                self.assertEqual(
                    submit(client, approve(client, cycle_id="cyc-time-stop-2")).status_code,
                    201,
                )
            # 同一数据目录、更短的 1h 配置重建：必须按“原期限”而不是新配置重算
            service = _reopen(
                case,
                settings=dataclasses.replace(case.settings, auto_time_stop_hours=1.0),
                clock=lambda: clock[0],
            )
            # 该仓位“建仓于 1.5h 前、原计划期限是 now+0.5h”（2h 期限）。
            # 若新实例用 1h 重新推导，就会误判成已到期并提前平仓。
            opened = datetime.fromtimestamp(clock[0] - 5400, tz=timezone.utc)
            deadline = datetime.fromtimestamp(clock[0] + 1800, tz=timezone.utc)
            service.ledger.execute(
                "UPDATE positions SET opened_at=?, deadline_at=?",
                (_iso(opened), _iso(deadline)),
            )
            service._enforce_time_stop()
            self.assertEqual(
                len(case.fake.positions), 1, "原期限未到，不得用新配置提前平仓"
            )

            clock[0] += 3600  # 越过原期限
            service._enforce_time_stop()
            self.assertEqual(case.fake.positions, {})
            operations = [
                row
                for row in service.ledger.operations()
                if row["action"] == "time_stop_close"
            ]
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]["status"], "succeeded")
            self.assertEqual(operations[0]["summary"]["closed"], True)
            self.assertEqual(operations[0]["summary"]["symbol"], "BTCUSDT")

            # 再跑一个周期：同一命令标识已记录，不重复平仓
            service._enforce_time_stop()
            self.assertEqual(
                len(
                    [
                        row
                        for row in service.ledger.operations()
                        if row["action"] == "time_stop_close"
                    ]
                ),
                1,
            )
        finally:
            case.close()

    def test_external_position_is_not_time_stopped(self) -> None:
        clock = [1_800_000_000.0]
        case = self._case(hours=1.0, clock=lambda: clock[0])
        try:
            case.fake.add_position("BTCUSDT", "LONG", "0.01", "100000")
            case.service.ensure_started()
            case.service.broker.reconcile()
            row = case.service.ledger.get_position("BTCUSDT", "LONG")
            self.assertEqual(row["origin"], "EXTERNAL")
            opened = datetime.fromtimestamp(clock[0] - 7200, tz=timezone.utc)
            case.service.ledger.execute(
                "UPDATE positions SET opened_at=?", (_iso(opened),)
            )
            case.service._enforce_time_stop()
            self.assertEqual(len(case.fake.positions), 1, "外部仓位不自动套用系统策略")
            self.assertEqual(
                [
                    row
                    for row in case.service.ledger.operations()
                    if row["action"] == "time_stop_close"
                ],
                [],
            )
        finally:
            case.close()

    def test_time_stop_is_opt_in(self) -> None:
        clock = [1_800_000_000.0]
        case = self._case(hours=0.0, clock=lambda: clock[0])
        client = case.client()
        try:
            with client:
                self.assertEqual(
                    submit(
                        client, approve(client, cycle_id="cyc-time-stop-optin")
                    ).status_code,
                    201,
                )
            row = case.service.ledger.get_position("BTCUSDT", "LONG")
            self.assertIsNone(row["deadline_at"], "未启用时不应写入期限")
            opened = datetime.fromtimestamp(clock[0] - 7200, tz=timezone.utc)
            case.service.ledger.execute(
                "UPDATE positions SET opened_at=?", (_iso(opened),)
            )
            case.service._enforce_time_stop()
            self.assertEqual(len(case.fake.positions), 1)
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
