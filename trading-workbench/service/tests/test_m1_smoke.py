"""M1.0 离线冒烟测试：不访问币安，全部走本地假上游 / 临时目录。

覆盖：
- demo-only 守卫（host/scheme/path/credentials）；
- SQLite 初始化幂等与重启持久化（boot_count / ledger_id / schema_migrations）；
- 探针成功路径与 429/418/timeout 错误映射；
- 探针请求头不含任何凭据、方法只有 GET；
- /health 与 /api/ready 的字段与守卫失败原因码（不发网络请求）。
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from run_service import InstanceLock, _pid_alive  # noqa: E402
from workbench_service import security as security_mod  # noqa: E402
from workbench_service.api import market as market_api  # noqa: E402
from workbench_service.app import create_app  # noqa: E402
from workbench_service.config import Settings  # noqa: E402
from workbench_service.db import db_path_for, init_db  # noqa: E402
from workbench_service.guard import evaluate_demo_guard  # noqa: E402
from workbench_service.market_probe import probe_market  # noqa: E402

INTERVAL_MS = 900_000
FORBIDDEN_HEADERS = {"authorization", "x-mbx-apikey", "x-mbx-signature"}
NON_DEMO_BASE = "https://" + "fapi" + ".binance.com"


def _klines(now_ms: int) -> list[list[Any]]:
    base = now_ms // INTERVAL_MS * INTERVAL_MS
    rows: list[list[Any]] = []
    for offset in (2, 1, 0):
        open_ms = base - offset * INTERVAL_MS
        close_ms = open_ms + INTERVAL_MS - 1
        rows.append(
            [open_ms, "100.0", "101.0", "99.0", "100.5", "10.0", close_ms,
             "1005.0", "5", "5.0", "502.5", "0"]
        )
    return rows


class _FakeBinance(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_FakeServer"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 协议
        self.server.seen.append(
            {"path": self.path, "headers": {key.lower(): value for key, value in self.headers.items()}}
        )
        mode = self.server.mode
        if mode == "429":
            self._json(429, {"code": -1003, "msg": "too many requests"}, {"Retry-After": "60"})
            return
        if mode == "418":
            self._json(418, {"code": -1000, "msg": "banned"}, {"Retry-After": "480"})
            return
        if mode == "timeout":
            time.sleep(0.4)
            self._json(200, {})
            return

        path = self.path.split("?", 1)[0]
        now_ms = int(time.time() * 1000)
        if path == "/fapi/v1/time":
            self._json(200, {"serverTime": now_ms})
        elif path == "/fapi/v1/exchangeInfo":
            self._json(200, {
                "timezone": "UTC",
                "symbols": [{
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "contractType": "PERPETUAL",
                    "quoteAsset": "USDT",
                    "pricePrecision": 2,
                    "quantityPrecision": 3,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }],
            })
        elif path == "/fapi/v1/klines":
            self._json(200, _klines(now_ms))
        elif path == "/fapi/v1/ticker/24hr":
            self._json(200, {
                "symbol": "BTCUSDT",
                "lastPrice": "100.5",
                "priceChangePercent": "1.2",
                "highPrice": "102.0",
                "lowPrice": "98.0",
                "quoteVolume": "1234567.0",
                "count": 1000,
            })
        elif path == "/fapi/v1/premiumIndex":
            self._json(200, {
                "symbol": "BTCUSDT",
                "markPrice": "100.4",
                "indexPrice": "100.3",
                "lastFundingRate": "0.0001",
                "nextFundingTime": now_ms + 3_600_000,
            })
        elif path == "/fapi/v1/openInterest":
            self._json(200, {"symbol": "BTCUSDT", "openInterest": "12345.6", "time": now_ms})
        else:
            self._json(404, {"code": -1121, "msg": "invalid symbol"})

    def _json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:  # 静默测试日志
        return


class _FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    seen: list[dict[str, Any]]
    mode: str
    base_url: str


class _ServerFixture:
    def __init__(self) -> None:
        self.server = _FakeServer(("127.0.0.1", 0), _FakeBinance)
        self.server.seen = []
        self.server.mode = "ok"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _FakeServer:
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.server.base_url = f"http://{host}:{port}"
        return self.server

    def __exit__(self, *exc_info: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class GuardTests(unittest.TestCase):
    def test_accepts_demo_base(self) -> None:
        result = evaluate_demo_guard("https://demo-fapi.binance.com")
        self.assertTrue(result.ok)
        self.assertEqual(result.reason_code, "DEMO_GUARD_OK")

    def test_rejects_non_demo_host(self) -> None:
        result = evaluate_demo_guard(NON_DEMO_BASE)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, "E_DEMO_GUARD_HOST")

    def test_rejects_http_scheme(self) -> None:
        self.assertEqual(
            evaluate_demo_guard("http://demo-fapi.binance.com").reason_code,
            "E_DEMO_GUARD_SCHEME",
        )

    def test_rejects_credentials_and_path(self) -> None:
        self.assertEqual(
            evaluate_demo_guard("https://user:pass@demo-fapi.binance.com").reason_code,
            "E_DEMO_GUARD_CREDENTIALS",
        )
        self.assertEqual(
            evaluate_demo_guard("https://demo-fapi.binance.com/fapi/v1").reason_code,
            "E_DEMO_GUARD_PATH",
        )


class DatabaseTests(unittest.TestCase):
    def test_init_is_idempotent_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            first = init_db(data_dir, pid=111, version="test")
            self.assertTrue(first.ok, first.error)
            self.assertEqual(first.schema_version, 1)
            self.assertEqual(first.boot_count, 1)
            self.assertIsNotNone(first.ledger_id)
            self.assertEqual(first.journal_mode.lower(), "wal")
            self.assertEqual(first.migrations_applied, 1)

            second = init_db(data_dir, pid=222, version="test")
            self.assertTrue(second.ok, second.error)
            self.assertEqual(second.boot_count, 2)
            self.assertEqual(second.ledger_id, first.ledger_id)
            # migrations_applied 表示“库中累计已应用版本数”，不是本次新增数
            self.assertEqual(second.migrations_applied, 1)

            path = db_path_for(data_dir)
            self.assertTrue(path.exists())
            conn = sqlite3.connect(path)
            try:
                migrations = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                boots = conn.execute("SELECT COUNT(*) FROM service_boots").fetchone()[0]
                ledger = conn.execute("SELECT value FROM meta WHERE key='ledger_id'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(migrations, 1)
            self.assertEqual(boots, 2)
            self.assertEqual(ledger, first.ledger_id)


class ProbeTests(unittest.TestCase):
    def test_success_path_is_read_only_and_complete(self) -> None:
        with _ServerFixture() as server:
            payload = probe_market(server.base_url, timeout_s=2.0)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verdict"], "GOOD")
        self.assertFalse(payload["request"]["signed"])
        self.assertEqual(payload["request"]["auth"], "none")
        self.assertEqual(payload["request"]["method"], "GET")
        self.assertEqual(len(payload["steps"]), 6)
        self.assertTrue(all(step["ok"] for step in payload["steps"]))

        klines = next(step for step in payload["steps"] if step["name"] == "klines")
        self.assertTrue(klines["sample"]["has_live_bar"])
        self.assertEqual(klines["sample"]["closed_count"], 2)
        self.assertIsNotNone(klines["sample"]["last_closed"])

        exchange = next(step for step in payload["steps"] if step["name"] == "exchange_info")
        self.assertTrue(exchange["sample"]["symbol"]["found"])
        self.assertEqual(exchange["sample"]["symbol"]["tick_size"], "0.10")

        seen_headers = [key for item in server.seen for key in item["headers"]]
        self.assertFalse(FORBIDDEN_HEADERS.intersection(seen_headers), seen_headers)
        self.assertTrue(all(item["path"].startswith("/fapi/") for item in server.seen))

    def test_rate_limit_maps_to_reason_code(self) -> None:
        with _ServerFixture() as server:
            server.mode = "429"
            payload = probe_market(server.base_url, timeout_s=2.0)

        self.assertEqual(payload["verdict"], "BLOCKED")
        self.assertFalse(payload["ok"])
        self.assertIn("PROBE_RATE_LIMITED", payload["degradation"])
        klines = next(step for step in payload["steps"] if step["name"] == "klines")
        self.assertEqual(klines["error_code"], "PROBE_RATE_LIMITED")
        self.assertEqual(klines["retry_after_s"], 60.0)

    def test_ip_ban_maps_to_reason_code(self) -> None:
        with _ServerFixture() as server:
            server.mode = "418"
            payload = probe_market(server.base_url, timeout_s=2.0)

        self.assertEqual(payload["verdict"], "BLOCKED")
        self.assertIn("PROBE_IP_BANNED", payload["degradation"])
        time_step = next(step for step in payload["steps"] if step["name"] == "time")
        self.assertEqual(time_step["status_code"], 418)
        self.assertEqual(time_step["retry_after_s"], 480.0)

    def test_timeout_maps_to_reason_code(self) -> None:
        with _ServerFixture() as server:
            server.mode = "timeout"
            payload = probe_market(server.base_url, timeout_s=0.05)

        self.assertEqual(payload["verdict"], "BLOCKED")
        codes = {step["error_code"] for step in payload["steps"]}
        self.assertEqual(codes, {"PROBE_TIMEOUT"})


class AppTests(unittest.TestCase):
    def _settings(self, tmp: str, *, base_url: str | None = None) -> Settings:
        settings = Settings.from_env()
        overrides: dict[str, Any] = {"data_dir": Path(tmp)}
        if base_url is not None:
            overrides["demo_base_url"] = base_url
        return dataclasses.replace(settings, **overrides)

    def test_health_and_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._settings(tmp))
            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                body = health.json()
                self.assertEqual(body["service"], "trading-workbench")
                self.assertEqual(body["venue"], "demo")
                self.assertTrue(body["db"]["ok"])
                self.assertEqual(body["db"]["schema_version"], 1)
                self.assertTrue(body["ready"])
                self.assertEqual(body["guard"]["reason_code"], "DEMO_GUARD_OK")
                self.assertIn("uptime_s", body)
                self.assertEqual(health.headers.get("cache-control"), "no-store")
                self.assertTrue(health.headers.get("x-trace-id"))

                ready = client.get("/api/ready")
                self.assertEqual(ready.status_code, 200)
                payload = ready.json()
                self.assertFalse(payload["ready"])
                self.assertTrue(payload["service_ready"])
                self.assertFalse(payload["new_entry_allowed"])
                self.assertFalse(payload["credentials_configured"])
                self.assertTrue(payload["reasons"])
                self.assertIn("E_DEMO_CREDENTIALS_MISSING", payload["reasons"])
                self.assertEqual(payload["guard"]["reason_code"], "DEMO_GUARD_OK")
                self.assertEqual(ready.headers.get("x-auth-mode"), "dev-open")

    def test_guard_failure_blocks_probe_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._settings(tmp, base_url=NON_DEMO_BASE))
            with TestClient(app) as client:
                ready = client.get("/api/ready")
                self.assertEqual(ready.status_code, 200)
                payload = ready.json()
                self.assertFalse(payload["ready"])
                self.assertIn("E_DEMO_GUARD_HOST", payload["reasons"])

                probe = client.get("/api/market/probe")
                self.assertEqual(probe.status_code, 403)
                body = probe.json()
                self.assertEqual(body["error"]["code"], "E_DEMO_GUARD_HOST")
                self.assertEqual(body["error"]["details"]["base_url"], NON_DEMO_BASE)

    def test_invalid_symbol_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(self._settings(tmp))
            with TestClient(app) as client:
                response = client.get("/api/market/probe", params={"symbol": "BTC/USDT"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "E_INVALID_SYMBOL")


PROBE_STEP_NAMES = (
    "time",
    "exchange_info",
    "klines",
    "ticker_24hr",
    "premium_index",
    "open_interest",
)


def _probe_payload(
    verdict: str,
    statuses: list[int | None],
    *,
    retry_after: float | None = None,
) -> dict[str, Any]:
    """构造与 ``market_probe.probe_market`` 同形的探针结果（供 API 级测试）。"""

    steps: list[dict[str, Any]] = []
    for name, status_code in zip(PROBE_STEP_NAMES, statuses):
        step: dict[str, Any] = {
            "name": name,
            "method": "GET",
            "path": f"/fapi/v1/{name}",
            "status_code": status_code,
            "ok": status_code == 200,
            "latency_ms": 1.0,
            "sample": {},
        }
        if status_code != 200:
            if status_code == 429:
                step["error_code"] = "PROBE_RATE_LIMITED"
            elif status_code == 418:
                step["error_code"] = "PROBE_IP_BANNED"
            elif status_code is not None and status_code >= 500:
                step["error_code"] = "PROBE_HTTP_5XX"
            else:
                step["error_code"] = "PROBE_TIMEOUT"
            if retry_after is not None and status_code in (418, 429):
                step["retry_after_s"] = retry_after
        steps.append(step)

    return {
        "ok": verdict != "BLOCKED",
        "verdict": verdict,
        "base_url": "https://demo-fapi.binance.com",
        "symbol": "BTCUSDT",
        "interval": "15m",
        "limit": 30,
        "checked_at": "2026-11-09T00:00:00Z",
        "checked_at_ms": 1,
        "total_latency_ms": 5.0,
        "steps": steps,
        "degradation": [] if verdict != "BLOCKED" else ["PROBE_UPSTREAM"],
        "request": {
            "method": "GET",
            "signed": False,
            "auth": "none",
            "follow_redirects": False,
            "timeout_s": 10.0,
        },
    }


class ApiProbeMappingTests(unittest.TestCase):
    """API 级回归（t12 F-01/F-02/F-06）：缓存只在成功时生效，失败每次重新探测。"""

    def setUp(self) -> None:
        market_api._cache_clear()

    def _app(self, tmp: str, **overrides: Any):
        settings = dataclasses.replace(Settings.from_env(), data_dir=Path(tmp), **overrides)
        return create_app(settings)

    def test_all_429_returns_503_with_retry_after_and_is_not_cached(self) -> None:
        payload = _probe_payload("BLOCKED", [429] * 6, retry_after=60)
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            with mock.patch.object(
                market_api, "probe_market", side_effect=lambda *a, **k: copy.deepcopy(payload)
            ) as mocked:
                with TestClient(app) as client:
                    first = client.get("/api/market/probe")
                    second = client.get("/api/market/probe")

        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.json()["error"]["code"], "E_UPSTREAM_RATE_LIMITED")
        self.assertTrue(first.json()["error"]["retryable"])
        self.assertEqual(first.headers.get("retry-after"), "60")
        self.assertEqual(len(first.json()["error"]["details"]["steps"]), 6)

        self.assertEqual(second.status_code, 503)
        self.assertEqual(second.json()["error"]["code"], "E_UPSTREAM_RATE_LIMITED")
        self.assertEqual(second.headers.get("retry-after"), "60")
        self.assertEqual(mocked.call_count, 2, "BLOCKED 结果不得缓存，第二次必须重新探测")

    def test_ip_ban_returns_503_with_retry_after_and_is_not_cached(self) -> None:
        payload = _probe_payload("BLOCKED", [418] * 6, retry_after=480)
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            with mock.patch.object(
                market_api, "probe_market", side_effect=lambda *a, **k: copy.deepcopy(payload)
            ) as mocked:
                with TestClient(app) as client:
                    first = client.get("/api/market/probe")
                    second = client.get("/api/market/probe")

        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.json()["error"]["code"], "E_UPSTREAM_BANNED")
        self.assertFalse(first.json()["error"]["retryable"])
        self.assertEqual(first.headers.get("retry-after"), "480")
        self.assertEqual(second.status_code, 503)
        self.assertEqual(second.json()["error"]["code"], "E_UPSTREAM_BANNED")
        self.assertEqual(mocked.call_count, 2)

    def test_mixed_failure_returns_502_without_retry_after(self) -> None:
        payload = _probe_payload("BLOCKED", [None] * 6)
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            with mock.patch.object(
                market_api, "probe_market", side_effect=lambda *a, **k: copy.deepcopy(payload)
            ) as mocked:
                with TestClient(app) as client:
                    first = client.get("/api/market/probe")
                    second = client.get("/api/market/probe")

        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.json()["error"]["code"], "E_UPSTREAM_UNAVAILABLE")
        self.assertTrue(first.json()["error"]["retryable"])
        self.assertNotIn("retry-after", first.headers)
        self.assertEqual(second.status_code, 502)
        self.assertEqual(mocked.call_count, 2)

    def test_success_is_cached_on_second_call(self) -> None:
        payload = _probe_payload("GOOD", [200] * 6)
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            with mock.patch.object(
                market_api, "probe_market", side_effect=lambda *a, **k: copy.deepcopy(payload)
            ) as mocked:
                with TestClient(app) as client:
                    first = client.get("/api/market/probe")
                    second = client.get("/api/market/probe")

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["cached"])
        self.assertEqual(first.headers.get("x-auth-mode"), "dev-open")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["cached"])
        self.assertIn("cache_age_s", second.json())
        self.assertEqual(mocked.call_count, 1, "成功结果应在 TTL 内命中缓存")

    def test_cache_false_does_not_read_or_write_cache(self) -> None:
        payload = _probe_payload("GOOD", [200] * 6)
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            with mock.patch.object(
                market_api, "probe_market", side_effect=lambda *a, **k: copy.deepcopy(payload)
            ) as mocked:
                with TestClient(app) as client:
                    first = client.get("/api/market/probe", params={"cache": "false"})
                    second = client.get("/api/market/probe", params={"cache": "false"})

        self.assertFalse(first.json()["cached"])
        self.assertFalse(second.json()["cached"])
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(dict(market_api._CACHE), {}, "cache=false 不得写入缓存")

    def test_cache_false_bypasses_existing_cache_entry(self) -> None:
        payload = _probe_payload("GOOD", [200] * 6)
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp)
            with mock.patch.object(
                market_api, "probe_market", side_effect=lambda *a, **k: copy.deepcopy(payload)
            ) as mocked:
                with TestClient(app) as client:
                    client.get("/api/market/probe")
                    bypass = client.get("/api/market/probe", params={"cache": "false"})

        self.assertFalse(bypass.json()["cached"])
        self.assertEqual(mocked.call_count, 2)

    def test_bearer_mode_rejects_missing_token(self) -> None:
        payload = _probe_payload("GOOD", [200] * 6)
        with tempfile.TemporaryDirectory() as tmp:
            app = self._app(tmp, api_token="test-token")
            with mock.patch.object(
                market_api, "probe_market", side_effect=lambda *a, **k: copy.deepcopy(payload)
            ):
                with TestClient(app) as client:
                    denied = client.get("/api/market/probe")
                    allowed = client.get(
                        "/api/market/probe",
                        headers={"Authorization": "Bearer test-token"},
                    )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["error"]["code"], "E_AUTH_REQUIRED")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers.get("x-auth-mode"), "bearer")


class InstanceLockTests(unittest.TestCase):
    """单实例锁（t12 F-05）：pid/start_at、二次获取失败、崩溃后可重入。"""

    def test_acquire_blocks_second_and_release_allows_reacquire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.lock"
            first = InstanceLock(path)
            ok, payload = first.acquire(host="127.0.0.1", port=8791)
            self.assertTrue(ok)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertIn("started_at", payload)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["pid"], os.getpid())

            second = InstanceLock(path)
            ok_second, holder = second.acquire()
            self.assertFalse(ok_second)
            self.assertEqual(holder.get("pid"), os.getpid())

            first.release()
            released = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("released_at", released)

            third = InstanceLock(path)
            ok_third, _ = third.acquire()
            self.assertTrue(ok_third)
            third.release()

    def test_stale_lock_content_is_reacquirable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.lock"
            path.write_text(
                json.dumps({"pid": 999999, "started_at": "2026-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            lock = InstanceLock(path)
            ok, payload = lock.acquire()
            self.assertTrue(ok)
            self.assertEqual(payload["pid"], os.getpid())
            lock.release()

    def test_pid_alive_check(self) -> None:
        self.assertTrue(_pid_alive(os.getpid()))
        self.assertFalse(_pid_alive(999999))
        self.assertFalse(_pid_alive(0))


class ReadOnlySurfaceTests(unittest.TestCase):
    """行情/健康面保持只读（t12 F-03）。

    M1.2 起 ``/api/execution/*``、``/api/control`` 等 POST 由执行里程碑提供，
    不再属于本测试范围；这里锁定 market/health 路由不出现写方法。
    """

    def test_all_routes_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(dataclasses.replace(Settings.from_env(), data_dir=Path(tmp)))
            openapi = app.openapi()
            paths = set(openapi["paths"])
            market_paths = {
                path
                for path in paths
                if path.startswith("/api/market") or path in {"/health", "/api/ready"}
            }
            methods = {
                method.lower()
                for path in market_paths
                for method in openapi["paths"][path]
            }
            self.assertLessEqual(methods, {"get", "head", "options"}, methods)
            self.assertLessEqual(
                {"/health", "/api/ready", "/api/market/probe", "/api/market/health"},
                paths,
            )
            for path in market_paths:
                for method in openapi["paths"][path]:
                    self.assertIn(method, {"get", "head", "options"}, (path, method))

    def test_m1_auth_exception_is_documented(self) -> None:
        self.assertEqual(security_mod.AUTH_MODE_DEV_OPEN, "dev-open")
        self.assertIn("M1.1", security_mod.M1_AUTH_INCOMPLETE)
        self.assertIn("data/.api-token", security_mod.M1_AUTH_INCOMPLETE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
