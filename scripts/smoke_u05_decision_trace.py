"""U05 冒烟（离线）：真实耗时、模型降级与决策原因。

统一回归入口的冒烟形态：真正执行的是
``tests/integration/test_decision_trace.py``，本脚本只把它的退出码透传给
``scripts/run_offline_regression.py``（check=True），保证"脚本可运行即通过"。

全程离线：不联网、不读 data/.env、不构造 Broker、不启动服务。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "tests" / "integration" / "test_decision_trace.py"


def main() -> int:
    print(f"[smoke_u05_decision_trace] -> {TARGET.relative_to(PROJECT_ROOT).as_posix()}")
    return int(subprocess.run([sys.executable, str(TARGET)], cwd=PROJECT_ROOT).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
