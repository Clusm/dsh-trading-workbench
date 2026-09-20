"""r3：在 HEAD 的接线补丁上做**最小改动**（保留原 CR context 与函数名后缀）。

改动：Q-NEW-3 缺省必需源非空、Q-NEW-2 轮次分量非空（共 +8 行），
随之校正 hunk 头计数/起始行，并按重建脚本复算 index post-hash。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "tests" / "fixtures" / "evidence" / "u03_orchestrator_integration.patch"

HEADER_FIXES = [
    ("@@ -680,6 +681,100 @@", "@@ -680,6 +681,108 @@"),
    ("@@ -816,6 +911,7 @@", "@@ -816,6 +919,7 @@"),
    ("@@ -906,6 +1002,10 @@", "@@ -906,6 +1010,10 @@"),
    ("@@ -1014,6 +1114,14 @@", "@@ -1014,6 +1122,14 @@"),
    ("@@ -1058,6 +1166,10 @@", "@@ -1058,6 +1174,10 @@"),
]

OLD_FLOOR = (
    "+#: 策略侧缺省必需分源（支持字段级写法，如 ``mark_funding.funding_rate``）。留空表示\n"
    "+#: \"只按意图自报的 required_sources 设防\"；策略卡就绪后在此收紧口径。\n"
    "+RESEARCH_EVIDENCE_REQUIRED_SOURCES: tuple[str, ...] = ()\n"
)
NEW_FLOOR = (
    "+#: 策略侧缺省必需分源（支持字段级写法，如 ``mark_funding.funding_rate``）。**非空 =\n"
    "+#: 字段级分源门禁在生产路径真实生效**：每笔开仓都必须有可核对的实时报价源\n"
    "+#: （源可用 + 有时点 + 未过期 + ``last_price/pct_24h/quote_volume_24h`` 一个不缺），\n"
    "+#: 否则该意图结构化拒绝（``SOURCE_UNAVAILABLE/SOURCE_STALE/SOURCE_FIELD_*``）。\n"
    "+#: **不要留空**：留空即等于该门禁整体关闭（D-P1-2 原始缺口）。策略卡就绪后可在此\n"
    "+#: 加严到资金费/OI，例如 ``(\"ticker\", \"mark_funding.funding_rate\")``；这是缺省下限，\n"
    "+#: 意图自报只能加严、不能顶掉（模块内取并集）。\n"
    "+RESEARCH_EVIDENCE_REQUIRED_SOURCES: tuple[str, ...] = (\"ticker\",)\n"
)

OLD_CYCLE = '+        cycle_id=str(getattr(decision, "pipeline_decision_id", "") or ""),\n'
NEW_CYCLE = (
    "+        # 轮次分量必须非空：pipeline_decision_id 由 decision_pipeline.sign_execution\n"
    "+        # 在 RiskGuard **之后**写入，门禁处恒为 None ⇒ 退回本轮 run_id（service 侧每轮\n"
    "+        # uuid4），使 binding_id 不跨轮碰撞、同轮可复现。\n"
    '+        cycle_id=str(getattr(decision, "pipeline_decision_id", "") or run_id or ""),\n'
)


def main() -> int:
    src = subprocess.run(
        ["git", "show", "HEAD:tests/fixtures/evidence/u03_orchestrator_integration.patch"],
        cwd=ROOT, check=True, capture_output=True).stdout.decode("utf-8")
    text = src
    for old, new in HEADER_FIXES:
        assert text.count(old) == 1, f"header 未命中: {old}"
        text = text.replace(old, new)
    for old, new in ((OLD_FLOOR, NEW_FLOOR), (OLD_CYCLE, NEW_CYCLE)):
        assert text.count(old) == 1, f"内容未命中: {old[:40]!r}"
        text = text.replace(old, new)
    PATCH.write_text(text, encoding="utf-8", newline="\n")

    # 复算 index post-hash（用 rebuild_patch 的复现口径）
    sys.path.insert(0, str(ROOT / "_scratch_author_r3"))
    import rebuild_patch as rb  # noqa: E402
    pre_lines = subprocess.run(["git", "show", "HEAD:src/agents/orchestrator.py"],
                               cwd=ROOT, check=True,
                               capture_output=True).stdout.decode("utf-8").split("\n")
    hunks = rb.parse_hunks(PATCH.read_text(encoding="utf-8"))
    post = "\n".join(rb.apply_hunks(pre_lines, hunks))
    post_blob = subprocess.run(["git", "hash-object", "--stdin"], cwd=ROOT, check=True,
                               input=post.encode("utf-8"),
                               capture_output=True).stdout.decode().strip()
    text = text.replace("index 7ac28fb..4c340da", f"index 7ac28fb..{post_blob[:7]}")
    PATCH.write_text(text, encoding="utf-8", newline="\n")
    print(f"inserted={len(post.splitlines()) - len(pre_lines)} post_blob={post_blob[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
