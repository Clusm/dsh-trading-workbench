"""U06b 采样侧：固定目标时点、全白名单独立采样计划、时钟可信度与选价。

三件事，全部**纯函数 / 纯数据**（不读数据库、不读系统时钟、不发网络请求）：

1. **固定目标时点**（:func:`target_at` / :func:`window_bounds`）：给定决策时刻与
   horizon，目标时刻就是 ``decided_at + horizon``，容差窗口是它两侧的等长区间。
   这与 legacy v1「轮转刷新时顺手取当前价」的口径**不同**，也是 F11「5 分钟标签
   晚 8 分钟」的根因。

2. **独立全白名单采样**（:func:`build_sampling_plan`）：采样网格按**白名单全量
   × 目标时刻**生成，不依赖「这一轮恰好轮到哪个标的」——legacy v1 的轮转/非优先
   标的在刷新时缺价就直接消失。计划是**有界**的：超出 ``max_targets`` 的部分被
   计数披露（:attr:`SamplingPlan.skipped`），不静默丢弃。

3. **时钟可信度**（:func:`clock_guard`，F17）：墙钟回拨/跳变、monotonic 倒退都判
   ``UNTRUSTED``，此时对应窗口的标签一律落 ``CLOCK_UNTRUSTED`` 缺失，**不**按
   错误时点出标签。判定是相对的（两次快照比较），不硬编码任何日期。

选价（:func:`select_sample`）只在容差窗口内挑**离目标时刻最近**的样本，并显式区分
``NO_PRICE``（窗口内一条都没有）、``OVER_TOLERANCE``（有样本但都偏太远）与
``DATA_GAP``（目标两侧都有样本、中间是长空洞，即断流）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from intel.shadow_v2.models import (
    DEFAULT_EXPECTED_INTERVAL_SECONDS,
    DEFAULT_TOLERANCE_SECONDS,
    MAX_TOLERANCE_SECONDS,
    REASON_DATA_GAP,
    REASON_NO_PRICE,
    REASON_OVER_TOLERANCE,
    LabelRequest,
    LeakageError,
    PriceSample,
    as_utc,
)

#: 时钟快照状态。
CLOCK_STABLE = "STABLE"
CLOCK_UNTRUSTED = "UNTRUSTED"

#: 相对判定阈值：monotonic 允许的最大倒退（秒）与墙钟允许的最大回拨（秒）。
#: 这两个是**容差**而非日期，避免任何「硬编码某天」的时区/日切问题。
DEFAULT_MAX_MONOTONIC_REGRESSION_SECONDS = 0.5
DEFAULT_MAX_WALL_ROLLBACK_SECONDS = 5.0


@dataclass(frozen=True)
class ClockSnapshot:
    """一次时钟快照：墙钟 + 单调钟 + 自报版本（供相对比较）。"""

    wall: datetime
    monotonic: float
    boot_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class ClockVerdict:
    """时钟可信度判定结果。"""

    state: str
    issues: tuple[str, ...] = ()

    @property
    def trusted(self) -> bool:
        """``True`` 表示可用于固定时点标签。"""
        return self.state == CLOCK_STABLE


def clock_guard(previous: ClockSnapshot | None, current: ClockSnapshot, *,
                max_monotonic_regression_seconds: float = DEFAULT_MAX_MONOTONIC_REGRESSION_SECONDS,
                max_wall_rollback_seconds: float = DEFAULT_MAX_WALL_ROLLBACK_SECONDS
                ) -> ClockVerdict:
    """比较两次时钟快照，判定墙钟/monotonic 是否可信（F17）。

    Args:
        previous: 上一次快照（``None`` 表示首次，视为可信）。
        current: 本次快照。
        max_monotonic_regression_seconds: 允许的 monotonic 倒退容差。
        max_wall_rollback_seconds: 允许的墙钟回拨容差。

    Returns:
        :class:`ClockVerdict`；``issues`` 逐条列出触发原因，供如实披露。
    """
    if previous is None:
        return ClockVerdict(CLOCK_STABLE)
    issues: list[str] = []
    if str(current.boot_id) and str(previous.boot_id) and current.boot_id != previous.boot_id:
        issues.append("boot_id_changed")
    delta_mono = float(current.monotonic) - float(previous.monotonic)
    if delta_mono < 0 and abs(delta_mono) > float(max_monotonic_regression_seconds):
        issues.append("monotonic_regressed")
    delta_wall = (as_utc(current.wall) - as_utc(previous.wall)).total_seconds()
    if delta_wall < 0 and abs(delta_wall) > float(max_wall_rollback_seconds):
        issues.append("wall_rolled_back")
    elif delta_wall - delta_mono > float(max_wall_rollback_seconds):
        issues.append("wall_ahead_of_monotonic")
    return ClockVerdict(CLOCK_UNTRUSTED if issues else CLOCK_STABLE, tuple(issues))


def target_at(decided_at: datetime, horizon_minutes: int) -> datetime:
    """返回固定目标时刻 = ``decided_at + horizon``（UTC）。"""
    return as_utc(decided_at) + timedelta(minutes=int(horizon_minutes))


def window_bounds(target: datetime, tolerance_seconds: float) -> tuple[datetime, datetime]:
    """返回目标时刻两侧的等长容差窗口 ``[target-tol, target+tol]``。

    容差被夹到 ``(0, MAX_TOLERANCE_SECONDS]``：0 或负值会让一切样本超容差，
    而过大容差等于把 legacy v1「晚几分钟也算合格」的缺陷重新引入。
    """
    tol = _clamp_tolerance(tolerance_seconds)
    moment = as_utc(target)
    return (moment - timedelta(seconds=tol), moment + timedelta(seconds=tol))


def clamp_tolerance(tolerance_seconds: float) -> float:
    """把容差夹到 ``(0, MAX_TOLERANCE_SECONDS]``；非法输入取默认值。

    0/负容差会让一切样本超容差，过大容差等于重新引入 legacy v1「晚几分钟也算
    合格」的缺陷，因此这里做**上界**约束而不是简单透传。
    """
    try:
        tol = float(tolerance_seconds)
    except (TypeError, ValueError):
        return DEFAULT_TOLERANCE_SECONDS
    if tol <= 0:
        return 1e-3
    return min(tol, float(MAX_TOLERANCE_SECONDS))


#: 兼容调用点的私有别名（同一函数对象，避免两份实现漂移）。
_clamp_tolerance = clamp_tolerance


@dataclass(frozen=True)
class SamplingTarget:
    """一个「某标的需要在某个固定时刻有价」的采样目标。"""

    symbol: str
    target_at: datetime
    horizon_minutes: int
    decision_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SamplingPlan:
    """独立全白名单采样计划（有界、可披露）。

    Attributes:
        targets: 需要采样的 ``(symbol, target_at)`` 目标（去重后）。
        symbols: 本次纳入计划的白名单标的（**全量**，不按轮转裁剪）。
        skipped: 因超预算而未纳入的目标数（**计数披露**，不静默丢弃）。
        skipped_by_reason: 逐原因计数。
        tolerance_seconds: 本计划采用的容差。
    """

    targets: tuple[SamplingTarget, ...]
    symbols: tuple[str, ...]
    skipped: int = 0
    skipped_by_reason: Mapping[str, int] = field(default_factory=dict)
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS
    scope: str = "whitelist"

    def pairs(self) -> tuple[tuple[str, datetime], ...]:
        """返回去重后的 ``(symbol, target_at)`` 对。"""
        return tuple((item.symbol, item.target_at) for item in self.targets)

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的计划摘要（含预算披露）。"""
        return {
            "scope": str(self.scope),
            "symbols": list(self.symbols),
            "targets": len(self.targets),
            "skipped": int(self.skipped),
            "skipped_by_reason": dict(self.skipped_by_reason),
            "tolerance_seconds": float(self.tolerance_seconds),
        }


def build_sampling_plan(requests: Iterable[LabelRequest], *,
                        whitelist: Sequence[str] | None = None,
                        tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
                        max_targets: int = 20000) -> SamplingPlan:
    """按**全白名单 × 目标时刻**生成独立采样计划（不依赖轮转命中）。

    Args:
        requests: 标签请求集合。
        whitelist: 白名单标的；``None`` 时退化为「请求里出现过的标的」。
        tolerance_seconds: 容差。
        max_targets: 目标上限；超出部分计数披露而非静默丢弃。

    Returns:
        :class:`SamplingPlan`；``targets`` 已按 ``(target_at, symbol)`` 排序，
        保证同输入必得同顺序（可重放）。
    """
    reqs = list(requests)
    tol = _clamp_tolerance(tolerance_seconds)
    if whitelist is None:
        symbols = tuple(sorted({str(r.symbol) for r in reqs}))
        scope = "requests"
    else:
        symbols = tuple(sorted({str(item) for item in whitelist}))
        scope = "whitelist"
    index: dict[tuple[str, datetime], list[str]] = {}
    skipped = 0
    skipped_by_reason: dict[str, int] = {}
    # 全白名单语义：每个白名单标的 × 每个出现过的目标时刻都要有采样点，
    # 即使该标的此刻没有决策——研究机会集合不被「谁恰好有信号」污染。
    horizons = sorted({int(r.horizon_minutes) for r in reqs})
    targets: list[tuple[str, datetime]] = []
    for horizon in horizons:
        moments = sorted({target_at(r.decided_at, r.horizon_minutes)
                          for r in reqs if int(r.horizon_minutes) == horizon})
        for moment in moments:
            for symbol in symbols:
                targets.append((symbol, moment))
    for symbol, moment in targets:
        key = (symbol, moment)
        if len(index) >= max(1, int(max_targets)):
            skipped += 1
            skipped_by_reason["budget_exceeded"] = skipped_by_reason.get("budget_exceeded", 0) + 1
            continue
        index.setdefault(key, [])
    for req in reqs:
        key = (str(req.symbol), target_at(req.decided_at, req.horizon_minutes))
        if key in index:
            index[key].append(str(req.decision_id))
    ordered = tuple(
        SamplingTarget(symbol=key[0], target_at=key[1],
                       horizon_minutes=_horizon_of(reqs, key[1]),
                       decision_ids=tuple(sorted(index[key])))
        for key in sorted(index, key=lambda item: (item[1], item[0]))
    )
    return SamplingPlan(targets=ordered, symbols=symbols, skipped=skipped,
                        skipped_by_reason=skipped_by_reason, tolerance_seconds=tol,
                        scope=scope)


def _horizon_of(reqs: list[LabelRequest], moment: datetime) -> int:
    for req in reqs:
        if target_at(req.decided_at, req.horizon_minutes) == moment:
            return int(req.horizon_minutes)
    return 0


def samples_before(samples: Iterable[PriceSample], cutoff: datetime) -> tuple[PriceSample, ...]:
    """返回 ``sampled_at <= cutoff`` 的样本（按时间升序）。

    「无未来信息回填」的第一道闸：标签路径只允许看到截止时刻之前的观测。
    """
    limit = as_utc(cutoff)
    return tuple(sorted((item for item in samples if as_utc(item.sampled_at) <= limit),
                        key=lambda item: item.sampled_at))


def assert_no_future(samples: Iterable[PriceSample], cutoff: datetime) -> None:
    """校验样本集合不含 ``cutoff`` 之后的行；有则抛 :class:`LeakageError`。

    研究与执行两侧都调用它，确保任何「拿后面的价回填前面的标签」都在测试里炸掉，
    而不是悄悄算出一个偏乐观的数字。
    """
    limit = as_utc(cutoff)
    offenders = [item for item in samples if as_utc(item.sampled_at) > limit]
    if offenders:
        raise LeakageError(
            f"{len(offenders)} 条样本晚于截止时刻 {limit.isoformat()}，"
            f"违反『无未来信息回填』：首条={as_utc(offenders[0].sampled_at).isoformat()}")


@dataclass(frozen=True)
class Selection:
    """容差窗口内的选价结果。

    Attributes:
        sample: 选中的样本（``None`` 表示未选中）。
        deviation_seconds: 选中样本相对目标时刻的有符号偏差（秒）。
        reason: 未选中时的缺失原因（``NO_PRICE``/``OVER_TOLERANCE``/``DATA_GAP``）。
        nearest_deviation_seconds: 窗口内最近样本的绝对偏差（诊断用，即使超容差）。
        before/after: 目标时刻前后最近的样本（诊断断流用）。
    """

    sample: PriceSample | None
    deviation_seconds: float | None
    reason: str | None
    nearest_deviation_seconds: float | None = None
    before: PriceSample | None = None
    after: PriceSample | None = None

    @property
    def ok(self) -> bool:
        """``True`` 表示选中了合格固定时点价。"""
        return self.sample is not None


def select_sample(samples: Iterable[PriceSample], *, target: datetime,
                  tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
                  known_at: datetime | None = None,
                  expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS
                  ) -> Selection:
    """在容差窗口内挑离目标时刻最近的样本；否则给出**具体缺失原因**。

    Args:
        samples: 候选价格观测（同标的）。
        target: 固定目标时刻。
        tolerance_seconds: 容差。
        known_at: 决策时可见的知识截止时刻；给定时先做无未来信息校验。
        expected_interval_seconds: 预期采样间隔，用于判定「断流」而非「超容差」。

    Returns:
        :class:`Selection`。判定顺序：无样本 → ``NO_PRICE``；有样本但窗口内
        一条都没有 → 若目标两侧都有样本且中间空洞超过一个预期间隔 → ``DATA_GAP``，
        否则 ``OVER_TOLERANCE``。
    """
    moment = as_utc(target)
    window_open, window_close = window_bounds(moment, tolerance_seconds)
    rows = sorted(samples, key=lambda item: item.sampled_at)
    if known_at is not None:
        rows = [item for item in rows if as_utc(item.sampled_at) <= as_utc(known_at)]
    if not rows:
        return Selection(None, None, REASON_NO_PRICE)
    in_window = [item for item in rows if window_open <= item.sampled_at <= window_close]
    if in_window:
        chosen = min(in_window,
                     key=lambda item: (abs((item.sampled_at - moment).total_seconds()),
                                       item.sampled_at))
        deviation = (chosen.sampled_at - moment).total_seconds()
        return Selection(chosen, deviation, None,
                         nearest_deviation_seconds=abs(deviation))
    before = [item for item in rows if item.sampled_at < moment]
    after = [item for item in rows if item.sampled_at > moment]
    nearest_before = before[-1] if before else None
    nearest_after = after[0] if after else None
    nearest = min(rows, key=lambda item: abs((item.sampled_at - moment).total_seconds()))
    nearest_deviation = abs((nearest.sampled_at - moment).total_seconds())
    if nearest_before is not None and nearest_after is not None:
        hole = (nearest_after.sampled_at - nearest_before.sampled_at).total_seconds()
        if hole > max(1.0, float(expected_interval_seconds)):
            return Selection(None, None, REASON_DATA_GAP,
                             nearest_deviation_seconds=nearest_deviation,
                             before=nearest_before, after=nearest_after)
    return Selection(None, None, REASON_OVER_TOLERANCE,
                     nearest_deviation_seconds=nearest_deviation,
                     before=nearest_before, after=nearest_after)


def group_samples(samples: Iterable[PriceSample]) -> dict[str, tuple[PriceSample, ...]]:
    """按标的分组并按时间升序，供批量标签复用（一次排序多次查询）。"""
    buckets: dict[str, list[PriceSample]] = {}
    for item in samples:
        buckets.setdefault(str(item.symbol), []).append(item)
    return {symbol: tuple(sorted(items, key=lambda item: item.sampled_at))
            for symbol, items in buckets.items()}


def clock_verdict_for_window(previous: ClockSnapshot | None, current: ClockSnapshot, *,
                             target: datetime, tolerance_seconds: float) -> ClockVerdict:
    """只在容差窗口覆盖到当前快照时才判时钟；否则时钟问题不影响历史窗口。"""
    _, window_close = window_bounds(target, tolerance_seconds)
    if as_utc(current.wall) < window_close:
        return ClockVerdict(CLOCK_STABLE)
    verdict = clock_guard(previous, current)
    if not verdict.trusted:
        return ClockVerdict(CLOCK_UNTRUSTED, verdict.issues)
    return verdict
