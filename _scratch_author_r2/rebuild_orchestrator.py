"""在 orchestrator.py 原文件上做 U03 接线插入，保留原行结尾、新增行用 LF。

流程：把原文件按 LF 视角做纯插入编辑 → SequenceMatcher 对齐 → 未变行沿用**原始
字节**（保留混用的 CRLF/LF），新增行用 LF 写出。这样 git diff 只有纯插入，
补丁可干净应用（`git apply --check`），且不改动未涉及的行。
"""
from __future__ import annotations

import ast
import difflib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "agents" / "orchestrator.py"
ORIG = ROOT / "_scratch_author_r2" / "orchestrator.py.orig"

GATE = '''#: U15 未接线前：研究证据门禁在"身份未固定"下的放行开关。**缺省 False = fail-closed**
#: ——未固定身份的 OPEN 一律丢单；只有显式置 True（仅限 paper/离线）才允许在
#: identity_pinned=False 下开仓，且每轮都会写一条审计事件留痕。
ALLOW_UNPINNED_RESEARCH_EVIDENCE = False

#: 策略侧缺省必需分源（支持字段级写法，如 ``mark_funding.funding_rate``）。留空表示
#: "只按意图自报的 required_sources 设防"；策略卡就绪后在此收紧口径。
RESEARCH_EVIDENCE_REQUIRED_SOURCES: tuple[str, ...] = ()


def _research_evidence_gate(decision: Any, snapshots: dict[Any, Any],
                            db: AuditDatabase, run_id: str) -> tuple[Any, list[str]]:
    """U03 逐意图证据门禁：只丢证据不足/数值矛盾的开仓意图，退出不受影响。

    - claim 由程序比较真实快照数值（744.13>744.9、78550<78443 确定性拒绝）；
      模型自报 claim 只能附加，不能替换程序基准、也不能用自报 claim 顶掉
      "开仓必须给出可解析入场价"。
    - 身份未固定（U15 未接线）时按 ALLOW_UNPINNED_RESEARCH_EVIDENCE fail-closed。
    - 放行路径把逐意图 (evidence_id, binding_id) 写进审计事件，执行侧可证明
      "这一单的证据是哪个"；把绑定落到订单字段属 K02 README §12 C#12 / U05 缺口。
    - 只做减量：不改风险档/白名单/live 开关，不动 pipeline_authorized（授权位由
      core.decision_pipeline 统一置位，避免误报"未授权开仓"critical 事件）。
    """
    open_indexes = [index for index, item in enumerate(decision.intents)
                    if getattr(item, "action", None) == "open"]
    if not open_indexes:
        return decision, []

    def _binding_key(index: int, item: Any) -> str:
        # client_order_id 是执行集合里唯一稳定的逐单键（风险层 model_copy 会保留；
        # 位置序号在缩减数量/跳过失败项后不再可靠），缺失时退回本轮序号。
        return str(getattr(item, "client_order_id", None) or f"open[{index}]")

    payloads: list[dict[str, Any]] = []
    for index in open_indexes:
        item = decision.intents[index]
        payload = item.model_dump(mode="json")
        payload["intent_id"] = _binding_key(index, item)
        evidence = getattr(item, "evidence", None)
        payload["interval"] = getattr(evidence, "confirmation_interval", None)
        payloads.append(payload)
    report = evaluate_open_intents(
        payloads, list(snapshots.values()), now_ms=int(time.time() * 1000),
        cycle_id=str(getattr(decision, "pipeline_decision_id", "") or ""),
        identity=None,  # TODO(U15): 接入 RuntimeIdentity 后改为传入真实身份
        allow_unpinned=ALLOW_UNPINNED_RESEARCH_EVIDENCE,
        default_required_sources=RESEARCH_EVIDENCE_REQUIRED_SOURCES,
    )
    accepted_bindings = [item for item in report.bindings() if item["accepted"]]
    if accepted_bindings:
        db.log_risk_event(
            event_type="research_evidence_binding", severity="info",
            details="逐意图证据绑定: " + "；".join(
                f"{item['intent_id']}={item['evidence_id']}"
                for item in accepted_bindings),
            action_taken="passed open intents carry evidence binding",
            raw={"run_id": run_id, "identity_pinned": report.identity_pinned,
                 "unpinned_allowed": report.unpinned_allowed,
                 "bindings": accepted_bindings},
        )
    blocked = {entry.intent_id for entry in report.intents if not entry.accepted}
    if not blocked:
        return decision, []
    blocked_symbols: list[str] = []
    kept: list[Any] = []
    for index, item in enumerate(decision.intents):
        if getattr(item, "action", None) != "open":
            kept.append(item)
            continue
        if _binding_key(index, item) in blocked:
            blocked_symbols.append(str(getattr(item, "symbol", "")))
        else:
            kept.append(item)
    db.log_risk_event(
        event_type="research_evidence_blocked", severity="critical",
        details="；".join(
            f"{entry.intent_id}:{','.join(entry.reason_codes)}"
            for entry in report.intents if not entry.accepted
        ),
        action_taken="drop open intents; preserve exits",
        raw={"run_id": run_id, "blocked": sorted(blocked),
             "identity_pinned": report.identity_pinned,
             "unpinned_allowed": report.unpinned_allowed},
    )
    action = ("close" if any(item.action in {"close", "reduce"} for item in kept)
              else "adjust" if kept else "no_trade")
    note = (" | [U03] 逐意图证据门禁拒绝开仓: " + ",".join(blocked_symbols)
            + ("" if report.identity_pinned else " (identity_pinned=False)"))
    return decision.model_copy(update={
        "intents": kept, "decision": action,
        "reasoning": (decision.reasoning or "") + note,
    }), []


'''

EDITS: list[tuple[str, str]] = [
    ("    return issues\n\n\ndef _build_decision(",
     "    return issues\n\n\n" + GATE + "def _build_decision("),
    ("    query_evidence: dict[str, dict[str, Any]] = {}\n    query_details:",
     "    query_evidence: dict[str, dict[str, Any]] = {}\n"
     "    query_snapshots: dict[tuple[str, str], Any] = {}\n    query_details:"),
    ("                                    query_details[symbol] = dict(result)\n"
     "                                    research_detail_done = True",
     "                                    query_details[symbol] = dict(result)\n"
     "                                    # U03：保留本轮快照，供逐意图证据门禁按\n"
     "                                    # (symbol, interval) 独立建包（15m 不得冒充 4h）。\n"
     "                                    # 出参缺周期标签就不建快照（fail closed，不猜周期）。\n"
     "                                    _interval = str(result.get(\"interval\") or \"\").strip()\n"
     "                                    if _interval:\n"
     "                                        query_snapshots[(symbol, _interval)] = (\n"
     "                                            snapshot_from_tool_result(\n"
     "                                                result, interval=_interval))\n"
     "                                    research_detail_done = True"),
    ("                        decision, issues = _build_decision(args, strict=strict)\n"
     "                        if not issues:\n"
     "                            issues = _check_open_query_provenance("
     "decision.intents, query_evidence)\n"
     "                            if issues and strict:",
     "                        decision, issues = _build_decision(args, strict=strict)\n"
     "                        if not issues:\n"
     "                            issues = _check_open_query_provenance("
     "decision.intents, query_evidence)\n"
     "                            if not issues:\n"
     "                                decision, issues = _research_evidence_gate(\n"
     "                                    decision, query_snapshots, db, run_id,\n"
     "                                )\n"
     "                            if issues and strict:"),
    ("                if not issues:\n"
     "                    issues = _check_open_query_provenance("
     "decision.intents, query_evidence)\n"
     "                if not issues:\n"
     "                    policy_checks = [",
     "                if not issues:\n"
     "                    issues = _check_open_query_provenance("
     "decision.intents, query_evidence)\n"
     "                    if not issues:\n"
     "                        decision, issues = _research_evidence_gate(\n"
     "                            decision, query_snapshots, db, run_id,\n"
     "                        )\n"
     "                if not issues:\n"
     "                    policy_checks = ["),
    ("from core.decision_policy import enforced_issues, evaluate_open_intent\n",
     "from core.decision_policy import enforced_issues, evaluate_open_intent\n"
     "from agents.research_evidence import evaluate_open_intents, "
     "snapshot_from_tool_result\n"),
]


def main() -> int:
    orig_bytes = ORIG.read_bytes().decode("utf-8")
    orig_lines = orig_bytes.splitlines(keepends=True)
    flat = "".join(line.replace("\r\n", "\n") for line in orig_lines)
    for index, (old, new) in enumerate(EDITS, start=1):
        count = flat.count(old)
        if count != 1:
            raise SystemExit(f"edit {index}: anchor found {count} times")
        flat = flat.replace(old, new, 1)
    new_lines = flat.splitlines(keepends=True)
    orig_plain = [line.rstrip("\r\n") for line in orig_lines]
    new_plain = [line.rstrip("\r\n") for line in new_lines]
    out: list[str] = []
    matcher = difflib.SequenceMatcher(None, orig_plain, new_plain, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.extend(orig_lines[i1:i2])
            continue
        if tag == "delete":
            raise SystemExit(f"unexpected deletion at {i1}:{i2} (只允许纯插入)")
        for line in new_lines[j1:j2]:
            out.append(line.rstrip("\n") + "\n")
    patched = "".join(out)
    ast.parse(patched)
    TARGET.write_bytes(patched.encode("utf-8"))
    print(f"REBUILT {TARGET} chars={len(patched)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
