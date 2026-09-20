"""U03 r2 修复后补充探针：自报 claim 的"附加"语义必须仍可用且仍受程序校验。"""
from __future__ import annotations

import json
import sys
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
results = json.loads((FIX / "market_tool_results.json").read_text(encoding="utf-8"))["results"]
SNAPS = [E.snapshot_from_tool_result(results["BNBUSDT_1h"], interval="1h")]


def run(claims, **over):
    intent = {
        "intent_id": "i", "symbol": "BNBUSDT", "action": "OPEN", "side": "LONG",
        "trigger_price": 744.0, "stop_loss": 700.0, "take_profit": 800.0,
        "evidence": {"confirmation_interval": "1h", "data_observed_at_ms": AS_OF},
    }
    intent.update(over)
    if claims is not None:
        intent["claims"] = claims
    rep = E.evaluate_open_intents([intent], SNAPS, now_ms=NOW, identity=IDENTITY,
                                  cycle_id="cyc-r2v")
    item = rep.intents[0]
    return (item.status, item.accepted, list(item.reason_codes),
            [(c["claim_id"], c["expr"], c["status"]) for c in item.claims])


print("== 事实基准：BNBUSDT 1h closed_close = 744.13 ==")
print("[A] 自报 claim 与程序一致（冗余声明）:", run(
    [{"kind": "close_above_price", "expr": "1h:closed_close > 744", "expected": True}]))
print("[B] 自报**附加**为真的额外 claim（LONG 侧加一条 close_below_price）:", run(
    [{"claim_id": "extra", "kind": "close_below_price",
      "expr": "1h:closed_close < 1000", "expected": True}]))
print("[C] 自报附加为假的额外 claim（仍受程序校验）:", run(
    [{"claim_id": "false-extra", "kind": "close_below_price",
      "expr": "1h:closed_close < 1", "expected": True}]))
print("[D] 自报附加越界操作数（引用不存在的事实）:", run(
    [{"claim_id": "oob", "kind": "close_above_price",
      "expr": "1h:closed_close > 1h:prior_high", "expected": True}]))
print("[E] 自报与程序基准同 kind 但不同阈值（替换尝试）:", run(
    [{"kind": "close_above_price", "expr": "1h:closed_close > 1", "expected": True}]))
print("[F] expected 非布尔:", run(
    [{"kind": "close_above_price", "expr": "1h:closed_close > 744", "expected": "yes"}]))
print("[G] 无 claims 字段（基线路径）:", run(None))
