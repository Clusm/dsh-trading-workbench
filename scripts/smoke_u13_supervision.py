"""U13 冒烟：托管 / 单实例 / RuntimePaths / 同 epoch 恢复包（全程离线）。

三个子步骤，全部只写临时目录、不读真实 ``.env``/``data``/密钥、不联网、
不启动服务、不下单：

1. ``tests/acceptance/test_supervision.py``（U13 验收套件，112 项断言）；
2. ``scripts/ops/verify_runtime_paths.py``（消费者清单 / 共享出口根唯一 / 锁不随代码根漂移）；
3. ``scripts/ops/verify_recovery_plan.py``（建包 → 校验 → 隔离演练 → 先对账再开仓）。

任一子步骤非零退出即整体失败。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STEPS = (
    ("acceptance", "tests/acceptance/test_supervision.py"),
    ("runtime_paths", "scripts/ops/verify_runtime_paths.py"),
    ("recovery_plan", "scripts/ops/verify_recovery_plan.py"),
)


def main() -> int:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 冒烟不得受宿主状态根影响：显式清空，走"未声明 = 旧布局"这一支。
    env.pop("MTA_STATE_ROOT", None)
    env.pop("MTA_DATA_ROOT", None)
    failures: list[str] = []
    for name, script in STEPS:
        print(f"\n=== U13 SMOKE {name} ({script}) ===", flush=True)
        proc = subprocess.run([sys.executable, script], cwd=ROOT, env=env,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        sys.stdout.write(proc.stdout or "")
        if proc.returncode != 0:
            sys.stdout.write((proc.stderr or "")[-4000:])
            failures.append(name)
    if failures:
        print(f"\nU13 SMOKE FAILED: {', '.join(failures)}")
        return 1
    print(f"\nU13 SMOKE OK steps={len(STEPS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
