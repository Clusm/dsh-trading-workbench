"""启动本地 Web 控制台与后台决策循环。"""
from __future__ import annotations

import argparse
import sys
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--interval-minutes", type=float, default=None)
    parser.add_argument("--no-autostart", action="store_true", help="不自动运行决策循环")
    args = parser.parse_args()

    if args.host.strip().lower() not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("控制台当前没有用户认证，只允许绑定到本机回环地址")

    settings = load_settings(PROJECT_ROOT)
    service = TradingService(settings)
    if not args.no_autostart:
        interval = args.interval_minutes or float(settings.raw_settings.get("decision_cycle", {}).get("interval_minutes", 10))
        service.start_background(interval)
    app = create_app(service)

    print(f"console: http://{args.host}:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        service.close()


if __name__ == "__main__":
    main()
