"""U03 逐意图可验证研究证据（DL02 / DL04 / F09 / DC06 / DC11 / DC12）离线回归。

独立可运行脚本（本项目没有 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：只读 tests/fixtures/evidence/ 下的合成夹具，不联网、不读写 data/.env、
不构造 Broker、不启动服务、不改风险档/live 开关。

期望值来源：任务卡 U03 offline_acceptance 六条、决策链路专项评估与优化报告
§5.2/§6.2/§9（DL02/DL04 验收列）、K02 契约 README §4 与 golden K02-P1/P2/N1。
反例数值（744.13/744.9、78550/78443）直接取自报告记录的真实决策叙述矛盾。
"""
from __future__ import annotations

import ast
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import agents.research_evidence as evidence  # noqa: E402
from core.contracts import NumericInputError, RuntimeIdentity, decimal_from_float  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "evidence"
AS_OF_MS = 1789862400000
NOW_MS = 1789862401000
IDENTITY = RuntimeIdentity(
    schema_version="3.1",
    environment="demo",
    account_ref="demo:SYNTH_ACCT",
    artifact_hash="artifact-synth-hash",
    git_commit="0123456789abcdef0123456789abcdef01234567",
    dependency_hash="dependency-synth-hash",
    effective_config_hash="effective-config-hash",
    strategy_hash="strategy-synth-hash",
    model_route_hash="model-route-synth-hash",
    runtime_instance_id="inst-synth-r1",
)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name} :: {detail}")
    return bool(condition)


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def build_snapshots(results: dict, keys: list[str]) -> list:
    snapshots = []
    for key in keys:
        payload = results[key]
        snapshots.append(evidence.snapshot_from_tool_result(
            payload, interval=str(payload.get("interval") or "1h"),
        ))
    return snapshots


def evaluate(scenario: dict, results: dict, **kwargs):
    snapshots = build_snapshots(results, scenario["snapshots"])
    return evidence.evaluate_open_intents(
        scenario["intents"], snapshots, now_ms=NOW_MS, identity=IDENTITY,
        cycle_id="cyc-u03", **kwargs,
    )


def reason_codes(report, intent_id: str) -> list[str]:
    item = report.by_intent(intent_id)
    return list(item.reason_codes) if item else []


def has(codes: list[str], needle: str) -> bool:
    return any(needle in code for code in codes)


# --------------------------------------------------------------- 1 正向链路


def test_happy_path(results: dict, scenarios: dict) -> None:
    print("\n[1] 事实来自有时效/有 finality 的快照；结构化 claim 全部程序计算")
    report = evaluate(scenarios["two_intents_partial_evidence"], results)
    item = report.by_intent("intent-A")
    check("A 通过证据门禁", item is not None and item.accepted, reason_codes(report, "intent-A"))
    check("A 状态 VALID", item is not None and item.status == "VALID", item and item.status)
    check("A 绑定自己的 intent/symbol/interval/evidence",
          item is not None and item.intent_id == "intent-A"
          and item.symbol == "BNBUSDT" and item.interval == "1h"
          and item.evidence_id.startswith("eb-"), item and item.as_dict())
    check("A 的 claim 由程序比较真实快照数值",
          item is not None and any(
              claim["expr"].startswith("1h:closed_close >") and claim["status"] == "PASS"
              for claim in item.claims), item and item.claims)


# --------------------------------------------------------------- 2 逐意图独立


def test_two_intents_isolated(results: dict, scenarios: dict) -> None:
    print("\n[2] 两个意图独立绑定：缺证据的 B 不得借用 A 的 PASS（F09 证据污染）")
    report = evaluate(scenarios["two_intents_partial_evidence"], results)
    item_a = report.by_intent("intent-A")
    item_b = report.by_intent("intent-B")
    check("A 与 B 各自出结论", item_a is not None and item_b is not None)
    check("A VALID 且 B 非 VALID", item_a.status == "VALID"
          and item_b.status != "VALID", (item_a.status, item_b.status))
    check("B 被拒且不借用 A", not item_b.accepted and report.rejected_intent_ids == ("intent-B",),
          report.rejected_intent_ids)
    check("A/B 的 evidence_id 不同", item_a.evidence_id != item_b.evidence_id,
          (item_a.evidence_id, item_b.evidence_id))
    check("A/B 的 binding_id 不同", item_a.binding_id != item_b.binding_id,
          (item_a.binding_id, item_b.binding_id))
    check("B 结论里出现缺证据原因（快照缺失/未闭合/无有效绑定）",
          has(item_b.reason_codes, "SNAPSHOT_MISSING")
          and has(item_b.reason_codes, "BUNDLE_NOT_CLOSED")
          and has(item_b.reason_codes, "MISSING_EVIDENCE_BINDING"),
          item_b.reason_codes)
    check("B 的摘要里没有 A 的行情数值",
          "744.13" not in item_b.summary and "743.5" not in item_b.summary,
          item_b.summary)
    check("A 的摘要里有真实事实值", "744.13" in item_a.summary, item_a.summary)

    print("\n[2b] B 指向 A 的 evidence_id 时命中 EVIDENCE_BUNDLE_MISMATCH")
    report2 = evaluate(scenarios["explicit_foreign_evidence_id"], results)
    codes = reason_codes(report2, "intent-borrow")
    check("借包被拒", not report2.ok and has(codes, "EVIDENCE_BUNDLE_MISMATCH"), codes)


# --------------------------------------------------------------- 3 数值矛盾


def test_numeric_contradictions(results: dict, scenarios: dict) -> None:
    print("\n[3] 数值矛盾确定性拒绝（DL02 真实反例）")
    bnb = evaluate(scenarios["bnb_price_contradiction"], results)
    codes = reason_codes(bnb, "intent-bnb")
    claims = bnb.by_intent("intent-bnb").claims
    check("744.13 > 744.9 为假 → 拒绝", not bnb.ok and has(codes, "CLAIM_FALSE"), codes)
    check("该 claim 的实际比较值为 False",
          bool(claims) and claims[0]["actual"] is False, claims)
    check("模型叙述（reasoning）不改变结论",
          bnb.by_intent("intent-bnb").accepted is False)

    btc = evaluate(scenarios["btc_price_contradiction"], results)
    codes = reason_codes(btc, "intent-btc")
    claims = btc.by_intent("intent-btc").claims
    check("78550 < 78443 为假 → 拒绝", not btc.ok and has(codes, "CLAIM_FALSE"), codes)
    check("该 claim 的实际比较值为 False",
          bool(claims) and claims[0]["actual"] is False, claims)

    rr = evaluate(scenarios["rr_narration_contradiction"], results)
    codes = reason_codes(rr, "intent-rr")
    geometry = rr.by_intent("intent-rr").geometry
    check("文字 RR 与程序 RR 不一致 → RR_CONTRADICTION",
          not rr.ok and has(codes, "RR_CONTRADICTION"), codes)
    check("程序算出的 RR 与手算一致",
          geometry.get("computed_rr") is not None
          and abs(Decimal(geometry["computed_rr"]) - Decimal("1.777777777777777777777777778"))
          < Decimal("0.0000001"), geometry)


# --------------------------------------------------------------- 4 周期与快照


def test_interval_and_snapshot_quality(results: dict, scenarios: dict) -> None:
    print("\n[4] 时点/周期/finality：15m 结论不得冒充 4h 证据")
    report = evaluate(scenarios["interval_conflation_15m_as_4h"], results)
    codes = reason_codes(report, "intent-eth-4h")
    check("周期不符被拒", not report.ok and has(codes, "INTERVAL_MISMATCH"), codes)
    check("不产出任何 4h 事实", report.by_intent("intent-eth-4h").claims
          and report.by_intent("intent-eth-4h").claims[0]["status"] == "UNRESOLVED",
          report.by_intent("intent-eth-4h").claims)

    print("\n[4b] 未闭合/无收盘时间的 bar 绝不进入事实")
    source = {"ticker": {"lastPrice": "744.13", "closeTime": AS_OF_MS - 500}}
    live_intent = {
        "intent_id": "i-live", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
        "trigger_price": 742.0,
        "evidence": {"confirmation_interval": "1h", "trigger_price": 742.0,
                     "data_observed_at_ms": AS_OF_MS},
    }
    snap = evidence.snapshot_from_records(
        symbol="BNBUSDT", interval="1h", observed_at_ms=AS_OF_MS,
        bars=[
            {"input_id": "x-1", "interval": "1h", "close_time_ms": AS_OF_MS - 3_600_000,
             "values": {"high": "743.5", "close": "742.8"}},
            {"input_id": "x-2", "interval": "1h", "close_time_ms": None,
             "finality": "unknown", "values": {"high": "999.0", "close": "999.0"}},
        ],
        source_payloads=source,
    )
    report_live = evidence.evaluate_open_intents(
        [live_intent], [snap], now_ms=NOW_MS, identity=IDENTITY, cycle_id="cyc-live",
    )
    item_live = report_live.by_intent("i-live")
    check("未闭合 bar 不参与事实（无 999 值）",
          "999" not in item_live.summary, item_live.summary)
    check("事实只来自已闭合 bar（742.8）", "742.8" in item_live.summary, item_live.summary)
    check("未闭合 bar 不进 bar_close_time/prior_high 事实",
          "1h:prior_high" not in item_live.summary and item_live.accepted,
          (item_live.accepted, item_live.reason_codes))

    only_unclosed = evidence.snapshot_from_records(
        symbol="BNBUSDT", interval="1h", observed_at_ms=AS_OF_MS,
        bars=[{"input_id": "y-1", "interval": "1h", "close_time_ms": None,
               "finality": "unknown", "values": {"high": "999.0", "close": "999.0"}}],
        source_payloads=source,
    )
    report_unclosed = evidence.evaluate_open_intents(
        [live_intent], [only_unclosed], now_ms=NOW_MS, identity=IDENTITY,
        cycle_id="cyc-unclosed",
    )
    codes = reason_codes(report_unclosed, "i-live")
    check("只有未闭合 bar 时证据包不闭合 → 拒绝",
          not report_unclosed.ok and has(codes, "BUNDLE_NOT_CLOSED")
          and has(codes, "MISSING_REQUIRED_INTERVAL"), codes)

    print("\n[4c] 快照时点：未来快照/过期快照一律拒绝（F17）")
    future = evidence.snapshot_from_records(
        symbol="BNBUSDT", interval="1h", observed_at_ms=AS_OF_MS,
        bars=[{"input_id": "f-1", "interval": "1h", "close_time_ms": AS_OF_MS,
               "values": {"high": "743.5", "close": "744.13"}}],
        source_payloads={"ticker": {"lastPrice": "744.13", "closeTime": AS_OF_MS}},
    )
    report_future = evidence.evaluate_open_intents(
        [{"intent_id": "i-future", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
          "trigger_price": 744.0,
          "evidence": {"confirmation_interval": "1h", "trigger_price": 744.0}}],
        [future], now_ms=AS_OF_MS - 5_000, identity=IDENTITY, cycle_id="cyc-f",
    )
    check("快照时间晚于校验时刻 → SNAPSHOT_FROM_FUTURE",
          has(reason_codes(report_future, "i-future"), "SNAPSHOT_FROM_FUTURE"),
          reason_codes(report_future, "i-future"))
    report_stale = evidence.evaluate_open_intents(
        [{"intent_id": "i-stale", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
          "trigger_price": 744.0,
          "evidence": {"confirmation_interval": "1h", "trigger_price": 744.0}}],
        [future], now_ms=AS_OF_MS + 900_000, identity=IDENTITY, cycle_id="cyc-s",
    )
    check("快照超过时效 → SNAPSHOT_STALE",
          has(reason_codes(report_stale, "i-stale"), "SNAPSHOT_STALE"),
          reason_codes(report_stale, "i-stale"))

    print("\n[4d] 意图自报观测时刻必须等于本轮快照时刻")
    report_time = evaluate(scenarios["evidence_time_mismatch"], results)
    check("时点不一致 → EVIDENCE_TIME_MISMATCH",
          has(reason_codes(report_time, "intent-time"), "EVIDENCE_TIME_MISMATCH"),
          reason_codes(report_time, "intent-time"))


# --------------------------------------------------------------- 5 分源质量 S01


def test_source_quality(results: dict, scenarios: dict) -> None:
    print("\n[5] S01 分源证据质量：funding/OI 缺失不得当 0（DC11）")
    snap = evidence.snapshot_from_tool_result(
        results["ETHUSDT_1h_sources_missing"], interval="1h")
    funding = snap.source("mark_funding")
    oi = snap.source("open_interest")
    check("mark_funding 明确不可用", funding is not None and funding.available is False)
    check("缺失的 funding_rate 不出现在 values", funding is not None
          and "funding_rate" not in funding.values, funding and funding.values)
    check("缺失的 mark_price 不出现在 values", funding is not None
          and "mark_price" not in funding.values, funding and funding.values)
    check("缺失字段进 missing_fields（含 OI）",
          funding is not None and "funding_rate" in funding.missing_fields
          and "mark_funding.funding_rate" in snap.missing_fields
          and "open_interest.open_interest_usd" in snap.missing_fields,
          (funding and funding.missing_fields, snap.missing_fields))
    check("工具出参回带的 0.0 没有被当成真实费率",
          funding is not None and funding.value("funding_rate") is None)
    check("OI 缺失不当 0", oi is not None and oi.available is False
          and oi.value("open_interest_usd") is None, oi and oi.as_dict())
    check("ticker 正常可用且有源时点", snap.source("ticker").available is True
          and snap.source("ticker").source_at_ms == AS_OF_MS - 500,
          snap.source("ticker").as_dict())

    required = evaluate(scenarios["funding_missing_not_zero"], results)
    codes = reason_codes(required, "intent-eth-funding")
    check("依赖缺失源的意图被拒", not required.ok
          and has(codes, "SOURCE_UNAVAILABLE"), codes)
    check("缺失源的拒因带源名", has(codes, "SOURCE_UNAVAILABLE:mark_funding"), codes)

    optional = evaluate(scenarios["optional_source_missing_not_blocking"], results)
    item = optional.by_intent("intent-eth-open")
    check("未声明依赖的缺失源不阻断但如实记录",
          optional.ok and item.accepted and "mark_funding.funding_rate" in item.missing_fields,
          (item.accepted, item.missing_fields))
    check("事实摘要把缺失渲染为 MISSING（不是 0）",
          "mark_funding=MISSING" in item.summary and "funding_rate=0" not in item.summary,
          item.summary)

    stale = evaluate(scenarios["stale_required_source"], results)
    codes = reason_codes(stale, "intent-eth-oi")
    check("源时点陈旧 → SOURCE_STALE", not stale.ok and has(codes, "SOURCE_STALE"), codes)


# --------------------------------------------------------------- 6 模型不得覆盖事实


def test_model_cannot_override_facts(results: dict, scenarios: dict) -> None:
    print("\n[6] 模型解释不得覆盖行情事实")
    report = evaluate(scenarios["model_fact_override"], results)
    codes = reason_codes(report, "intent-override")
    check("自报行情字段 → MODEL_FACT_OVERRIDE",
          not report.ok and has(codes, "MODEL_FACT_OVERRIDE"), codes)
    check("自报字段被点名", has(codes, "last_price") and has(codes, "close"), codes)
    item = report.by_intent("intent-override")
    check("摘要仍只含快照真实数值",
          "last_price=900" not in item.summary and "close=900" not in item.summary
          and "744.13" in item.summary, item.summary)

    print("\n[6b] NaN/Infinity 不得参与比较（DC06）")
    nan_claims = [{
        "claim_id": "nan-claim",
        "expr": "1h:closed_close > NaN", "expected": True,
    }]
    report_nan = evidence.evaluate_open_intents(
        [{"intent_id": "i-nan", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
          "interval": "1h", "claims": nan_claims,
          "evidence": {"confirmation_interval": "1h"}}],
        build_snapshots(results, ["BNBUSDT_1h"]), now_ms=NOW_MS,
        identity=IDENTITY, cycle_id="cyc-nan",
    )
    codes = reason_codes(report_nan, "i-nan")
    check("NaN 操作数被拒", not report_nan.ok and has(codes, "NON_FINITE_VALUE"), codes)
    check("适配器不产出非有限数值",
          evidence.decimal_or_none(float("nan")) is None
          and evidence.decimal_or_none(float("inf")) is None
          and evidence.decimal_or_none("NaN") is None)


# --------------------------------------------------------------- 7 Decimal 适配


def test_decimal_adapter(results: dict, scenarios: dict) -> None:
    print("\n[7] 新增 Decimal 经适配器兼容既有 float/REAL（R16，不触发全仓重构）")
    check("十进制文本精确解析", evidence.decimal_or_none("744.13") == Decimal("744.13"))
    check("float 经 str 适配无二进制尾差",
          evidence.decimal_or_none(744.13) == Decimal("744.13"))
    check("空值/布尔/非法文本不当 0",
          evidence.decimal_or_none(None) is None
          and evidence.decimal_or_none("") is None
          and evidence.decimal_or_none(True) is None
          and evidence.decimal_or_none("abc") is None)
    check("契约侧仍显式要求 decimal_from_float",
          decimal_from_float(0.1) == Decimal("0.1"))
    try:
        decimal_from_float(float("nan"))
    except NumericInputError:
        check("非有限 float 在边界被拒", True)
    else:
        check("非有限 float 在边界被拒", False, "no NumericInputError")

    report = evaluate(scenarios["two_intents_partial_evidence"], results)
    payload = report.as_dict()
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except TypeError as exc:  # pragma: no cover - Decimal 泄漏即失败
        check("报告可直接 JSON 序列化（无 Decimal 泄漏）", False, exc)
    else:
        check("报告可直接 JSON 序列化（无 Decimal 泄漏）", "Decimal" not in text)
    check("几何字段以十进制字符串导出",
          isinstance(report.by_intent("intent-A").geometry.get("entry"), str)
          and isinstance(report.by_intent("intent-A").geometry.get("computed_rr"), str),
          report.by_intent("intent-A").geometry)


# --------------------------------------------------------------- 8 非开仓不误伤


def test_non_open_actions(results: dict, scenarios: dict) -> None:
    print("\n[8] CLOSE/REDUCE 不受开仓证据门禁误伤（DL02 边界）")
    report = evaluate(scenarios["non_open_actions_untouched"], results)
    close_item = report.by_intent("intent-close")
    reduce_item = report.by_intent("intent-reduce")
    check("非开仓动作为 NOT_APPLICABLE",
          close_item.status == "NOT_APPLICABLE" and reduce_item.status == "NOT_APPLICABLE",
          (close_item.status, reduce_item.status))
    check("非开仓动作不被丢单", report.ok and report.decision_gate()["drop_intent_ids"] == [],
          report.decision_gate())
    check("非开仓动作不进开仓门禁", close_item.applies_to_open is False)


# --------------------------------------------------------------- 9 注入文本


def test_injection_and_side_effects(results: dict, scenarios: dict) -> None:
    print("\n[9] DC12：注入文本只能当数据，不改变工具权限/风险档/live")
    injection = scenarios["injection"]
    texts = injection["texts"]
    source_path = Path(evidence.__file__)
    source_text = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source_text)
    allowed_modules = {
        "__future__", "re", "collections.abc", "dataclasses", "decimal", "typing",
        "core.contracts", "core.contracts.k02_evidence",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(str(node.module or ""))
    check("模块只导入契约层与标准库（不导入行情/交易/权限模块）",
          imported <= allowed_modules, sorted(imported - allowed_modules))
    danger_bare = {"open", "exec", "eval", "compile", "__import__", "input"}
    danger_attr = {
        "system", "popen", "run", "call", "Popen", "place_order", "set_leverage",
        "write_text", "write_bytes", "unlink", "remove", "rmtree", "chmod",
    }
    bare_calls = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    attr_calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    check("模块不调用执行/写盘/下单接口",
          not (bare_calls & danger_bare) and not (attr_calls & danger_attr),
          sorted((bare_calls & danger_bare) | (attr_calls & danger_attr)))
    network_tokens = ("httpx", "requests", "urllib", "socket", "subprocess")
    check("模块无联网/子进程能力",
          not any(token in imported for token in network_tokens)
          and not any(f"{token}." in source_text for token in network_tokens))
    protected_tokens = (
        "risk_profile", "allow_live", "live_enabled", "whitelist",
        "risk_parameters", "set_live", "enable_live",
    )
    check("模块不触碰风险档/白名单/live 开关",
          not any(token in source_text for token in protected_tokens))

    decorated = dict(scenarios["funding_missing_not_zero"])
    decorated["intents"] = [
        dict(decorated["intents"][0], reasoning=texts[1], notes=texts[0],
             injected_payload=texts[2], summary=texts[3])
        for text_index in range(1)
    ]
    report = evaluate(decorated, results)
    codes = reason_codes(report, "intent-eth-funding")
    check("注入'funding 已确认为 0'不能洗白缺失源",
          not report.ok and has(codes, "SOURCE_UNAVAILABLE"), codes)
    check("注入文本未进入原因码",
          not any(any(token in code for token in ("忽略", "SYSTEM", "system", "skip"))
                  for code in codes), codes)
    gate = report.decision_gate()
    check("门禁视图只含丢单与身份固定信息，不含权限/风险字段",
          set(gate) == {"ok", "identity_pinned", "drop_intent_ids", "reasons"},
          sorted(gate))

    print("\n[9b] U15 未接线时身份必须显式暴露为未固定")
    unpinned = evidence.evaluate_open_intents(
        scenarios["two_intents_partial_evidence"]["intents"],
        build_snapshots(results, scenarios["two_intents_partial_evidence"]["snapshots"]),
        now_ms=NOW_MS, cycle_id="cyc-unpinned",
    )
    check("缺省身份 → identity_pinned=False 且不冒充已验证",
          unpinned.identity_pinned is False
          and unpinned.as_dict()["identity_pinned"] is False)
    check("显式传身份 → identity_pinned=True",
          evidence.evaluate_open_intents(
              scenarios["two_intents_partial_evidence"]["intents"],
              build_snapshots(results, scenarios["two_intents_partial_evidence"]["snapshots"]),
              now_ms=NOW_MS, identity=IDENTITY, cycle_id="cyc-pinned",
          ).identity_pinned is True)
    item = report.by_intent("intent-eth-funding")
    check("注入文本不改变失败源的真实记录",
          "mark_funding=MISSING" in item.summary and "0.0000" not in item.summary,
          item.summary)


# --------------------------------------------------------------- 10 规则与确定性


def test_rules_and_determinism(results: dict, scenarios: dict) -> None:
    print("\n[10] 验证方 rules 必传且同源；结论可确定性重放")
    scenario = scenarios["two_intents_partial_evidence"]
    first = evaluate(scenario, results)
    second = evaluate(scenario, results)
    check("两次运行完全一致（evidence_id/binding_id/摘要）",
          first.as_dict() == second.as_dict())
    item = first.by_intent("intent-A")
    check("摘要模板版本显式", item.summary.startswith(
        "[" + evidence.FACT_SUMMARY_TEMPLATE_VERSION + "]"), item.summary)

    snapshots = build_snapshots(results, scenario["snapshots"])
    rules = evidence.build_rules(interval="1h", required_claim_kinds=["close_above_price"])
    check("规则含必需字段与分源 TTL",
          rules["required_intervals"] == ["1h"]
          and rules["required_claim_kinds"] == ["close_above_price"]
          and set(rules["source_ttls"]) == set(evidence.SOURCE_NAMES), rules)
    check("规则哈希稳定",
          evidence.canonical_hash(rules) == evidence.canonical_hash(
              evidence.build_rules(interval="1h",
                                   required_claim_kinds=["close_above_price"])))
    check("快照夹具与断言基准一致",
          snapshots[0].observed_at_ms == AS_OF_MS and len(snapshots[0].bars) == 2,
          snapshots[0].as_dict())


# --------------------------------------------------------------- 11 快照适配


def test_snapshot_adapter(results: dict) -> None:
    print("\n[11] 工具出参 → 快照适配（列式 K 线 / 分源质量）")
    snap = evidence.snapshot_from_tool_result(results["BNBUSDT_1h"], interval="1h")
    check("快照 ok 且币种规范", snap.ok and snap.symbol == "BNBUSDT")
    check("收盘时间由列式相对时间还原",
          [bar["close_time_ms"] for bar in snap.bars]
          == [AS_OF_MS - 3_600_000 - 1, AS_OF_MS - 1],
          [bar["close_time_ms"] for bar in snap.bars])
    check("bar 输入带显式 finality=closed",
          all(bar["finality"] == "closed" for bar in snap.bars))
    check("bar 值以十进制字符串保存",
          all(isinstance(value, str) for bar in snap.bars for value in bar["values"].values()))
    bad = evidence.snapshot_from_tool_result({"error": "closed kline data is stale"}, interval="1h")
    check("工具失败快照不可用", bad.ok is False and bad.bars == () and bad.issues,
          bad.as_dict())
    unknown = evidence.snapshot_from_tool_result(results["BNBUSDT_1h"], interval="3m")
    check("未知周期不猜时间（fail closed）", unknown.bars == (), unknown.as_dict())


def main() -> int:
    results = load("market_tool_results.json")["results"]
    scenarios = load("intent_scenarios.json")["scenarios"]
    meta = load("intent_scenarios.json")
    scenarios = dict(scenarios)
    scenarios["injection"] = meta["injection"]

    print("=" * 72)
    print("U03 逐意图可验证研究证据 离线回归（独立脚本，无 pytest）")
    print("=" * 72)
    test_happy_path(results, scenarios)
    test_two_intents_isolated(results, scenarios)
    test_numeric_contradictions(results, scenarios)
    test_interval_and_snapshot_quality(results, scenarios)
    test_source_quality(results, scenarios)
    test_model_cannot_override_facts(results, scenarios)
    test_decimal_adapter(results, scenarios)
    test_non_open_actions(results, scenarios)
    test_injection_and_side_effects(results, scenarios)
    test_rules_and_determinism(results, scenarios)
    test_snapshot_adapter(results)

    print("\n" + "=" * 72)
    print(f"结果：PASS {PASS} / FAIL {FAIL}")
    if FAILURES:
        print("失败项：")
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
