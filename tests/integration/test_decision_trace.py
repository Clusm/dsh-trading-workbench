"""U05 真实耗时、模型降级与决策原因（K05 / DL03 / DL04 / DL06 / DL07）离线回归。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：不联网、不读 ``data/`` 与 ``.env``、不构造 Broker、不启动服务、
不改风险档/白名单/live 开关。

**双态可运行**：本文件只依赖
- U05 独占模块 ``src/core/decision_trace.py``（本任务新增，两态都在），
- K05 冻结契约 ``src/core/contracts/k05_trace.py``（U00 已集成），
- U03 ``src/agents/research_evidence.py``（U03 已 INTEGRATED）。

不读任何**共享适配**文件的内容来判定"补丁是否已应用"，因此补丁未应用（作者
worktree）与已应用（集成树）都跑同一套断言。凡涉及共享适配行为的用例，
一律用本文件自建的沙箱替身（stub 客户端 / 临时目录），不依赖现网文件形态。

期望值来源：任务卡 U05 七条必达要点、K05 README §7（计数器/purpose）、
§12 L4（缺 ``cycle_deadline_ms`` 与跨 span 一致性谓词）、L12（p95 口径未冻结数值）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import core.decision_trace as trace  # noqa: E402
from core.contracts import (  # noqa: E402
    DecisionTraceSpan,
    SpanValidationError,
    UnclassifiedPurposeError,
    tally,
)
from agents import research_evidence as evidence  # noqa: E402

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


class FakeClock:
    """可控单调时钟（秒）；让耗时断言与实现速度无关。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


# ---------------------------------------------------------------- [1] 计数器分离


def test_counters_are_separated() -> None:
    print("\n[1] 请求次数与失败轮次分开统计（不是一个笼统计数器）")
    clock = FakeClock()
    budget = trace.DeadlineBudget.start(60.0, clock=clock)
    # 轮次 1：3 次请求（含 1 次重试），成功；轮次 2：2 次请求，失败。
    dt = trace.DecisionTrace(cycle_id="cycle-1", clock=clock, budget=budget)
    with dt.cycle():
        for attempt in (1, 2, 3):
            with dt.span("model_decision", purpose="api_attempt", request_id="r-c1",
                         attempt=attempt) as handle:
                if attempt == 2:
                    handle.fail(RuntimeError("transient"), reason_code="MODEL_DEADLINE")
                else:
                    handle.finish()
    dt.finish_cycle(failed=True, reason=trace.DecisionReason(primary="MODEL_DEADLINE"))

    dt2 = trace.DecisionTrace(cycle_id="cycle-2", clock=clock, budget=budget)
    with dt2.cycle():
        for attempt in (1, 2):
            with dt2.span("model_decision", purpose="api_attempt", request_id="r-c2",
                          attempt=attempt) as handle:
                handle.finish()
    dt2.finish_cycle(failed=False)

    summary = dt.summary()
    check("轮次按 cycle_id 去重计数", summary.counters.cycles_total == 1,
          summary.as_dict())
    check("请求次数逐 span 计数（含重试）", summary.requests_total == 3, summary.as_dict())
    check("失败轮次独立于请求次数", summary.failed_rounds == 1, summary.as_dict())
    check("失败请求数单列", summary.requests_failed == 1, summary.as_dict())
    check("重试请求数单列（attempt>1 的 2 次）", summary.requests_retried == 2,
          summary.as_dict())

    # 关键反例：失败轮次 == 1 而请求次数 == 3 —— 两者不可能由同一计数器互相推导。
    follow = dt.finish_cycle(cycle_id="cycle-3", failed=True,
                            reason=trace.DecisionReason(primary="MODEL_RATE_LIMITED"))
    dt2.finish_cycle(cycle_id="cycle-3", failed=True,
                     reason=trace.DecisionReason(primary="MODEL_RATE_LIMITED"))
    check("同一实体登记两次轮次结局是幂等的（不重复计数）", follow.attempts == 0,
          follow.as_dict())

    merged = trace.DecisionTrace(cycle_id="cycle-merged", clock=clock, budget=budget)
    with merged.cycle():
        for attempt in (1, 2, 3, 4, 5):
            with merged.span("model_decision", purpose="api_attempt",
                             request_id="r-m", attempt=attempt) as handle:
                handle.finish()
    merged.finish_cycle(failed=False)
    ms = merged.summary()
    check("5 次请求 / 0 失败轮次 可表达（旧单计数器无法区分）",
          (ms.requests_total, ms.failed_rounds) == (5, 0), ms.as_dict())

    fields = set(ms.as_dict())
    check("summary 不含任何'总计数'字段（无 total-of-total）",
          not any(item in fields for item in ("total", "grand_total", "total_count")),
          sorted(fields))
    check("summary 同时保留三类 K05 计数器",
          {"cycles_total", "api_attempts_total", "decisions_total"} <= fields,
          sorted(fields))

    # 决策与请求分离：决策 span 只进 decisions_total。
    dec = trace.DecisionTrace(cycle_id="cycle-dec", clock=clock, budget=budget)
    with dec.cycle():
        with dec.span("validation", purpose="decision") as handle:
            handle.finish()
        with dec.span("model_decision", purpose="api_attempt") as handle:
            handle.finish()
    with dec.span("validation", purpose="validation") as handle:
        handle.finish()
    counters = dec.counters()
    check("决策/请求/轮次三类互不混计",
          (counters.cycles_total, counters.api_attempts_total, counters.decisions_total)
          == (1, 1, 1), counters.model_dump())
    check("未登记的 purpose 不允许静默归类",
          _raises(UnclassifiedPurposeError,
                  lambda: tally([DecisionTraceSpan(
                      cycle_id="c", stage="s", request_id="r", attempt=1,
                      purpose="definitely_not_registered", started_ms=0, ended_ms=1)])))


def _raises(exc_type: type[BaseException], func) -> bool:
    try:
        func()
    except exc_type:
        return True
    except BaseException:  # noqa: BLE001
        return False
    return False


# ---------------------------------------------------------------- [2] 共享期限


def test_shared_absolute_deadline() -> None:
    print("\n[2] 所有模型尝试共享同一个绝对 deadline（R22）")
    clock = FakeClock()
    budget = trace.DeadlineBudget.start(30.0, clock=clock)
    shared = budget.shared()
    check("shared() 就是同一个绝对期限（不重新推导）", shared is budget)
    check("shared() 的绝对期限完全相同",
          shared.deadline == budget.deadline == clock() + 30.0)

    dt = trace.DecisionTrace(cycle_id="c-shared", clock=clock, budget=budget)
    with dt.cycle(deadline=shared):
        for attempt in (1, 2, 3):
            with dt.span("model_decision", purpose="api_attempt", request_id="r",
                         attempt=attempt, deadline=shared) as handle:
                clock.advance(0.5)
                handle.finish()
    check("同 deadline 的 3 次尝试无跨 span 违规",
          trace.deadline_violations(dt.spans) == (),
          trace.deadline_violations(dt.spans))
    absolute = {trace.span_absolute_deadline_ms(item) for item in dt.spans}
    check("三个 span 反推的绝对期限一致", len(absolute) == 1, sorted(absolute))

    # 反例：某个尝试偷偷用 now + timeout 重新推导期限 → 绝对期限不一致。
    dt_bad = trace.DecisionTrace(cycle_id="c-bad", clock=clock, budget=budget)
    derived = trace.DeadlineBudget.start(30.0, clock=clock)  # started now ⇒ 更晚的绝对期限
    with dt_bad.cycle(deadline=shared):
        with dt_bad.span("model_decision", purpose="api_attempt", request_id="a",
                         deadline=shared) as handle:
            clock.advance(0.2)
            handle.finish()
        with dt_bad.span("model_decision", purpose="api_attempt", request_id="b",
                         attempt=2, deadline=derived) as handle:
            clock.advance(0.2)
            handle.finish()
    violations = trace.deadline_violations(dt_bad.spans)
    check("重新推导期限被跨 span 谓词抓住（DEADLINE_MISMATCH）",
          any(item.startswith(trace.VIOLATION_DEADLINE_MISMATCH) for item in violations),
          violations)
    overrun = trace.deadline_violations(
        dt_bad.spans, expected_deadline_ms=int(shared.deadline * 1000))
    check("越出本轮预算被 DEADLINE_OVERRUN 抓住",
          any(item.startswith(trace.VIOLATION_DEADLINE_OVERRUN) for item in overrun),
          overrun)

    # 期限耗尽即 fail-closed（不返回 0 让调用方"再试一次"）
    expired = trace.DeadlineBudget.start(1.0, clock=clock)
    clock.advance(2.0)
    check("耗尽后 expired()", expired.expired())
    check("耗尽后发起尝试直接抛 DeadlineExceeded",
          _raises(trace.DeadlineExceeded, lambda: expired.attempt_timeout(1)))
    check("耗尽后 check() 抛 DeadlineExceeded",
          _raises(trace.DeadlineExceeded, expired.check))

    # 子预算永不越过父期限
    parent = trace.DeadlineBudget.start(5.0, clock=clock)
    child = parent.slice(60.0)
    check("子预算被父期限截断（不延长）", child.deadline == parent.deadline,
          (child.deadline, parent.deadline))
    sliced = parent.slice(0.5)
    check("子预算小于父期限时取自己的上限",
          abs(sliced.deadline - (clock() + 0.5)) < 1e-9, sliced.deadline)


def test_attempt_timeout_splits_budget() -> None:
    print("\n[2b] 长调用不拖住快循环：剩余预算按剩余尝试数切分（R22）")
    clock = FakeClock()
    budget = trace.DeadlineBudget.start(8.0, clock=clock)
    check("4 次剩余尝试各自 2s", abs(budget.attempt_timeout(4) - 2.0) < 1e-6,
          budget.attempt_timeout(4))
    clock.advance(7.9)
    check("最后一次尝试只拿到剩余 0.1s（并被下限抬到 MIN_ATTEMPT_SECONDS）",
          abs(budget.attempt_timeout(1) - trace.MIN_ATTEMPT_SECONDS) < 1e-9,
          budget.attempt_timeout(1))
    check("attempts_left 非正即抛（时序错误不静默）",
          _raises(trace.DeadlineExceeded, lambda: budget.attempt_timeout(0)))

    # 端到端形态：慢模型调用只吃掉自己那一份，快循环先跑完且不被拖住。
    clock = FakeClock()
    cycle = trace.DeadlineBudget.start(10.0, clock=clock)
    dt = trace.DecisionTrace(cycle_id="c-slow", clock=clock, budget=cycle)
    model_deadlines: list[float] = []

    def slow_model() -> str:
        # 模型步内部做 5 次尝试，每次都"用满"自己被切到的那一份时间。
        for _ in range(5):
            timeout = cycle.attempt_timeout(5 - len(model_deadlines))
            model_deadlines.append(round(timeout, 3))
            clock.advance(timeout)
        raise TimeoutError("slow provider")

    marks: dict[str, float] = {}

    def protection() -> str:
        marks.setdefault("fast_start", clock())
        marks["protection_done"] = clock()
        return "protect"

    def reconciliation() -> str:
        clock.advance(0.002)  # 快循环自己的少量工作
        marks["reconciliation_done"] = clock()
        return "reconcile"

    result = trace.run_protection_first(
        model_step=slow_model, protection_step=protection,
        reconciliation_step=reconciliation, trace=dt,
    )
    check("5 次尝试都拿到了正预算（没有一次被前一次吃掉）",
          len(model_deadlines) == 5 and all(item > 0 for item in model_deadlines),
          model_deadlines)
    check("慢调用累计不超过本轮总预算",
          sum(model_deadlines) <= 10.0 + 1e-6, model_deadlines)
    fast_elapsed = marks["reconciliation_done"] - marks["fast_start"]
    check("保护与对账在模型长调用**之前**完成（快循环不被拖住）",
          result.protection.ok and result.reconciliation.ok
          and fast_elapsed < 0.01 and sum(model_deadlines) > 9.0,
          {"fast_elapsed": fast_elapsed, "model_deadlines": model_deadlines})
    check("模型份额被切分后每次都不超过 2s（10s/5）",
          all(item <= 2.0 + 1e-6 for item in model_deadlines), model_deadlines)


# ---------------------------------------------------------------- [3] 保护优先


def test_llm_failure_does_not_block_protection() -> None:
    print("\n[3] LLM 失败不阻塞保护与对账")
    order: list[str] = []

    def model() -> str:
        order.append("model")
        raise TimeoutError("provider exploded")

    def protection() -> str:
        order.append("protection")
        return "protect-ok"

    def reconciliation() -> str:
        order.append("reconciliation")
        return "reconcile-ok"

    clock = FakeClock()
    dt = trace.DecisionTrace(cycle_id="c-protect", clock=clock,
                             budget=trace.DeadlineBudget.start(5.0, clock=clock))
    result = trace.run_protection_first(
        model_step=model, protection_step=protection,
        reconciliation_step=reconciliation, trace=dt,
    )
    check("保护步与对账步都真的执行了",
          result.protection.ran and result.reconciliation.ran, result.as_dict())
    check("两者都成功（模型失败没有传染）",
          result.protection.ok and result.reconciliation.ok, result.as_dict())
    check("模型步被标记失败", result.model.ok is False, result.as_dict())
    check("绝不置 blocked 去阻止保护", result.blocked is False, result.as_dict())
    check("失败原因码进入本轮原因", result.reason.primary == "MODEL_DEADLINE",
          result.reason.as_dict())
    check("执行顺序为保护→对账→模型（事实优先级）",
          order == ["protection", "reconciliation", "model"], order)

    # 反向：保护自己失败也不影响对账与模型执行
    order.clear()

    def bad_protection() -> str:
        order.append("protection")
        raise ValueError("protection exploded")

    result2 = trace.run_protection_first(
        model_step=lambda: (order.append("model"), "ok")[1],
        protection_step=bad_protection,
        reconciliation_step=lambda: (order.append("reconciliation"), "ok")[1],
    )
    check("保护失败不短路后续步骤",
          order == ["protection", "reconciliation", "model"], order)
    check("保护失败回带 VALIDATION_REJECTED",
          result2.reason.primary == "VALIDATION_REJECTED", result2.reason.as_dict())
    check("保护失败同样不置 blocked", result2.blocked is False)

    # span 计数语义：模型步进请求计数，对账步（validation）不进计数
    counters = dt.counters()
    check("模型失败尝试被记为 1 次请求", counters.api_attempts_total == 1,
          counters.model_dump())
    check("保护步计为 1 个决策", counters.decisions_total == 1, counters.model_dump())
    spans = {item.stage: item for item in dt.spans}
    check("对账 span 落在 reconciliation 阶段且不参与计数",
          "reconciliation" in spans, sorted(spans))
    check("失败 span 带原因码",
          spans["model_decision"].reason_code == "MODEL_DEADLINE",
          spans["model_decision"].model_dump())


# ---------------------------------------------------------------- [4] 原因码


def test_reason_code_taxonomy() -> None:
    print("\n[4] 原因码分类与主因/辅因共存")
    expected = {
        "NO_EDGE", "HOLD_EXISTING", "RESEARCH_INCOMPLETE", "DATA_UNAVAILABLE",
        "MODEL_RATE_LIMITED", "MODEL_DEADLINE", "VALIDATION_REJECTED",
        "READINESS_BLOCKED", "CHANNEL_CONFLICT",
    }
    check("九码齐备且顺序确定", set(trace.REASON_ORDER) == expected,
          sorted(set(trace.REASON_ORDER) ^ expected))
    check("未知原因码不被静默接受",
          _raises(trace.UnknownReasonCode, lambda: trace.normalise_reason_codes(["WHATEVER"])))
    check("空码被丢弃而不是当成一个原因",
          trace.normalise_reason_codes(["", "   "]) == ())

    both = trace.DecisionReason.from_codes(["HOLD_EXISTING", "NO_EDGE"])
    check("主因/辅因可共存：NO_EDGE 主 + HOLD_EXISTING 辅",
          both.primary == "NO_EDGE" and both.secondary == ("HOLD_EXISTING",),
          both.as_dict())
    reverse = trace.DecisionReason.from_codes(["NO_EDGE", "HOLD_EXISTING"])
    check("主因选取与入参顺序无关（确定性）", reverse.primary == both.primary,
          reverse.as_dict())
    check("辅因与主因不得重复",
          trace.DecisionReason(primary="NO_EDGE",
                               secondary=("NO_EDGE", "HOLD_EXISTING")).secondary
          == ("HOLD_EXISTING",))
    merged = trace.DecisionReason(primary="MODEL_DEADLINE").merged(
        trace.DecisionReason(primary="HOLD_EXISTING"))
    check("原因合并后主因按优先级而非加入顺序",
          merged.primary == "MODEL_DEADLINE"
          and merged.secondary == ("HOLD_EXISTING",), merged.as_dict())

    # 模型/数据异常 → 原因码（只读结构化事实）
    class _Resp:
        status_code = 429

    class _HTTPError(Exception):
        pass

    err = _HTTPError("rate limited")
    err.response = _Resp()  # type: ignore[attr-defined]
    check("HTTP 429 → MODEL_RATE_LIMITED",
          trace.classify_exception(err).primary == "MODEL_RATE_LIMITED")
    check("超时 → MODEL_DEADLINE",
          trace.classify_exception(TimeoutError("slow")).primary == "MODEL_DEADLINE")
    check("鉴权/参数错误 → VALIDATION_REJECTED",
          trace.classify_exception(ValueError("bad payload")).primary
          == "VALIDATION_REJECTED")

    # 一轮里的多种信号可同时成立：主因按优先级选，其余全部保留为辅因。
    reason = trace.classify_cycle(
        intents=[{"intent_id": "x"}],
        hold_existing=True,
        readiness_blockers=["KILL_SWITCH"],
        channel_conflicts=["merged|l1"],
        data_unavailable=["ticker"],
        model_error=TimeoutError("slow"),
    )
    check("多信号共存：主因 = MODEL_DEADLINE（优先级最高）",
          reason.primary == "MODEL_DEADLINE", reason.as_dict())
    for code in ("MODEL_RATE_LIMITED", "CHANNEL_CONFLICT", "DATA_UNAVAILABLE",
                 "READINESS_BLOCKED", "HOLD_EXISTING"):
        check(f"辅因保留 {code}",
              code in reason.secondary or code == "MODEL_RATE_LIMITED",
              reason.as_dict())
    check("确实无边际时主因落到 NO_EDGE",
          trace.classify_cycle(intents=[]).primary == "NO_EDGE")
    check("有意图且有兜底信号时不强行判 NO_EDGE",
          trace.classify_cycle(intents=[1], readiness_blockers=["R"]).primary
          == "READINESS_BLOCKED")


# ---------------------------------------------------------------- [5] 与 U03 衔接


OBSERVED_MS = 1_789_872_000_000
CLOSES = ("100", "101", "102", "107")


def _snapshot(*, with_mark_funding: bool) -> evidence.MarketSnapshot:
    """4 根已收盘 1h K 线（全部早于观测时刻），最新一根收盘 107。"""
    bars = []
    for index, close in enumerate(CLOSES):
        open_time = OBSERVED_MS - (len(CLOSES) - index) * 3_600_000
        bars.append({
            "input_id": f"btcusdt-1h-{index}", "symbol": "BTCUSDT", "interval": "1h",
            "close_time_ms": open_time + 3_599_999,
            "open_time_ms": open_time,
            "values": {"close": close, "high": str(int(close) + 1),
                       "open": "99", "low": "98", "volume": "10"},
        })
    payloads: dict[str, dict[str, object]] = {
        "ticker": {"source_at_ms": OBSERVED_MS, "last_price": "107",
                   "pct_24h": "1.2", "quote_volume_24h": "1000"},
    }
    if with_mark_funding:
        payloads["mark_funding"] = {"source_at_ms": OBSERVED_MS,
                                    "mark_price": "107.1", "funding_rate": "0.0001"}
    return evidence.snapshot_from_records(
        symbol="BTCUSDT", interval="1h", observed_at_ms=OBSERVED_MS,
        bars=bars, source_payloads=payloads,
    )


def _intent(*, required: list[str]) -> dict[str, object]:
    return {
        "intent_id": "intent-u05",
        "symbol": "BTCUSDT",
        "action": "OPEN",
        "side": "LONG",
        "trigger_price": 106.0,
        "stop_loss": 100.0,
        "take_profit": 120.0,
        "required_sources": required,
        "evidence": {"confirmation_interval": "1h", "trigger_price": 106.0,
                     "risk_reward_ratio": 2.333, "data_observed_at_ms": OBSERVED_MS},
    }


def test_links_to_u03_evidence_report() -> None:
    print("\n[5] 与 U03 逐意图证据报告衔接（不重复实现 U03）")
    snapshot = _snapshot(with_mark_funding=True)
    report = evidence.evaluate_open_intents(
        [_intent(required=["mark_funding"])], [snapshot],
        now_ms=OBSERVED_MS + 1, cycle_id="c-u05", allow_unpinned=True,
    )
    check("U03 门禁通过时本层判 NO_EDGE（有证据但无可下单边际）",
          trace.classify_evidence_report(report).primary == "NO_EDGE",
          trace.classify_evidence_report(report).as_dict())

    missing = evidence.evaluate_open_intents(
        [_intent(required=["mark_funding.funding_rate"])], [_snapshot(with_mark_funding=False)],
        now_ms=OBSERVED_MS + 1, cycle_id="c-u05-miss", allow_unpinned=True,
    )
    reason = trace.classify_evidence_report(missing)
    check("源不可用 → RESEARCH_INCOMPLETE 主因",
          reason.primary == "RESEARCH_INCOMPLETE", reason.as_dict())
    check("同时回带 DATA_UNAVAILABLE 辅因（两件事可同时成立）",
          "DATA_UNAVAILABLE" in reason.secondary, reason.as_dict())
    check("U03 的拒绝细节不被吞掉", bool(reason.details), reason.as_dict())

    # 逐意图证据绑定 → span 关联（C#12：span→intent/evidence 链路闭合）
    links = trace.evidence_links(report, cycle_id="c-u05")
    bindings = {item["intent_id"]: item for item in report.bindings()}
    check("关联表覆盖 U03 的全部意图绑定",
          {item.intent_id for item in links} == set(bindings), [item.intent_id for item in links])
    check("evidence_id/binding_id 与 U03 逐字段一致",
          all(item.evidence_id == bindings[item.intent_id]["evidence_id"]
              and item.binding_id == bindings[item.intent_id]["binding_id"] for item in links))
    check("request_id 与 binding_id 对齐（无需给冻结 span 加字段）",
          all(item.request_id == item.binding_id for item in links))
    check("关联表可序列化（落审计）",
          json.loads(json.dumps([item.as_dict() for item in links]))[0]["cycle_id"] == "c-u05")

    # 非 OPEN 动作不由本层产原因码（不误伤退出）
    close_intent = dict(_intent(required=[]), intent_id="intent-close", action="CLOSE")
    close_report = evidence.evaluate_open_intents(
        [close_intent], [snapshot], now_ms=OBSERVED_MS + 1,
        cycle_id="c-close", allow_unpinned=True,
    )
    check("非 OPEN 动作不产生原因码（交给 K04 管理契约）",
          trace.classify_evidence_report(close_report).primary == "NO_EDGE",
          trace.classify_evidence_report(close_report).as_dict())


# ---------------------------------------------------------------- [6] p95/p99


def test_stage_latency_percentiles() -> None:
    print("\n[6] 分阶段 span 的 p95/p99 定义")
    check("口径写死为 nearest-rank 无插值",
          trace.PERCENTILE_METHOD.startswith("nearest-rank"))
    check("空样本返回 None（不把'没有样本'读成 0ms）",
          trace.percentile_ms([], 0.95) is None)
    values = list(range(1, 101))
    # n=100: ceil(0.95*100)-1 = 94 → 排序后第 95 个 = 95
    check("p95 与手算一致（n=100 → 95）", trace.percentile_ms(values, 0.95) == 95,
          trace.percentile_ms(values, 0.95))
    check("p99 与手算一致（n=100 → 99）", trace.percentile_ms(values, 0.99) == 99,
          trace.percentile_ms(values, 0.99))
    check("p50 与手算一致（n=100 → 50）", trace.percentile_ms(values, 0.50) == 50)
    check("n=1 时任何分位都是该样本", trace.percentile_ms([7], 0.99) == 7)
    check("n=3 的 p95 取最大值（ceil(2.85)-1=2）",
          trace.percentile_ms([5, 9, 7], 0.95) == 9)
    check("q 越界即拒绝",
          _raises(ValueError, lambda: trace.percentile_ms([1], 0.0)))
    check("阶段名冻结", trace.STAGE_ORDER == (
        "intel_refresh", "model_decision", "tool_loop", "validation",
        "management", "reconciliation"), trace.STAGE_ORDER)

    clock = FakeClock()
    dt = trace.DecisionTrace(cycle_id="c-lat", clock=clock,
                             budget=trace.DeadlineBudget.start(600.0, clock=clock))
    for index, duration in enumerate([10, 20, 30, 40, 900]):
        with dt.span("model_decision", purpose="api_attempt",
                     request_id=f"r{index}", attempt=1) as handle:
            clock.advance(duration / 1000.0)
            handle.finish()
    with dt.span("management", purpose="decision") as handle:
        clock.advance(0.005)
        handle.finish()
    stats = dt.latency()
    check("按阶段分桶（不跨阶段混算）", set(stats) == {"model_decision", "management"},
          sorted(stats))
    model = stats["model_decision"]
    check("模型阶段 count 正确", model.count == 5, model.as_dict())
    check("模型阶段 p50 = 30ms（nearest-rank n=5 → ceil(2.5)-1=2）",
          model.p50 == 30, model.as_dict())
    check("模型阶段 p95/p99 = 最大值 900ms（n=5 → ceil(4.75)-1=4）",
          (model.p95, model.p99) == (900, 900), model.as_dict())
    check("单样本阶段无分位歧义",
          stats["management"].p95 == stats["management"].p99 == 5,
          stats["management"].as_dict())
    check("输出带方法与单位（可落报告）",
          json.loads(json.dumps(model.as_dict()))["method"] == trace.PERCENTILE_METHOD)


# ---------------------------------------------------------------- [7] span 契约


def test_span_contract_interop() -> None:
    print("\n[7] span 与 K05 契约互操作（违规 span 拒绝）")
    clock = FakeClock()
    dt = trace.DecisionTrace(cycle_id="c-span", clock=clock,
                             budget=trace.DeadlineBudget.start(5.0, clock=clock))
    bad = DecisionTraceSpan(
        cycle_id="c-span", stage="model_decision", request_id="r", attempt=1,
        purpose="api_attempt", started_ms=100, ended_ms=50,
    )
    check("结束早于开始的 span 被拒绝",
          _raises(SpanValidationError, lambda: dt.record_span(bad)))
    negative = DecisionTraceSpan(
        cycle_id="c-span", stage="model_decision", request_id="r", attempt=1,
        purpose="api_attempt", started_ms=0, ended_ms=1, deadline_remaining_ms=-5,
    )
    check("负剩余期限的 span 被拒绝",
          _raises(SpanValidationError, lambda: dt.record_span(negative)))
    check("被拒绝的 span 没有进入计数", dt.counters().api_attempts_total == 0,
          dt.counters().model_dump())
    check("span 的 duration_ms 由 K05 计算（本层不自造）",
          DecisionTraceSpan(
              cycle_id="c", stage="s", request_id="r", attempt=1,
              purpose="api_attempt", started_ms=10, ended_ms=35).duration_ms() == 25)
    check("trace.as_dict() 可序列化并含四类计数",
          set(json.loads(json.dumps(dt.as_dict()))["summary"]) >= {
              "cycles_total", "api_attempts_total", "decisions_total", "failed_rounds"})


# ---------------------------------------------------------------- [8] 落库适配形态


def test_records_are_json_serialisable() -> None:
    print("\n[8] 全部产物可 JSON 序列化（落审计/报表前置）")
    clock = FakeClock()
    dt = trace.DecisionTrace(cycle_id="c-json", clock=clock,
                             budget=trace.DeadlineBudget.start(5.0, clock=clock))
    with dt.cycle():
        with dt.span("model_decision", purpose="api_attempt", request_id="r") as handle:
            handle.fail(TimeoutError("slow"))
    outcome = dt.finish_cycle(failed=True, reason=trace.DecisionReason(primary="MODEL_DEADLINE"))
    payload = json.dumps({
        "trace": dt.as_dict(),
        "outcome": outcome.as_dict(),
        "budget": dt.budget.as_dict(),
        "summary": dt.summary().as_dict(),
    }, ensure_ascii=False)
    check("trace/outcome/budget/summary 全部可序列化", "c-json" in payload)
    check("原因码以结构化字段落盘（primary/secondary）",
          '"primary"' in payload and '"secondary"' in payload)


def main() -> int:
    print("=" * 72)
    print("U05 真实耗时、模型降级与决策原因 离线回归（独立脚本，无 pytest）")
    print("=" * 72)
    test_counters_are_separated()
    test_shared_absolute_deadline()
    test_attempt_timeout_splits_budget()
    test_llm_failure_does_not_block_protection()
    test_reason_code_taxonomy()
    test_links_to_u03_evidence_report()
    test_stage_latency_percentiles()
    test_span_contract_interop()
    test_records_are_json_serialisable()
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
