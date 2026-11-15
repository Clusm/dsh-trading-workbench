"""安全边界与账本持久化测试。

验收映射：
- 无 live 常量/路径（source scan + guard 构造/请求两层）；
- 密钥只从 env/``.env.local`` 读取，响应与 repr 不泄漏；
- 旧库 0 读写（size/mtime/bytes 不变）；
- 新库 ``workbench-v1.sqlite`` 幂等建表，订单/Outbox/审批/权益键约束生效。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support import ServiceCase, approve, submit  # noqa: E402

from workbench_service.config import DEMO_HOST, Settings  # noqa: E402
from workbench_service.execution.client import DemoBinanceClient  # noqa: E402
from workbench_service.execution.credentials import (  # noqa: E402
    DemoCredentials,
    load_demo_credentials,
)
from workbench_service.execution.errors import DemoGuardViolation  # noqa: E402
from workbench_service.execution.outbox import OrderOutbox  # noqa: E402
from workbench_service.ledger.store import (  # noqa: E402
    DuplicateOrderError,
    Ledger,
    PendingOrderConflict,
)
from workbench_service.risk.approval import ApprovalError, ApprovalManager  # noqa: E402
from workbench_service.risk.models import OrderPlan, RiskVerdict  # noqa: E402

SERVICE_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = SERVICE_DIR / "workbench_service"
NON_DEMO_HOST = "fapi" + ".binance.com"
NON_DEMO_URL = "https://" + NON_DEMO_HOST


def _plan() -> OrderPlan:
    return OrderPlan.from_payload(
        {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "leverage": 8,
            "qty": "0.01",
            "sl": "99000",
            "tp": "102800",
        },
        cycle_id="unit",
    )


def _verdict() -> RiskVerdict:
    return RiskVerdict(verdict="approved", approved_qty=Decimal("0.01"), leverage=8)


class CredentialsTests(unittest.TestCase):
    def test_env_credentials(self) -> None:
        creds = load_demo_credentials(
            Path(tempfile.gettempdir()),
            env={"WORKBENCH_DEMO_API_KEY": "key-abc", "WORKBENCH_DEMO_API_SECRET": "sec-xyz"},
        )
        assert creds is not None
        self.assertEqual(creds.source, "env")
        self.assertEqual(len(creds.fingerprint()), 8)
        self.assertEqual(creds.fingerprint(), hashlib.sha256(b"key-abc").hexdigest()[:8])
        self.assertNotIn("key-abc", repr(creds))
        self.assertNotIn("sec-xyz", repr(creds))
        public = creds.as_public_dict()
        self.assertNotIn("api_key", public)
        self.assertNotIn("api_secret", public)

    def test_env_file_credentials_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env.local"
            path.write_text(
                "# demo creds\nBINANCE_DEMO_API_KEY=k-file\nBINANCE_DEMO_API_SECRET=s-file\n",
                encoding="utf-8",
            )
            if os.name != "nt":
                os.chmod(path, 0o600)
            creds = load_demo_credentials(tmp, env={})
            assert creds is not None
            self.assertEqual(creds.source, "env_file")
            self.assertEqual(creds.api_key, "k-file")

    def test_incomplete_credentials_return_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_demo_credentials(tmp, env={"WORKBENCH_DEMO_API_KEY": "only"}))
            (Path(tmp) / ".env.local").write_text("BINANCE_DEMO_API_KEY=only\n", encoding="utf-8")
            self.assertIsNone(load_demo_credentials(tmp, env={}))

    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_demo_credentials(tmp, env={}))


class DemoGuardTests(unittest.TestCase):
    def test_client_rejects_non_demo_base_before_any_request(self) -> None:
        calls: list[str] = []

        def handler(request):  # pragma: no cover - 不应被调用
            calls.append(str(request.url))
            raise AssertionError("guard must reject before any request")

        import httpx

        with self.assertRaises(DemoGuardViolation):
            DemoBinanceClient(NON_DEMO_URL, DemoCredentials("k", "s"), transport=httpx.MockTransport(handler))
        self.assertEqual(calls, [])

    def test_request_guard_blocks_mutated_base_url(self) -> None:
        calls: list[str] = []
        import httpx

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"serverTime": 1}, request=request)

        client = DemoBinanceClient(
            "https://" + DEMO_HOST, DemoCredentials("k", "s"), transport=httpx.MockTransport(handler)
        )
        client._base_url = NON_DEMO_URL  # 模拟配置被篡改
        with self.assertRaises(DemoGuardViolation):
            client.request("GET", "/fapi/v1/time", signed=False)
        self.assertEqual(calls, [])
        client.close()

    def test_live_endpoint_and_credentials_stay_in_their_controlled_modules(self) -> None:
        """§6 M2 后的新不变量：Live 支持是**刻意**的，但必须被限制在两处。

        旧用例要求“全仓不出现 fapi.binance.com / BINANCE_LIVE”，那是 Demo-only 时代的
        保证。现在开了 Live，这条改为：非 demo 的 fapi host 只能出现在 config.py 的
        受控映射里；BINANCE_LIVE_* 只能出现在 credentials.py 的专用 loader 里。
        这样“意外多出一条 live 路径”仍然会被抓到。
        """

        live_host = re.compile(r"(?<!demo-)fapi\.binance\.com")
        live_env = re.compile(r"BINANCE_LIVE")
        allowed_host_files = {PACKAGE_DIR / "config.py"}
        allowed_cred_files = {PACKAGE_DIR / "execution" / "credentials.py"}
        offenders: list[str] = []
        for path in PACKAGE_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if live_host.search(text) and path not in allowed_host_files:
                offenders.append(f"{path.name}: non-demo fapi host outside config mapping")
            if live_env.search(text) and path not in allowed_cred_files:
                offenders.append(f"{path.name}: BINANCE_LIVE outside the live credential loader")
        self.assertEqual(offenders, [])
        self.assertEqual(DEMO_HOST, "demo-fapi.binance.com")


class SecretsNotLeakedTests(unittest.TestCase):
    def test_responses_never_include_secret_values(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                approval = approve(client, cycle_id="leak-check")
                self.assertEqual(submit(client, approval).status_code, 201)
                for path in ("/api/status", "/api/risk/envelope", "/health"):
                    response = client.get(path)
                    text = response.text
                    self.assertNotIn(case.fake.api_key, text)
                    self.assertNotIn(case.fake.api_secret, text)
                status = client.get("/api/status").json()
                self.assertTrue(status["credentials"]["configured"])
                self.assertEqual(
                    status["credentials"]["key_fingerprint"],
                    hashlib.sha256(case.fake.api_key.encode()).hexdigest()[:8],
                )
        finally:
            case.close()

    def test_old_database_is_never_touched(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            old_db = case.data_dir / "agent_trader.sqlite"
            payload = b"legacy-demo-history" * 100
            old_db.write_bytes(payload)
            before = old_db.stat()
            client = case.client()
            with client:
                approval = approve(client, cycle_id="old-db")
                self.assertEqual(submit(client, approval).status_code, 201)
                client.get("/api/status")
                client.post("/api/execution/close", json={"symbol": "BTCUSDT"})
            after = old_db.stat()
            self.assertEqual(old_db.read_bytes(), payload)
            self.assertEqual(before.st_size, after.st_size)
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(
                (case.data_dir / "workbench-v1.sqlite").exists(), True
            )
        finally:
            case.close()


class LedgerConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self._tmp.name))

    def tearDown(self) -> None:
        self.ledger.close()
        self._tmp.cleanup()

    def test_schema_tables_exist(self) -> None:
        tables = set(self.ledger.iter_table_names())
        expected = {
            "orders",
            "order_outbox",
            "fills",
            "positions",
            "position_events",
            "equity_snapshots",
            "risk_state",
            "risk_events",
            "cycle_runs",
            "control_state",
            "approvals",
        }
        self.assertTrue(expected.issubset(tables), expected - tables)
        self.assertEqual(self.ledger.path.name, "workbench-v1.sqlite")
        self.assertTrue(self.ledger.ledger_id)

    def test_order_primary_key_is_idempotency_boundary(self) -> None:
        record = {
            "client_order_id": "cid-1",
            "symbol": "BTCUSDT",
            "side": "BUY",
            "order_type": "MARKET",
            "qty_requested": "0.01",
            "status": "NEW",
        }
        self.ledger.insert_order(record)
        with self.assertRaises(DuplicateOrderError):
            self.ledger.insert_order(record)

    def test_outbox_pending_entry_conflict(self) -> None:
        outbox = OrderOutbox(self.ledger)
        outbox.prepare(
            client_order_id="cid-a",
            purpose="ENTRY",
            symbol="BTCUSDT",
            side="BUY",
            position_side=None,
            order_type="MARKET",
            endpoint="/fapi/v1/order",
            request={},
            qty="0.01",
        )
        with self.assertRaises(PendingOrderConflict):
            outbox.prepare(
                client_order_id="cid-b",
                purpose="ENTRY",
                symbol="BTCUSDT",
                side="BUY",
                position_side=None,
                order_type="MARKET",
                endpoint="/fapi/v1/order",
                request={},
                qty="0.01",
            )
        # 重复 clientOrderId：Outbox 以 duplicate 结果返回（由 broker 走幂等分支）
        duplicate = outbox.prepare(
            client_order_id="cid-a",
            purpose="EXIT",
            symbol="BTCUSDT",
            side="SELL",
            position_side=None,
            order_type="MARKET",
            endpoint="/fapi/v1/order",
            request={},
            qty="0.01",
        )
        self.assertTrue(duplicate.duplicate)

    def test_equity_bucket_is_idempotent(self) -> None:
        for value in ("5000", "4999"):
            self.ledger.insert_equity_snapshot(
                {
                    "ts_bucket": "2026-11-09T00:00:00Z",
                    "equity_exchange": value,
                    "equity_session": value,
                }
            )
        rows = self.ledger.query("SELECT * FROM equity_snapshots")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["equity_session"], "4999")

    def test_approval_single_use_and_expiry(self) -> None:
        now = [1000.0]
        manager = ApprovalManager(self.ledger, ttl_s=120, clock=lambda: now[0])
        issued = manager.issue(_plan(), _verdict(), context={"market": {"mark": "100000"}})
        payload = manager.consume(issued["token"], plan=_plan())
        self.assertEqual(payload["jti"], issued["jti"])
        with self.assertRaises(ApprovalError) as replay:
            manager.consume(issued["token"], plan=_plan())
        self.assertEqual(replay.exception.code, "E_APPROVAL_REPLAY")

        second = manager.issue(_plan(), _verdict())
        now[0] += 121
        with self.assertRaises(ApprovalError) as expired:
            manager.consume(second["token"], plan=_plan())
        self.assertEqual(expired.exception.code, "E_APPROVAL_EXPIRED")

    def test_approval_plan_mismatch(self) -> None:
        manager = ApprovalManager(self.ledger, ttl_s=120)
        issued = manager.issue(_plan(), _verdict())
        other = OrderPlan.from_payload(
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "leverage": 8,
                "qty": "0.02",
                "sl": "99000",
                "tp": "102800",
            },
            cycle_id="unit",
        )
        with self.assertRaises(ApprovalError) as mismatch:
            manager.consume(issued["token"], plan=other)
        self.assertEqual(mismatch.exception.code, "E_APPROVAL_PLAN_MISMATCH")


class ControlStateTests(unittest.TestCase):
    def test_control_persists_and_normal_blocked_by_latches(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            state = service.state
            control = state.set_control("close_only", "unit test", updated_by="test")
            self.assertEqual(control.state, "close_only")
            # 新实例读同一 DB：状态必须沿用
            from workbench_service.risk.state import RiskStateStore

            reloaded = RiskStateStore(service.ledger, service.limits).control()
            self.assertEqual(reloaded.state, "close_only")
            service.ledger.save_risk_state(
                {
                    **{k: v for k, v in (service.ledger.load_risk_state() or {}).items()},
                    "anchor_id": state.anchor_id,
                    "date": "2026-11-09",
                    "daily_loss_latched": 1,
                }
            )
            with self.assertRaises(Exception) as blocked:
                state.set_control("normal", "try recover")
            self.assertEqual(getattr(blocked.exception, "code", ""), "E_CONTROL_LOCKED")
        finally:
            case.close()

    def test_control_requires_reason(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            with self.assertRaises(Exception):
                case.service.state.set_control("pause", "  ")
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
