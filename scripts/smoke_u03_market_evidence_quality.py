"""U03 / S01 冒烟（离线）：分源证据质量与 market_query 共享适配补丁。

统一回归入口的冒烟形态：真正执行的是
``tests/integration/test_market_evidence_quality.py``，本脚本只把它的退出码
透传给 ``scripts/run_offline_regression.py``（check=True）。
默认（无参数）行为在 ``run_offline_regression.py`` 中保持不变。

该测试会把 ``tests/fixtures/evidence/s01_market_query_adaptation.patch``
应用到 ``src/data/market_query.py`` 的内存副本上验证行为，不修改共享文件。
测试本身对**两种状态**都成立（U03 r4）：补丁未应用时正向应用，补丁已应用
（集成树，S01 口径已落到生产路径）时识别"已应用"并绝不二次应用。

可选参数 ``--simulate-integration-state``：真实复现集成态——把 ``src/`` 与
``tests/`` 复制到临时树，用 ``git apply`` 把该补丁**真的落到副本的
``src/data/market_query.py``** 上，再在副本里跑同一测试。用于证明"补丁已应用"
这条路径也能通过（作者 worktree 默认只能覆盖"未应用"路径）。

全程离线：不联网、不读 data/.env、不构造 Broker、不启动服务。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGET = PROJECT_ROOT / "tests" / "integration" / "test_market_evidence_quality.py"
TARGET_REL = TARGET.relative_to(PROJECT_ROOT).as_posix()
PATCH = PROJECT_ROOT / "tests" / "fixtures" / "evidence" / "s01_market_query_adaptation.patch"
APPLIED_MARKER = "_source_number"


def _run(target: Path, cwd: Path) -> int:
    return int(subprocess.run([sys.executable, str(target)], cwd=cwd).returncode)


def _simulate_integration_state() -> int:
    """在临时副本树上应用 S01 补丁后跑同一测试（模拟 I 的集成树）。"""
    work = Path(tempfile.mkdtemp(prefix="u03_integration_state_"))
    try:
        for name in ("src", "tests"):
            shutil.copytree(
                PROJECT_ROOT / name, work / name,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        target = work / TARGET_REL
        applied = subprocess.run(
            ["git", "apply", "--unsafe-paths", "--directory", str(work),
             "-p1", str(PATCH)],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True,
        )
        if applied.returncode != 0:
            print("[模拟集成态] git apply 失败，无法模拟（不静默当作通过）："
                  f"{applied.stderr.strip()}", file=sys.stderr)
            return 2
        landed = (work / "src" / "data" / "market_query.py").read_text(
            encoding="utf-8", errors="replace")
        if APPLIED_MARKER not in landed:
            print("[模拟集成态] 补丁未真正落到副本，拒绝继续（不静默当作通过）",
                  file=sys.stderr)
            return 2
        print(f"[模拟集成态] 已把 {PATCH.name} 实际应用到副本树 {work}")
        print(f"[模拟集成态] src/data/market_query.py 已含 {APPLIED_MARKER!r}；"
              f"现在在副本上跑 {TARGET_REL}")
        code = _run(target, work)
        print(f"[模拟集成态] 副本树测试退出码 = {code}")
        return code
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--simulate-integration-state" in args:
        return _simulate_integration_state()
    print(f"[smoke_u03_market_evidence_quality] -> {TARGET_REL}")
    return _run(TARGET, PROJECT_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
