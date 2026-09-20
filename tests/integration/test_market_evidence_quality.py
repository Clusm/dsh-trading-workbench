"""U03 / S01 分源证据质量离线回归（DC11 / DC12 / DL04）。

覆盖两件事，全部离线、独立可运行（无 pytest；失败退出码 1）：

A. **共享适配补丁可复核且行为正确**：把
   ``tests/fixtures/evidence/s01_market_query_adaptation.patch`` 用严格 unified diff
   应用器打到当前 ``src/data/market_query.py`` 的内存副本上（基线漂移会立刻暴露），
   再把结果作为独立模块导入，验证：
     - ``funding/OI/mark 缺失 → None``，绝不再回落到 ``0.0``；
     - 交易所真实返回的 0 保留为 0（"缺失" ≠ "数值为零"）；
     - 逐源 ``available/source_at_ms/observed_at_ms/fresh_until_ms/missing_fields``；
     - 源时钟晚于本地观测时不得把未来时间洗成"仍在有效期"。

B. **证据层口径**（``agents.research_evidence``）对 patched / legacy 两种工具出参
   都给出同一结论：缺失就是缺失（missing_fields + values 无该键），真实 0 是数据；
   注入文本不改变可用性、时点或权限。

补丁本身不落在共享文件上：作者只提交统一 diff，由 I 归档并应用。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import agents.research_evidence as evidence  # noqa: E402
from core.contracts import RuntimeIdentity  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "evidence"
AS_OF_MS = 1789862400000
NOW_MS = 1789862401000
IDENTITY = RuntimeIdentity(
    schema_version="3.1", environment="demo", account_ref="demo:SYNTH_ACCT",
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


# ------------------------------------------------------- 严格 unified diff 应用器


def apply_unified_diff(original: str, diff_text: str) -> str:
    """把单文件 unified diff 打到 ``original`` 上；任何上下文不符立即抛错。

    只支持 ``@@ -a,b +c,d @@`` 单文件补丁（本任务的适配补丁形态），
    不做模糊匹配：基线一漂移就必须失败，而不是静默打歪。
    """
    orig_lines = original.splitlines(keepends=True)
    diff_lines = diff_text.splitlines(keepends=True)
    hunks: list[tuple[int, list[str]]] = []
    index = 0
    while index < len(diff_lines):
        line = diff_lines[index]
        if line.startswith("@@"):
            header = line.rstrip("\r\n")
            spec = header.split("@@")[1].strip().split(" ")[0]
            old_start = int(spec[1:].split(",")[0])
            index += 1
            body: list[str] = []
            while index < len(diff_lines) and not diff_lines[index].startswith("@@"):
                body.append(diff_lines[index])
                index += 1
            hunks.append((old_start, body))
            continue
        index += 1
    if not hunks:
        raise ValueError("patch contains no hunk")
    out: list[str] = []
    cursor = 0
    for old_start, body in hunks:
        start = old_start - 1
        if start < cursor:
            raise ValueError("overlapping hunks")
        out.extend(orig_lines[cursor:start])
        expect = []
        produced = []
        for line in body:
            if line.startswith("\\"):
                continue
            content = line[1:] if line[:1] in (" ", "+", "-") else line
            if line.startswith(" "):
                expect.append(content)
                produced.append(content)
            elif line.startswith("-"):
                expect.append(content)
            elif line.startswith("+"):
                produced.append(content)
            else:
                raise ValueError(f"unexpected diff line: {line!r}")
        actual = orig_lines[start:start + len(expect)]
        if actual != expect:
            raise ValueError(
                "patch context mismatch at line "
                f"{old_start}: {actual[:1]!r} != {expect[:1]!r}"
            )
        out.extend(produced)
        cursor = start + len(expect)
    out.extend(orig_lines[cursor:])
    return "".join(out)


def load_patched_market_query():
    """把补丁应用结果作为独立模块导入（不写仓库、不改共享文件）。"""
    original = (ROOT / "src" / "data" / "market_query.py").read_bytes().decode("utf-8")
    patch = (FIXTURES / "s01_market_query_adaptation.patch").read_bytes().decode("utf-8")
    patched = apply_unified_diff(original, patch)
    work = Path(tempfile.mkdtemp(prefix="u03_mq_"))
    target = work / "market_query_patched.py"
    target.write_bytes(patched.encode("utf-8"))
    spec = importlib.util.spec_from_file_location("u03_market_query_patched", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, original, patched


# ------------------------------------------------------------------- A. 补丁验证


def test_patch(module, original: str, patched: str) -> None:
    print("\n[A] S01 共享适配补丁：可应用、口径正确")
    check("补丁确实改变了目标文件", patched != original)
    check("补丁未触碰开仓白名单/交易范围代码",
          patched.count("scope.allows_open") == original.count("scope.allows_open")
          and patched.count("TradingScope") == original.count("TradingScope"))
    check("funding 缺失不再回落到 0.0",
          'float(premium.get("lastFundingRate") or 0.0)' not in patched
          and 'float(premium.get("markPrice") or 0.0)' not in patched)
    check("OI 缺失不再回落到 0.0",
          'float(oi.get("openInterest") or 0.0)' not in patched)
    check("出参仍保留 funding_rate/mark_price/open_interest_usd 键（兼容既有消费端）",
          all(f'"{field}":' in patched for field in
              ("funding_rate", "mark_price", "open_interest_usd")))

    number = module._source_number
    check("缺失键 → None", number({}, "lastFundingRate") is None)
    check("显式 None → None", number({"lastFundingRate": None}, "lastFundingRate") is None)
    check("真实 0 保留为 0（缺失 != 零）",
          number({"lastFundingRate": 0.0}, "lastFundingRate") == 0.0)
    check("非数值文本 → None", number({"lastFundingRate": "n/a"}, "lastFundingRate") is None)
    check("NaN/Infinity → None",
          number({"openInterest": float("nan")}, "openInterest") is None
          and number({"openInterest": float("inf")}, "openInterest") is None)
    check("空载荷/非映射 → None",
          number(None, "openInterest") is None and number([], "openInterest") is None)

    quality = module._source_quality(
        {"markPrice": 2410.4, "lastFundingRate": 0.0, "time": 1789862399000},
        observed_at_ms=AS_OF_MS, ttl_seconds=60.0,
        fields=("markPrice", "lastFundingRate"),
    )
    check("真实 0 的源仍算可用", quality["available"] is True
          and quality["missing_fields"] == [], quality)
    check("源时点/有效期/观测时点齐备",
          quality["source_at_ms"] == 1789862399000
          and quality["fresh_until_ms"] == 1789862459000
          and quality["observed_at_ms"] == AS_OF_MS, quality)
    missing = module._source_quality(
        {}, observed_at_ms=AS_OF_MS, ttl_seconds=60.0, fields=("openInterest",))
    check("缺失源：available=False 且字段全列 missing",
          missing["available"] is False and missing["missing_fields"] == ["openInterest"]
          and missing["fresh_until_ms"] is None, missing)
    future = module._source_quality(
        {"openInterest": 1.0, "time": AS_OF_MS + 3_600_000},
        observed_at_ms=AS_OF_MS, ttl_seconds=60.0, fields=("openInterest",))
    check("源时钟晚于本地观测：以本地观测为准（不延长有效期）",
          future["source_at_ms"] == AS_OF_MS + 3_600_000
          and future["fresh_until_ms"] == AS_OF_MS + 60_000, future)


def test_patch_rejects_drift() -> None:
    print("\n[A2] 补丁对基线漂移敏感（不能静默打歪）")
    patch = (FIXTURES / "s01_market_query_adaptation.patch").read_bytes().decode("utf-8")
    original = (ROOT / "src" / "data" / "market_query.py").read_bytes().decode("utf-8")
    mangled = original.replace(
        '    funding_rate = float(premium.get("lastFundingRate") or 0.0)\r\n',
        '    funding_rate = 0.0  # 已被其他改动替换\r\n',
    ).replace(
        '    funding_rate = float(premium.get("lastFundingRate") or 0.0)\n',
        '    funding_rate = 0.0  # 已被其他改动替换\n',
    )
    check("夹具基线确实被改动", mangled != original)
    try:
        apply_unified_diff(mangled, patch)
    except ValueError as exc:
        check("上下文不符时拒绝应用并报错", True and "mismatch" in str(exc), str(exc))
    else:
        check("上下文不符时拒绝应用并报错", False, "patch was applied silently")


# ------------------------------------------------------------- B. 证据层口径


def test_source_quality_contract(results: dict) -> None:
    print("\n[B] 分源证据质量：patched 与 legacy 出参结论一致")

    legacy = evidence.snapshot_from_tool_result(
        results["ETHUSDT_1h_sources_missing"], interval="1h")
    patched = evidence.snapshot_from_tool_result(
        results["ETHUSDT_1h_patched_sources_missing"], interval="1h")
    for name, snap in (("legacy", legacy), ("patched", patched)):
        funding = snap.source("mark_funding")
        check(f"{name}：mark_funding 不可用且不产出 0",
              funding.available is False
              and "funding_rate" not in funding.values
              and "mark_price" not in funding.values, funding.as_dict())
        check(f"{name}：missing_fields 覆盖 funding/OI",
              "mark_funding.funding_rate" in snap.missing_fields
              and "open_interest.open_interest_usd" in snap.missing_fields,
              snap.missing_fields)
        check(f"{name}：ticker 仍可用且有源时点",
              snap.source("ticker").available is True
              and snap.source("ticker").source_at_ms == AS_OF_MS - 500,
              snap.source("ticker").as_dict())

    print("\n[B2] 真实 0 是数据，不是缺失（关键判别）")
    zero = evidence.snapshot_from_tool_result(
        results["ETHUSDT_1h_patched_genuine_zero"], interval="1h")
    funding = zero.source("mark_funding")
    check("真实 funding_rate=0 保留为 \"0\" 且不列为缺失",
          funding.value("funding_rate") == "0"
          and "funding_rate" not in funding.missing_fields, funding.as_dict())
    check("真实 mark 价保留", funding.value("mark_price") == "2410.4", funding.values)
    check("真实 OI 保留为十进制文本",
          zero.source("open_interest").value("open_interest_usd") == "138.2",
          zero.source("open_interest").values)
    check("该轮整体 missing_fields 为空", zero.missing_fields == (), zero.missing_fields)

    print("\n[B2b] 自报 fresh_until_ms 只能缩短不能延长（不得自证新鲜）")
    snap_extended = evidence.snapshot_from_records(
        symbol="ETHUSDT", interval="1h", observed_at_ms=AS_OF_MS,
        bars=[],
        source_payloads={"ticker": {
            "available": True, "lastPrice": "2410.5",
            "source_at_ms": AS_OF_MS - 500,
            "fresh_until_ms": AS_OF_MS + 100 * 3_600_000,
        }},
    )
    ticker = snap_extended.source("ticker")
    check("未来自报有效期被夹到 min(源时点, 本地观测)+TTL",
          ticker.fresh_until_ms == AS_OF_MS - 500 + evidence.SOURCE_TTL_MS["ticker"]
          and ticker.fresh_at(AS_OF_MS + 60_000) is False,
          ticker.as_dict())
    snap_shortened = evidence.snapshot_from_records(
        symbol="ETHUSDT", interval="1h", observed_at_ms=AS_OF_MS, bars=[],
        source_payloads={"ticker": {
            "available": True, "lastPrice": "2410.5",
            "source_at_ms": AS_OF_MS - 500,
            "fresh_until_ms": AS_OF_MS - 400,
        }},
    )
    check("自报更短的有效期被尊重",
          snap_shortened.source("ticker").fresh_until_ms == AS_OF_MS - 400,
          snap_shortened.source("ticker").as_dict())

    print("\n[B3] 逐源 expires/fresh 进证据层（时点可核对）")
    check("源有效期来自逐源 TTL 或显式 fresh_until_ms",
          zero.source("mark_funding").fresh_until_ms == 1789862459000,
          zero.source("mark_funding").as_dict())
    check("源时点早于本地观测且被保留",
          zero.source("ticker").source_at_ms == 1789862399500,
          zero.source("ticker").as_dict())


def test_injection_does_not_change_permissions(results: dict) -> None:
    print("\n[B4] DC12：注入文本不改变分源可用性/时点")
    scenarios = json.loads((FIXTURES / "intent_scenarios.json").read_text(encoding="utf-8"))
    texts = scenarios["injection"]["texts"]
    poisoned = dict(results["ETHUSDT_1h_patched_sources_missing"])
    poisoned["notes"] = texts[0]
    poisoned["data_quality"] = dict(poisoned["data_quality"])
    poisoned["data_quality"]["summary_note"] = texts[1]
    poisoned["data_quality"]["sources"] = {
        name: dict(item, note=texts[2], injected=texts[3])
        for name, item in poisoned["data_quality"]["sources"].items()
    }
    snap = evidence.snapshot_from_tool_result(poisoned, interval="1h")
    check("注入文本不把缺失源变成可用",
          snap.source("mark_funding").available is False
          and "funding_rate" not in snap.source("mark_funding").values,
          snap.source("mark_funding").as_dict())
    check("注入文本不进入 missing_fields 之外的口径",
          all("忽略" not in item and "SYSTEM" not in item for item in snap.missing_fields),
          snap.missing_fields)
    report = evidence.evaluate_open_intents(
        [{"intent_id": "i-inject", "symbol": "ETHUSDT", "action": "OPEN", "side": "LONG",
          "trigger_price": 2405.0, "required_sources": ["mark_funding"],
          "reasoning": texts[1],
          "evidence": {"confirmation_interval": "1h", "trigger_price": 2405.0,
                       "data_observed_at_ms": AS_OF_MS}}],
        [snap], now_ms=NOW_MS, identity=IDENTITY, cycle_id="cyc-inject",
    )
    item = report.by_intent("i-inject")
    check("依赖缺失源的注入意图仍被拒",
          not report.ok and any("SOURCE_UNAVAILABLE" in code for code in item.reason_codes),
          item.reason_codes)
    check("注入文本未出现在任何原因码",
          not any(token in code for code in item.reason_codes
                  for token in ("忽略", "SYSTEM", "system", "tool_permissions")),
          item.reason_codes)


def test_legacy_zero_echo_is_not_trusted(results: dict) -> None:
    print("\n[B5] 逐源元数据是权威口径：出参回带 0 也不得当成真实值")
    legacy = results["ETHUSDT_1h_sources_missing"]
    check("夹具确实回带了 0.0（模拟旧口径）",
          legacy["funding_rate"] == 0.0 and legacy["mark_price"] == 0.0)
    snap = evidence.snapshot_from_tool_result(legacy, interval="1h")
    check("适配层不信任回带的 0",
          snap.source("mark_funding").value("funding_rate") is None
          and snap.source("mark_funding").value("mark_price") is None)
    check("缺失字段仍被完整记录",
          "mark_funding.mark_price" in snap.missing_fields, snap.missing_fields)


# ------------------------------------------------- C. 接线补丁的缺省口径（r3）


def load_patched_orchestrator() -> tuple[str, str]:
    """把 U03 接线补丁打到 ``src/agents/orchestrator.py`` 的内存副本上（不写仓库）。"""
    original = (ROOT / "src" / "agents" / "orchestrator.py").read_bytes().decode("utf-8")
    patch = (FIXTURES / "u03_orchestrator_integration.patch").read_bytes().decode("utf-8")
    return original, apply_unified_diff(original, patch)


def _literal_assignment(source: str, name: str):
    """取 ``NAME = <literal>`` 的求值结果（接线常量断言用）。"""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == name:
            return ast.literal_eval(value)
    raise AssertionError(f"{name} 未在补丁结果中出现")


def test_orchestrator_wiring_policy() -> None:
    """r3（Q-NEW-2/3）：接线补丁必须在**默认配置**下让两道控制真实生效。"""
    print("\n[C] U03 接线补丁：分源门禁缺省生效 + 轮次分量非空")
    original, patched = load_patched_orchestrator()
    check("补丁确实改变了 orchestrator", patched != original)
    patch_text = (FIXTURES / "u03_orchestrator_integration.patch").read_text(encoding="utf-8")
    check("补丁仍是纯插入（无删除行）",
          not [line for line in patch_text.splitlines()
               if line.startswith("-") and not line.startswith("---")])
    check("身份开关缺省 False（未固定身份下开仓仍 fail-closed）",
          _literal_assignment(patched, "ALLOW_UNPINNED_RESEARCH_EVIDENCE") is False)
    floor = _literal_assignment(patched, "RESEARCH_EVIDENCE_REQUIRED_SOURCES")
    check(f"策略侧缺省必需源**非空**（默认路径下字段级分源门禁真实生效）：{floor!r}",
          isinstance(floor, tuple) and len(floor) > 0)
    for source in floor:
        check(f"缺省源 {source!r} 是模块认识的源名（源级或字段级）",
              str(source).split(".")[0] in evidence.SOURCE_NAMES)
    check("门禁处 identity=None（未固定身份，待 U15 接线）",
          "identity=None" in patched)
    check("门禁传 allow_unpinned=ALLOW_UNPINNED_RESEARCH_EVIDENCE（不写死 True）",
          "allow_unpinned=ALLOW_UNPINNED_RESEARCH_EVIDENCE" in patched)
    check("轮次分量不再恒为空串（pipeline_decision_id 之后才写入）",
          'or run_id or ""' in patched
          and 'pipeline_decision_id", "") or ""' not in patched)


def main() -> int:
    results = json.loads(
        (FIXTURES / "market_tool_results.json").read_text(encoding="utf-8"))["results"]
    print("=" * 72)
    print("U03 / S01 分源证据质量 离线回归（独立脚本，无 pytest）")
    print("=" * 72)
    module, original, patched = load_patched_market_query()
    test_patch(module, original, patched)
    test_patch_rejects_drift()
    test_source_quality_contract(results)
    test_injection_does_not_change_permissions(results)
    test_legacy_zero_echo_is_not_trusted(results)
    test_orchestrator_wiring_policy()
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
