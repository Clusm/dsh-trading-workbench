"""只读端点测试：``/api/status`` / ``/api/positions`` / ``/api/orders`` / ``/api/fills`` /
``/api/equity-curve`` / ``/api/risk/envelope`` 与 Bearer 鉴权。

覆盖验收：
- 只读端点可用，返回 ``demo_only`` 与 ``ledger_id``；
- 无 Demo 凭据时明确返回 ``credentials_configured=false`` 与阻断原因，
  且 P2 直接 503（不静默改纸面）；
- Bearer 模式下缺少 token 返回 401 ``E_AUTH_REQUIRED``。
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from support import ServiceCase, approve, make_app, submit  # noqa: E402

from workbench_service.config import Settings  # noqa: E402
from workbench_service.execution.client import DemoBinanceClient  # noqa: E402
from workbench_service.execution.credentials import DemoCredentials  # noqa: E402
from workbench_service.execution.service import build_execution_service  # noqa: E402


class ReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=False)
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_status_has_demo_only_and_ledger_id(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["demo_only"])
        self.assertEqual(body["venue"], "demo")
        self.assertTrue(body["ledger_id"])
        self.assertFalse(body["credentials_configured"])
        self.assertEqual(body["positions"], [])
        self.assertTrue(body["exchange"]["stale"])
        self.assertEqual(body["control"]["state"], "normal")
        self.assertEqual(body["startup"]["state"], "read_only")
        self.assertIn("E_DEMO_CREDENTIALS_MISSING", body["readiness"]["reasons"])
        # 不下单、不请求交易所
        self.assertFalse(body["readiness"]["new_entry_allowed"])

    def test_positions_orders_fills_curve_shapes(self) -> None:
        positions = self.client.get("/api/positions")
        self.assertEqual(positions.status_code, 200)
        body = positions.json()
        self.assertTrue(body["demo_only"])
        self.assertEqual(body["count"], 0)

        orders = self.client.get("/api/orders", params={"limit": 5})
        self.assertEqual(orders.status_code, 200)
        self.assertEqual(orders.json()["orders"], [])

        fills = self.client.get("/api/fills")
        self.assertEqual(fills.status_code, 200)
        self.assertEqual(fills.json()["fills"], [])

        curve = self.client.get("/api/equity-curve", params={"range": "1d", "bucket": "1m"})
        self.assertEqual(curve.status_code, 200)
        self.assertEqual(curve.json()["points"], [])

    def test_ready_status_envelope_share_the_same_readiness(self) -> None:
        ready = self.client.get("/api/ready")
        self.assertEqual(ready.status_code, 200, ready.text)
        ready_body = ready.json()
        status = self.client.get("/api/status").json()
        envelope = self.client.get("/api/risk/envelope").json()
        self.assertFalse(ready_body["ready"])
        self.assertTrue(ready_body["service_ready"])
        self.assertFalse(ready_body["new_entry_allowed"])
        self.assertEqual(ready_body["ready"], status["readiness"]["new_entry_allowed"])
        self.assertEqual(ready_body["new_entry_allowed"], status["readiness"]["new_entry_allowed"])
        self.assertEqual(
            ready_body["new_entry_allowed"], envelope["new_entry_allowed"]
        )
        self.assertEqual(
            set(ready_body["reasons"]), set(status["readiness"]["reasons"])
        )
        self.assertEqual(
            set(status["readiness"]["reasons"]), set(envelope["blocking_reasons"])
        )
        self.assertEqual(ready_body["control_state"], status["control"]["state"])
        self.assertEqual(ready_body["control_state"], envelope["control"]["state"])

    def test_risk_envelope_lists_all_rules(self) -> None:
        response = self.client.get("/api/risk/envelope")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        rules = body["rules"]
        self.assertEqual(len(rules), 26)
        self.assertEqual(rules[0]["rule_id"], "R00")
        self.assertEqual(rules[-1]["rule_id"], "R25")
        enabled = {rule["rule_id"]: rule["enabled"] for rule in rules}
        # docs/19 §2 R12：R20/R21 提前打开；对旧链同样生效，只会增加拒绝。
        self.assertTrue(enabled["R20"], "R20 默认开启")
        self.assertTrue(enabled["R21"], "R21 默认开启")
        self.assertTrue(enabled["R01"])
        self.assertEqual(body["limits"]["portfolio"]["max_open_positions"], 1)
        self.assertEqual(body["approval"]["ttl_s"], 120)
        freshness = body["limits"]["data_freshness"]
        self.assertEqual(freshness["decision_evidence_max_age_ms"], 400_000)
        self.assertEqual(freshness["entry_snapshot_max_age_ms"], 15_000)
        self.assertEqual(freshness["equity_block_new_ms"], 60_000)
        # rules[] 展示文案必须与 limits 一致，不能残留旧的 120s
        r14 = next(rule for rule in rules if rule["rule_id"] == "R14")
        self.assertIn("证据 ≤400s", r14["summary"])
        self.assertNotIn("120s", r14["summary"])
        self.assertIn("行情 ≤15s", r14["summary"])
        self.assertFalse(body["new_entry_allowed"])
        self.assertIn("E_DEMO_CREDENTIALS_MISSING", body["blocking_reasons"])

    def test_p1_without_credentials_is_rejected_by_equity(self) -> None:
        response = self.client.post(
            "/api/risk/evaluate",
            json={
                "cycle_id": "c-no-key",
                "plan": {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "leverage": 8,
                    "qty": "0.02",
                    "sl": "99000",
                    "tp": "102800",
                },
                "market": {"mark": "100000", "snapshot_age_ms": 1000},
                "evidence_age_ms": 1000,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_EQUITY_UNAVAILABLE")

    def test_p2_without_credentials_returns_503(self) -> None:
        response = self.client.post(
            "/api/execution/order", json={"approval_token": "whatever"}
        )
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_DEMO_CREDENTIALS_MISSING")

    def test_invalid_plan_returns_validation_error(self) -> None:
        response = self.client.post(
            "/api/risk/evaluate",
            json={"cycle_id": "c", "plan": {"symbol": "BTCUSDT"}, "market": {}},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "E_VALIDATION")




class TracingMigrationOnStartupTests(unittest.TestCase):
    """T13: a pre-T13 database is migrated in place at service start.

    The migration must be idempotent, keep every historical value readable and leave
    the read-only API usable (no credentials, no orders).
    """

    def test_pre_t13_database_is_migrated_in_place(self) -> None:
        import sqlite3
        import tempfile

        from workbench_service.cycles.schema import _AI_RUNS_LEGACY_DDL
        from workbench_service.db import db_path_for

        with tempfile.TemporaryDirectory() as tmp:
            db = db_path_for(Path(tmp))
            conn = sqlite3.connect(db)
            try:
                conn.executescript(_AI_RUNS_LEGACY_DDL.format(table="ai_runs"))
                conn.executescript(
                    """
                    CREATE TABLE ai_calls (
                        call_id TEXT PRIMARY KEY, ai_run_id TEXT NOT NULL,
                        cycle_id TEXT NOT NULL, role TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 1, latency_ms INTEGER,
                        timeout_ms INTEGER, status TEXT, created_at TEXT NOT NULL
                    );
                    CREATE TABLE ai_run_events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, ai_run_id TEXT NOT NULL,
                        cycle_id TEXT NOT NULL, ts TEXT NOT NULL, from_state TEXT,
                        to_state TEXT, event TEXT NOT NULL, reason_code TEXT,
                        role TEXT, payload_ref TEXT
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO ai_runs (ai_run_id, cycle_id, mode, trigger, state, "
                    "started_at, created_at, updated_at) VALUES ('legacy-1', 'cyc-l', "
                    "'FULL', 'manual', 'DONE', '2026-11-13T18:22:36Z', "
                    "'2026-11-13T18:22:36Z', '2026-11-13T18:22:37Z')",
                )
                conn.execute(
                    "INSERT INTO ai_calls (call_id, ai_run_id, cycle_id, role, attempt, "
                    "timeout_ms, created_at) VALUES ('call-l', 'legacy-1', 'cyc-l', "
                    "'analysis', 1, NULL, '2026-11-13T18:20:21Z')",
                )
                conn.execute(
                    "INSERT INTO ai_run_events (ai_run_id, cycle_id, ts, event) "
                    "VALUES ('legacy-1', 'cyc-l', '2026-11-13T18:22:37Z', 'event')",
                )
                conn.commit()
            finally:
                conn.close()

            settings = dataclasses.replace(
                Settings.from_env(data_dir=tmp),
                auth_mode="dev-open",
                api_token=None,
                auth_token_source="none",
            )
            service = build_execution_service(settings, use_env_credentials=False)
            app = make_app(settings, service)
            try:
                with TestClient(app) as client:
                    status = client.get("/api/status")
                    self.assertEqual(status.status_code, 200, status.text)
                    self.assertFalse(status.json()["credentials_configured"])
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                try:
                    columns = {
                        row[1]: row[3]
                        for row in conn.execute("PRAGMA table_info(ai_runs)").fetchall()
                    }
                    self.assertEqual(columns["started_at"], 0)
                    run = conn.execute(
                        "SELECT * FROM ai_runs WHERE ai_run_id='legacy-1'"
                    ).fetchone()
                    # History untouched by the in-place migration.
                    self.assertEqual(run["started_at"], "2026-11-13T18:22:36Z")
                    self.assertEqual(run["updated_at"], "2026-11-13T18:22:37Z")
                    self.assertIsNone(run["started_at_source"])
                    call = conn.execute(
                        "SELECT timeout_ms, timeout_source, cost_estimate FROM ai_calls "
                        "WHERE call_id='call-l'",
                    ).fetchone()
                    self.assertIsNone(call["timeout_ms"])
                    self.assertIsNone(call["timeout_source"])
                    self.assertIsNone(call["cost_estimate"])
                    event = conn.execute("SELECT event, event_source FROM ai_run_events").fetchone()
                    self.assertEqual(event["event"], "event")
                    self.assertIsNone(event["event_source"])
                finally:
                    conn.close()
            finally:
                try:
                    app.state.services.ledger.close()
                except Exception:  # noqa: BLE001
                    pass


class BearerAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=False)
        base = self.case.settings
        self.settings = dataclasses.replace(
            base, auth_mode="bearer", api_token="test-token", auth_token_source="test"
        )
        self.app = make_app(self.settings, self.case.service)
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_missing_token_is_401(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "E_AUTH_REQUIRED")

    def test_valid_token_is_200(self) -> None:
        response = self.client.get(
            "/api/status", headers={"Authorization": "Bearer test-token"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("x-auth-mode"), "bearer")

    def test_health_remains_open(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)


class _BoundedReadCase:
    """构造一个“读超时/读预算/失败冷却”可调的服务实例（离线 fake 上游）。"""

    def __init__(
        self,
        *,
        read_timeout_s: float = 0.15,
        budget_s: float = 0.4,
        cooldown_s: float = 15.0,
        delay_s: float = 0.0,
        cache_ttl_s: float = 0.0,
    ) -> None:
        from fake_demo import FakeDemoExchange

        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        base = Settings.from_env(data_dir=self.data_dir)
        self.settings = dataclasses.replace(
            base, auth_mode="dev-open", api_token=None, auth_token_source="none"
        )
        self.fake = FakeDemoExchange()
        self.fake.delay_s = delay_s
        credentials = DemoCredentials(
            api_key=self.fake.api_key, api_secret=self.fake.api_secret, source="test"
        )
        self.client = DemoBinanceClient(
            self.settings.demo_base_url,
            credentials,
            transport=self.fake.transport,
            timeout_s=12.0,
            read_timeout_s=read_timeout_s,
            clock=time.time,
        )
        self.service = build_execution_service(
            self.settings,
            client=self.client,
            credentials=credentials,
            broker_options={
                "poll_interval_s": 0.0,
                "unknown_query_interval_s": 0.0,
                "cache_ttl_s": cache_ttl_s,
                "snapshot_budget_s": budget_s,
                "failure_cooldown_s": cooldown_s,
                "clock": time.time,
            },
            use_env_credentials=False,
            clock=time.time,
        )
        self.app = make_app(self.settings, self.service)

    def api(self) -> TestClient:
        return TestClient(self.app)

    def close(self) -> None:
        try:
            self.app.state.services.ledger.close()
        except Exception:  # noqa: BLE001
            pass
        self.tmp.cleanup()


class BoundedReadTimeoutTests(unittest.TestCase):
    """T20：交易所故障时只读端点必须“有界返回 + 说清未知”，不能挂住 12s。"""

    def _assert_no_signed_write(self, case: _BoundedReadCase) -> None:
        posts = [call for call in case.fake.signed_calls if call["method"] == "POST"]
        self.assertEqual(posts, [], "只读取数不得产生任何签名写请求")
        self.assertEqual(case.fake.signed_calls_for("/fapi/v1/order", "POST"), [])

    def test_read_gets_are_bounded_but_order_post_keeps_write_timeout(self) -> None:
        case = _BoundedReadCase(read_timeout_s=3.0, budget_s=4.0, cooldown_s=15.0)
        try:
            self.assertEqual(case.service.client.read_timeout_s, 3.0)
            self.assertEqual(case.service.client.write_timeout_s, 12.0)
            api = case.api()
            with api:
                status = api.get("/api/status")
                self.assertEqual(status.status_code, 200, status.text)
                gets = [
                    call
                    for call in case.fake.calls
                    if call["method"] == "GET" and call["path"] != "/fapi/v1/time"
                ]
                self.assertTrue(gets, "状态接口应读取账户事实")
                for call in gets:
                    self.assertIsNotNone(call["timeout"], call["path"])
                    self.assertLessEqual(
                        call["timeout"]["read"], 3.01, f"{call['path']} 必须用 3s 读超时"
                    )

                approved = approve(api, cycle_id="t20-timeout")
                response = submit(api, approved)
                self.assertEqual(response.status_code, 201, response.text)
            posts = [
                call
                for call in case.fake.signed_calls
                if call["method"] == "POST" and call["path"] == "/fapi/v1/order"
            ]
            self.assertTrue(posts, "应真的下过一笔 Demo 单")
            self.assertGreaterEqual(
                posts[0]["timeout"]["read"], 11.9, "下单 POST 必须保留 12s 语义"
            )
        finally:
            case.close()

    def test_status_returns_200_stale_and_bounded_when_upstream_hangs(self) -> None:
        case = _BoundedReadCase(
            read_timeout_s=0.15, budget_s=0.4, cooldown_s=15.0, delay_s=0.5
        )
        try:
            api = case.api()
            with api:
                started = time.perf_counter()
                response = api.get("/api/status")
                elapsed = time.perf_counter() - started
                self.assertEqual(response.status_code, 200, response.text)
                self.assertLess(
                    elapsed, 3.0, f"交易所挂住时 status 必须有界返回，实际 {elapsed:.2f}s"
                )
                body = response.json()
                exchange = body["exchange"]
                self.assertEqual(exchange["source"], "none")
                self.assertFalse(exchange["reachable"])
                self.assertTrue(exchange["stale"])
                self.assertIsNone(
                    exchange["last_success_at"], "从未成功过时不能伪造最后成功时间"
                )
                self.assertTrue(exchange["unknown"], "必须说明为什么未知")
                self.assertTrue(
                    any("E_EXCHANGE_UNAVAILABLE" in reason for reason in exchange["unknown"])
                )
                self.assertIn("E_EXCHANGE_UNAVAILABLE", body["readiness"]["reasons"])
                self.assertFalse(body["readiness"]["new_entry_allowed"])
                self.assertEqual(
                    body["reconcile"].get("skipped"),
                    "E_EXCHANGE_UNAVAILABLE",
                    "交易所不可达时不应再叠加一轮对账上游请求",
                )
                # 结构完整：故障时也是同一套字段，不是 5xx/空壳
                for key in (
                    "venue",
                    "demo_only",
                    "ledger_id",
                    "equity",
                    "positions",
                    "control",
                    "income_sync",
                    "readiness",
                ):
                    self.assertIn(key, body)
                self.assertEqual(body["control"]["state"], "normal")
            self._assert_no_signed_write(case)
        finally:
            case.close()

    def test_pnl_and_positions_degrade_to_ledger_or_none(self) -> None:
        case = _BoundedReadCase(
            read_timeout_s=0.15, budget_s=0.4, cooldown_s=15.0, delay_s=0.5
        )
        try:
            api = case.api()
            with api:
                started = time.perf_counter()
                pnl = api.get("/api/pnl?range=1d")
                pnl_elapsed = time.perf_counter() - started
                self.assertEqual(pnl.status_code, 200, pnl.text)
                self.assertLess(pnl_elapsed, 3.0, f"/api/pnl 必须有界返回，实际 {pnl_elapsed:.2f}s")
                body = pnl.json()
                calibers = body["calibers"]
                self.assertIn(calibers["source"], {"ledger", "none"})
                self.assertTrue(calibers["stale"])
                self.assertTrue(calibers["unknown"], "unknown 不得为空，也不能折叠成 0")
                self.assertIsNone(calibers["wallet_balance"])
                self.assertIsNone(calibers["margin_equity"])
                self.assertIsNone(calibers["last_success_at"])
                self.assertIn("read", body)
                self.assertIsNone(body["read"]["last_success_at"])
                self.assertTrue(body["read"]["unknown"])

                started = time.perf_counter()
                positions = api.get("/api/positions")
                pos_elapsed = time.perf_counter() - started
                self.assertEqual(positions.status_code, 200, positions.text)
                self.assertLess(
                    pos_elapsed, 3.0, f"/api/positions 必须有界返回，实际 {pos_elapsed:.2f}s"
                )
                pos_body = positions.json()
                self.assertIn(pos_body["source"], {"ledger", "none"})
                self.assertTrue(pos_body["stale"])
                self.assertTrue(pos_body["unknown"])
                self.assertEqual(pos_body["count"], 0)
            self._assert_no_signed_write(case)
        finally:
            case.close()

    def test_failure_cooldown_stops_upstream_calls_and_keeps_last_success(self) -> None:
        case = _BoundedReadCase(read_timeout_s=0.15, budget_s=0.4, cooldown_s=30.0)
        try:
            api = case.api()
            with api:
                first = api.get("/api/status").json()
                self.assertEqual(first["exchange"]["source"], "exchange")
                last_success = first["exchange"]["last_success_at"]
                self.assertTrue(last_success, "成功读取后必须给出最后成功时间")
                self.assertIsNone(first["exchange"]["cooldown_until"])

                case.fake.delay_s = 0.5  # 上游开始挂住
                second = api.get("/api/status").json()
                self.assertNotEqual(second["exchange"]["source"], "exchange")
                self.assertTrue(second["exchange"]["stale"])
                self.assertEqual(
                    second["exchange"]["last_success_at"],
                    last_success,
                    "失败时显示的最后成功时间不能变成失败时间",
                )
                self.assertIsNotNone(second["exchange"]["cooldown_until"])

                calls_after_failure = len(case.fake.calls)
                for _ in range(3):
                    again = api.get("/api/status")
                    self.assertEqual(again.status_code, 200, again.text)
                    self.assertTrue(again.json()["exchange"]["stale"])
                self.assertEqual(
                    len(case.fake.calls),
                    calls_after_failure,
                    "冷却期内不得再打上游（否则每个请求都要等超时）",
                )
            self._assert_no_signed_write(case)
        finally:
            case.close()

    def test_supervisor_survives_bounded_failure_and_keeps_ticking(self) -> None:
        case = _BoundedReadCase(
            read_timeout_s=0.15, budget_s=0.4, cooldown_s=15.0, delay_s=0.5
        )
        try:
            service = case.service
            ticks = {"n": 0}
            original = service._income_sync_tick

            def _counting_tick() -> None:
                ticks["n"] += 1
                original()

            service._income_sync_tick = _counting_tick
            self.assertTrue(service.start_monitor(interval_s=1.0))
            try:
                deadline = time.time() + 8
                while time.time() < deadline and ticks["n"] < 2:
                    time.sleep(0.05)
                self.assertGreaterEqual(
                    ticks["n"], 2, "一次失败周期不得拖垮下一轮监督循环"
                )
                self.assertIsNotNone(service._monitor_thread)
                self.assertTrue(service._monitor_thread.is_alive())
            finally:
                service.stop_monitor()
        finally:
            case.close()


class ExecutionRulesVersionFenceTests(unittest.TestCase):
    """T20 第 6 条：执行侧也必须校验规则版本，不能靠 risk 侧兜底。"""

    def _strip(self, service, mutate):
        original = service.approvals.consume

        def _patched(token, *, plan=None):
            payload = original(token, plan=plan)
            mutate(payload)
            return payload

        service.approvals.consume = _patched

    def test_token_without_rules_version_is_rejected_by_execution_side(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            api = case.client()
            with api:
                approved = approve(api, cycle_id="no-rules-version")
                # 绕过 risk 侧的封装：直接抹掉 rules_version，模拟“只有执行侧能看到”
                # 的令牌；执行侧必须自己拒绝，而不是靠 risk 侧补位。
                self._strip(case.service, lambda payload: payload.pop("rules_version", None))
                response = submit(api, approved)
            self.assertEqual(response.status_code, 409, response.text)
            error = response.json()["error"]
            self.assertEqual(error["code"], "E_APPROVAL_RULES_CHANGED")
            self.assertEqual(error["details"]["reason"], "rules_version_missing")
            self.assertEqual(case.fake.positions, {}, "被拒绝的令牌不得产生任何成交")
        finally:
            case.close()

    def test_token_with_stale_rules_version_is_rejected(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            api = case.client()
            with api:
                approved = approve(api, cycle_id="stale-rules-version")
                self._strip(
                    case.service,
                    lambda payload: payload.__setitem__("rules_version", "risk.v1:deadbeef"),
                )
                response = submit(api, approved)
            self.assertEqual(response.status_code, 409, response.text)
            error = response.json()["error"]
            self.assertEqual(error["code"], "E_APPROVAL_RULES_CHANGED")
            self.assertEqual(error["details"]["reason"], "rules_version_mismatch")
            self.assertEqual(case.fake.positions, {})
        finally:
            case.close()


if __name__ == "__main__":
    unittest.main()
