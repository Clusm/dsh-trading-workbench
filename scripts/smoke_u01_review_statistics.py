"""U01 复盘统计回归（RV01–RV03 + query_review_library 只读）的离线冒烟入口。

主体在 ``tests/integration/test_review_statistics.py``；全离线、只写临时目录。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "integration"))


def main() -> int:
    from test_review_statistics import main as run  # noqa: PLC0415
    return int(run())


if __name__ == "__main__":
    raise SystemExit(main())
