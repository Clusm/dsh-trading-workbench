"""Windows SCM 正式托管入口。

默认模式只作为 Windows Service Control Manager 的服务进程运行；不会在服务注册失败
时偷偷退化成普通后台进程。``--foreground`` 仅供隔离演练和本机人工调试使用。
服务入口与普通控制台共用 ``scripts/run_web.py``，不复制 TradingService 的启动逻辑。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.windows_service import (  # noqa: E402
    WindowsServiceConfig,
    load_windows_service_config,
)


def _load_config(path: Path) -> WindowsServiceConfig:
    """加载正式配置并要求本机解释器、入口和工作目录存在。

    KI02：解释器按"可注入 + 记录来源"解析（``MTA_PYTHON`` 优先），与
    ``validate_windows_service_plan.py`` 使用同一 ``resolve_python_executable``
    口径——安装期与运行期不会解析出两个不同的解释器。

    r2 收口（KI02 回退面）：本函数是**安装期与运行期同一个入口**，
    ``allow_fallback=False`` 让"声明/显式指定的解释器不存在"**失败**，而不是
    回退到 ``sys.executable`` 后只打印一条 warn（SCM 场景下 warn 通常只落进
    服务日志，等于把"到底哪份运行时在跑"变成不可审计）。显式来源
    （``config`` / ``env`` / ``argument``）照常接受，因此 ``MTA_PYTHON``
    这条官方推荐的部署写法不受影响。
    """
    config = load_windows_service_config(
        path, project_root=PROJECT_ROOT, require_files=True, allow_fallback=False)
    if not config.python_executable_is_explicit:
        # 兜底断言：allow_fallback=False 之后仍出现猜测性来源，说明解析链被改坏；
        # 宁可拒绝启动，也不要"跑起来了但不知道是谁"。
        raise RuntimeError(
            "解释器来源不是显式声明（拒绝启动）: "
            f"{config.python_executable} (source={config.python_executable_source})")
    return config


def _configure_log(config: WindowsServiceConfig) -> Any:
    """将服务标准输出追加到 data 日志；日志文件本身不包含凭据。"""
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    if config.log_path.exists() and config.log_path.stat().st_size >= config.log_max_bytes:
        # 只在 data/ 内滚动有限份数；任何轮转失败都让服务启动失败，避免“日志不可审计”
        # 时仍被 SCM 误认为已经健康运行。
        for index in range(config.log_backup_count, 0, -1):
            source = config.log_path.with_name(f"{config.log_path.name}.{index}")
            target = config.log_path.with_name(f"{config.log_path.name}.{index + 1}")
            if index == config.log_backup_count:
                if source.exists():
                    source.unlink()
            elif source.exists():
                os.replace(source, target)
        os.replace(config.log_path, config.log_path.with_name(f"{config.log_path.name}.1"))
    stream = config.log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = stream
    sys.stderr = stream
    return stream


def _run_application(config: WindowsServiceConfig, stop_event: threading.Event) -> None:
    """调用统一 Web 入口；停止由 stop_event 转换成 Uvicorn 有界退出。"""
    scripts_dir = str(PROJECT_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from run_web import run_web_app

    run_web_app(
        host=config.host,
        port=config.port,
        interval_minutes=config.interval_minutes,
        no_autostart=config.no_autostart,
        stop_event=stop_event,
        # decl-6：把托管配置里显式声明的 profile 传给统一入口，让 SCM 托管的
        # 进程状态根/端口与配置解析结果一致（dual-profile 托管的关键一环）。
        profile=config.profile,
    )


if os.name == "nt":
    _DWORD = ctypes.c_uint32
    _SERVICE_MAIN = ctypes.WINFUNCTYPE(
        None, _DWORD, ctypes.POINTER(ctypes.c_wchar_p),
    )
    _HANDLER = ctypes.WINFUNCTYPE(None, _DWORD)

    class _ServiceStatus(ctypes.Structure):
        _fields_ = [
            ("dwServiceType", _DWORD),
            ("dwCurrentState", _DWORD),
            ("dwControlsAccepted", _DWORD),
            ("dwWin32ExitCode", _DWORD),
            ("dwServiceSpecificExitCode", _DWORD),
            ("dwCheckPoint", _DWORD),
            ("dwWaitHint", _DWORD),
        ]

    class _ServiceTableEntry(ctypes.Structure):
        _fields_ = [
            ("lpServiceName", ctypes.c_wchar_p),
            ("lpServiceProc", _SERVICE_MAIN),
        ]

    _SERVICE_WIN32_OWN_PROCESS = 0x00000010
    _SERVICE_ACCEPT_STOP = 0x00000001
    _SERVICE_CONTROL_STOP = 0x00000001
    _SERVICE_START_PENDING = 0x00000002
    _SERVICE_RUNNING = 0x00000004
    _SERVICE_STOP_PENDING = 0x00000003
    _SERVICE_STOPPED = 0x00000001
    _ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063
    _ADVAPI = ctypes.WinDLL("advapi32", use_last_error=True)
    _ADVAPI.RegisterServiceCtrlHandlerW.argtypes = [wintypes.LPCWSTR, _HANDLER]
    _ADVAPI.RegisterServiceCtrlHandlerW.restype = ctypes.c_void_p
    _ADVAPI.SetServiceStatus.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ServiceStatus)]
    _ADVAPI.SetServiceStatus.restype = wintypes.BOOL
    _ADVAPI.StartServiceCtrlDispatcherW.argtypes = [
        ctypes.POINTER(_ServiceTableEntry),
    ]
    _ADVAPI.StartServiceCtrlDispatcherW.restype = wintypes.BOOL


class _WindowsServiceRuntime:
    """保存 SCM 回调需要跨线程共享的最小状态。"""

    def __init__(self, config: WindowsServiceConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self.status_handle: Any = None
        self.status_lock = threading.Lock()
        self.handler: Any = None

    def set_status(self, state: int, *, exit_code: int = 0, wait_hint: int = 0) -> None:
        """向 SCM 报告状态；控制回调只做轻量状态更新和发停止信号。"""
        if os.name != "nt" or not self.status_handle:
            return
        status = _ServiceStatus()
        status.dwServiceType = _SERVICE_WIN32_OWN_PROCESS
        status.dwCurrentState = state
        status.dwControlsAccepted = (
            _SERVICE_ACCEPT_STOP if state in {_SERVICE_RUNNING, _SERVICE_STOP_PENDING} else 0
        )
        status.dwWin32ExitCode = int(exit_code)
        status.dwServiceSpecificExitCode = 0
        status.dwCheckPoint = 0
        status.dwWaitHint = int(wait_hint)
        with self.status_lock:
            _ADVAPI.SetServiceStatus(
                self.status_handle, ctypes.byref(status),
            )


def _run_foreground(config: WindowsServiceConfig) -> int:
    """隔离演练入口；不注册 SCM，也不创建第二套服务逻辑。"""
    stream = _configure_log(config)
    try:
        print("windows service foreground start", flush=True)
        _run_application(config, threading.Event())
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"windows service foreground failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        stream.close()


def _run_scm(config: WindowsServiceConfig) -> int:
    """注册并运行 SCM 服务；若不是由 SCM 启动则返回明确错误。"""
    if os.name != "nt":
        print("SCM 托管入口只能在 Windows 运行", file=sys.stderr)
        return 2
    runtime = _WindowsServiceRuntime(config)

    @_HANDLER
    def handler(control: int) -> None:
        if int(control) == _SERVICE_CONTROL_STOP:
            runtime.set_status(_SERVICE_STOP_PENDING, wait_hint=config.stop_timeout_seconds * 1000)
            runtime.stop_event.set()

    runtime.handler = handler

    @_SERVICE_MAIN
    def service_main(_argc: int, _argv: Any) -> None:
        runtime.status_handle = _ADVAPI.RegisterServiceCtrlHandlerW(
            config.service_name, runtime.handler,
        )
        if not runtime.status_handle:
            return
        runtime.set_status(_SERVICE_START_PENDING, wait_hint=10_000)
        stream = None
        try:
            stream = _configure_log(config)
            print("windows service start", flush=True)
            runtime.set_status(_SERVICE_RUNNING)
            _run_application(config, runtime.stop_event)
            if not runtime.stop_event.is_set():
                raise RuntimeError("Web server stopped without SCM stop request")
            runtime.set_status(_SERVICE_STOPPED)
        except Exception as exc:
            if stream is not None:
                print(f"windows service failed: {type(exc).__name__}: {exc}", flush=True)
            runtime.set_status(_SERVICE_STOPPED, exit_code=1)
        finally:
            if stream is not None:
                stream.close()

    table = (_ServiceTableEntry * 2)()
    table[0].lpServiceName = config.service_name
    table[0].lpServiceProc = service_main
    table[1].lpServiceName = None
    table[1].lpServiceProc = None
    _ADVAPI.StartServiceCtrlDispatcherW(table)
    error = ctypes.get_last_error()
    if error == _ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
        print("未由 Windows SCM 启动；请使用 --foreground 做隔离演练", file=sys.stderr)
        return 2
    if error:
        print(f"StartServiceCtrlDispatcherW failed: {error}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows SCM 托管入口（不含凭据）")
    parser.add_argument(
        "--config", type=Path, default=PROJECT_ROOT / "config" / "windows_service.json",
    )
    parser.add_argument("--service-name", default=None,
                        help="仅用于与 SCM 命令行显式绑定；默认采用配置值")
    parser.add_argument("--foreground", action="store_true",
                        help="仅隔离演练使用，不注册 SCM")
    args = parser.parse_args()
    try:
        config = _load_config(args.config)
        if args.service_name and args.service_name != config.service_name:
            raise ValueError("--service-name 与配置 service_name 不一致")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"托管配置错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.foreground:
        return _run_foreground(config)
    return _run_scm(config)


if __name__ == "__main__":
    raise SystemExit(main())
