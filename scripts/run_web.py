"""启动本地 Web 控制台与后台决策循环。"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import warnings
from pathlib import Path

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning  # noqa: E402
warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
# 决策循环每轮都会做特征工程，pandas 的碎片化告警会淹没控制台且没有可操作信息
warnings.filterwarnings("ignore", message="DataFrame is highly fragmented")
warnings.simplefilter("ignore", category=FutureWarning)
try:
    from pandas.errors import PerformanceWarning  # noqa: E402
    warnings.filterwarnings("ignore", category=PerformanceWarning)
except Exception:  # pragma: no cover - pandas 未安装时忽略
    pass
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 必须在任何业务模块（进而任何 httpx 调用）之前设置网络出口。
# 出口不通会导致持仓同步与行情集体超时，表现为 502 / 持仓加载失败。
from core.net_env import apply_proxy_env  # noqa: E402

_applied_proxy = apply_proxy_env()
if _applied_proxy:
    print(f"net: 出口={_applied_proxy.get('HTTP_PROXY')} no_proxy={_applied_proxy.get('NO_PROXY')}")
else:
    print("net: 未配置代理，沿用启动进程的环境变量")

import uvicorn  # noqa: E402

from core.config_loader import load_settings  # noqa: E402
from core.service import TradingService  # noqa: E402
from dashboard.app import create_app  # noqa: E402


def run_web_app(
    *, host: str, port: int | None, interval_minutes: float | None = None,
    no_autostart: bool = False, stop_event: threading.Event | None = None,
    profile: str | None = None,
) -> None:
    """运行统一 Web 入口；可由控制台或 Windows SCM 托管入口复用。

    ``stop_event`` 只由托管层设置，服务本身仍通过 Uvicorn 的有界关闭流程退出，
    从而让 TradingService 的 ``finally`` 执行资源排空、数据库关闭和实例锁释放。
    """
    settings = load_settings(PROJECT_ROOT, profile=profile)
    port = settings.console_port if port is None else port
    # U13：启动横幅显式打印状态根与共享出口根。运维必须能一眼看出"这次运行写
    # 哪份账本"（R12：换代码根不该换状态根）。settings 可能被隔离桩替换，
    # 取不到 runtime_paths 时静默跳过，不改变启动语义。
    paths = getattr(settings, "runtime_paths", None)
    if paths is not None:
        print(f"state: base={paths.state_base} declared={paths.state_base_declared} "
              f"profile={paths.profile or 'legacy'} "
              f"shared_egress={paths.shared_egress_root} lock={paths.lock_file.name}")
    service = TradingService(settings)
    if not no_autostart:
        interval = interval_minutes or float(
            settings.raw_settings.get("decision_cycle", {}).get("interval_minutes", 10))
        service.start_background(interval)
    app = create_app(service)
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning",
    ))
    if stop_event is not None:
        def request_shutdown() -> None:
            stop_event.wait()
            server.should_exit = True

        threading.Thread(
            target=request_shutdown, name="windows-service-stop-watcher", daemon=True,
        ).start()
    print(f"console: http://{host}:{port} profile={settings.profile_name or 'legacy'}")
    try:
        server.run()
    finally:
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--profile", choices=("demo", "live"), default=None)
    parser.add_argument("--interval-minutes", type=float, default=None)
    parser.add_argument("--no-autostart", action="store_true", help="不自动运行决策循环")
    parser.add_argument(
        "--state-root", default=None,
        help="显式状态根（等价于 MTA_STATE_ROOT）；换代码目录部署时用它保持同一份账本",
    )
    args = parser.parse_args()

    if args.host.strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("控制台当前没有用户认证，只允许绑定到本机回环地址")
    if args.state_root:
        # 与 MTA_STATE_ROOT 同一个注入点（RuntimePaths.resolve_state_base 的口径），
        # 在加载配置**之前**写入，保证状态根解析一次定型、不中途切换。
        os.environ["MTA_STATE_ROOT"] = str(args.state_root)

    run_web_app(
        host=args.host, port=args.port, interval_minutes=args.interval_minutes,
        no_autostart=args.no_autostart,
        profile=args.profile,
    )


if __name__ == "__main__":
    main()
