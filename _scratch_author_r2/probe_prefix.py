"""U03 r2 作者探针：复现 D_r1 / B_r1 的 MUST FIX（修复前）。

只读 tracked 夹具，离线。输出用于报告的"修复前失败"对照。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agents.research_evidence as E  # noqa: E402
from core.contracts import RuntimeIdentity  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "evidence"
AS_OF = 1789862400000
NOW = 1789862401000
IDENTITY = RuntimeIdentity(
    schema_version="3.1", environment="demo", account_ref="demo:SYNTH_ACCT",
    artifact_hash="artifact-synth-hash",
    git_commit="0123456789abcdef0123456789abcdef01234567",
    dependency_hash="dependency-synth-hash",
    effective_config_hash="effective-config-hash",
    strategy_hash="strategy-synth-hash", model_route_hash="model-route-synth-hash",
    runtime_instance_id="inst-synth-r1",
)


def results() -> dict:
    return json.loads((FIX / "market_tool_results.json").read_text(encoding="utf-8"))["results"]


def snap_bnb():
    payload = results()["BNBUSDT_1h"]
    return [E.snapshot_from_tool_result(payload, interval="1h")]


def base_intent(**over):
    item = {
        "intent_id": "intent-bnb", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
        "trigger_price": 900.0, "stop_loss": 890.0, "take_profit": 940.0,
        "evidence": {"confirmation_interval": "1h", "data_observed_at_ms": AS_OF},
    }
    item.update(over)
    return item


def show(tag, **kw):
    try:
        rep = E.evaluate_open_intents([base_intent(**kw)], snap_bnb(), now_ms=NOW, **{
            "identity": IDENTITY, "cycle_id": "cyc-r2"} )
    except Exception as exc:  # noqa: BLE001
        print(f"{tag}: RAISED {type(exc).__name__}: {exc}")
        return None
    it = rep.intents[0]
    print(f"{tag}: status={it.status} accepted={it.accepted} "
          f"reasons={list(it.reason_codes)} k02={list(it.bundle_reasons)} "
          f"claims={[(c.get('claim_id'), c['expr'], c['status']) for c in it.claims]}")
    return rep


print("== 事实基准：BNBUSDT 1h 最新已收盘 close = 744.13，trigger_price=900 应 CLAIM_FALSE ==")

print("\n[1] D-P1-1 模型自报 claims 覆盖程序 claim")
show("  A 对照(不报 claims)")
show("  B 反例(自报 close_above_price 1h:closed_close > 0.0001)",
     claims=[{"claim_id": "x", "kind": "close_above_price",
              "expr": "1h:closed_close > 0.0001", "expected": True}])
print("  C 反例(不给 trigger_price，只报 claims)：")
try:
    it = E.evaluate_open_intents(
        [{"intent_id": "i", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
          "stop_loss": 890.0, "take_profit": 940.0,
          "evidence": {"confirmation_interval": "1h", "data_observed_at_ms": AS_OF},
          "claims": [{"kind": "close_above_price",
                      "expr": "1h:closed_close > 0.0001", "expected": True}]}],
        snap_bnb(), now_ms=NOW, identity=IDENTITY, cycle_id="c")
    print(f"    status={it.intents[0].status} accepted={it.intents[0].accepted} "
          f"geometry.entry={it.intents[0].geometry.get('entry')}")
except Exception as exc:  # noqa: BLE001
    print(f"    RAISED {type(exc).__name__}: {exc}")

print("\n[2] B-P1-1 identity=None fail-open")
try:
    rep = E.evaluate_open_intents(
        [base_intent(trigger_price=744.0, stop_loss=700.0, take_profit=800.0)],
        snap_bnb(), now_ms=NOW, cycle_id="c")
    print(f"  identity_pinned={rep.identity_pinned} ok={rep.ok} "
          f"accepted={rep.accepted_intent_ids} gate={rep.decision_gate()}")
except Exception as exc:  # noqa: BLE001
    print(f"  RAISED {type(exc).__name__}: {exc}")

print("\n[3] B-P1-2 畸形 claim 元素使门禁崩溃")
for bad in ([None], ["abc"], [123], [[]], {"a": 1}):
    try:
        E.evaluate_open_intents([base_intent(claims=bad)], snap_bnb(),
                                now_ms=NOW, identity=IDENTITY, cycle_id="c")
        print(f"  claims={bad!r} -> NO RAISE")
    except Exception as exc:  # noqa: BLE001
        print(f"  claims={bad!r} -> RAISED {type(exc).__name__}: {exc}")

print("\n[4] D-P1-2 required_sources 源级粒度：mark_funding 只有 markPrice")
snap = E.snapshot_from_records(
    symbol="BNBUSDT", interval="1h", observed_at_ms=AS_OF,
    bars=[{"input_id": "b1", "close_time_ms": AS_OF - 1, "open_time_ms": AS_OF - 3600000,
           "values": {"close": "744.13"}, "finality": "closed"}],
    source_payloads={"mark_funding": {"available": True, "markPrice": "744.2",
                                      "source_at_ms": AS_OF - 1000},
                     "ticker": {"available": True, "lastPrice": "744.2",
                                "source_at_ms": AS_OF - 500}},
)
mq = snap.source("mark_funding")
print(f"  mark_funding available={mq.available} values={dict(mq.values)} "
      f"missing={mq.missing_fields}")
try:
    rep = E.evaluate_open_intents(
        [{"intent_id": "eth-fund", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
          "trigger_price": 744.0, "stop_loss": 700.0, "take_profit": 800.0,
          "required_sources": ["mark_funding"],
          "evidence": {"confirmation_interval": "1h", "data_observed_at_ms": AS_OF}}],
        [snap], now_ms=AS_OF + 1000, identity=IDENTITY, cycle_id="c")
    it = rep.intents[0]
    print(f"  声明依赖 mark_funding -> status={it.status} accepted={it.accepted} "
          f"reasons={list(it.reason_codes)}")
except Exception as exc:  # noqa: BLE001
    print(f"  RAISED {type(exc).__name__}: {exc}")

print("\n[5] B-P1-3 同周期两单 evidence_id 相同 / 放行路径无落地绑定")
try:
    rep = E.evaluate_open_intents([base_intent(trigger_price=744.0, stop_loss=700.0,
                                               take_profit=800.0, intent_id="A"),
                                   base_intent(trigger_price=745.0, stop_loss=700.0,
                                               take_profit=800.0, intent_id="B")],
                                  snap_bnb(), now_ms=NOW, identity=IDENTITY, cycle_id="c")
    a, b = rep.intents
    print(f"  A evid={a.evidence_id} binding={a.binding_id} accepted={a.accepted}")
    print(f"  B evid={b.evidence_id} binding={b.binding_id} accepted={b.accepted}")
    print(f"  SAME evidence_id ? {a.evidence_id == b.evidence_id}")
except Exception as exc:  # noqa: BLE001
    print(f"  RAISED {type(exc).__name__}: {exc}")

print("\n[6] D-P2-1 status=VALID 而 accepted=False（作者自备夹具）")
try:
    payload = results()["ETHUSDT_1h_sources_missing"]
    snap = E.snapshot_from_tool_result(payload, interval="1h")
    rep = E.evaluate_open_intents(
        [{"intent_id": "intent-eth-funding", "symbol": "ETHUSDT", "action": "OPEN",
          "side": "LONG", "trigger_price": 2400.0, "stop_loss": 2300.0,
          "take_profit": 2600.0, "required_sources": ["mark_funding"],
          "evidence": {"confirmation_interval": "1h", "data_observed_at_ms": AS_OF}}],
        [snap], now_ms=NOW, identity=IDENTITY, cycle_id="c")
    it = rep.intents[0]
    print(f"  status={it.status} accepted={it.accepted} reasons={list(it.reason_codes)}")
    print(f"  decision_gate.drop={rep.decision_gate()['drop_intent_ids']}")
except Exception as exc:  # noqa: BLE001
    traceback.print_exc()
