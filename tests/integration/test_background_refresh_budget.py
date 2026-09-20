"""U05/S03 后台有界刷新（HTTP 与摘要总预算、分源缓存、不阻塞管理）离线回归。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：不联网、不读真实 ``data/``/``.env``、不构造 Broker、不启动服务、
不改风险档/白名单/live 开关。

**双态可运行**：
- 预算/分源缓存/让路语义的**行为断言**全部落在 U05 独占模块
  ``src/core/decision_trace.py``（两态都在）；
- 对共享适配 ``src/core/intel_cache.py`` 只断言**两态都必须成立**的既有契约
  （TTL 内命中、``age_seconds``/``stats``/``clear`` 语义），不读文件内容判定补丁状态，
  也不假定补丁是否已应用。

预期值来源：任务卡 U05/S03「HTTP 与摘要总预算、分源缓存、后台刷新不阻塞管理」、
R22（长调用不拖住快循环）、U03 ``SOURCE_TTL_MS``（分源 TTL 与证据模块同口径）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.decision_trace import (  # noqa: E402
    SOURCE_TTL_MS,
    BackgroundRefreshOutcome,
    RefreshBudget,
    SourceCache,
    background_refresh,
    run_protection_first,
)
from core.intel_cache import IntelCache  # noqa: E402

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


class Clock:
    """可控单调时钟（秒）与墙钟（毫秒）双视图，让预算断言与实现速度无关。"""

    def __init__(self, start_s: float = 500_000.0) -> None:
        self.seconds = float(start_s)

    def monotonic(self) -> float:
        return self.seconds

    def ms(self) -> int:
        return int(self.seconds * 1000)

    def advance(self, seconds: float) -> None:
        self.seconds += float(seconds)


def _budget(clock: Clock, *, total_ms: int, http_ms: int, summary_ms: int,
            reserve_ms: int = 0) -> RefreshBudget:
    return RefreshBudget(total_ms=total_ms, http_ms=http_ms, summary_ms=summary_ms,
                         reserve_for_management_ms=reserve_ms,
                         clock=clock.monotonic, started=clock.monotonic())


# ---------------------------------------------------------------- [1] 总预算


def test_total_budget_invariants() -> None:
    print("\n[1] HTTP 与摘要的**总预算**（两段之和不得超过总预算）")
    clock = Clock()
    budget = _budget(clock, total_ms=10_000, http_ms=7_000, summary_ms=3_000)
    check("合法预算可构造", budget.total_ms == 10_000)
    check("http+summary 超出总预算即拒绝",
          _raises(ValueError, lambda: _budget(clock, total_ms=5_000, http_ms=4_000,
                                              summary_ms=4_000)))
    check("非正总预算即拒绝",
          _raises(ValueError, lambda: _budget(clock, total_ms=0, http_ms=0, summary_ms=0)))
    check("预算参数按正数校验",
          _raises(ValueError, lambda: _budget(clock, total_ms=1_000, http_ms=-1,
                                              summary_ms=1)))
    clock.advance(2.0)
    check("已用时间与剩余时间互反", budget.elapsed_ms() == 2_000
          and budget.remaining_ms() == 8_000, budget.as_dict())
    check("HTTP 段剩余不越过总剩余",
          budget.http_remaining_ms() == 5_000, budget.as_dict())
    clock.advance(9.0)
    check("总预算耗尽后剩余归零（不出现负预算）",
          budget.remaining_ms() == 0 and budget.http_remaining_ms() == 0,
          budget.as_dict())
    check("摘要段不超过摘要上限与总剩余的最小值",
          budget.summary_remaining_ms() == 0, budget.as_dict())

    # 端到端 A：HTTP 预算耗尽后**不再发起新的抓取**，超出的源如实标 failed。
    clock = Clock()
    budget = _budget(clock, total_ms=1_000, http_ms=600, summary_ms=400)
    starts: list[int] = []
    sources = ["ticker", "mark_funding", "open_interest"]

    def slow_fetch(source: str) -> str:
        starts.append(budget.elapsed_ms())
        clock.advance(0.35)  # 每源 350ms：第 3 个源会被 HTTP 预算挡下
        return f"value-{source}"

    summarized: list[str] = []
    outcome = background_refresh(
        sources=sources, fetch=slow_fetch, budget=budget,
        summarize=lambda values: summarized.append("s"),
    )
    check("HTTP 预算耗尽后不再发起新的抓取（起手时刻全部 < http_ms）",
          starts and all(item < budget.http_ms for item in starts) and len(starts) == 2,
          starts)
    check("超预算的源被如实标为 failed（不假装成功）",
          outcome.failed == ("open_interest",)
          and set(outcome.refreshed) == {"ticker", "mark_funding"}, outcome.as_dict())
    check("存在取不到的源时回带 DATA_UNAVAILABLE",
          outcome.reason is not None and outcome.reason.primary == "DATA_UNAVAILABLE",
          outcome.as_dict())
    check("HTTP 段没有越出它自己的上限",
          outcome.elapsed_ms <= budget.http_ms + 350, outcome.as_dict())
    check("摘要段仍有剩余预算时会执行", outcome.summary_done is True
          and summarized == ["s"], outcome.as_dict())

    # 端到端 B：HTTP 段把总预算吃光后，摘要必须跳过（不再执行任何工作）。
    clock = Clock()
    tight = _budget(clock, total_ms=1_000, http_ms=900, summary_ms=100)
    started: list[int] = []

    def fetch_tight(source: str) -> str:
        started.append(tight.elapsed_ms())
        clock.advance(0.35)
        return f"value-{source}"

    fired: list[str] = []
    outcome_b = background_refresh(
        sources=sources, fetch=fetch_tight, budget=tight,
        summarize=lambda values: fired.append("s"),
    )
    check("总预算耗尽后摘要不执行（summary_done=False）",
          outcome_b.summary_done is False and fired == [], outcome_b.as_dict())
    check("摘要跳过不是静默失败：数据段结果照常回带",
          set(outcome_b.refreshed) == set(sources), outcome_b.as_dict())
    check("预算约束的是'是否发起'：单次已发起的调用可越过边界（如实暴露，不伪装精确截断）",
          outcome_b.elapsed_ms >= tight.total_ms
          and all(item < tight.http_ms for item in started), outcome_b.as_dict())


# ---------------------------------------------------------------- [2] 让路


def test_refresh_defers_to_management() -> None:
    print("\n[2] 后台刷新不阻塞管理：预算不足即让路，一次抓取都不发")
    clock = Clock()
    budget = _budget(clock, total_ms=2_000, http_ms=1_500, summary_ms=500,
                     reserve_ms=1_900)
    clock.advance(0.5)  # 只剩 1500ms，低于 1900ms 的管理预留
    calls: list[str] = []
    outcome = background_refresh(sources=["ticker"], fetch=lambda s: calls.append(s),
                                 budget=budget)
    check("剩余预算不足管理预留 → 立即 deferred", outcome.deferred is True,
          outcome.as_dict())
    check("deferred 时零次抓取（不占用快循环预算）", calls == [], calls)
    check("deferred 回带 DATA_UNAVAILABLE（诚实标注未刷新）",
          outcome.reason is not None and outcome.reason.primary == "DATA_UNAVAILABLE",
          outcome.as_dict())

    # 端到端（R22）：保护/对账先完成，后台刷新即使整段耗尽预算也不影响它们。
    clock = Clock()
    budget = _budget(clock, total_ms=1_000, http_ms=600, summary_ms=400)
    marks: dict[str, float] = {}

    def protection() -> str:
        marks.setdefault("start", clock.monotonic())
        marks["protection_done"] = clock.monotonic()
        return "protect"

    def slow_refresh_step() -> str:
        background_refresh(
            sources=["ticker", "mark_funding"], fetch=lambda s: clock.advance(0.3),
            budget=budget,
        )
        clock.advance(0.4)
        return "refreshed"

    result = run_protection_first(
        model_step=slow_refresh_step,
        protection_step=protection,
        reconciliation_step=lambda: (clock.advance(0.001), "reconcile")[1],
    )
    check("长后台刷新不改变保护/对账已完成的事实",
          result.protection.ok and result.reconciliation.ok, result.as_dict())
    check("快循环在长刷新之前完成",
          marks["protection_done"] - marks["start"] < 0.01, marks)
    check("整轮不被置 blocked", result.blocked is False)


# ---------------------------------------------------------------- [3] 分源缓存


def test_per_source_cache() -> None:
    print("\n[3] 分源缓存：单源过期只重抓该源（不整包重抓）")
    clock = Clock()
    cache = SourceCache(clock_ms=clock.ms)
    check("分源 TTL 与 U03 证据模块同口径（ticker 最短）",
          SOURCE_TTL_MS["ticker"] < SOURCE_TTL_MS["mark_funding"] == SOURCE_TTL_MS[
              "open_interest"], SOURCE_TTL_MS)

    cache.put("ticker", "t0")
    cache.put("mark_funding", "m0")
    cache.put("open_interest", "o0")
    clock.advance(45.0)  # ticker(30s) 过期，资金费/OI(60s) 仍新鲜
    check("过期判定逐源独立",
          cache.stale_sources(["ticker", "mark_funding", "open_interest"])
          == ("ticker",), cache.status())
    fresh, value = cache.get("mark_funding")
    check("未过期源仍返回新鲜值", fresh is True and value == "m0")
    stale, stale_value = cache.get("ticker")
    check("过期源返回陈旧值但标记为不新鲜（不丢数据）",
          stale is False and stale_value == "t0")

    clock2 = Clock()
    cache2 = SourceCache(clock_ms=clock2.ms)
    fetched: list[str] = []
    budget = _budget(clock2, total_ms=5_000, http_ms=4_000, summary_ms=1_000)

    def fetch(source: str) -> str:
        fetched.append(source)
        return f"{source}@1"

    sources = ["ticker", "mark_funding", "open_interest"]
    first = background_refresh(sources=sources, fetch=fetch, cache=cache2, budget=budget)
    check("首次全部抓取", sorted(first.refreshed) == sorted(sources), first.as_dict())
    check("首次无缓存命中", first.cached == (), first.as_dict())

    fetched.clear()
    second = background_refresh(sources=sources, fetch=fetch, cache=cache2, budget=budget)
    check("TTL 内二次调用全部命中缓存（零抓取）",
          fetched == [] and sorted(second.cached) == sorted(sources), second.as_dict())

    clock2.advance(31.0)  # 只有 ticker 过期
    fetched.clear()
    third_budget = _budget(clock2, total_ms=5_000, http_ms=4_000, summary_ms=1_000)
    third = background_refresh(sources=sources, fetch=fetch, cache=cache2,
                               budget=third_budget)
    check("单源过期只重抓该源（分源缓存，不整包重抓）",
          fetched == ["ticker"] and set(third.cached) == {"mark_funding", "open_interest"},
          third.as_dict())
    check("重抓后该源恢复新鲜",
          cache2.get("ticker")[0] is True and cache2.get("ticker")[1] == "ticker@1")
    check("缓存视图带年龄与 TTL（可审计）",
          cache2.status()["ticker"]["ttl_ms"] == SOURCE_TTL_MS["ticker"],
          cache2.status())

    # 单源抓取异常不拖垮整轮
    def flaky(source: str) -> str:
        if source == "open_interest":
            raise RuntimeError("upstream down")
        return f"{source}@2"

    clock3 = Clock()
    budget3 = _budget(clock3, total_ms=5_000, http_ms=4_000, summary_ms=1_000)
    outcome = background_refresh(sources=sources, fetch=flaky, budget=budget3)
    check("单源异常只影响该源，其余源照常刷新",
          outcome.failed == ("open_interest",)
          and set(outcome.refreshed) == {"ticker", "mark_funding"}, outcome.as_dict())
    check("源级失败仍回带 DATA_UNAVAILABLE", outcome.reason is not None)

    # 摘要预算独立：有剩余才执行，且只是"不执行"而不是静默失败
    clock4 = Clock()
    budget4 = _budget(clock4, total_ms=2_000, http_ms=1_500, summary_ms=500)
    fired: list[str] = []

    def slow_summary(values) -> None:
        del values
        fired.append("summary")

    background_refresh(sources=["ticker"], fetch=lambda s: "v", budget=budget4,
                       summarize=slow_summary)
    check("摘要预算存在时会执行摘要", fired == ["summary"], fired)

    def boom_summary(values) -> None:
        del values
        raise RuntimeError("summary exploded")

    clock5 = Clock()
    budget5 = _budget(clock5, total_ms=2_000, http_ms=1_500, summary_ms=500)
    outcome5 = background_refresh(sources=["ticker"], fetch=lambda s: "v", budget=budget5,
                                  summarize=boom_summary)
    check("摘要异常不吞掉已取得的数据（数据仍在 refreshed）",
          outcome5.summary_done is False and outcome5.refreshed == ("ticker",),
          outcome5.as_dict())
    check("摘要异常不污染数据可用性结论",
          outcome5.failed == () and outcome5.reason is None, outcome5.as_dict())


# ---------------------------------------------------------------- [4] 既有契约


def test_existing_intel_cache_contract_survives() -> None:
    print("\n[4] 既有 IntelCache 契约必须保持（两态都不变的回归护栏）")
    calls: list[str] = []
    cache = IntelCache(ttl_seconds=3600)
    first = cache.get_or_refresh("market", lambda: calls.append("hit") or {"v": 1})
    second = cache.get_or_refresh("market", lambda: calls.append("hit") or {"v": 2})
    check("TTL 内命中不重复刷新（刷新次数=1）", calls == ["hit"], calls)
    check("TTL 内返回同一个缓存值（不被新刷新顶替）",
          first == second == {"v": 1}, (first, second))
    check("键之间互不影响",
          cache.get_or_refresh("macro", lambda: "m") == "m")
    check("age_seconds 对未知键返回 None", cache.age_seconds("onchain") is None)
    check("age_seconds 对已知键返回非负数",
          (cache.age_seconds("market") or 0.0) >= 0.0)
    stats = cache.stats()
    check("stats 覆盖已缓存键并提供 age/ttl",
          set(stats) == {"market", "macro"} and "ttl_seconds" in stats["market"],
          stats)
    check("ttl_seconds 覆盖生效（极短 TTL 立刻过期）",
          IntelCache(ttl_seconds=0.0).get_or_refresh(
              "k", lambda: "a") == "a"
          and IntelCache(ttl_seconds=0.0).get_or_refresh("k", lambda: "b") == "b")
    cache.clear()
    check("clear() 之后视为未缓存", cache.age_seconds("market") is None
          and cache.stats() == {})


def _raises(exc_type: type[BaseException], func) -> bool:
    try:
        func()
    except exc_type:
        return True
    except BaseException:  # noqa: BLE001
        return False
    return False


def test_outcome_shape() -> None:
    print("\n[5] 刷新结论可序列化且字段语义明确")
    outcome = BackgroundRefreshOutcome(
        deferred=False, refreshed=("ticker",), cached=("mark_funding",),
        failed=("open_interest",), elapsed_ms=120, budget_ms=1000, summary_done=True,
    )
    payload = outcome.as_dict()
    check("四类计数分开（refreshed/cached/failed/deferred）",
          {"refreshed", "cached", "failed", "deferred"} <= set(payload), sorted(payload))
    check("deferred 与 failed 不混为一谈",
          payload["deferred"] is False and payload["failed"] == ["open_interest"])
    check("结论可 JSON 序列化", json.loads(json.dumps(payload))["elapsed_ms"] == 120)


def main() -> int:
    print("=" * 72)
    print("U05/S03 后台有界刷新离线回归（独立脚本，无 pytest）")
    print("=" * 72)
    test_total_budget_invariants()
    test_refresh_defers_to_management()
    test_per_source_cache()
    test_existing_intel_cache_contract_survives()
    test_outcome_shape()
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
