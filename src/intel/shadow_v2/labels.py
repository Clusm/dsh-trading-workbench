"""U06b 标签侧：固定时点标签、缺失原因标签与覆盖率勾稽。

纯函数层。输入是**决策侧事实**（:class:`~intel.shadow_v2.models.LabelRequest`）与
**价格观测**（:class:`~intel.shadow_v2.models.PriceSample`），输出是
:class:`~intel.shadow_v2.models.LabelResult`。没有隐式系统时钟读取：``now`` 必须
显式传入，因此「未成熟」分支可以在测试里被确定性地覆盖，也不会出现「跑得慢一点
标签就变样」的不确定行为。

四条不变量
----------
1. **固定目标时点**：退出价只能取 ``decided_at + horizon`` 附近（±容差）的样本；
   超容差即 ``OVER_TOLERANCE`` 缺失，**不**退回 legacy v1「刷新时顺手取一个」的口径。
2. **缺失不静默**：拿不到合格价时按 ``NO_PRICE``/``OVER_TOLERANCE``/``DATA_GAP``/
   ``CLOCK_UNTRUSTED`` 逐条留痕，:func:`coverage_report` 逐原因计数。
3. **无未来信息**：路径只取 ``[decided_at, target]`` 的样本；任何越界行由
   :func:`~intel.shadow_v2.sampler.assert_no_future` 抛
   :class:`~intel.shadow_v2.models.LeakageError`。
4. **不产生执行后果**：每个结果的 ``counts_toward_release_gate`` 恒为 ``False``，
   ``execution_weight`` 恒为 ``0.0``；:func:`~intel.shadow_v2.models.gate_isolation_facts`
   提供机器可读的隔离事实。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from intel import shadow_status as _ss

from intel.shadow_v2.models import (
    COUNTS_TOWARD_RELEASE_GATE,
    DEFAULT_EXPECTED_INTERVAL_SECONDS,
    DEFAULT_TOLERANCE_SECONDS,
    REASON_CLOCK_UNTRUSTED,
    REASON_DATA_GAP,
    REASON_NO_PRICE,
    REASON_NOT_MATURED,
    REASON_OVER_TOLERANCE,
    SCHEMA_VERSION,
    STATE_LABELED,
    STATE_MISSING,
    STATE_PENDING,
    ExperimentIdentity,
    LabelRequest,
    LabelResult,
    PriceSample,
    V2_CALIBER_ID,
    as_utc,
)
from intel.shadow_v2 import sampler as _sampler
from intel.shadow_v2.sampler import ClockVerdict, ClockSnapshot, SamplingPlan

#: 研究成本假设的默认值（与 legacy v1 同口径，便于对照；不是真实成交成本）。
DEFAULT_ROUND_TRIP_FEE_BPS = 10.0
DEFAULT_SLIPPAGE_BPS = 4.0

#: 资金费成本按 horizon÷480 折算（8 小时为一个资金费周期），与 legacy v1 一致。
FUNDING_PERIODS_PER_DAY_HORIZON = 480.0

_DIRECTION_BY_SIGNAL = {"bullish": 1.0, "bearish": -1.0}


def _clamp_seconds(value: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return number


def label_request(request: LabelRequest, samples: Iterable[PriceSample], *,
                  now: datetime,
                  tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
                  round_trip_fee_bps: float = DEFAULT_ROUND_TRIP_FEE_BPS,
                  slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                  expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
                  clock_verdict: ClockVerdict | None = None,
                  clock_issues: Sequence[str] = ()) -> LabelResult:
    """对单个请求做固定时点标签；拿不到合格价时按原因落缺失。

    Args:
        request: 决策侧事实。
        samples: 该标的的价格观测（可含该标的以外的行，按 symbol 过滤）。
        now: 当前时刻（UTC，显式传入）。
        tolerance_seconds: 目标时刻容差。
        round_trip_fee_bps/slippage_bps: 研究成本假设（基点）。
        expected_interval_seconds: 预期采样间隔，用于区分断流与超容差。
        clock_verdict: 时钟判定；``UNTRUSTED`` 时不出标签。
        clock_issues: 时钟问题明细（写进 ``note``）。

    Returns:
        :class:`LabelResult`；``state`` ∈ ``LABELED``/``MISSING``/``PENDING``。
    """
    moment = as_utc(now)
    target = request.target_at
    _, window_close = _sampler.window_bounds(target, tolerance_seconds)
    mine = [item for item in samples if str(item.symbol) == str(request.symbol)]
    identity = request.identity

    if clock_verdict is not None and clock_verdict.state == _sampler.CLOCK_UNTRUSTED:
        return _missing(request, target, REASON_CLOCK_UNTRUSTED, tolerance_seconds,
                        identity, note="时钟不可信（F17）：" + ",".join(
                            list(clock_issues) or list(clock_verdict.issues)))
    # 窗口未走完：现在判「缺价」会把还在路上的正常样本误报成缺失，也可能诱使
    # 实现去抓一个未来价。一律先挂 PENDING（未成熟），窗口关闭后才结算。
    if moment < window_close:
        return _missing(request, target, REASON_NOT_MATURED, tolerance_seconds, identity,
                        state=STATE_PENDING,
                        note="目标时刻容差窗口尚未关闭，现在不出标签（禁止未来价回填）。")

    selection = _sampler.select_sample(
        mine, target=target, tolerance_seconds=tolerance_seconds,
        known_at=moment, expected_interval_seconds=expected_interval_seconds)
    if not selection.ok:
        return _missing(request, target, selection.reason or REASON_NO_PRICE,
                        tolerance_seconds, identity,
                        nearest_deviation=selection.nearest_deviation_seconds)

    chosen = selection.sample
    assert chosen is not None  # 上面的 ok 分支保证非空
    entry = float(request.reference_price)
    if entry <= 0:
        return _missing(request, target, REASON_NO_PRICE, tolerance_seconds, identity,
                        state=STATE_MISSING, note="入场参考价非正，无法计算收益。")
    exit_price = float(chosen.price)
    direction = _DIRECTION_BY_SIGNAL.get(str(request.signal).strip().lower(), 0.0)
    ret = (exit_price / entry - 1.0) * 100.0
    gross = ret * direction if direction else None
    fee_cost = max(0.0, _clamp_seconds(round_trip_fee_bps,
                                      DEFAULT_ROUND_TRIP_FEE_BPS)) / 100.0
    slip_cost = max(0.0, _clamp_seconds(slippage_bps, DEFAULT_SLIPPAGE_BPS)) / 100.0
    funding_cost = (direction * float(request.funding_rate) * 100.0
                    * (int(request.horizon_minutes) / FUNDING_PERIODS_PER_DAY_HORIZON))
    net = (gross - fee_cost - slip_cost - funding_cost) if gross is not None else None
    correct = None if not direction else int(
        (request.signal == "bullish" and ret > 0) or (request.signal == "bearish" and ret < 0))
    mfe, mae, path_count = _path_metrics(request, mine, target, exit_price, direction)
    return LabelResult(
        decision_id=str(request.decision_id), source=str(request.source),
        symbol=str(request.symbol), signal=str(request.signal),
        horizon_minutes=int(request.horizon_minutes), target_at=target,
        state=STATE_LABELED, missing_reason=None, observed_at=chosen.sampled_at,
        exit_price=exit_price, deviation_seconds=selection.deviation_seconds,
        tolerance_seconds=_sampler.clamp_tolerance(tolerance_seconds),
        gross_return_pct=gross, net_return_pct=net, fee_cost_pct=fee_cost,
        slippage_cost_pct=slip_cost, funding_cost_pct=funding_cost, correct=correct,
        mfe_pct=mfe, mae_pct=mae, path_samples=path_count, experiment=identity,
        caliber=V2_CALIBER_ID, counts_toward_release_gate=COUNTS_TOWARD_RELEASE_GATE,
        execution_weight=_ss.EXECUTION_WEIGHT,
        note=(f"固定时点取价：偏差 {selection.deviation_seconds:+.1f}s"
              f"（容差 ±{_sampler.clamp_tolerance(tolerance_seconds):.1f}s）。"),
    )


def _path_metrics(request: LabelRequest, samples: Iterable[PriceSample],
                  target: datetime, exit_price: float,
                  direction: float) -> tuple[float | None, float | None, int]:
    """在 ``[decided_at, target]`` 上调 MFE/MAE；不含任何未来样本。

    Returns:
        ``(mfe_pct, mae_pct, path_samples)``；非方向样本返回 ``(None, None, n)``。
    """
    entry = float(request.reference_price)
    cutoff = as_utc(target)
    inside = _sampler.samples_before(samples, cutoff)
    inside = [item for item in inside if item.sampled_at >= as_utc(request.decided_at)]
    # 无未来信息：任何晚于目标时刻的观测都不允许进入路径（退出价单独追加）。
    _sampler.assert_no_future(inside, cutoff)
    path = [entry] + [float(item.price) for item in inside] + [float(exit_price)]
    if not direction:
        return None, None, len(path)
    returns = [(price / entry - 1.0) * 100.0 * direction for price in path]
    return max(returns), min(returns), len(path)


def _missing(request: LabelRequest, target: datetime, reason: str,
             tolerance_seconds: float, identity: ExperimentIdentity,
             *, state: str = STATE_MISSING, nearest_deviation: float | None = None,
             note: str = "") -> LabelResult:
    """构造一条缺失/未成熟结果（字段齐备，便于直接落库与逐原因计数）。"""
    detail = {
        REASON_NO_PRICE: "窗口内该标的没有任何价格观测（legacy v1 会把该样本直接丢掉）。",
        REASON_OVER_TOLERANCE: (
            "最近样本偏离固定目标时刻超出容差（F11 反例：5 分钟标签晚 8 分钟）。"),
        REASON_DATA_GAP: "目标时刻前后都有样本但中间是长空洞：判为断流而非缺价。",
        REASON_CLOCK_UNTRUSTED: "时钟回拨/跳变，拒绝按错误时点出标签。",
        REASON_NOT_MATURED: "未成熟：容差窗口尚未关闭。",
    }.get(reason, "")
    suffix = "" if nearest_deviation is None else f" 最近偏差 {nearest_deviation:.1f}s。"
    return LabelResult(
        decision_id=str(request.decision_id), source=str(request.source),
        symbol=str(request.symbol), signal=str(request.signal),
        horizon_minutes=int(request.horizon_minutes), target_at=target,
        state=state, missing_reason=reason, observed_at=None, exit_price=None,
        deviation_seconds=None,
        tolerance_seconds=_sampler.clamp_tolerance(tolerance_seconds),
        gross_return_pct=None, net_return_pct=None, fee_cost_pct=None,
        slippage_cost_pct=None, funding_cost_pct=None, correct=None,
        mfe_pct=None, mae_pct=None, path_samples=0, experiment=identity,
        caliber=V2_CALIBER_ID, counts_toward_release_gate=COUNTS_TOWARD_RELEASE_GATE,
        execution_weight=_ss.EXECUTION_WEIGHT,
        note=(note or detail) + suffix,
    )


def label_batch(requests: Iterable[LabelRequest], samples: Iterable[PriceSample], *,
                now: datetime,
                tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
                round_trip_fee_bps: float = DEFAULT_ROUND_TRIP_FEE_BPS,
                slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
                clock_verdict: ClockVerdict | None = None,
                plan: SamplingPlan | None = None) -> list[LabelResult]:
    """批量标签：一次分组，逐请求独立判定，顺序稳定（可重放）。

    Args:
        requests: 请求集合。
        samples: 全部价格观测。
        now: 当前时刻。
        tolerance_seconds / round_trip_fee_bps / slippage_bps / expected_interval_seconds:
            与 :func:`label_request` 同义。
        clock_verdict: 时钟判定（``UNTRUSTED`` 时整批落缺失）。
        plan: 采样计划；给定时只对**计划内**的 ``(symbol, target)`` 出标签，
            计划外的请求不静默消失而是记 ``NO_PRICE``（保持分母一致）。

    Returns:
        与 ``requests`` 同序的 :class:`LabelResult` 列表。
    """
    buckets = _sampler.group_samples(samples)
    planned: set[tuple[str, datetime]] | None = None
    if plan is not None:
        planned = set(plan.pairs())
    out: list[LabelResult] = []
    for request in requests:
        if planned is not None and (str(request.symbol), request.target_at) not in planned:
            out.append(_missing(request, request.target_at, REASON_NO_PRICE,
                                tolerance_seconds, request.identity,
                                note="该 (标的, 目标时刻) 不在独立采样计划内：按缺价留痕。"))
            continue
        out.append(label_request(
            request, buckets.get(str(request.symbol), ()), now=now,
            tolerance_seconds=tolerance_seconds, round_trip_fee_bps=round_trip_fee_bps,
            slippage_bps=slippage_bps, expected_interval_seconds=expected_interval_seconds,
            clock_verdict=clock_verdict))
    return out


def coverage_report(results: Iterable[LabelResult]) -> dict[str, Any]:
    """逐状态/逐原因统计覆盖率（补上 legacy v1 只能给缺失总数的缺口）。

    Args:
        results: :func:`label_batch` 的输出。

    Returns:
        含 ``decided``/``labeled``/``pending``/``missing``/``coverage``/
        ``by_reason``/``unexplained`` 的字典。``unexplained`` 必须恒为 0：
        「缺失总数 ≠ 逐原因之和」本身就是缺陷。
    """
    rows = list(results)
    decided = len(rows)
    labeled = sum(1 for item in rows if item.state == STATE_LABELED)
    pending = sum(1 for item in rows if item.state == STATE_PENDING)
    by_reason: dict[str, int] = {}
    for item in rows:
        if item.state in (STATE_MISSING, STATE_PENDING) and item.missing_reason:
            by_reason[item.missing_reason] = by_reason.get(item.missing_reason, 0) + 1
    missing = sum(count for reason, count in by_reason.items() if reason != REASON_NOT_MATURED)
    accounted = missing + by_reason.get(REASON_NOT_MATURED, 0)
    total_closed = decided - pending
    return {
        "schema": SCHEMA_VERSION,
        "caliber": V2_CALIBER_ID,
        "decided": decided,
        "labeled": labeled,
        "pending": pending,
        "missing": missing,
        "coverage": (round(labeled / total_closed, 6) if total_closed else None),
        "by_reason": dict(sorted(by_reason.items())),
        "unexplained": max(0, (decided - labeled) - accounted),
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        "note": ("v2 逐原因区分未成熟/缺价/超容差/断流；legacy v1 只能给缺失总数。"
                 "未成熟不计入损失。"),
    }


@dataclass(frozen=True)
class LabelBatch:
    """一次批量标签的完整产物（结果 + 计划 + 覆盖率），便于落盘与复现。"""

    results: tuple[LabelResult, ...]
    coverage: Mapping[str, Any]
    plan_summary: Mapping[str, Any] | None = None
    clock_state: str = _sampler.CLOCK_STABLE

    def to_rows(self) -> list[dict[str, Any]]:
        """返回可落库/可写夹具的行列表。"""
        return [item.to_dict() for item in self.results]


def build_batch(requests: Iterable[LabelRequest], samples: Iterable[PriceSample], *,
                now: datetime,
                whitelist: Sequence[str] | None = None,
                tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
                round_trip_fee_bps: float = DEFAULT_ROUND_TRIP_FEE_BPS,
                slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
                expected_interval_seconds: float = DEFAULT_EXPECTED_INTERVAL_SECONDS,
                max_targets: int = 20000,
                previous_clock: ClockSnapshot | None = None,
                current_clock: ClockSnapshot | None = None) -> LabelBatch:
    """一步产出「计划 → 标签 → 覆盖率」，供离线冒烟与集成适配复用。"""
    reqs = list(requests)
    plan = _sampler.build_sampling_plan(reqs, whitelist=whitelist,
                                        tolerance_seconds=tolerance_seconds,
                                        max_targets=max_targets)
    verdict: ClockVerdict | None = None
    clock_state = _sampler.CLOCK_STABLE
    if current_clock is not None:
        verdict = _sampler.clock_guard(previous_clock, current_clock)
        clock_state = verdict.state
    results = label_batch(reqs, samples, now=now, tolerance_seconds=tolerance_seconds,
                          round_trip_fee_bps=round_trip_fee_bps,
                          slippage_bps=slippage_bps,
                          expected_interval_seconds=expected_interval_seconds,
                          clock_verdict=verdict, plan=plan)
    return LabelBatch(results=tuple(results), coverage=coverage_report(results),
                      plan_summary=plan.to_dict(), clock_state=clock_state)
