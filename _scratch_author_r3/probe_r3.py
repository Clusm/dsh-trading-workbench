"""U03 修复轮 r3 的**前后对照**探针（离线，只写 _scratch_author_r3/）。

覆盖：
  S1  Q-NEW-1 哨兵逃逸：旧模块（5ffc6c0 的 research_evidence.py）vs 新模块
  S2  Q-NEW-3 分源门禁是否在生产路径（接线补丁）默认生效：default=() vs ("ticker",)
  S3  Q-NEW-2 binding_id 的轮次分量：cycle_id 恒空 vs run_id
  S4  Q-NEW-4/5 并集语义与畸形声明
全部结论只依赖本文件实跑输出。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "_scratch_author_r3"
sys.path.insert(0, str(ROOT / "src"))

import agents.research_evidence as new_ev  # noqa: E402
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


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass() 需要模块已在 sys.modules 里
    spec.loader.exec_module(module)
    return module


def load_old_evidence():
    """5ffc6c0 的 research_evidence.py（HEAD 未改动该文件，等于修复前版本）。"""
    src = subprocess.run(["git", "show", "HEAD:src/agents/research_evidence.py"],
                         cwd=ROOT, check=True, capture_output=True).stdout
    path = SCRATCH / "re_old.py"
    path.write_bytes(src)
    return load_module(path, "re_old_5ffc6c0")


def load_patched_orchestrator():
    """接线补丁应用到 orchestrator.py 内存副本后的模块（不写仓库）。"""
    src = subprocess.run(["git", "show", "HEAD:src/agents/orchestrator.py"],
                         cwd=ROOT, check=True, capture_output=True).stdout
    path = SCRATCH / "tree_apply.py"
    path.write_bytes(src)
    # 用 git apply 真打到 scratch 文件上（--directory 指到 scratch 根）
    tree = SCRATCH / "apply_root"
    (tree / "src" / "agents").mkdir(parents=True, exist_ok=True)
    (tree / "src" / "agents" / "orchestrator.py").write_bytes(src)
    subprocess.run(["git", "apply", "--directory=_scratch_author_r3/apply_root",
                    "tests/fixtures/evidence/u03_orchestrator_integration.patch"],
                   cwd=ROOT, check=True)
    # 注册成 agents.orchestrator 才能满足 `from . import lessons` 相对导入
    return load_module(tree / "src" / "agents" / "orchestrator.py", "agents.orchestrator")


def snap_ticker_degraded():
    """ticker 源可用但只有 lastPrice（缺 pct_24h/quote_volume_24h）。"""
    return new_ev.snapshot_from_records(
        symbol="BNBUSDT", interval="1h", observed_at_ms=AS_OF_MS,
        bars=[{"input_id": "b1", "interval": "1h", "close_time_ms": AS_OF_MS - 1,
               "values": {"close": "744.13"}, "finality": "closed"}],
        source_payloads={"ticker": {"available": True, "lastPrice": "744.13",
                                    "closeTime": AS_OF_MS - 500}},
    )


def snap_full():
    results = json.loads((FIXTURES / "market_tool_results.json")
                         .read_text(encoding="utf-8"))["results"]
    return new_ev.snapshot_from_tool_result(results["BNBUSDT_1h"], interval="1h")


INTENT = {
    "intent_id": "A", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
    "trigger_price": 744.0, "stop_loss": 700.0, "take_profit": 800.0,
    "evidence": {"confirmation_interval": "1h", "data_observed_at_ms": AS_OF_MS},
}


def sect(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def s1_sentinel() -> None:
    sect("S1  Q-NEW-1 未固定身份哨兵逃逸：修复前(5ffc6c0) vs 修复后")
    old = load_old_evidence()
    snaps = [snap_full()]
    forms = {
        "UNPINNED_IDENTITY（模块常量）": lambda m: m.UNPINNED_IDENTITY,
        "UNPINNED_IDENTITY.model_copy()": lambda m: m.UNPINNED_IDENTITY.model_copy(),
        "等值裸 dict": lambda m: m.UNPINNED_IDENTITY.model_dump(mode="json"),
    }
    for label, make in forms.items():
        for tag, mod in (("修复前", old), ("修复后", new_ev)):
            rep = mod.evaluate_open_intents(
                [dict(INTENT)], snaps, now_ms=NOW_MS, identity=make(mod),
                cycle_id="cyc-sentinel")
            gate = rep.decision_gate()
            print(f"  {tag} {label:<30} ok={rep.ok!s:<5} "
                  f"identity_pinned={rep.identity_pinned!s:<5} "
                  f"accepted={rep.accepted_intent_ids} "
                  f"gate.ok={gate['ok']!s:<5} drop={gate['drop_intent_ids']} "
                  f"reasons={list(rep.by_intent('A').reason_codes)}")
    rep = new_ev.evaluate_open_intents([dict(INTENT)], snaps, now_ms=NOW_MS,
                                       identity=None, cycle_id="cyc-none")
    print(f"  对照 修复后 identity=None          ok={rep.ok!s:<5} "
          f"identity_pinned={rep.identity_pinned!s:<5} "
          f"gate.ok={rep.decision_gate()['ok']!s:<5} "
          f"reasons={list(rep.by_intent('A').reason_codes)}")
    rep = new_ev.evaluate_open_intents([dict(INTENT)], snaps, now_ms=NOW_MS,
                                       identity=IDENTITY, cycle_id="cyc-real")
    print(f"  对照 修复后 真实身份                identity_pinned={rep.identity_pinned!s:<5} "
          f"gate.ok={rep.decision_gate()['ok']!s:<5} "
          f"is_unpinned(真实身份)={new_ev.is_unpinned_identity(IDENTITY)}")


def s2_source_policy() -> None:
    sect("S2  Q-NEW-3 字段级分源门禁在生产路径是否默认生效")
    orch = load_patched_orchestrator()
    floor = orch.RESEARCH_EVIDENCE_REQUIRED_SOURCES
    print(f"  接线常量 RESEARCH_EVIDENCE_REQUIRED_SOURCES = {floor!r}"
          f"   ALLOW_UNPINNED_RESEARCH_EVIDENCE = {orch.ALLOW_UNPINNED_RESEARCH_EVIDENCE}")
    print(f"  _resolve_required_sources((), {floor!r}) = "
          f"{new_ev._resolve_required_sources((), floor)!r}")

    degraded = snap_ticker_degraded()
    print("  夹具：ticker 源可用但只有 lastPrice，"
          f"missing_fields={degraded.source('ticker').missing_fields}")
    intent = [dict(INTENT, interval="1h")]

    # 生产入口 A/B：直接改接线常量后调用同一个门禁函数
    class DB:
        def __init__(self):
            self.events = []

        def log_risk_event(self, **kwargs):
            self.events.append(kwargs)

    def real_intent(payload: dict):
        """真实 TradeIntent（含 DecisionEvidence），门禁入参口径与生产一致。"""
        from agents.models import DecisionEvidence, TradeIntent
        return TradeIntent(
            symbol=payload["symbol"], action="open", side=payload["side"],
            qty=0.01, client_order_id="cid-1", stop_loss=payload["stop_loss"],
            take_profit=payload["take_profit"],
            evidence=DecisionEvidence(
                market_regime="range", higher_timeframe_trend="up",
                supporting_evidence=["synth"], trigger_price=payload["trigger_price"],
                invalidation_price=payload["stop_loss"],
                invalidation_operator="close_below", confirmation_interval="1h",
                risk_reward_ratio=1.5, chase_distance_pct=0.5,
                data_observed_at_ms=AS_OF_MS, confidence_basis="synth"),
        )

    class Decision:
        pipeline_decision_id = None

        def __init__(self):
            self.intents = [real_intent(intent[0])]
            self.reasoning = ""
            self.decision = "open"

        def model_copy(self, update):
            clone = Decision.__new__(Decision)
            clone.intents = update.get("intents", self.intents)
            clone.reasoning = update.get("reasoning", self.reasoning)
            clone.decision = update.get("decision", self.decision)
            return clone

    for label, value in (("修复前 default=()", ()), ("修复后 default=('ticker',)", ("ticker",))):
        orch.RESEARCH_EVIDENCE_REQUIRED_SOURCES = value
        db = DB()
        out, issues = orch._research_evidence_gate(
            Decision(), {("BNBUSDT", "1h"): degraded}, db, "run-1")
        blocked = [e for e in db.events if e["event_type"] == "research_evidence_blocked"]
        reasons = blocked[0]["details"] if blocked else "(无 blocked 事件)"
        print(f"  {label:<28} issues={issues} 剩下 intents={len(out.intents)} "
              f"decision={out.decision}")
        print(f"      blocked 理由 = {reasons}")
    orch.RESEARCH_EVIDENCE_REQUIRED_SOURCES = floor

    # 隔离身份因素：显式固定身份 + 接线缺省集合
    for label, snap in (("ticker 字段不齐", degraded), ("ticker 字段齐全", snap_full())):
        rep = new_ev.evaluate_open_intents(
            [dict(INTENT, interval="1h")], [snap], now_ms=NOW_MS, identity=IDENTITY,
            cycle_id="cyc-floor", default_required_sources=floor)
        print(f"  固定身份 + 接线缺省 {label:<18} accepted={rep.accepted_intent_ids} "
              f"reasons={list(rep.by_intent('A').reason_codes)}")
    rep = new_ev.evaluate_open_intents(
        [dict(INTENT, interval="1h")], [degraded], now_ms=NOW_MS, identity=IDENTITY,
        cycle_id="cyc-floor", default_required_sources=())
    print(f"  固定身份 + 修复前缺省 ()           accepted={rep.accepted_intent_ids}"
          f"  ← 缺 ticker 字段仍放行（原始缺口）")


def s3_cycle() -> None:
    sect("S3  Q-NEW-2 binding_id 的轮次分量")
    orch = load_patched_orchestrator()
    snap = snap_full()

    class DB:
        def __init__(self):
            self.events = []

        def log_risk_event(self, **kwargs):
            self.events.append(kwargs)

    # allow_unpinned 关着时开仓会被丢，放行事件不会落 ⇒ 为观察 binding，
    # 这里直接调用模块层并复刻接线的 cycle_id 传法（run_id 为 service 侧每轮 uuid4）。
    for label, cycle in (
            ("修复前（cycle_id 恒 ''）第1轮", ""),
            ("修复前（cycle_id 恒 ''）第2轮", ""),
            ("修复后 run_id=run-aaaa 第1轮", "run-aaaa"),
            ("修复后 run_id=run-bbbb 第2轮", "run-bbbb")):
        rep = new_ev.evaluate_open_intents(
            [dict(INTENT)], [snap], now_ms=NOW_MS, identity=IDENTITY,
            cycle_id=str(getattr(None, "pipeline_decision_id", "") or cycle))
        b = rep.by_intent("A")
        print(f"  {label:<30} cycle={rep.cycle_id!r:<11} "
              f"evid={b.evidence_id} bind={b.binding_id}")
    print("  接线补丁实际写入："
          f"{'or run_id or \"\"' in (SCRATCH / 'apply_root' / 'src' / 'agents' / 'orchestrator.py').read_text(encoding='utf-8')}")


def s4_policy_floor() -> None:
    sect("S4  Q-NEW-4/5 并集语义 + 畸形声明 fail-closed")
    snap = snap_ticker_degraded()

    def run(required=None, default=None) -> list[str]:
        payload = dict(INTENT, interval="1h")
        if required is not None:
            payload["required_sources"] = required
        kwargs = {"default_required_sources": default} if default is not None else {}
        rep = new_ev.evaluate_open_intents([payload], [snap], now_ms=NOW_MS,
                                           identity=IDENTITY, cycle_id="cyc-floor", **kwargs)
        return list(rep.by_intent("A").reason_codes)

    cases = [
        ("declared=[] default=()（控制组）", None, None),
        ("declared=[] default=('ticker',)", None, ("ticker",)),
        ("declared=('ticker.last_price',) default=('mark_funding.funding_rate',)", ("ticker.last_price",), ("mark_funding.funding_rate",)),
        ("declared='mark_funding'（裸字符串）", "mark_funding", None),
        ("declared=('mark_funding.',)（尾点）", ("mark_funding.",), None),
        ("declared=('',)（空条目）", ("",), None),
    ]
    for label, req, dflt in cases:
        print(f"  {label:<62} reasons={run(req, dflt)}")


def main() -> int:
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                          capture_output=True).stdout.decode().strip()
    print(f"HEAD={head}")
    s1_sentinel()
    s2_source_policy()
    s3_cycle()
    s4_policy_floor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
