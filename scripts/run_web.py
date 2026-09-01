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
