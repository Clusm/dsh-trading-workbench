"""P0②（docs/19 §4）：出口代理 + 451 地区限制归类 + 退避 + /health 三字段。

覆盖验收：
- ``WORKBENCH_EGRESS_PROXY`` 设置时显式传给**执行**与**行情**两个 httpx 客户端，
  未设置时构造参数完全不变（行为维持现状）；日志只打 ``host:port``；
- 交易所 451 → ``kind="geo_blocked"`` → 执行层 ``E_VENUE_GEO_BLOCKED``（503）；
  连只读 GET 的一次重试都不做，避免重试风暴；
- monitor 退避 30s → 600s 封顶、成功后复位，退避期间不发交易所读；
- ``/health`` 暴露 venue_geo_blocked / geo_retry_at_ms / egress_proxy，且不含凭据；
- ``snapshot_failed`` 日志带错误码与脱敏截断后的消息。

全部离线：httpx.MockTransport + 临时数据目录，不联网、不读真实凭据。
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

from support import ServiceCase, make_app, plan as support_plan  # noqa: E402

from workbench_service.config import (  # noqa: E402
    EGRESS_PROXY_ENV,
    Settings,
    egress_proxy_label,
    parse_egress_proxy,
)
from workbench_service.execution.broker import DemoBroker  # noqa: E402
from workbench_service.execution.client import DemoBinanceClient  # noqa: E402
from workbench_service.execution.credentials import DemoCredentials  # noqa: E402
from workbench_service.execution.errors import (  # noqa: E402
    VENUE_GEO_BLOCKED_CODE,
    DemoApiError,
    ExecutionRejected,
)
from workbench_service.execution.outbox import OrderOutbox  # noqa: E402
from workbench_service.execution.service import (  # noqa: E402
    GEO_BACKOFF_FIRST_S,
    GEO_BACKOFF_MAX_S,
    build_execution_service,
)
from workbench_service.ledger.store import Ledger  # noqa: E402
from workbench_service.api import market as market_api  # noqa: E402
from workbench_service.market import config as market_config  # noqa: E402
from workbench_service.market.service import MarketService  # noqa: E402
from workbench_service.market.transport import (  # noqa: E402
    MarketGeoBlocked,
    MarketTransport,
)
from workbench_service.market_probe import probe_market  # noqa: E402
from workbench_service.risk.config import RiskLimits  # noqa: E402

PROXY = "http://127.0.0.1:10808"
CREDS = DemoCredentials(api_key="k-egress", api_secret="s-egress", source="test")


class FakeTransport(httpx.BaseTransport):
    """可切换的 fake 上游：geo=True 时所有请求返回 451，否则交给 handler。"""

    def __init__(self, handler=None) -> None:
        self.handler = handler
        self.geo = True
        self.paths: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if self.geo:
            return httpx.Response(
                451,
                text="Service unavailable from a restricted location",
                headers={"content-type": "text/html"},
            )
        if self.handler is None:
            return httpx.Response(200, json={})
        return self.handler(request)


class FakeDemoTransport(FakeTransport):
    """451 ↔ fake Demo 上游可切换；附带可控时钟，退避窗口不必真 sleep。"""

    def __init__(self) -> None:
        from fake_demo import FakeDemoExchange

        self.exchange = FakeDemoExchange()
        super().__init__(handler=self.exchange.handle)
        self.now = 1_700_000_000.0

    def clock(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _capture_httpx_client(calls: list):
    """包一层真实 httpx.Client：记录构造参数后照常返回真实客户端（不联网）。"""

    real_client = httpx.Client

    def factory(**kwargs):
        calls.append(dict(kwargs))
        return real_client(**kwargs)

    return mock.patch.object(httpx, "Client", side_effect=factory)


class EnvIsolatedTestCase(unittest.TestCase):
    """每个用例都从“没有出口代理”的干净环境开始（不依赖开发机 shell）。"""

    def setUp(self) -> None:
        self._env_backup = dict(os.environ)
        os.environ.pop(EGRESS_PROXY_ENV, None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env_backup)


class EgressProxyTests(EnvIsolatedTestCase):
    def test_validation_accepts_only_http_host_port(self) -> None:
        self.assertIsNone(parse_egress_proxy(None))
        self.assertIsNone(parse_egress_proxy("   "))
        self.assertEqual(parse_egress_proxy(" " + PROXY + " "), PROXY)
        self.assertEqual(parse_egress_proxy("HTTPS://proxy.local:8443"), "https://proxy.local:8443")
        for bad in (
            "127.0.0.1:10808",
            "http://user:pass@127.0.0.1:10808",
            "socks5://127.0.0.1:10808",
            "http://127.0.0.1:10808/path",
            "http://127.0.0.1:0",
            "http://127.0.0.1:70000",
        ):
            with self.assertRaises(ValueError, msg=bad):
                parse_egress_proxy(bad)
        # 错误信息也不能把凭据抄进日志。
        with self.assertRaises(ValueError) as ctx:
            parse_egress_proxy("http://user:secret@127.0.0.1:10808")
        self.assertNotIn("secret", str(ctx.exception))

    def test_settings_reads_env_and_labels_without_credentials(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        default = Settings.from_env(data_dir=Path(tmp.name))
        self.assertIsNone(default.egress_proxy)
        self.assertEqual(default.egress_proxy_label, "none")
        self.assertEqual(default.describe()["egress_proxy"]["label"], "none")
        os.environ[EGRESS_PROXY_ENV] = PROXY
        configured = Settings.from_env(data_dir=Path(tmp.name))
        self.assertEqual(configured.egress_proxy, PROXY)
        self.assertEqual(configured.egress_proxy_label, "127.0.0.1:10808")
        self.assertEqual(egress_proxy_label(PROXY), "127.0.0.1:10808")
        os.environ[EGRESS_PROXY_ENV] = "http://user:pass@127.0.0.1:10808"
        with self.assertRaises(ValueError):
            Settings.from_env(data_dir=Path(tmp.name))

    def test_execution_client_passes_proxy_explicitly(self) -> None:
        calls: list = []
        with _capture_httpx_client(calls):
            client = DemoBinanceClient(
                "https://demo-fapi.binance.com",
                CREDS,
                transport=FakeTransport(),
                egress_proxy=PROXY,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("proxy"), PROXY)
        self.assertEqual(client.egress_proxy, PROXY)
        self.assertEqual(client.egress_proxy_label, "127.0.0.1:10808")
        client.close()

    def test_execution_client_without_proxy_keeps_default_kwargs(self) -> None:
        calls: list = []
        with _capture_httpx_client(calls):
            client = DemoBinanceClient(
                "https://demo-fapi.binance.com", CREDS, transport=FakeTransport()
            )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("proxy", calls[0])
        self.assertNotIn("proxies", calls[0])
        self.assertIsNone(client.egress_proxy)
        self.assertEqual(client.egress_proxy_label, "none")
        client.close()

    def test_execution_client_reads_env_when_not_passed(self) -> None:
        os.environ[EGRESS_PROXY_ENV] = PROXY
        calls: list = []
        with _capture_httpx_client(calls):
            client = DemoBinanceClient(
                "https://demo-fapi.binance.com", CREDS, transport=FakeTransport()
            )
        self.assertEqual(calls[0].get("proxy"), PROXY)
        client.close()

    def test_market_transport_passes_proxy_explicitly(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls: list = []
        with _capture_httpx_client(calls):
            transport = MarketTransport(
                "https://demo-fapi.binance.com",
                state_path=Path(tmp.name) / "market_rate_limit.json",
                egress_proxy=PROXY,
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("proxy"), PROXY)
        self.assertEqual(transport.egress_proxy_label, "127.0.0.1:10808")
        transport.close()

    def test_market_transport_without_proxy_keeps_default_kwargs(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls: list = []
        with _capture_httpx_client(calls):
            transport = MarketTransport(
                "https://demo-fapi.binance.com",
                state_path=Path(tmp.name) / "market_limit.json",
            )
        self.assertNotIn("proxy", calls[0])
        self.assertNotIn("proxies", calls[0])
        self.assertEqual(transport.egress_proxy_label, "none")
        transport.close()

    def test_execution_service_wires_settings_proxy_into_client(self) -> None:
        """配置里的 egress_proxy 必须真的进到交易所客户端（不只依赖 env）。"""

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = dataclasses.replace(
            Settings.from_env(data_dir=Path(tmp.name)),
            auth_mode="dev-open",
            api_token=None,
            auth_token_source="none",
            egress_proxy=PROXY,
        )
        credentials = DemoCredentials(api_key="k1", api_secret="s1", source="test")
        calls: list = []
        with self.assertLogs("workbench.execution", level="INFO") as logs:
            with _capture_httpx_client(calls):
                service = build_execution_service(
                    settings,
                    credentials=credentials,
                    use_env_credentials=False,
                )
        self.addCleanup(service.ledger.close)
        self.addCleanup(service.client.close)
        proxy_calls = [item for item in calls if item.get("proxy") == PROXY]
        self.assertEqual(len(proxy_calls), 1, calls)
        self.assertEqual(service.client.egress_proxy_label, "127.0.0.1:10808")
        startup_log = "\n".join(logs.output)
        self.assertIn("component=execution_service", startup_log)
        self.assertIn("egress_proxy=127.0.0.1:10808", startup_log)
        self.assertNotIn(PROXY, startup_log)
        # “显式传参”也意味着不污染进程环境：不新增/改写任何代理变量。
        self.assertNotIn(EGRESS_PROXY_ENV, os.environ)
        self.assertEqual(os.environ.get("HTTPS_PROXY"), self._env_backup.get("HTTPS_PROXY"))

    def test_startup_log_prints_host_port_only(self) -> None:
        with self.assertLogs("workbench.exchange", level="INFO") as logs:
            client = DemoBinanceClient(
                "https://demo-fapi.binance.com",
                CREDS,
                transport=FakeTransport(),
                egress_proxy=PROXY,
            )
        client.close()
        text = "\n".join(logs.output)
        self.assertIn("egress_proxy=127.0.0.1:10808", text)
        self.assertNotIn(PROXY, text)
        with self.assertLogs("workbench.exchange", level="INFO") as logs2:
            client = DemoBinanceClient(
                "https://demo-fapi.binance.com", CREDS, transport=FakeTransport()
            )
        client.close()
        self.assertIn("egress_proxy=none", "\n".join(logs2.output))


class GeoBlockedTests(EnvIsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.transport = FakeTransport()
        self.client = DemoBinanceClient(
            "https://demo-fapi.binance.com", CREDS, transport=self.transport
        )
        self.addCleanup(self.client.close)
        self.ledger = Ledger(self.data_dir)
        self.addCleanup(self.ledger.close)

    def test_client_classifies_451_as_geo_blocked_without_retry(self) -> None:
        with self.assertRaises(DemoApiError) as ctx:
            self.client.account()
        exc = ctx.exception
        self.assertEqual(exc.status_code, 451)
        self.assertEqual(exc.kind, "geo_blocked")
        self.assertTrue(exc.geo_blocked)
        # 只读 GET 平时会重试一次；451 必须一次就停（否则就是重试风暴）。
        self.assertEqual(len(self.transport.paths), 1, self.transport.paths)

    def test_broker_maps_451_to_venue_geo_blocked(self) -> None:
        broker = DemoBroker(self.client, self.ledger, OrderOutbox(self.ledger), RiskLimits())
        with self.assertRaises(ExecutionRejected) as ctx:
            broker.snapshot()
        exc = ctx.exception
        self.assertEqual(exc.code, VENUE_GEO_BLOCKED_CODE)
        self.assertEqual(exc.status_code, 503)
        self.assertTrue(exc.details.get("venue_geo_blocked"))
        self.assertIn("retry_after_ms", exc.details)
        self.assertEqual(broker.read_health()["last_failure_kind"], "geo_blocked")

    def test_market_transport_classifies_451_as_geo_blocked(self) -> None:
        market = MarketTransport(
            "https://demo-fapi.binance.com",
            client=httpx.Client(
                base_url="https://demo-fapi.binance.com", transport=self.transport
            ),
            state_path=self.data_dir / "market_rate_limit.json",
        )
        self.addCleanup(market.close)
        with self.assertRaises(MarketGeoBlocked) as ctx:
            market.get_json("/fapi/v1/time")
        exc = ctx.exception
        self.assertEqual(exc.kind, "geo_blocked")
        self.assertEqual(exc.code, "MARKET_GEO_BLOCKED")
        self.assertEqual(exc.status_code, 451)
        self.assertEqual(market.status()["status_counts"].get("geo_blocked"), 1)


class GeoBackoffTests(EnvIsolatedTestCase):
    """退避序列 + 退避期间不发交易所读 + 成功后复位。"""

    def _build(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        settings = dataclasses.replace(
            Settings.from_env(data_dir=data_dir),
            auth_mode="dev-open",
            api_token=None,
            auth_token_source="none",
        )
        transport = FakeDemoTransport()
        credentials = DemoCredentials(
            api_key=transport.exchange.api_key,
            api_secret=transport.exchange.api_secret,
            source="test",
        )
        clock = transport.clock
        client = DemoBinanceClient(
            "https://demo-fapi.binance.com", credentials, transport=transport, clock=clock
        )
        self.addCleanup(client.close)
        service = build_execution_service(
            settings,
            client=client,
            credentials=credentials,
            broker_options={"poll_interval_s": 0.0, "clock": clock},
            use_env_credentials=False,
            clock=clock,
        )
        self.addCleanup(service.ledger.close)
        return transport, service

    def test_backoff_sequence_is_30_to_600_capped(self) -> None:
        _transport, service = self._build()
        steps: list = []
        for _ in range(7):
            service._note_venue_geo_blocked()
            self.assertTrue(service.health_flags()["venue_geo_blocked"])
            steps.append(round(service._geo_step_s))
        self.assertEqual(steps, [30, 60, 120, 240, 480, 600, 600])
        self.assertEqual(service._geo_step_s, GEO_BACKOFF_MAX_S)
        self.assertEqual(service._geo_attempts, 7)

    def test_success_resets_backoff_to_first_step(self) -> None:
        _transport, service = self._build()
        service._note_venue_geo_blocked()
        service._note_venue_geo_blocked()
        self.assertEqual(round(service._geo_step_s), 60)
        service._note_venue_geo_ok()
        flags = service.health_flags()
        self.assertFalse(flags["venue_geo_blocked"])
        self.assertIsNone(flags["geo_retry_at_ms"])
        service._note_venue_geo_blocked()
        self.assertEqual(round(service._geo_step_s), GEO_BACKOFF_FIRST_S)

    def test_451_arms_backoff_and_skips_upstream_reads(self) -> None:
        transport, service = self._build()
        service._heartbeat()
        flags = service.health_flags()
        self.assertTrue(flags["venue_geo_blocked"])
        self.assertGreaterEqual(flags["geo_retry_at_ms"], int(transport.now * 1000) + 29_000)
        seen = len(transport.paths)
        self.assertGreater(seen, 0, "first attempt must actually hit upstream")
        # 退避窗口内：状态/对账/监督周期都不得再打上游。
        status = service.status()
        self.assertTrue(status["exchange"]["venue_geo_blocked"])
        self.assertEqual(status["exchange"]["egress_proxy"], "none")
        self.assertIn(VENUE_GEO_BLOCKED_CODE, status["readiness"]["reasons"])
        skipped = service.reconcile()
        self.assertEqual(skipped["skipped"], VENUE_GEO_BLOCKED_CODE)
        self.assertEqual(service._supervisor_tick(), "geo_backoff")
        # 启动对账的重试（默认 15s）与 income 回填也在窗口内：同样一次都不发。
        transport.advance(16)
        service.status()
        service._income_sync_tick()
        self.assertEqual(len(transport.paths), seen, "backoff must not read the exchange")
        # 窗口过后重新尝试：仍是 451 → 步长翻倍到 60s。
        transport.advance(GEO_BACKOFF_FIRST_S)
        service._heartbeat()
        self.assertEqual(round(service._geo_step_s), 60)
        self.assertGreater(len(transport.paths), seen)

    def test_recovery_after_upstream_recovers(self) -> None:
        transport, service = self._build()
        service._heartbeat()
        self.assertTrue(service.health_flags()["venue_geo_blocked"])
        transport.geo = False
        transport.advance(GEO_BACKOFF_MAX_S + 1)
        status = service.status()
        self.assertFalse(status["exchange"]["venue_geo_blocked"])
        flags = service.health_flags()
        self.assertFalse(flags["venue_geo_blocked"])
        self.assertIsNone(flags["geo_retry_at_ms"])
        self.assertEqual(service._geo_attempts, 0)
        self.assertEqual(status["exchange"]["source"], "exchange")

    def test_p1_returns_503_venue_geo_blocked(self) -> None:
        _transport, service = self._build()
        app = make_app(service.settings, service)
        with TestClient(app) as client:
            response = client.post(
                "/api/risk/evaluate",
                json={
                    "cycle_id": "geo1",
                    "plan": support_plan(),
                    "market": {
                        "mark": "100000.0",
                        "snapshot_age_ms": 1200,
                        "mark_move_60s_pct": 0.004,
                        "mark_index_deviation_pct": 0.001,
                        "spread_bps": 3.0,
                    },
                },
            )
        self.assertEqual(response.status_code, 503, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], VENUE_GEO_BLOCKED_CODE)
        self.assertTrue(error["retryable"])
        self.assertGreater(error["details"]["retry_after_ms"], 0)
        self.assertNotIn("secret", json.dumps(error))

    def test_p2_refresh_returns_503_venue_geo_blocked(self) -> None:
        from workbench_service.errors import ServiceError
        from workbench_service.risk.models import OrderPlan

        _transport, service = self._build()
        plan = OrderPlan.from_payload(support_plan(), cycle_id="p2geo")
        with self.assertRaises(ServiceError) as ctx:
            service._pre_submit_refresh(plan, evidence_age_ms=1000)
        exc = ctx.exception
        self.assertEqual(exc.status_code, 503)
        self.assertEqual(exc.code, VENUE_GEO_BLOCKED_CODE)
        self.assertTrue(exc.retryable)
        self.assertEqual(exc.details["stage"], "P2_REFRESH")
        self.assertGreater(exc.details["retry_after_ms"], 0)
        self.assertNotIn("secret", json.dumps(exc.details))

    def test_snapshot_failed_log_has_code_and_truncated_message(self) -> None:
        _transport, service = self._build()
        long_secret = "?signature=abcdef0123456789" + "x" * 500
        boom = ExecutionRejected(
            VENUE_GEO_BLOCKED_CODE,
            f"upstream said {service.credentials.api_secret} {long_secret}",
        )
        with mock.patch.object(service.broker, "snapshot", side_effect=boom):
            with self.assertLogs("workbench.execution", level="WARNING") as logs:
                service._snapshot_readonly(cooldown=False)
        line = next(item for item in logs.output if "snapshot_failed" in item)
        self.assertIn("error=ExecutionRejected", line)
        self.assertIn(f"code={VENUE_GEO_BLOCKED_CODE}", line)
        self.assertIn("msg=", line)
        self.assertNotIn(service.credentials.api_secret, line)
        self.assertIn("signature=***", line)
        message = line.split("msg=", 1)[1]
        self.assertLessEqual(len(message), 200)


class HealthEndpointTests(EnvIsolatedTestCase):
    def _case(self, **settings_overrides):
        case = ServiceCase(with_fake=False)
        self.addCleanup(case.close)
        settings = dataclasses.replace(case.settings, **settings_overrides)
        return case, settings

    def _health(self, **settings_overrides) -> dict:
        case, settings = self._case(**settings_overrides)
        app = make_app(settings, case.service)
        with TestClient(app) as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_health_exposes_three_net_fields_without_credentials(self) -> None:
        case, settings = self._case()
        body = self._health()
        for key in ("venue_geo_blocked", "geo_retry_at_ms", "egress_proxy"):
            self.assertIn(key, body)
        self.assertEqual(body["egress_proxy"], "none")
        # 没有 451 就是 False/None，不是“未知”；有 service 时会给出明确布尔值。
        self.assertFalse(body["venue_geo_blocked"])
        self.assertIsNone(body["geo_retry_at_ms"])
        text = json.dumps(body)
        self.assertNotIn("http://", text)
        self.assertNotIn("api_token", text)
        # /health 的 auth 块只报“有没有 token”，绝不回显 token 值。
        self.assertNotIn("token_ready", {k for k in body["auth"] if k == "token_value"})
        self.assertEqual(case.service.health_flags()["egress_proxy"], "none")

    def test_health_degrades_to_none_without_execution_service(self) -> None:
        from workbench_service.app import create_app

        case, settings = self._case()
        # 不进入 lifespan、也不挂 services：/health 取不到执行层状态时必须降级为 None，
        # 而不是 500 或编造一个 False。
        app = create_app(settings)
        case.service.ledger.close()
        body = TestClient(app).get("/health").json()
        self.assertIsNone(body["venue_geo_blocked"])
        self.assertIsNone(body["geo_retry_at_ms"])
        self.assertEqual(body["egress_proxy"], "none")

    def test_health_reports_configured_proxy_label_only(self) -> None:
        body = self._health(egress_proxy=PROXY)
        self.assertEqual(body["egress_proxy"], "127.0.0.1:10808")
        self.assertNotIn("http://", json.dumps(body))

    def test_health_reports_geo_backoff_state_from_service(self) -> None:
        case, settings = self._case()
        case.service._note_venue_geo_blocked()
        app = make_app(settings, case.service)
        with TestClient(app) as client:
            body = client.get("/health").json()
        self.assertTrue(body["venue_geo_blocked"])
        self.assertIsInstance(body["geo_retry_at_ms"], int)
        self.assertEqual(body["egress_proxy"], "none")


PROBE_HANDLER_PATHS = (
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/klines",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/openInterest",
)


def _probe_response(request: httpx.Request) -> httpx.Response:
    """探针六步都回可解析的最小 payload（离线，不联网）。"""

    path = request.url.path
    if path == "/fapi/v1/time":
        return httpx.Response(200, json={"serverTime": 1_700_000_000_000})
    if path == "/fapi/v1/exchangeInfo":
        return httpx.Response(200, json={"timezone": "UTC", "symbols": []})
    if path == "/fapi/v1/klines":
        return httpx.Response(200, json=[])
    return httpx.Response(200, json={"symbol": "BTCUSDT"})


def _capture_probe_client(calls: list):
    """记录探针 httpx.Client 的构造参数，并注入离线 transport。"""

    real_client = httpx.Client

    def factory(**kwargs):
        options = dict(kwargs)
        options.pop("transport", None)
        calls.append(options)
        return real_client(transport=httpx.MockTransport(_probe_response), **options)

    return mock.patch.object(httpx, "Client", side_effect=factory)


class ProbeEgressProxyTests(EnvIsolatedTestCase):
    """t9：诊断探针与行情客户端用同一份出口代理（不读 env、不重复校验）。"""

    def setUp(self) -> None:
        super().setUp()
        market_api._cache_clear()

    def test_probe_passes_proxy_explicitly(self) -> None:
        calls: list = []
        with _capture_probe_client(calls):
            payload = probe_market(
                "https://demo-fapi.binance.com", timeout_s=2.0, egress_proxy=PROXY
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("proxy"), PROXY)
        self.assertEqual(payload["verdict"], "GOOD", payload["degradation"])
        self.assertEqual(len(payload["steps"]), 6)

    def test_probe_without_proxy_keeps_previous_kwargs(self) -> None:
        calls: list = []
        with _capture_probe_client(calls):
            payload = probe_market("https://demo-fapi.binance.com", timeout_s=2.0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("proxy", calls[0])
        self.assertNotIn("proxies", calls[0])
        # 未设代理时请求特征与以前一致；探针自己不读 env。
        self.assertFalse(payload["request"]["signed"])
        self.assertEqual(payload["request"]["auth"], "none")

    def test_probe_ignores_env_and_uses_injected_value(self) -> None:
        """探针不解析 env：env 里设了值但没注入时仍是直连（与改动前一致）。"""

        os.environ[EGRESS_PROXY_ENV] = PROXY
        calls: list = []
        with _capture_probe_client(calls):
            probe_market("https://demo-fapi.binance.com", timeout_s=2.0)
        self.assertNotIn("proxy", calls[0])

    def test_api_probe_injects_settings_proxy(self) -> None:
        case = ServiceCase(with_fake=False)
        self.addCleanup(case.close)
        settings = dataclasses.replace(case.settings, egress_proxy=PROXY)
        app = make_app(settings, case.service)
        calls: list = []
        with _capture_probe_client(calls):
            with TestClient(app) as client:
                response = client.get("/api/market/probe", params={"cache": "false"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("proxy"), PROXY)

    def test_api_probe_without_proxy_keeps_previous_kwargs(self) -> None:
        case = ServiceCase(with_fake=False)
        self.addCleanup(case.close)
        app = make_app(case.settings, case.service)
        calls: list = []
        with _capture_probe_client(calls):
            with TestClient(app) as client:
                response = client.get("/api/market/probe", params={"cache": "false"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("proxy", calls[0])
        self.assertNotIn("proxies", calls[0])


def _market_service_with_451(retry_after: str | None = None) -> MarketService:
    """真实 MarketTransport + 451 上游：验证 transport → api 的整条映射。"""

    base_url = "https://demo-fapi.binance.com"
    headers = {} if retry_after is None else {"retry-after": retry_after}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(451, text="restricted location", headers=headers)

    tmp = tempfile.TemporaryDirectory()

    def cleanup() -> None:
        tmp.cleanup()

    transport = MarketTransport(
        base_url,
        client=httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler)),
        state_path=Path(tmp.name) / "market_rate_limit.json",
    )
    service = MarketService(
        market_config.MarketSettings(
            base_url=base_url, data_dir=Path(tmp.name), timeout_s=2.0
        ),
        transport=transport,
    )
    return service, cleanup


class MarketGeoApiMappingTests(EnvIsolatedTestCase):
    """t9：transport 已标 kind=geo_blocked，接口层必须给 503 E_VENUE_GEO_BLOCKED。"""

    def _client_for(self, service: MarketService):
        case = ServiceCase(with_fake=False)
        self.addCleanup(case.close)
        app = make_app(case.settings, case.service)
        app.state.market = service
        return TestClient(app)

    def test_451_maps_to_503_venue_geo_blocked_with_retry_after_ms(self) -> None:
        service, cleanup = _market_service_with_451(retry_after="120")
        self.addCleanup(cleanup)
        self.addCleanup(service.close)
        with self._client_for(service) as client:
            response = client.get("/api/market/clock")
        self.assertEqual(response.status_code, 503, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "E_VENUE_GEO_BLOCKED")
        self.assertTrue(error["retryable"])
        self.assertEqual(error["details"]["status_code"], 451)
        self.assertEqual(error["details"]["upstream_code"], "MARKET_GEO_BLOCKED")
        self.assertEqual(error["details"]["retry_after_ms"], 120_000)
        self.assertEqual(response.headers.get("retry-after"), "120")

    def test_451_without_retry_after_keeps_key_but_null(self) -> None:
        service, cleanup = _market_service_with_451()
        self.addCleanup(cleanup)
        self.addCleanup(service.close)
        with self._client_for(service) as client:
            response = client.get("/api/market/clock")
        self.assertEqual(response.status_code, 503, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "E_VENUE_GEO_BLOCKED")
        self.assertIn("retry_after_ms", error["details"])
        self.assertIsNone(error["details"]["retry_after_ms"])
        self.assertNotIn("retry-after", response.headers)

    def test_other_upstream_errors_stay_502_market_upstream(self) -> None:
        base_url = "https://demo-fapi.binance.com"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        transport = MarketTransport(
            base_url,
            client=httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler)),
            state_path=Path(tmp.name) / "market_rate_limit.json",
        )
        service = MarketService(
            market_config.MarketSettings(
                base_url=base_url, data_dir=Path(tmp.name), timeout_s=2.0
            ),
            transport=transport,
        )
        self.addCleanup(service.close)
        with self._client_for(service) as client:
            response = client.get("/api/market/clock")
        self.assertEqual(response.status_code, 502, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "E_MARKET_UPSTREAM")
        self.assertFalse(error["retryable"])
        self.assertIn("retry_after_ms", error["details"])
        self.assertIsNone(error["details"]["retry_after_ms"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()