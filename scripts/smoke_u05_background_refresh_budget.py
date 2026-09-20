"""U05/S03 冒烟（离线）：后台有界刷新（HTTP/摘要总预算、分源缓存、不阻塞管理）。

真正执行的是 ``tests/integration/test_background_refresh_budget.py``；本脚本只透传
退出码给 ``scripts/run_offline_regression.py``（check=True）。

全程离线：预算与缓存断言全部走可控假时钟，无网络、无文件系统副作用。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "tests" / "integration" / "test_background_refresh_budget.py"


def main() -> int:
    print(f"[smoke_u05_background_refresh_budget] -> "
          f"{TARGET.relative_to(PROJECT_ROOT).as_posix()}")
    return int(subprocess.run([sys.executable, str(TARGET)], cwd=PROJECT_ROOT).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
