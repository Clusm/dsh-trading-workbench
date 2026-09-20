"""U01 收入分页/幂等/归因/会话勾稽的离线冒烟入口（全离线）。

主体在 ``tests/integration/test_income_reconciliation.py``；本脚本只做入口，
保证统一回归 ``scripts/run_offline_regression.py`` 能执行到同一份断言。
期望值来自 ``tests/fixtures/income/f10_window_sync.json`` 的独立推导。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))


def main() -> int:
    from test_income_reconciliation import main as run  # noqa: PLC0415
    return int(run())


if __name__ == "__main__":
    raise SystemExit(main())
