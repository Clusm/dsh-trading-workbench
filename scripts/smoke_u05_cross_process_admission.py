"""U05/S04 冒烟（离线）：跨进程 LLM 准入（脱敏别名共享、互斥、崩溃回收）。

真正执行的是 ``tests/integration/test_cross_process_llm_admission.py``；本脚本只透传
退出码给 ``scripts/run_offline_regression.py``（check=True）。

全程离线：子进程用 ``sys.executable`` 起本仓代码，协调目录被指向临时沙箱
（``MTA_LLM_COORDINATION_DIR``），不触碰真实 ``data/shared/``。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "tests" / "integration" / "test_cross_process_llm_admission.py"


def main() -> int:
    print(f"[smoke_u05_cross_process_admission] -> "
          f"{TARGET.relative_to(PROJECT_ROOT).as_posix()}")
    return int(subprocess.run([sys.executable, str(TARGET)], cwd=PROJECT_ROOT).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
