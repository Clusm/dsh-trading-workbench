"""U03 / S01 冒烟（离线）：分源证据质量与 market_query 共享适配补丁。

统一回归入口的冒烟形态：真正执行的是
``tests/integration/test_market_evidence_quality.py``，本脚本只把它的退出码
透传给 ``scripts/run_offline_regression.py``（check=True）。

该测试会把 ``tests/fixtures/evidence/s01_market_query_adaptation.patch``
应用到 ``src/data/market_query.py`` 的**内存副本**上验证行为，不修改共享文件。

全程离线：不联网、不读 data/.env、不构造 Broker、不启动服务。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "tests" / "integration" / "test_market_evidence_quality.py"


def main() -> int:
    print("[smoke_u03_market_evidence_quality] -> "
          f"{TARGET.relative_to(PROJECT_ROOT).as_posix()}")
    return int(subprocess.run([sys.executable, str(TARGET)], cwd=PROJECT_ROOT).returncode)


if __name__ == "__main__":
    raise SystemExit(main())
