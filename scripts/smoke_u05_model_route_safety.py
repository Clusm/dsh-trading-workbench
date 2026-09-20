"""U05/S02 冒烟（离线）：模型路由安全（静默 Mock/早返回/参数继承/跨进程额度）。

真正执行的是 ``tests/integration/test_model_route_safety.py``；本脚本只透传退出码
给 ``scripts/run_offline_regression.py``（check=True）。

全程离线：``httpx.Client`` 与子进程 runner 均在测试内被替身替换，不发网络请求。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "tests" / "integration" / "test_model_route_safety.py"


def main() -> int:
    print(f"[smoke_u05_model_route_safety] -> {TARGET.relative_to(PROJECT_ROOT).as_posix()}")
    return int(subprocess.run([sys.executable, str(TARGET)], cwd=PROJECT_ROOT).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
