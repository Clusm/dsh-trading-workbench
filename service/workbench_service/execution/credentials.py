"""Demo 凭据加载：从进程 env 或 dotenv 文件读取。

安全约束（docs/03 §3.3）：
- 凭据值**不写日志、不返回、不写库**；对外只暴露 ``source`` 与 ``fingerprint``（sha256 前 8 位）；
- 支持两组环境变量名（``WORKBENCH_DEMO_*`` 与 ``BINANCE_DEMO_*``），优先前者；
- dotenv 只解析 ``KEY=VALUE``，忽略注释/空行；不执行任何 shell；
- 文件权限允许时给出提示（POSIX 下建议 0600），但不因此拒绝启动。

复用原项目凭据（零明文复制）：
  ``WORKBENCH_DOTENV_FILE`` 可指向已有项目的 dotenv（例如 multi-agent-trader 的
  ``.env``）。本模块只从中挑 Demo key/secret 两个键，不会把整份文件注入进程环境，
  也不会在任何位置创建凭据副本。未设置该变量时行为与原先一致，仅读
  ``service/.env.local``。显式文件优先于默认文件；两者都缺失/不完整则返回 None。
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

__all__ = [
    "DemoCredentials",
    "load_demo_credentials",
    "load_live_credentials",
    "ENV_KEY_NAMES",
    "ENV_SECRET_NAMES",
    "ENV_FILE_OVERRIDE_NAME",
]

logger = logging.getLogger("workbench.credentials")

ENV_KEY_NAMES = ("WORKBENCH_DEMO_API_KEY", "BINANCE_DEMO_API_KEY")
ENV_SECRET_NAMES = ("WORKBENCH_DEMO_API_SECRET", "BINANCE_DEMO_API_SECRET")
ENV_FILE_NAME = ".env.local"
ENV_FILE_OVERRIDE_NAME = "WORKBENCH_DOTENV_FILE"
#: settings 写入的凭据目录：<data_dir>/credentials/。放 data_dir 而不放包目录，
#: 因为包安装后位于 node_modules，升级会整目录替换（凭据与轮次归档一样必须活在升级之外）。
CREDENTIALS_DIR_NAME = "credentials"
DEMO_ENV_FILE_NAME = "demo.env"
LIVE_ENV_FILE_NAME = "live.env"

# §6 M2：Live 凭据单独一组，来源显式（env 优先，其次显式指定的 live.env）。
# 绝不从 Demo 那一组“借用”，也不自动加载 .env.local —— 环境串味比缺凭据更危险。
LIVE_ENV_KEY_NAMES = ("BINANCE_LIVE_API_KEY", "WORKBENCH_LIVE_API_KEY")
LIVE_ENV_SECRET_NAMES = ("BINANCE_LIVE_API_SECRET", "WORKBENCH_LIVE_API_SECRET")
LIVE_ENV_FILE_OVERRIDE_NAME = "WORKBENCH_LIVE_ENV_FILE"
LIVE_ENV_FILE_NAME = "live.env"
_MAX_ENV_FILE_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class DemoCredentials:
    """一对 Demo key/secret；``repr`` 已脱敏。"""

    api_key: str
    api_secret: str
    source: str = "env"

    def fingerprint(self) -> str:
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:8]

    def as_public_dict(self) -> dict[str, object]:
        return {
            "configured": True,
            "source": self.source,
            "key_fingerprint": self.fingerprint(),
            "api_key_env": ENV_KEY_NAMES[0],
        }

    def __repr__(self) -> str:  # pragma: no cover - 防御性
        return f"DemoCredentials(key=**{self.fingerprint()}, source={self.source})"

    __str__ = __repr__


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        if path.stat().st_size > _MAX_ENV_FILE_BYTES:
            logger.warning("env_file_too_large path=%s (ignored)", path)
            return values
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            values[key] = value
    return values


def _check_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "env_file_permissions path=%s mode=%o (recommend 0600)", path, stat.S_IMODE(mode)
        )


def _pick_from(values: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = (values.get(name) or "").strip()
        if value:
            return value
    return None


def _credentials_dir(data_dir: Path | str | None) -> Path | None:
    """返回 <data_dir>/credentials；未给 data_dir 时返回 None（旧调用面行为不变）。"""
    if data_dir is None:
        return None
    return Path(data_dir) / CREDENTIALS_DIR_NAME


def _env_file_candidates(
    service_dir: Path,
    environ: Mapping[str, str],
    data_dir: Path | str | None = None,
) -> list[Path]:
    """显式指定的外部 dotenv 优先，其次是 settings 写入的 data_dir 凭据，最后才是 service/.env.local。"""
    candidates: list[Path] = []
    override = (environ.get(ENV_FILE_OVERRIDE_NAME) or "").strip()
    if override:
        candidates.append(Path(override).expanduser())
    credentials_dir = _credentials_dir(data_dir)
    if credentials_dir is not None:
        candidates.append(credentials_dir / DEMO_ENV_FILE_NAME)
    candidates.append(service_dir / ENV_FILE_NAME)
    return candidates


def load_live_credentials(
    service_dir: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    data_dir: Path | str | None = None,
) -> DemoCredentials | None:
    """§6 M2：返回 Live 凭据；缺失/不完整返回 ``None``。

    与 Demo 的差别（刻意保留）：
    - 只读 ``BINANCE_LIVE_*`` / ``WORKBENCH_LIVE_*`` 与显式指定的 ``live.env``；
    - **不**回退到 ``BINANCE_DEMO_*``，也不读 ``.env.local``；
    - 缺失时返回 None 由调用方决定如何报告——绝不能静默用 Demo 顶上。
    """

    environ: Mapping[str, str] = env if env is not None else os.environ

    key = _pick_from(environ, LIVE_ENV_KEY_NAMES)
    secret = _pick_from(environ, LIVE_ENV_SECRET_NAMES)
    if key and secret:
        return DemoCredentials(api_key=key, api_secret=secret, source="env")
    if bool(key) != bool(secret):
        logger.warning(
            "live_credentials_incomplete source=env (both key and secret are required)"
        )

    override = environ.get(LIVE_ENV_FILE_OVERRIDE_NAME)
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())
    else:
        # settings 写入的 data_dir 凭据优先；包内 config/ 只作旧安装兜底。
        credentials_dir = _credentials_dir(data_dir)
        if credentials_dir is not None:
            candidates.append(credentials_dir / LIVE_ENV_FILE_NAME)
        candidates.append(Path(service_dir) / "config" / LIVE_ENV_FILE_NAME)
    for env_file in candidates:
        if not env_file.is_file():
            continue
        _check_permissions(env_file)
        file_values = _read_env_file(env_file)
        file_key = _pick_from(file_values, LIVE_ENV_KEY_NAMES)
        file_secret = _pick_from(file_values, LIVE_ENV_SECRET_NAMES)
        if file_key and file_secret:
            return DemoCredentials(api_key=file_key, api_secret=file_secret, source="env_file")
        if file_key or file_secret:
            logger.warning(
                "live_credentials_incomplete source=env_file path=%s",
                env_file,
            )
    return None


def load_demo_credentials(
    service_dir: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    data_dir: Path | str | None = None,
) -> DemoCredentials | None:
    """返回 Demo 凭据；缺失/不完整时返回 ``None``（不抛异常、不打印值）。"""

    environ: Mapping[str, str] = env if env is not None else os.environ

    key = _pick_from(environ, ENV_KEY_NAMES)
    secret = _pick_from(environ, ENV_SECRET_NAMES)
    if key and secret:
        return DemoCredentials(api_key=key, api_secret=secret, source="env")
    if bool(key) != bool(secret):
        logger.warning(
            "demo_credentials_incomplete source=env (both key and secret are required)"
        )

    for env_file in _env_file_candidates(Path(service_dir), environ, data_dir):
        if not env_file.is_file():
            continue
        _check_permissions(env_file)
        file_values = _read_env_file(env_file)
        file_key = _pick_from(file_values, ENV_KEY_NAMES)
        file_secret = _pick_from(file_values, ENV_SECRET_NAMES)
        if file_key and file_secret:
            return DemoCredentials(api_key=file_key, api_secret=file_secret, source="env_file")
        if file_key or file_secret:
            logger.warning(
                "demo_credentials_incomplete source=env_file path=%s (both key and secret are required)",
                env_file,
            )
    return None
