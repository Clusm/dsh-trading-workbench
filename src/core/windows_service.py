"""Windows Service Control Manager 托管配置的纯校验与命令构造。

这个模块不安装、启动或停止 Windows 服务。它只负责把发布配置解析成不可变对象，
并在真正交给 SCM 之前检查路径、回环绑定、退出时限和重启退避。这样部署脚本、
服务入口和离线冒烟使用同一套约束，避免出现“文档一套命令、实际进程另一套参数”的
分裂启动方式。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_SERVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_FORBIDDEN_CONFIG_TOKENS = {
    "api_key", "api_secret", "secret", "token", "password", "private_key",
    "http_proxy", "https_proxy", "all_proxy", "proxy_url",
}

#: 显式注入解释器的环境变量（优先级高于配置里的相对路径）。
PYTHON_EXECUTABLE_ENV_VAR = "MTA_PYTHON"
#: 项目内约定虚拟环境（与配置里声明的 ``.venv/...`` 同义）。
PYTHON_DEFAULT_RELATIVE = (
    ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
)
#: profile → 控制台端口。必须与 ``config_loader.Settings.console_port`` 同口径，
#: 否则 SCM 会把 ``--port`` 显式传给一个与 profile 不符的端口（decl-6）。
CONSOLE_PORT_BY_PROFILE = {"demo": 8000, "live": 8001}
ALLOWED_PROFILES = ("demo", "live")
#: 解释器回退来源标记（非 ``config`` 即为回退，部署审查必须能看到）。
PYTHON_SOURCE_CONFIG = "config"
#: **显式**来源：调用方/部署方主动指定的解释器（不是"猜"出来的）。
#: KI02 收口（r2）：安装期的"不得回退"只禁止**猜测性**来源
#: （``project-venv`` / ``virtualenv`` / ``interpreter``），不禁止显式指定，
#: 否则 ``MTA_PYTHON`` 这条官方推荐的部署写法会被误伤。
PYTHON_SOURCE_ARGUMENT = "argument"
PYTHON_SOURCE_ENV = "env"
EXPLICIT_PYTHON_SOURCES = (PYTHON_SOURCE_ARGUMENT, PYTHON_SOURCE_ENV, PYTHON_SOURCE_CONFIG)
#: **猜测性**来源（按约定猜的路径）：安装期必须拒绝这些来源。
FALLBACK_PYTHON_SOURCES = ("project-venv", "virtualenv", "interpreter")


def _inside(root: Path, candidate: Path, *, label: str) -> Path:
    """解析路径并确保它不能跳出项目根目录。"""
    resolved_root = Path(root).resolve()
    resolved = candidate.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于项目根目录内: {candidate}") from exc
    return resolved


def resolve_python_executable(
    root: Path,
    declared: Any,
    *,
    override: str | Path | None = None,
    require_files: bool = True,
    strict: bool = False,
    allow_fallback: bool = True,
    env: Any = None,
) -> tuple[Path, str]:
    """解析托管服务使用的 Python 解释器，返回 ``(路径, 来源)``。

    **KI02**：配置里的 ``python_executable`` 是**相对代码根**的路径
    （``.venv/Scripts/python.exe``）。把同一份配置放到没有 ``.venv`` 的目录
    （worktree / 只读发布目录 / CI 检出）时，硬编码校验会直接 ``ValueError``
    并中止整条离线回归。这里改为"可注入 + 可回退 + 记录来源"：

    解析顺序（``require_files=False`` 时只做路径解析，不查存在性，与升级前
    逐字节一致）：

    1. ``override`` 形参（代码级注入，测试/演练用）；
    2. ``MTA_PYTHON`` 环境变量（部署显式指定，推荐写法）；
    3. 配置里声明的相对路径（**必须仍在项目根内**，防目录穿越）；
    4. 项目内约定虚拟环境 ``.venv/...``；
    5. ``VIRTUAL_ENV`` 指定的虚拟环境；
    6. 当前解释器 ``sys.executable``。

    第 2/5/6 项来自**进程级受信来源**（不是配置文件内容），因此不受第 3 项的
    根目录边界约束；只有配置文件里的值才做 ``_inside`` 校验。

    ``strict=True`` 时禁止回退：声明的路径不存在即报错（部署前自检用）。

    r2 收口（KI02 回退面）：

    - **显式声明必须有效**：``MTA_PYTHON`` 一旦被显式设置，指向不存在的路径时
      直接 ``ValueError``——"我指定的解释器不存在"与"我没指定"是两件事，绝不
      静默落到 ``sys.executable``；
    - ``allow_fallback=False``（安装期/宿主脚本口径）只接受**显式来源**
      （``override`` / ``MTA_PYTHON`` / 配置声明），拒绝 ``project-venv`` /
      ``virtualenv`` / ``sys.executable`` 这些**猜测性**来源；全都不可用时
      ``ValueError``（失败，而不是回退后只打印一条 warn）。
    """
    environ = os.environ if env is None else env
    resolved_root = Path(root).resolve()
    declared_text = str(declared or "").strip()
    declared_path: Path | None = None
    if declared_text:
        declared_path = _inside(
            resolved_root, resolved_root / declared_text, label="python_executable",
        )
    if not require_files:
        return (declared_path or (resolved_root / PYTHON_DEFAULT_RELATIVE), PYTHON_SOURCE_CONFIG)

    candidates: list[tuple[Path, str]] = []
    if override is not None:
        candidates.append((Path(override), PYTHON_SOURCE_ARGUMENT))
    env_value = str(environ.get(PYTHON_EXECUTABLE_ENV_VAR) or "").strip()
    if env_value:
        # 显式声明但无效 → fail-closed（不静默忽略、不继续回退）。
        if not Path(env_value).is_file():
            raise ValueError(
                f"{PYTHON_EXECUTABLE_ENV_VAR} 显式指定的解释器不存在: {env_value}")
        candidates.append((Path(env_value), PYTHON_SOURCE_ENV))
    if declared_path is not None:
        candidates.append((declared_path, PYTHON_SOURCE_CONFIG))
    if allow_fallback:
        candidates.append((resolved_root / PYTHON_DEFAULT_RELATIVE, "project-venv"))
        virtual_env = str(environ.get("VIRTUAL_ENV") or "").strip()
        if virtual_env:
            candidates.append((Path(virtual_env) / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"), "virtualenv"))
        if sys.executable:
            candidates.append((Path(sys.executable), "interpreter"))

    if strict:
        if declared_path is None:
            raise ValueError("python_executable 未在托管配置中声明")
        if not declared_path.is_file():
            raise ValueError(f"python_executable 不存在（strict）: {declared_path}")
        return declared_path, PYTHON_SOURCE_CONFIG

    for candidate, source in candidates:
        if candidate.is_file():
            return candidate.resolve(), source
    tried = ", ".join(f"{path}({source})" for path, source in candidates)
    if not allow_fallback:
        # 安装期/宿主脚本：**不得**回退到"猜"出来的解释器，必须失败。
        raise ValueError(
            "解释器解析失败（安装期禁止回退到猜测性来源）: "
            f"没有任何显式声明的解释器存在；已尝试: {tried}"
        )
    raise ValueError(f"找不到可用的 python_executable；已尝试: {tried}")


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
    #: decl-6：显式 profile（``None`` = 升级前的单 profile 语义）。
    profile: str | None = None
    #: 配置里声明的原始服务名（``service_name`` 在 profile 非空时带 profile 后缀）。
    service_name_base: str = ""
    #: 解释器来源：``config`` / ``argument`` / ``env`` / ``project-venv`` /
    #: ``virtualenv`` / ``interpreter``。非 ``config`` 即发生了回退（KI02）。
    python_executable_source: str = PYTHON_SOURCE_CONFIG

    @property
    def python_executable_from_fallback(self) -> bool:
        return self.python_executable_source != PYTHON_SOURCE_CONFIG

    @property
    def python_executable_is_explicit(self) -> bool:
        """解释器是否来自**显式**指定（配置/``MTA_PYTHON``/注入参数）。

        与 :attr:`python_executable_from_fallback` 的区别：``MTA_PYTHON``
        属于显式部署指定，不算"猜出来的回退"——安装期禁止的是后者。
        """
        return self.python_executable_source in EXPLICIT_PYTHON_SOURCES

    @property
    def service_host_script(self) -> Path:
        """返回 SCM 实际调用的服务入口。"""
        return self.project_root / "scripts" / "windows_service_host.py"

    def run_arguments(self) -> list[str]:
        """返回传给统一 Web 入口的参数；不从配置注入代理或凭据。

        decl-6：显式 profile 时追加 ``--profile``，让 SCM 托管的进程与配置
        解析出的状态根/端口一致（``scripts/run_web.py`` 原生支持该参数）。
        """
        arguments = ["--host", self.host, "--port", str(self.port)]
        if self.profile:
            arguments.extend(["--profile", self.profile])
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
    python_executable: str | Path | None = None, strict_python: bool = False,
    allow_fallback: bool = True,
) -> WindowsServiceConfig:
    """读取并严格校验托管模板。

    ``require_files=False`` 用于发布包/CI 只检查配置语义；本机安装前应使用
    ``require_files=True``，确保解释器和统一入口确实存在。

    ``python_executable`` 是解释器的**显式注入点**（测试/演练/换目录部署），
    ``strict_python=True`` 则禁止回退（解释器必须恰好是配置声明的那个）。二者
    共同解决 KI02：配置里的相对 ``.venv`` 路径不应让整条离线回归硬失败。

    ``allow_fallback=False``（r2 收口）用于**安装期/宿主脚本**：只接受显式来源，
    拒绝猜测性回退（不存在的声明 → 失败，而不是回退后只打印 warn）。
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

    service_name_base = str(raw.get("service_name") or "").strip()
    if not _SERVICE_NAME.fullmatch(service_name_base):
        raise ValueError("service_name 只能包含字母、数字、点、下划线和短横线")
    profile_value = raw.get("profile")
    if profile_value is None or str(profile_value).strip() == "":
        profile: str | None = None
    else:
        profile = str(profile_value).strip().lower()
        if profile not in ALLOWED_PROFILES:
            raise ValueError(f"profile 只允许 {ALLOWED_PROFILES} 或 null")
    # decl-6：双 profile 托管必须有**唯一**服务名，否则第二个实例会覆盖第一个。
    # 用确定性的后缀，无需人工在两个配置文件之间保持同步。
    service_name = (f"{service_name_base}-{profile.capitalize()}" if profile
                    else service_name_base)
    if not _SERVICE_NAME.fullmatch(service_name):
        raise ValueError("service_name 加上 profile 后缀后不合法")
    display_name = str(raw.get("display_name") or service_name).strip()[:256]
    description = str(raw.get("description") or "").strip()[:1024]
    host = str(raw.get("host") or "").strip().lower()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("托管服务只能绑定本机回环地址")
    port = _as_int(raw.get("port"), label="port", minimum=1, maximum=65535)
    if profile is not None and port != CONSOLE_PORT_BY_PROFILE[profile]:
        # 端口必须与 profile 一致：run_web.py 收到显式 --port 时会覆盖
        # console_port，两者不一致会让"SCM 托管的是哪个 profile"变成谜。
        raise ValueError(
            f"profile={profile} 要求 port={CONSOLE_PORT_BY_PROFILE[profile]}，"
            f"配置为 {port}"
        )
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

    python_executable, python_source = resolve_python_executable(
        root, raw.get("python_executable"),
        override=python_executable, require_files=require_files, strict=strict_python,
        allow_fallback=allow_fallback,
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
    if profile == "live":
        # live 状态根是 data/live（RuntimePaths / config_loader 同口径）；
        # 两个 profile 的 SCM 实例若共用一份日志，事故复盘分不清是谁写的。
        try:
            log_path.relative_to(data_dir / "live")
        except ValueError as exc:
            raise ValueError("profile=live 的 log_path 必须位于 data/live/ 内") from exc
    expected_entrypoint = root / "scripts" / "run_web.py"
    if entrypoint != expected_entrypoint.resolve():
        raise ValueError("entrypoint 必须统一指向 scripts/run_web.py")
    if require_files:
        for candidate, label in (
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
        profile=profile,
        service_name_base=service_name_base,
        python_executable_source=python_source,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
    )
