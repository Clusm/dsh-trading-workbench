"""验证主循环「最后一轮兜底 submit_decision」修复。

事故形态：某个工具坏了，模型每轮都重试它、从不 submit，
5 轮用完 -> decision is None -> 笼统报「主Agent连续多轮未输出最终JSON」。

修复要点：
1. 最后一轮无条件把 tool_choice 钉死为 submit_decision；
2. 最后一轮的校验改为宽松（严格纠错已无下一轮可重试），不合规的降级成 no_trade 并记事件。

双向自证：--inject-bug 会把修复注回成旧逻辑，应当看到 FAIL 且退出码非 0。
不联网（execute_tool 被替身替换）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

SRC = ROOT / "src" / "agents" / "orchestrator.py"

OLD_LINE = 'if (force_submit or is_final_round) else "auto")'
NEW_LINE = 'if force_submit else "auto")'
OLD_STRICT = "strict = submit_attempts <= MAX_STRICT_SUBMITS and not is_final_round"
NEW_STRICT = "strict = submit_attempts <= MAX_STRICT_SUBMITS"

FIXED_PAYLOAD = {
    "decision": "no_trade",
    "reasoning": "行情工具连续失败，无法获取可信数据，本轮不交易。",
    # intents 是必填数组（校验里 is None 会直接判失败），无操作必须显式给 []
    "intents": [],
    "position_reviews": [],
    "strategy_refs": [],
}
BAD_PAYLOAD = {   # 只让 decision 非法，失败原因单一，便于断言降级文案
    "decision": "not_a_valid_action",
    "intents": [],
    "position_reviews": [],
}

_results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, extra: str = "") -> None:
    _results.append((ok, name))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))


# --------------------------------------------------------------------------- 替身


class FakeLLM:
    """tool_choice 被强制时提交决策，否则模拟模型重试坏掉的工具。"""

    def __init__(self, payload: dict | None = None, always_query: bool = True) -> None:
        self.choices: list = []
        self.payload = payload if payload is not None else FIXED_PAYLOAD
        self.always_query = always_query

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.choices.append(tool_choice)
        forced = isinstance(tool_choice, dict)
        if forced or not self.always_query:
            fn = {
                "name": "submit_decision",
                "arguments": json.dumps(self.payload, ensure_ascii=False),
            }
            cid = "call_s"
        else:
            fn = {
                "name": "query_symbol_detail",
                "arguments": json.dumps({"symbol": "BTCUSDT", "interval": "15m"}),
            }
            cid = f"call_q{len(self.choices)}"
        return {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": cid, "type": "function", "function": fn}]},
            }],
            "usage": {},
        }


class FakeDB:
    def __init__(self) -> None:
        self.risk_events: list[dict] = []

    def log_llm_call(self, **kw) -> None:
        pass

    def log_risk_event(self, **kw) -> None:
        self.risk_events.append(kw)

    def resolve_risk_incidents_by_type(self, _event_type: str) -> None:
        return None


class FakeLogger:
    def __init__(self) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="smoke_fs_"))

    def log_llm(self, name: str, obj: dict) -> Path:
        p = self._dir / f"{name}.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        return p


class FakeRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record(self, **kw) -> None:
        self.calls.append(kw)


def run_once(*, llm: FakeLLM, db: FakeDB | None = None,
             logger: FakeLogger | None = None, recorder: FakeRecorder | None = None):
    """跑一次 run_orchestrator，返回 (decision, llm, db, logger, recorder)。"""
    import agents.orchestrator as orch
    from agents.models import MacroBrief, MarketBrief, OnchainBrief, StrategyRefs

    db = db or FakeDB()
    logger = logger or FakeLogger()
    recorder = recorder or FakeRecorder()

    # 工具替身：恒定失败，模拟被名字遮蔽搞坏的 query_symbol_detail
    orch.execute_tool = lambda name, args: {"error": "模拟工具故障（不联网）"}

    decision = orch.run_orchestrator(
        llm=llm, model="fake-model", recorder=recorder, db=db, logger=logger,
        run_id="test-run",
        market=MarketBrief(), macro=MacroBrief(), onchain=OnchainBrief(),
        strategy=StrategyRefs(), risk_envelope={}, action_space=None, prices={},
    )
    return decision, llm, db, logger, recorder


# --------------------------------------------------------------------------- 用例


def suite() -> None:
    import agents.orchestrator as orch

    print("[1] 修复生效：模型一直重试坏工具时，最后一轮被钉死 submit_decision")
    llm = FakeLLM()
    d, llm, db, _, _ = run_once(llm=llm)
    n = len(llm.choices)
    check(n == orch.MAX_ROUNDS, f"轮次 == MAX_ROUNDS({orch.MAX_ROUNDS})", f"实际 {n}")
    check(isinstance(llm.choices[-1], dict)
          and llm.choices[-1].get("function", {}).get("name") == "submit_decision",
          "最后一轮 tool_choice 强制为 submit_decision")
    check(all(c == "auto" for c in llm.choices[:-1]),
          "其余轮次仍为 auto（不挤压正常查询轮次）")
    check("主Agent连续多轮未输出最终JSON" not in (d.reasoning or ""),
          "不再落到「连续多轮未输出」兜底文案", f"reasoning={d.reasoning[:40]!r}")
    check(d.decision == "no_trade" and "行情工具连续失败" in (d.reasoning or ""),
          "拿到模型给出的明确决策与原因")

    print()
    print("[2] 修复生效：最后一轮校验失败时宽松降级，而不是变成「未产出决策」")
    llm = FakeLLM(payload=BAD_PAYLOAD)
    d, llm, db, _, _ = run_once(llm=llm)
    check(d is not None, "仍产出决策对象（不是 None）")
    check(d.decision == "no_trade", "非法 payload 被降级为 no_trade", f"实际 {d.decision}")
    check("主Agent连续多轮未输出最终JSON" not in (d.reasoning or ""),
          "降级有具体原因，不是笼统兜底文案", f"reasoning={d.reasoning[:50]!r}")
    check(any(e.get("event_type") == "decision_downgraded" for e in db.risk_events),
          "记 decision_downgraded 事件", f"事件 {len(db.risk_events)} 条")

    print()
    print("[3] 正常路径不受影响：模型第一轮就 submit")
    llm = FakeLLM(always_query=False)
    d, llm, db, _, _ = run_once(llm=llm)
    check(len(llm.choices) == 1, "只跑 1 轮", f"实际 {len(llm.choices)}")
    check(llm.choices[0] == "auto", "首轮 tool_choice 保持 auto")
    check(d.decision == "no_trade" and "行情工具连续失败" in (d.reasoning or ""),
          "决策内容正确透传")

    print()
    print("[4] 结构性检查")
    src = SRC.read_text(encoding="utf-8")
    check(OLD_LINE in src, "修复代码在位（最后一轮兜底）")
    check(OLD_STRICT in src, "修复代码在位（末轮宽松校验）")
    check("for round_no in range(MAX_ROUNDS)" in src, "轮次使用 MAX_ROUNDS 常量")


def suite_injected() -> None:
    """注回旧逻辑后，[1] 的核心断言必须全部 FAIL。"""
    print("[R] 反向验证：注回旧逻辑（最后一轮不兜底）")
    llm = FakeLLM()
    d, llm, db, _, _ = run_once(llm=llm)
    check(isinstance(llm.choices[-1], dict), "最后一轮 tool_choice 被强制  [期望 FAIL]")
    check("主Agent连续多轮未输出最终JSON" not in (d.reasoning or ""),
          "不再落到兜底文案  [期望 FAIL]", f"reasoning={d.reasoning[:50]!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inject-bug", action="store_true",
                    help="临时注回旧逻辑，验证测试确实会 FAIL")
    args = ap.parse_args()

    src = SRC.read_text(encoding="utf-8")
    if OLD_LINE not in src or OLD_STRICT not in src:
        print("源码里找不到修复代码，无法校验（是否已被改回？）")
        return 2

    if not args.inject_bug:
        suite()
    else:
        bak = SRC.with_suffix(".py.bak")
        shutil.copy2(SRC, bak)
        try:
            SRC.write_text(
                src.replace(OLD_LINE, NEW_LINE).replace(OLD_STRICT, NEW_STRICT),
                encoding="utf-8")
            import importlib
            import agents.orchestrator as orch
            importlib.reload(orch)
            suite_injected()
        finally:
            shutil.copy2(bak, SRC)
            bak.unlink()
            import importlib
            import agents.orchestrator as orch
            importlib.reload(orch)

    passed = sum(1 for ok, _ in _results if ok)
    total = len(_results)
    print()
    print("=" * 68)
    print(f"结果：PASS {passed} / FAIL {total - passed}")
    print("=" * 68)
    if args.inject_bug:
        print("（反向验证模式：FAIL 是预期结果，退出码应为非 0）")
        return 0 if (total - passed) > 0 else 1
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
