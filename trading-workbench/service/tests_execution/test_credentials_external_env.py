"""WORKBENCH_DOTENV_FILE：复用原项目 dotenv，零明文复制。

全部用临时文件构造，不读取、不打印任何真实凭据值。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from workbench_service.execution.credentials import (
    ENV_FILE_OVERRIDE_NAME,
    load_demo_credentials,
)


class ExternalDotenvTests(unittest.TestCase):
    def test_external_dotenv_is_used_without_copying_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service_dir = Path(tmp) / "service"
            service_dir.mkdir()
            external = Path(tmp) / "upstream" / ".env"
            external.parent.mkdir()
            external.write_text(
                "# 上游项目配置\n"
                "BINANCE_DEMO_API_KEY=k-upstream\n"
                "BINANCE_DEMO_API_SECRET=s-upstream\n"
                "EXECUTION_MODE=demo\n"
                "SOME_OTHER_KEY=unrelated\n",
                encoding="utf-8",
            )
            creds = load_demo_credentials(
                service_dir,
                env={ENV_FILE_OVERRIDE_NAME: str(external)},
            )
            assert creds is not None
            self.assertEqual(creds.api_key, "k-upstream")
            self.assertEqual(creds.api_secret, "s-upstream")
            self.assertEqual(creds.source, "env_file")
            self.assertNotIn("k-upstream", repr(creds))
            self.assertNotIn("s-upstream", repr(creds))
            public = creds.as_public_dict()
            self.assertNotIn("api_key", public)
            self.assertNotIn("api_secret", public)
            # 没有在服务目录里创建任何凭据副本
            self.assertEqual(list(service_dir.iterdir()), [])

    def test_external_file_wins_over_service_env_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service_dir = Path(tmp) / "service"
            service_dir.mkdir()
            (service_dir / ".env.local").write_text(
                "BINANCE_DEMO_API_KEY=k-local\nBINANCE_DEMO_API_SECRET=s-local\n",
                encoding="utf-8",
            )
            external = Path(tmp) / "upstream.env"
            external.write_text(
                "WORKBENCH_DEMO_API_KEY=k-external\nWORKBENCH_DEMO_API_SECRET=s-external\n",
                encoding="utf-8",
            )
            creds = load_demo_credentials(
                service_dir,
                env={ENV_FILE_OVERRIDE_NAME: str(external)},
            )
            assert creds is not None
            self.assertEqual(creds.api_key, "k-external")

    def test_incomplete_external_file_falls_back_to_service_env_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service_dir = Path(tmp) / "service"
            service_dir.mkdir()
            (service_dir / ".env.local").write_text(
                "BINANCE_DEMO_API_KEY=k-local\nBINANCE_DEMO_API_SECRET=s-local\n",
                encoding="utf-8",
            )
            external = Path(tmp) / "upstream.env"
            external.write_text("BINANCE_DEMO_API_KEY=only\n", encoding="utf-8")
            creds = load_demo_credentials(
                service_dir,
                env={ENV_FILE_OVERRIDE_NAME: str(external)},
            )
            assert creds is not None
            self.assertEqual(creds.api_key, "k-local")

    def test_missing_external_file_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service_dir = Path(tmp) / "service"
            service_dir.mkdir()
            (service_dir / ".env.local").write_text(
                "BINANCE_DEMO_API_KEY=k-local\nBINANCE_DEMO_API_SECRET=s-local\n",
                encoding="utf-8",
            )
            creds = load_demo_credentials(
                service_dir,
                env={ENV_FILE_OVERRIDE_NAME: str(Path(tmp) / "absent.env")},
            )
            assert creds is not None
            self.assertEqual(creds.api_key, "k-local")

    def test_process_env_still_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp) / "upstream.env"
            external.write_text(
                "BINANCE_DEMO_API_KEY=k-file\nBINANCE_DEMO_API_SECRET=s-file\n",
                encoding="utf-8",
            )
            creds = load_demo_credentials(
                tmp,
                env={
                    ENV_FILE_OVERRIDE_NAME: str(external),
                    "WORKBENCH_DEMO_API_KEY": "k-env",
                    "WORKBENCH_DEMO_API_SECRET": "s-env",
                },
            )
            assert creds is not None
            self.assertEqual(creds.source, "env")
            self.assertEqual(creds.api_key, "k-env")

    def test_only_demo_keys_are_picked_from_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp) / "upstream.env"
            external.write_text(
                "BINANCE_LIVE_API_KEY=must-not-be-used\n"
                "BINANCE_LIVE_API_SECRET=must-not-be-used\n",
                encoding="utf-8",
            )
            self.assertIsNone(
                load_demo_credentials(tmp, env={ENV_FILE_OVERRIDE_NAME: str(external)})
            )


if __name__ == "__main__":
    unittest.main()
