"""M1.1 Bearer 鉴权回归：默认拒绝、token 文件、env 优先、显式 dev-open。

全部离线：临时数据目录 + FastAPI TestClient；不访问币安。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest import mock

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import run_service  # noqa: E402
from workbench_service.app import create_app  # noqa: E402
from workbench_service.config import (  # noqa: E402
    AUTH_MODE_BEARER,
    AUTH_MODE_DEV_OPEN,
    AuthTokenError,
    Settings,
    resolve_runtime_auth,
)
from workbench_service.security import install_health_auth_view  # noqa: E402
from workbench_service import security as security_mod  # noqa: E402


@contextmanager
def clean_auth_env(**values: str) -> Iterator[None]:
    """只覆盖鉴权相关变量，保留 PATH 等系统环境。"""

    env = {
        "WORKBENCH_AUTH_MODE": "",
        "WORKBENCH_API_TOKEN": "",
        "WORKBENCH_DATA_DIR": "",
    }
    env.update(values)
    with mock.patch.dict(os.environ, env, clear=False):
        yield


def make_settings(tmp: str, *, mode: str | None = None, token: str | None = None) -> Settings:
    values: dict[str, str] = {}
    if mode is not None:
        values["WORKBENCH_AUTH_MODE"] = mode
    if token is not None:
        values["WORKBENCH_API_TOKEN"] = token
    with clean_auth_env(**values):
        settings = Settings.from_env(data_dir=tmp, default_auth_mode=AUTH_MODE_BEARER)
        return resolve_runtime_auth(settings)


class TokenResolutionTests(unittest.TestCase):
    def test_default_service_mode_generates_file_and_reuses_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            self.assertEqual(settings.auth_mode, AUTH_MODE_BEARER)
            self.assertTrue(settings.bearer_required)
            self.assertTrue(settings.token_ready)
            self.assertEqual(settings.auth_token_source, "generated")
            self.assertGreaterEqual(len(settings.api_token or ""), 32)

            token_path = Path(tmp) / ".api-token"
            self.assertTrue(token_path.exists())
            first = token_path.read_text(encoding="utf-8").strip()
            self.assertEqual(first, settings.api_token)
            self.assertNotIn(first, json.dumps(settings.describe()))
            self.assertNotIn(first, repr(settings))
            self.assertNotIn(first, str(settings))

            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)

            # A fresh Settings reads the same file, source=file, no rewrite.
            again = make_settings(tmp)
            self.assertEqual(again.auth_token_source, "file")
            self.assertEqual(again.api_token, first)

    def test_env_token_wins_over_file_and_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp, token="env-token-0123456789")
            self.assertEqual(settings.api_token, "env-token-0123456789")
            self.assertEqual(settings.auth_token_source, "env")
            self.assertFalse((Path(tmp) / ".api-token").exists())

    def test_explicit_dev_open_skips_token_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp, mode=AUTH_MODE_DEV_OPEN, token="should-be-ignored")
            self.assertEqual(settings.auth_mode, AUTH_MODE_DEV_OPEN)
            self.assertFalse(settings.bearer_required)
            self.assertIsNone(settings.api_token)
            self.assertFalse((Path(tmp) / ".api-token").exists())

    def test_invalid_auth_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                with clean_auth_env(WORKBENCH_AUTH_MODE="open"):
                    Settings.from_env(data_dir=tmp, default_auth_mode=AUTH_MODE_BEARER)

    def test_unreadable_token_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / ".api-token"
            token_path.mkdir()
            with self.assertRaises(AuthTokenError):
                make_settings(tmp)

    def test_short_env_token_is_rejected_without_leaking_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AuthTokenError) as ctx:
                make_settings(tmp, token="short")
            self.assertIn("at least", str(ctx.exception))
            self.assertNotIn("short", str(ctx.exception))

    def test_concurrent_first_start_creates_one_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with clean_auth_env():
                base = Settings.from_env(data_dir=tmp, default_auth_mode=AUTH_MODE_BEARER)
            workers = 8
            barrier = threading.Barrier(workers)

            def worker(_: int) -> Settings:
                barrier.wait(timeout=5)
                return resolve_runtime_auth(base)

            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(worker, range(workers)))

            tokens = {result.api_token for result in results}
            self.assertEqual(len(tokens), 1)
            token = next(iter(tokens))
            self.assertGreaterEqual(len(token or ""), 32)
            self.assertEqual(
                sum(1 for result in results if result.auth_token_source == "generated"),
                1,
                "O_EXCL 应只有一个首启者生成 token",
            )
            self.assertEqual(
                (Path(tmp) / ".api-token").read_text(encoding="utf-8").strip(),
                token,
            )

    def test_openapi_exposes_health_and_projection_assertion_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            app = create_app(settings)
            self.assertIn("/health", app.openapi().get("paths", {}))
            self.assertTrue(install_health_auth_view(app))

    def test_health_projection_requires_auth_and_lock_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            app = create_app(settings)
            with mock.patch(
                "workbench_service.api.health.auth_status",
                return_value={"mode": AUTH_MODE_BEARER},
            ):
                self.assertFalse(install_health_auth_view(app))
            with mock.patch(
                "workbench_service.api.health.read_service_lock_status",
                return_value={"state": "absent"},
            ):
                self.assertFalse(install_health_auth_view(app))
            self.assertTrue(install_health_auth_view(app))

    def test_run_service_refuses_when_health_projection_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.from_env(data_dir=tmp, default_auth_mode=AUTH_MODE_DEV_OPEN)
            lock = mock.Mock()
            lock.path = Path(tmp) / "service.lock"
            lock.acquire.return_value = (True, {"pid": os.getpid()})
            guard = SimpleNamespace(
                ok=True,
                reason_code="DEMO_GUARD_OK",
                detail="demo guard passed",
                base_url=settings.demo_base_url,
            )
            with mock.patch.object(run_service.Settings, "from_env", return_value=settings), \
                    mock.patch.object(run_service, "configure_logging", return_value=None), \
                    mock.patch.object(run_service, "evaluate_demo_guard", return_value=guard), \
                    mock.patch.object(run_service, "InstanceLock", return_value=lock), \
                    mock.patch.object(run_service, "_port_is_free", return_value=True), \
                    mock.patch.object(run_service, "create_app", return_value=object()), \
                    mock.patch.object(run_service, "install_health_auth_view", return_value=False), \
                    mock.patch.object(run_service.uvicorn, "run") as uvicorn_run:
                code = run_service.main(["--port", "8795", "--data-dir", tmp])

            self.assertEqual(code, run_service.EXIT_CONFIG)
            uvicorn_run.assert_not_called()
            lock.release.assert_called_once()

    def test_run_service_resolves_token_after_instance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.from_env(data_dir=tmp, default_auth_mode=AUTH_MODE_DEV_OPEN)
            order: list[str] = []
            lock = mock.Mock()
            lock.path = Path(tmp) / "service.lock"

            def acquire(**_: object) -> tuple[bool, dict[str, object]]:
                order.append("lock")
                return True, {"pid": os.getpid()}

            def resolve(current: Settings) -> Settings:
                order.append("auth")
                return current

            lock.acquire.side_effect = acquire
            guard = SimpleNamespace(
                ok=True,
                reason_code="DEMO_GUARD_OK",
                detail="demo guard passed",
                base_url=settings.demo_base_url,
            )
            with mock.patch.object(run_service.Settings, "from_env", return_value=settings), \
                    mock.patch.object(run_service, "configure_logging", return_value=None), \
                    mock.patch.object(run_service, "evaluate_demo_guard", return_value=guard), \
                    mock.patch.object(run_service, "InstanceLock", return_value=lock), \
                    mock.patch.object(run_service, "_port_is_free", return_value=True), \
                    mock.patch.object(run_service, "resolve_runtime_auth", side_effect=resolve), \
                    mock.patch.object(run_service, "create_app", return_value=object()), \
                    mock.patch.object(run_service, "install_health_auth_view", return_value=False), \
                    mock.patch.object(run_service.uvicorn, "run"):
                code = run_service.main(["--port", "8796", "--data-dir", tmp])

            self.assertEqual(code, run_service.EXIT_CONFIG)
            self.assertEqual(order[:2], ["lock", "auth"])


class AuthEndpointTests(unittest.TestCase):
    def _app(self, settings: Settings):
        app = create_app(settings)
        self.assertTrue(install_health_auth_view(app))
        return app

    def _client(self, settings: Settings) -> TestClient:
        return TestClient(self._app(settings))

    def test_health_and_root_are_public_but_api_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            token = settings.api_token or ""
            app = self._app(settings)
            with TestClient(app) as client:
                health = client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertNotIn(token, health.text)
                self.assertEqual(health.json()["auth"]["mode"], AUTH_MODE_BEARER)
                self.assertTrue(health.json()["auth"]["token_ready"])
                self.assertFalse(health.json()["auth"]["m1_incomplete"])
                self.assertIn("lock", health.json())

                root = client.get("/")
                self.assertEqual(root.status_code, 200)
                self.assertNotIn(token, root.text)

                denied = client.get("/api/ready")
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(denied.json()["error"]["code"], "E_AUTH_REQUIRED")

                wrong = client.get("/api/ready", headers={"Authorization": "Bearer wrong"})
                self.assertEqual(wrong.status_code, 401)
                self.assertEqual(wrong.json()["error"]["code"], "E_AUTH_REQUIRED")

                allowed = client.get("/api/ready", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(allowed.status_code, 200)
                self.assertTrue(allowed.json()["service_ready"])
                self.assertFalse(allowed.json()["ready"])
                self.assertFalse(allowed.json()["new_entry_allowed"])
                self.assertTrue(allowed.json()["reasons"])
                self.assertEqual(allowed.headers.get("x-auth-mode"), "bearer")

                probe_denied = client.get("/api/market/probe")
                self.assertEqual(probe_denied.status_code, 401)

    def test_bearer_mode_without_token_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with clean_auth_env():
                settings = Settings.from_env(data_dir=tmp, default_auth_mode=AUTH_MODE_BEARER)
            app = self._app(settings)
            with TestClient(app) as client:
                response = client.get("/api/ready")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "E_AUTH_NOT_CONFIGURED")
                health = client.get("/health")
                self.assertFalse(health.json()["auth"]["token_ready"])

    def test_explicit_dev_open_allows_api_and_reports_m1_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp, mode=AUTH_MODE_DEV_OPEN)
            app = self._app(settings)
            with TestClient(app) as client:
                response = client.get("/api/ready")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("x-auth-mode"), "dev-open")
                auth = client.get("/health").json()["auth"]
                self.assertEqual(auth["mode"], AUTH_MODE_DEV_OPEN)
                self.assertTrue(auth["m1_incomplete"])
                self.assertIn("M1.1", auth["m1_target"])
                self.assertIn("data/.api-token", auth["m1_target"])

    def test_token_is_never_in_service_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            token = settings.api_token or ""
            with self.assertLogs(level="INFO") as captured:
                with TestClient(self._app(settings)) as client:
                    client.get("/api/ready", headers={"Authorization": f"Bearer {token}"})
            self.assertNotIn(token, "\n".join(captured.output))
            self.assertNotIn(token, json.dumps(settings.describe()))

    def test_auth_status_shape_has_no_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            app = self._app(settings)
            with TestClient(app) as client:
                status = client.get("/health").json()["auth"]
            self.assertEqual(
                set(status),
                {
                    "mode",
                    "bearer_required",
                    "token_ready",
                    "token_source",
                    "token_path",
                    "m1_incomplete",
                    "m1_target",
                },
            )
            self.assertNotIn("token", status)
            self.assertIsNone(status["m1_target"])


if __name__ == "__main__":
    unittest.main()
