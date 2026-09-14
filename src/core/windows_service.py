"""Windows Service Control Manager 托管配置的纯校验与命令构造。

这个模块不安装、启动或停止 Windows 服务。它只负责把发布配置解析成不可变对象，
并在真正交给 SCM 之前检查路径、回环绑定、退出时限和重启退避。这样部署脚本、
服务入口和离线冒烟使用同一套约束，避免出现“文档一套命令、实际进程另一套参数”的
分裂启动方式。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_FORBIDDEN_CONFIG_TOKENS = {
    "api_key", "api_secret", "secret", "token", "password", "private_key",
    "http_proxy", "https_proxy", "all_proxy", "proxy_url",
}


def _inside(root: Path, candidate: Path, *, label: str) -> Path:
    """解析路径并确保它不能跳出项目根目录。"""
    resolved_root = Path(root).resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于项目根目录内: {candidate}") from exc
    return resolved


def _as_bool(value: Any, *, label: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{label} 必须是布尔值")


def _as_int(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{label} 必须在 {minimum}..{maximum} 内")
    return value


def _assert_no_forbidden_values(value: Any, *, path: str = "config") -> None:
    """拒绝把凭据、代理或类似敏感配置带入 SCM 模板。"""
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_CONFIG_TOKENS or any(
                    token in normalized for token in ("api_key", "api_secret", "password")):
                raise ValueError(f"{path}.{key} 不允许出现在托管配置中")
            _assert_no_forbidden_values(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_forbidden_values(nested, path=f"{path}[{index}]")


@dataclass(frozen=True)
class WindowsServiceConfig:
    """一套唯一的 Windows 托管运行参数；不包含环境密钥。"""

    project_root: Path
    config_path: Path
    service_name: str
    display_name: str
    description: str
    python_executable: Path
    entrypoint: Path
    working_directory: Path
    host: str
    port: int
    interval_minutes: float | None
    no_autostart: bool
    stop_timeout_seconds: int
    restart_delay_seconds: int
    log_path: Path
    log_max_bytes: int
    log_backup_count: int

    @property
    def service_host_script(self) -> Path:
        """返回 SCM 实际调用的服务入口。"""
        return self.project_root / "scripts" / "windows_service_host.py"

    def run_arguments(self) -> list[str]:
        """返回传给统一 Web 入口的参数；不从配置注入代理或凭据。"""
        arguments = ["--host", self.host, "--port", str(self.port)]
        if self.interval_minutes is not None:
            arguments.extend(["--interval-minutes", str(self.interval_minutes)])
        if self.no_autostart:
            arguments.append("--no-autostart")
        return arguments

    def scm_bin_path(self) -> str:
        """构造 ``sc.exe create`` 的 binPath 字符串，不执行命令。"""
        command = [
            str(self.python_executable), "-u", str(self.service_host_script),
            "--config", str(self.config_path), "--service-name", self.service_name,
        ]
        return subprocess.list2cmdline(command)


def load_windows_service_config(
    config_path: Path, *, project_root: Path | None = None, require_files: bool = False,
) -> WindowsServiceConfig:
    """读取并严格校验托管模板。

    ``require_files=False`` 用于发布包/CI 只检查配置语义；本机安装前应使用
    ``require_files=True``，确保解释器和统一入口确实存在。
    """
    path = Path(config_path).resolve()
    root = Path(project_root or path.parents[1]).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"托管配置不存在: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"托管配置无法读取: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError("托管配置必须是 JSON 对象")
    _assert_no_forbidden_values(raw)

    service_name = str(raw.get("service_name") or "").strip()
    if not _SERVICE_NAME.fullmatch(service_name):
        raise ValueError("service_name 只能包含字母、数字、点、下划线和短横线")
    display_name = str(raw.get("display_name") or service_name).strip()[:256]
    description = str(raw.get("description") or "").strip()[:1024]
    host = str(raw.get("host") or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("托管服务只能绑定本机回环地址")
    port = _as_int(raw.get("port"), label="port", minimum=1, maximum=65535)
    interval_value = raw.get("interval_minutes")
    if interval_value is not None:
        if isinstance(interval_value, bool) or not isinstance(interval_value, (int, float)):
            raise ValueError("interval_minutes 必须是数字或 null")
        if not 0.1 <= float(interval_value) <= 1440:
            raise ValueError("interval_minutes 必须在 0.1..1440 内")
        interval_minutes: float | None = float(interval_value)
    else:
        interval_minutes = None
    no_autostart = _as_bool(raw.get("no_autostart", False), label="no_autostart")
    if no_autostart:
        raise ValueError("正式托管配置不得关闭自动循环")
    stop_timeout = _as_int(
        raw.get("stop_timeout_seconds"), label="stop_timeout_seconds", minimum=10, maximum=120,
    )
    restart_delay = _as_int(
        raw.get("restart_delay_seconds"), label="restart_delay_seconds", minimum=5, maximum=3600,
    )
    log_max_bytes = _as_int(
        raw.get("log_max_bytes", 10 * 1024 * 1024), label="log_max_bytes",
        minimum=1 * 1024 * 1024, maximum=1 * 1024 * 1024 * 1024,
    )
    log_backup_count = _as_int(
        raw.get("log_backup_count", 5), label="log_backup_count", minimum=1, maximum=20,
    )

    python_executable = _inside(
        root, root / str(raw.get("python_executable") or ""), label="python_executable",
    )
    entrypoint = _inside(
        root, root / str(raw.get("entrypoint") or ""), label="entrypoint",
    )
    working_directory = _inside(
        root, root / str(raw.get("working_directory") or "."), label="working_directory",
    )
    log_path = _inside(root, root / str(raw.get("log_path") or ""), label="log_path")
    data_dir = _inside(root, root / "data", label="data")
    try:
        log_path.relative_to(data_dir)
    except ValueError as exc:
        raise ValueError("log_path 必须位于 data/ 内") from exc
    expected_entrypoint = root / "scripts" / "run_web.py"
    if entrypoint != expected_entrypoint.resolve():
        raise ValueError("entrypoint 必须统一指向 scripts/run_web.py")
    if require_files:
        for candidate, label in (
            (python_executable, "python_executable"),
            (entrypoint, "entrypoint"),
            (working_directory, "working_directory"),
        ):
            if not candidate.exists():
                raise ValueError(f"{label} 不存在: {candidate}")
        if not python_executable.is_file() or not entrypoint.is_file():
            raise ValueError("python_executable 和 entrypoint 必须是文件")
        if not working_directory.is_dir():
            raise ValueError("working_directory 必须是目录")

    return WindowsServiceConfig(
        project_root=root,
        config_path=path,
        service_name=service_name,
        display_name=display_name,
        description=description,
        python_executable=python_executable,
        entrypoint=entrypoint,
        working_directory=working_directory,
        host=host,
        port=port,
        interval_minutes=interval_minutes,
        no_autostart=no_autostart,
        stop_timeout_seconds=stop_timeout,
        restart_delay_seconds=restart_delay,
        log_path=log_path,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )
