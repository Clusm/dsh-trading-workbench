"""U07 影子 v2 配对实验与独立样本统计（后置研究线）。

**这不是首批功能。** 首批 F11 的记录保持 ``v2_labels=DEFERRED``（见
``src/intel/shadow_status.py``）；本模块是 W4+ 后置线交付，只把 U06b 的固定时点
标签（:class:`~intel.shadow_v2.models.LabelResult` 与
:func:`~intel.shadow_v2.labels.coverage_report`）拿来做**研究统计**，并且：

* **不产生执行后果**：所有产物 ``counts_toward_release_gate=False``、
  ``execution_weight=0.0``、``auto_promotion=False``（:func:`promotion_guard`）；
* **不进门禁分母**：统计产物不写 ``shadow_decisions``/``shadow_outcomes``，
  也**不建立第二套 GO 判断器**（:func:`statistics_isolation_facts`）；
* **不自动晋升**：研究结论永远是研究结论，晋升执行权重须另行审查。

本模块处理四件在 legacy v1 口径下必然出错的事
------------------------------------------------

1. **同机会配对**（:func:`pair_experiment`）：同一 ``(实验版本, 标的, 目标时刻,
   horizon)`` 上的两臂样本配成一对，逐对做差。legacy v1 的 A/B/C 三组「交易时点、
   覆盖标的与机会集合不同，不是同一样本的随机实验」（见
   ``shadow_status.LEGACY_LIMITATIONS`` 末两条），直接比平均收益是错的。
   配对**从严**：信号方向不同、入场参考价不一致、任一臂缺失/未成熟，一律
   **披露后剔除**，不静默丢、不用另一臂的价凑数。

2. **重叠样本不冒充独立**（:func:`independence_report`）：同标的上持仓窗口
   ``[决策时刻, 目标时刻]`` 相互重叠的样本被聚成**块**，报告的是
   ``independent_blocks`` 而不是原始行数。legacy v1 的「数千条样本」里大量是同轮
   相邻信号（``LEGACY_LIMITATIONS`` 第 5 条），拿行数当独立样本数会把区间算窄。

3. **费用压力**（:func:`fee_stress`）：在费用/滑点的研究假设上乘压力倍数重算净值，
   给出逐倍数结果与**盈亏平衡倍数**；费用是假设不是成交成本
   （``LEGACY_LIMITATIONS`` 第 4 条），所以压力必须显式披露。

4. **区间报告可重放**（:func:`bootstrap_interval` / :func:`report_digest`）：
   区块（cluster）自举用**显式种子**的 :class:`random.Random`，同一输入必得同一区间；
   :func:`report_digest` 给出整份报告的规范 JSON 摘要，重放时逐字节可比。

无副作用边界
------------
纯函数 / 纯数据：不读系统时钟、不读数据库、不发网络请求、不调 LLM、不写文件、
不读 ``.env``。``now`` 之类时点一律由调用方显式传入或从样本自身派生
（``decided_at = target_at - horizon``，与 U06b 的固定目标时点口径一致）。
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import NormalDist
from typing import Any, Iterable, Mapping, Sequence

from intel import shadow_status as _ss

from intel.shadow_v2.labels import coverage_report as _coverage_report
from intel.shadow_v2.models import (
    COUNTS_TOWARD_RELEASE_GATE,
    V2_CALIBER_ID,
    LabelResult,
    as_utc,
)

#: 本模块产物的 schema 版本；落库行/夹具/报告都带它，便于只消费匹配版本。
SCHEMA_VERSION = "u07-shadow-v2-statistics-v1"

#: 统计口径 id：与 legacy v1 口径、与 U06b 的标签口径都**不同字符串**，三者不可混读。
STATISTICS_CALIBER_ID = "v2-paired-independent-stats"
STATISTICS_CALIBER_LABEL = "v2 同机会配对与独立样本统计（目标时刻取价，仅研究参考）"

#: 配对的两臂角色。**由调用方显式指定**，本模块不猜、不自动挑「更好的那一臂」。
ARM_ROLE_TREATMENT = "treatment"
ARM_ROLE_CONTROL = "control"
ARM_ROLES: tuple[str, ...] = (ARM_ROLE_TREATMENT, ARM_ROLE_CONTROL)

#: 独立性判定状态。
INDEPENDENCE_INDEPENDENT = "INDEPENDENT"        # 每个块恰好 1 条样本
INDEPENDENCE_OVERLAPPING = "OVERLAPPING"        # 存在多行块：不得按行数当独立样本
INDEPENDENCE_INSUFFICIENT = "INSUFFICIENT"      # 0 条样本，不作任何推断

#: 剔除原因（**逐条披露**，不静默丢弃）。
REASON_MISSING_TREATMENT_ARM = "MISSING_TREATMENT_ARM"
REASON_MISSING_CONTROL_ARM = "MISSING_CONTROL_ARM"
REASON_TREATMENT_NOT_LABELED = "TREATMENT_NOT_LABELED"
REASON_CONTROL_NOT_LABELED = "CONTROL_NOT_LABELED"
REASON_SIGNAL_MISMATCH = "SIGNAL_MISMATCH"
REASON_ENTRY_DIVERGENT = "ENTRY_DIVERGENT"
REASON_DUPLICATE_OPPORTUNITY = "DUPLICATE_OPPORTUNITY"

#: 研究结论**永不**自动晋升执行权重（影子执行权重恒 0，见 U06a）。
EXECUTION_PROMOTION_ALLOWED = False

#: 后置线口径说明：交付配对统计不等于首批 F11 已验收。
PAIRED_STATISTICS_SCOPE_NOTE = (
    "U07 为 W4+ 后置线交付：提供同机会配对、独立样本分块、费用压力与可重放区间报告，"
    "但**不改变**首批记录 F11:v2_labels=DEFERRED，**不**把研究统计计入发布门禁"
    "（200 方向候选 / 30 完整闭合），也**不**自动晋升任何执行权重。"
)

#: 默认置信度与费用压力倍数（研究假设，不是成交成本）。
DEFAULT_CONFIDENCE = 0.95
DEFAULT_FEE_STRESS_MULTIPLIERS: tuple[float, ...] = (1.0, 2.0, 3.0)
#: 自举重采样次数与**显式种子**：可重放的区间报告要求同种子必得同结果。
DEFAULT_BOOTSTRAP_RESAMPLES = 2000
DEFAULT_BOOTSTRAP_SEED = 0xC0FFEE
#: 配对时入场参考价的容忍偏差（基点）。0 表示要求逐字一致（从严）。
DEFAULT_ENTRY_TOLERANCE_BPS = 0.0

_SIGNAL_DIRECTION = {"bullish": 1.0, "bearish": -1.0}


class StatisticsError(ValueError):
    """研究统计的输入不满足前提（本模块一律 fail-closed，不出「差不多的数」）。"""


class ExperimentMixError(StatisticsError):
    """把不同实验版本的样本混算（DL08 禁止：版本不同即不可比）。"""


class GateLeakError(StatisticsError):
    """样本携带执行/门禁语义（非纯研究），拒绝纳入研究统计。"""


# --------------------------------------------------------------------------- #
# 机会键与派生事实
# --------------------------------------------------------------------------- #
def decided_at(result: LabelResult) -> datetime:
    """从固定目标时点反推决策时刻：``target_at - horizon``。

    U06b 的标签只存 ``target_at`` 与 ``horizon_minutes``；这里按同一口径反推，
    **不**读取系统时钟，也不引入第二套时间语义。
    """
    return as_utc(result.target_at) - timedelta(minutes=int(result.horizon_minutes))


def holding_window(result: LabelResult) -> tuple[datetime, datetime]:
    """返回持仓窗口 ``[decided_at, target_at]``（闭区间，用于重叠分块）。"""
    return (decided_at(result), as_utc(result.target_at))


def opportunity_key(result: LabelResult) -> str:
    """返回「同机会」键：实验版本 + 标的 + 固定目标时刻 + horizon。

    四项任一不同即**不是**同一次机会：不同实验版本不可比（DL08），不同标的/
    目标时刻/horizon 的收益路径不是同一个样本。
    """
    return "|".join((
        result.experiment.label(),
        str(result.symbol),
        as_utc(result.target_at).isoformat(timespec="seconds"),
        f"{int(result.horizon_minutes)}m",
    ))


def implied_entry_price(result: LabelResult) -> float | None:
    """从标签反推入场参考价（用于核对两臂「同机会」是否真的同价入场）。

    ``gross_return_pct = (exit/entry - 1) * 100 * direction`` ⇒
    ``entry = exit / (1 + gross/direction/100)``。取不到（未出标签/非方向/异常）
    返回 ``None``，不猜一个值出来。
    """
    if not result.is_labeled or result.exit_price is None:
        return None
    if result.gross_return_pct is None:
        return None
    direction = _SIGNAL_DIRECTION.get(str(result.signal).strip().lower())
    if not direction:
        return None
    ratio = 1.0 + (float(result.gross_return_pct) / 100.0) / direction
    if ratio <= 0:
        return None
    return float(result.exit_price) / ratio


def assert_single_experiment(results: Iterable[LabelResult]) -> str | None:
    """校验样本集只含一个实验版本；混版本抛 :class:`ExperimentMixError`。

    Returns:
        唯一实验版本键；空集返回 ``None``。
    """
    keys = sorted({item.experiment.label() for item in results})
    if len(keys) > 1:
        raise ExperimentMixError(
            "样本含多个实验版本，不可混算（DL08）：" + ", ".join(keys))
    return keys[0] if keys else None


def assert_gate_neutral(results: Iterable[LabelResult]) -> int:
    """校验样本是**纯研究**行：不进门禁、零执行权重、口径为 v2 标签口径。

    这是研究侧的最后一道闸：任何带 ``counts_toward_release_gate=True`` 或非零
    ``execution_weight`` 的行都会让统计产物「看起来能进 GO」，必须当场炸掉。

    Returns:
        通过校验的样本条数。
    """
    rows = list(results)
    leaked = [item.decision_id for item in rows if item.counts_toward_release_gate]
    if leaked:
        raise GateLeakError(f"{len(leaked)} 条样本声明计入发布门禁，拒绝纳入研究统计："
                            f"{leaked[:5]}")
    weighted = [item.decision_id for item in rows if float(item.execution_weight or 0.0) != 0.0]
    if weighted:
        raise GateLeakError(f"{len(weighted)} 条样本带非零执行权重，拒绝纳入研究统计："
                            f"{weighted[:5]}")
    foreign = [item.decision_id for item in rows if str(item.caliber) != V2_CALIBER_ID]
    if foreign:
        raise GateLeakError(f"{len(foreign)} 条样本口径不是 v2 固定时点口径，拒绝混算："
                            f"{foreign[:5]}")
    return len(rows)


def promotion_guard() -> dict[str, Any]:
    """返回「研究不自动晋升执行权重」的机器可读事实（常量，非配置）。"""
    return {
        "auto_promotion": False,
        "execution_promotion_allowed": EXECUTION_PROMOTION_ALLOWED,
        "execution_weight": _ss.EXECUTION_WEIGHT,
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        "research_only": True,
        "reason": ("统计产物只是研究参考：执行权重恒为 0，晋升须另行审查，"
                   "本模块不输出任何 GO/promotion 结论。"),
    }


# --------------------------------------------------------------------------- #
# 独立样本：重叠窗口分块
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OverlapBlock:
    """一个「独立块」：同一标的上持仓窗口连成一片的一组样本。

    Attributes:
        symbol: 标的。
        size: 块内样本条数。
        start: 块内最早的窗口开始（决策时刻）。
        end: 块内最晚的窗口结束（目标时刻）。
        keys: 块内样本的机会键（有序）。
    """

    symbol: str
    size: int
    start: datetime
    end: datetime
    keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的诊断行。"""
        return {
            "symbol": self.symbol,
            "size": int(self.size),
            "start": self.start.isoformat(timespec="seconds"),
            "end": self.end.isoformat(timespec="seconds"),
            "keys": list(self.keys),
        }


def _blocks_from_windows(entries: Sequence[tuple[str, str, datetime, datetime]],
                         *, gap_seconds: float = 0.0) -> tuple[OverlapBlock, ...]:
    """把 ``(key, symbol, start, end)`` 列表按标的分块；相邻窗口相接即合并。

    合并判据 ``next.start <= current.end + gap_seconds``（默认 ``gap_seconds=0``）：
    相接（甚至只共享一个瞬间）的窗口算同一块——它们的收益共享同一段价格路径，
    按独立样本处理会低估区间宽度。
    """
    by_symbol: dict[str, list[tuple[str, datetime, datetime]]] = {}
    for key, symbol, start, end in entries:
        by_symbol.setdefault(str(symbol), []).append((str(key), as_utc(start), as_utc(end)))
    blocks: list[OverlapBlock] = []
    for symbol in sorted(by_symbol):
        rows = sorted(by_symbol[symbol], key=lambda item: (item[1], item[2], item[0]))
        current: list[tuple[str, datetime, datetime]] = []
        for row in rows:
            if current and row[1] <= current[-1][2] + timedelta(seconds=float(gap_seconds)):
                current.append(row)
                continue
            if current:
                blocks.append(_make_block(symbol, current))
            current = [row]
        if current:
            blocks.append(_make_block(symbol, current))
    return tuple(blocks)


def _make_block(symbol: str, rows: Sequence[tuple[str, datetime, datetime]]) -> OverlapBlock:
    return OverlapBlock(symbol=symbol, size=len(rows),
                        start=min(item[1] for item in rows),
                        end=max(item[2] for item in rows),
                        keys=tuple(item[0] for item in rows))


def overlap_blocks(results: Iterable[LabelResult], *,
                   gap_seconds: float = 0.0) -> tuple[OverlapBlock, ...]:
    """把标签结果按「同一标的 + 持仓窗口重叠」聚成独立块。"""
    entries = []
    for item in results:
        start, end = holding_window(item)
        entries.append((opportunity_key(item), str(item.symbol), start, end))
    return _blocks_from_windows(entries, gap_seconds=gap_seconds)


def independence_report(results: Iterable[LabelResult], *,
                        gap_seconds: float = 0.0) -> dict[str, Any]:
    """报告「原始行数 vs 独立块数」，明确重叠样本不得冒充独立。

    Returns:
        含 ``observations``/``independent_blocks``/``overlap_ratio``/
        ``largest_block``/``state``/``by_symbol`` 的字典。``state`` 为
        ``INDEPENDENT`` 仅当每块恰好 1 条；空集为 ``INSUFFICIENT``。
    """
    rows = list(results)
    blocks = overlap_blocks(rows, gap_seconds=gap_seconds)
    total = len(rows)
    largest = max((block.size for block in blocks), default=0)
    if total == 0:
        state = INDEPENDENCE_INSUFFICIENT
    elif largest <= 1:
        state = INDEPENDENCE_INDEPENDENT
    else:
        state = INDEPENDENCE_OVERLAPPING
    by_symbol: dict[str, dict[str, int]] = {}
    for block in blocks:
        item = by_symbol.setdefault(block.symbol, {"observations": 0, "blocks": 0})
        item["observations"] += int(block.size)
        item["blocks"] += 1
    return {
        "observations": total,
        "independent_blocks": len(blocks),
        "overlap_ratio": (round(1.0 - (len(blocks) / total), 6) if total else None),
        "largest_block": largest,
        "state": state,
        "gap_seconds": float(gap_seconds),
        "by_symbol": {name: by_symbol[name] for name in sorted(by_symbol)},
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        "note": ("同标的持仓窗口重叠的样本被聚成块：区间必须按块计算，"
                 "按行数计算会把区间算窄（legacy v1 的「数千条样本」即此陷阱）。"),
    }


# --------------------------------------------------------------------------- #
# 同机会配对
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PairedSample:
    """一对「同机会」样本（两臂都已出合格固定时点标签）。"""

    opportunity: str
    symbol: str
    signal: str
    target_at: datetime
    horizon_minutes: int
    experiment_key: str
    treatment: LabelResult
    control: LabelResult
    gross_diff_pct: float
    net_diff_pct: float
    entry_divergence_bps: float

    def window(self) -> tuple[datetime, datetime]:
        """两臂持仓窗口的并集（分块用）。"""
        starts = [decided_at(self.treatment), decided_at(self.control)]
        ends = [as_utc(self.treatment.target_at), as_utc(self.control.target_at)]
        return (min(starts), max(ends))

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的行（不复制整条标签，只留配对所需事实）。"""
        return {
            "opportunity": self.opportunity,
            "symbol": self.symbol,
            "signal": self.signal,
            "target_at": as_utc(self.target_at).isoformat(timespec="seconds"),
            "horizon_minutes": int(self.horizon_minutes),
            "experiment_key": self.experiment_key,
            "treatment_decision_id": self.treatment.decision_id,
            "control_decision_id": self.control.decision_id,
            "gross_diff_pct": self.gross_diff_pct,
            "net_diff_pct": self.net_diff_pct,
            "entry_divergence_bps": self.entry_divergence_bps,
            "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        }


@dataclass(frozen=True)
class PairingReport:
    """一次配对实验的完整产物：配成的对 + 逐条剔除原因。"""

    pairs: tuple[PairedSample, ...]
    dropped: tuple[Mapping[str, Any], ...]
    experiment_key: str | None
    require_matching_entry: bool
    entry_tolerance_bps: float

    def drop_counts(self) -> dict[str, int]:
        """按原因汇总剔除条数（**必须**能勾稽回候选总数）。"""
        counts: dict[str, int] = {}
        for item in self.dropped:
            reason = str(item.get("reason") or "")
            counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的配对摘要。"""
        return {
            "experiment_key": self.experiment_key,
            "pairs": [item.to_dict() for item in self.pairs],
            "paired": len(self.pairs),
            "dropped": len(self.dropped),
            "dropped_by_reason": self.drop_counts(),
            "dropped_rows": [dict(item) for item in self.dropped],
            "require_matching_entry": bool(self.require_matching_entry),
            "entry_tolerance_bps": float(self.entry_tolerance_bps),
            "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        }


def _drop_entry(key: str, treatment: LabelResult | None, control: LabelResult | None,
                reason: str) -> dict[str, Any]:
    return {
        "opportunity": key,
        "reason": reason,
        "treatment_decision_id": (treatment.decision_id if treatment else None),
        "control_decision_id": (control.decision_id if control else None),
        "treatment_state": (treatment.state if treatment else None),
        "control_state": (control.state if control else None),
    }


def _sorted_rows(results: Iterable[LabelResult]) -> list[LabelResult]:
    return sorted(results, key=lambda item: (str(item.symbol), as_utc(item.target_at),
                                             int(item.horizon_minutes), str(item.decision_id)))


def pair_experiment(treatment: Iterable[LabelResult], control: Iterable[LabelResult], *,
                    require_matching_entry: bool = True,
                    entry_tolerance_bps: float = DEFAULT_ENTRY_TOLERANCE_BPS,
                    require_same_signal: bool = True) -> PairingReport:
    """把两臂样本按**同机会键**配对，只对两臂都出合格标签的对做差。

    Args:
        treatment: 处理臂样本（研究中的变更侧）。
        control: 对照臂样本（基线侧）。
        require_matching_entry: ``True``（默认）时，两臂反推的入场参考价差异超过
            ``entry_tolerance_bps`` 即剔除并披露——同机会却不同价入场，不是配对。
        entry_tolerance_bps: 入场价容忍偏差（基点）。
        require_same_signal: ``True``（默认）时，两臂信号方向不同即剔除。

    Returns:
        :class:`PairingReport`。**每一对候选都有归宿**：要么进 ``pairs``，要么带原因
        进 ``dropped``；不存在静默消失（宁可比对样本少，也不拿不可比的行凑数）。

    Raises:
        ExperimentMixError: 两臂合起来含多个实验版本。
        GateLeakError: 任一样本带执行/门禁语义。
    """
    t_rows = _sorted_rows(treatment)
    c_rows = _sorted_rows(control)
    assert_gate_neutral(t_rows + c_rows)
    keys = {item.experiment.label() for item in t_rows + c_rows}
    if len(keys) > 1:
        raise ExperimentMixError("配对两臂含多个实验版本，不可混算（DL08）："
                                 + ", ".join(sorted(keys)))
    experiment_key = next(iter(keys)) if keys else None
    tolerance = abs(float(entry_tolerance_bps))
    t_map = _bucket_by_opportunity(t_rows)
    c_map = _bucket_by_opportunity(c_rows)
    pairs: list[PairedSample] = []
    dropped: list[dict[str, Any]] = []
    for key in sorted(set(t_map) | set(c_map)):
        t_bucket = t_map.get(key, [])
        c_bucket = c_map.get(key, [])
        if len(t_bucket) > 1 or len(c_bucket) > 1:
            dropped.append(_drop_entry(key, t_bucket[0] if t_bucket else None,
                                       c_bucket[0] if c_bucket else None,
                                       REASON_DUPLICATE_OPPORTUNITY))
            continue
        t_item = t_bucket[0] if t_bucket else None
        c_item = c_bucket[0] if c_bucket else None
        if t_item is None:
            dropped.append(_drop_entry(key, None, c_item, REASON_MISSING_TREATMENT_ARM))
            continue
        if c_item is None:
            dropped.append(_drop_entry(key, t_item, None, REASON_MISSING_CONTROL_ARM))
            continue
        if not t_item.is_labeled or not c_item.is_labeled:
            reason = (REASON_TREATMENT_NOT_LABELED if not t_item.is_labeled
                      else REASON_CONTROL_NOT_LABELED)
            dropped.append(_drop_entry(key, t_item, c_item, reason))
            continue
        if require_same_signal and str(t_item.signal) != str(c_item.signal):
            dropped.append(_drop_entry(key, t_item, c_item, REASON_SIGNAL_MISMATCH))
            continue
        divergence = _entry_divergence_bps(t_item, c_item)
        if divergence is None or (require_matching_entry and divergence > tolerance):
            dropped.append(_drop_entry(key, t_item, c_item, REASON_ENTRY_DIVERGENT))
            continue
        gross_diff = float(t_item.gross_return_pct) - float(c_item.gross_return_pct)
        net_diff = float(t_item.net_return_pct) - float(c_item.net_return_pct)
        pairs.append(PairedSample(
            opportunity=key, symbol=str(t_item.symbol), signal=str(t_item.signal),
            target_at=as_utc(t_item.target_at), horizon_minutes=int(t_item.horizon_minutes),
            experiment_key=str(experiment_key or ""), treatment=t_item, control=c_item,
            gross_diff_pct=gross_diff, net_diff_pct=net_diff,
            entry_divergence_bps=divergence))
    return PairingReport(pairs=tuple(pairs), dropped=tuple(dropped),
                         experiment_key=experiment_key,
                         require_matching_entry=bool(require_matching_entry),
                         entry_tolerance_bps=tolerance)


def _bucket_by_opportunity(rows: Sequence[LabelResult]) -> dict[str, list[LabelResult]]:
    buckets: dict[str, list[LabelResult]] = {}
    for item in rows:
        buckets.setdefault(opportunity_key(item), []).append(item)
    return buckets


def _entry_divergence_bps(treatment: LabelResult, control: LabelResult) -> float | None:
    """两臂入场参考价的相对偏差（基点）；任一臂推不出入场价则 ``None``。

    结果按 1e-6 基点取整：入场价是从 ``exit``/``gross`` 反推的浮点量，不取整会把
    1e-11 量级的浮点噪声当成「入场价不一致」，从而**误剔除**本该配对的样本。
    """
    t_entry = implied_entry_price(treatment)
    c_entry = implied_entry_price(control)
    if not t_entry or not c_entry or c_entry <= 0:
        return None
    return round(abs(t_entry - c_entry) / c_entry * 10000.0, 6)


def pair_overlap_blocks(pairs: Iterable[PairedSample], *,
                        gap_seconds: float = 0.0) -> tuple[OverlapBlock, ...]:
    """把配成的对按「同标的 + 窗口重叠」聚块（配对差也共享价格路径）。"""
    entries = []
    for item in pairs:
        start, end = item.window()
        entries.append((item.opportunity, item.symbol, start, end))
    return _blocks_from_windows(entries, gap_seconds=gap_seconds)


# --------------------------------------------------------------------------- #
# 区间与检验
# --------------------------------------------------------------------------- #
def _z(confidence: float) -> float:
    """标准正态分位数（stdlib，无需硬编码临界值表）。"""
    conf = min(max(float(confidence), 0.5), 0.999999)
    return float(NormalDist().inv_cdf(0.5 + conf / 2.0))


def mean_interval(values: Iterable[float], *, confidence: float = DEFAULT_CONFIDENCE,
                  assumption: str = "iid") -> dict[str, Any]:
    """样本均值的正态近似区间；``n<2`` 时不出区间（返回 ``None`` 边界）。

    Args:
        values: 观测值（这里通常是「独立块均值」或「配对差」）。
        confidence: 置信水平。
        assumption: 该区间所依赖的假设标签；调用方必须如实标注是 ``iid`` 还是
            ``independent_blocks``。

    Returns:
        含 ``n``/``mean``/``se``/``half_width``/``low``/``high`` 的字典。
    """
    rows = [float(item) for item in values]
    n = len(rows)
    if n == 0:
        return {"n": 0, "mean": None, "se": None, "half_width": None,
                "low": None, "high": None, "confidence": float(confidence),
                "method": "normal_approx", "assumption": assumption,
                "note": "无样本：不出区间，也不以 0 冒充。"}
    mean = sum(rows) / n
    if n < 2:
        return {"n": n, "mean": mean, "se": None, "half_width": None,
                "low": None, "high": None, "confidence": float(confidence),
                "method": "normal_approx", "assumption": assumption,
                "note": "单样本：方差不可估计，只给均值。"}
    variance = sum((item - mean) ** 2 for item in rows) / (n - 1)
    se = (variance / n) ** 0.5
    half = _z(confidence) * se
    return {"n": n, "mean": mean, "se": se, "half_width": half,
            "low": mean - half, "high": mean + half,
            "confidence": float(confidence), "method": "normal_approx",
            "assumption": assumption,
            "note": "区间的有效性取决于 assumption 是否成立。"}


def bootstrap_interval(values: Iterable[float], *,
                       confidence: float = DEFAULT_CONFIDENCE,
                       resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
                       seed: int = DEFAULT_BOOTSTRAP_SEED,
                       assumption: str = "independent_blocks") -> dict[str, Any]:
    """百分位自举区间；**显式种子** ⇒ 同输入同种子必得同区间（可重放）。

    Args:
        values: 独立单元的取值（务必是**块均值**，不是重叠行）。
        confidence: 置信水平。
        resamples: 重采样次数（至少 100）。
        seed: 随机种子（显式；默认常量而非系统熵，保证可重放）。
        assumption: 假设标签。

    Returns:
        含 ``n``/``mean``/``low``/``high``/``resamples``/``seed``/``digest`` 的字典。
        ``n<2`` 时不给区间（返回 ``None``）。
    """
    rows = [float(item) for item in values]
    n = len(rows)
    count = max(100, int(resamples))
    if n < 2:
        return {"n": n, "mean": (rows[0] if rows else None), "low": None, "high": None,
                "resamples": count, "seed": int(seed), "confidence": float(confidence),
                "method": "cluster_bootstrap_percentile", "assumption": assumption,
                "digest": None, "note": "样本不足 2 个独立单元：不出区间。"}
    rng = random.Random(int(seed))
    rng.randrange(n)  # 预热一次，避免不同 n 下首个采样值的平凡相关
    means: list[float] = []
    for _ in range(count):
        total = 0.0
        for _ in range(n):
            total += rows[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    alpha = (1.0 - min(max(float(confidence), 0.5), 0.999999)) / 2.0
    low = means[int(alpha * (count - 1))]
    high = means[int((1.0 - alpha) * (count - 1))]
    payload = {"n": n, "mean": sum(rows) / n, "low": low, "high": high,
               "resamples": count, "seed": int(seed), "confidence": float(confidence),
               "method": "cluster_bootstrap_percentile", "assumption": assumption}
    payload["digest"] = report_digest({"bootstrap": payload})
    payload["note"] = ("百分位自举：种子显式且常量 ⇒ 同输入必得同区间（可重放）；"
                       "重采样单元必须是独立块。")
    return payload


def sign_test(values: Iterable[float]) -> dict[str, Any]:
    """配对差的**精确二项**符号检验（双侧，分布无关，无随机成分）。

    Args:
        values: 配对差（``treatment - control``）。

    Returns:
        含 ``n``/``positive``/``negative``/``zeros``/``p_value`` 的字典；
        ``zeros`` 是差为 0 的对数，按惯例**排除**在符号检验之外并披露。
    """
    rows = [float(item) for item in values]
    positive = sum(1 for item in rows if item > 0)
    negative = sum(1 for item in rows if item < 0)
    zeros = len(rows) - positive - negative
    n = positive + negative
    if n == 0:
        return {"n": 0, "positive": positive, "negative": negative, "zeros": zeros,
                "p_value": None, "method": "exact_binomial_two_sided",
                "note": "全部配对差为 0（或没有配对）：不出 p 值。"}
    tail = max(positive, negative)
    total = float(2 ** n)
    extreme = sum(_comb(n, k) for k in range(tail, n + 1)) / total
    return {"n": n, "positive": positive, "negative": negative, "zeros": zeros,
            "p_value": min(1.0, 2.0 * extreme),
            "method": "exact_binomial_two_sided",
            "note": "分布无关的精确符号检验；不假设收益正态。"}


def _comb(n: int, k: int) -> float:
    """组合数（浮点，避免大整数在报告里膨胀）。"""
    if k < 0 or k > n:
        return 0.0
    result = 1.0
    for index in range(1, k + 1):
        result = result * (n - k + index) / index
    return result


def paired_statistics(pairs: Iterable[PairedSample], *,
                      confidence: float = DEFAULT_CONFIDENCE,
                      resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
                      seed: int = DEFAULT_BOOTSTRAP_SEED,
                      gap_seconds: float = 0.0) -> dict[str, Any]:
    """对配对差给出「朴素区间 + 分块区间 + 符号检验」，并标明哪个才可信。

    朴素区间把每一对当独立样本；只要存在重叠块，它就会**低估**区间宽度。
    因此两个区间都报，并用 ``naive_understates``（是否真的更窄）与
    ``independent_blocks``（真实独立单元数）如实披露，交给下游判断。
    """
    rows = list(pairs)
    diffs = [item.net_diff_pct for item in rows]
    gross_diffs = [float(item.gross_diff_pct) for item in rows]
    blocks = pair_overlap_blocks(rows, gap_seconds=gap_seconds)
    block_means: list[float] = []
    for block in blocks:
        members = [item.net_diff_pct for item in rows if item.opportunity in set(block.keys)]
        if members:
            block_means.append(sum(members) / len(members))
    naive = mean_interval(diffs, confidence=confidence, assumption="iid_pairs")
    cluster = bootstrap_interval(block_means, confidence=confidence, resamples=resamples,
                                 seed=seed, assumption="independent_blocks")
    naive_se = naive.get("se")
    block_se = None
    if len(block_means) >= 2:
        block_mean = sum(block_means) / len(block_means)
        variance = sum((item - block_mean) ** 2 for item in block_means) / (len(block_means) - 1)
        block_se = (variance / len(block_means)) ** 0.5
    understated = bool(isinstance(naive_se, float) and isinstance(block_se, float)
                       and block_se > 0 and naive_se < block_se)
    return {
        "pairs": len(rows),
        "independent_blocks": len(block_means),
        "naive": naive,
        "cluster": cluster,
        "block_means": block_means,
        "naive_se": naive_se,
        "cluster_se": block_se,
        "naive_understates": understated,
        "sign_test": sign_test(diffs),
        "mean_net_diff_pct": (sum(diffs) / len(diffs) if diffs else None),
        "mean_gross_diff_pct": (sum(gross_diffs) / len(gross_diffs) if gross_diffs else None),
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        "note": ("重叠样本不得冒充独立：以 ``cluster``（按独立块自举）为准，"
                 "``naive`` 只作对照；两者差异越大说明重叠越严重。"),
    }


# --------------------------------------------------------------------------- #
# 费用压力
# --------------------------------------------------------------------------- #
def stressed_net_pct(result: LabelResult, *, multiplier: float,
                     extra_slippage_bps: float = 0.0) -> float | None:
    """在费用/滑点假设上乘压力倍数后重算净值（百分点）。

    ``net = gross - (fee + slippage) * multiplier - extra_slippage_bps/100 - funding``
    资金费不随交易成本倍数放大（它是持仓成本，不是成交成本）。
    """
    if not result.is_labeled or result.gross_return_pct is None:
        return None
    fee = float(result.fee_cost_pct or 0.0)
    slip = float(result.slippage_cost_pct or 0.0)
    funding = float(result.funding_cost_pct or 0.0)
    extra = max(0.0, float(extra_slippage_bps)) / 100.0
    return float(result.gross_return_pct) - (fee + slip) * float(multiplier) - extra - funding


def fee_stress(pairs: Iterable[PairedSample], *,
               multipliers: Sequence[float] = DEFAULT_FEE_STRESS_MULTIPLIERS,
               extra_slippage_bps: float = 0.0,
               confidence: float = DEFAULT_CONFIDENCE,
               resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
               seed: int = DEFAULT_BOOTSTRAP_SEED) -> dict[str, Any]:
    """逐压力倍数重算两臂净值与配对差，并给出盈亏平衡倍数。

    Args:
        pairs: 配成的对。
        multipliers: 压力倍数（1.0 = 原研究假设）。
        extra_slippage_bps: 额外滑点（基点，加在倍数之上）。
        confidence/resamples/seed: 配对差区间的参数（可重放）。

    Returns:
        含 ``points``（逐倍数）、``break_even_multiplier``、``sign_flip_multiplier``
        的字典。盈亏平衡由相邻倍数**线性插值**给出；未被倍数区间覆盖时返回 ``None``
        （不编造一个外推值）。
    """
    rows = list(pairs)
    points: list[dict[str, Any]] = []
    for multiplier in sorted({float(item) for item in multipliers}):
        treatment_nets = [stressed_net_pct(item.treatment, multiplier=multiplier,
                                           extra_slippage_bps=extra_slippage_bps)
                          for item in rows]
        control_nets = [stressed_net_pct(item.control, multiplier=multiplier,
                                         extra_slippage_bps=extra_slippage_bps)
                        for item in rows]
        diffs = [t - c for t, c in zip(treatment_nets, control_nets)
                 if t is not None and c is not None]
        usable = [item for item in treatment_nets if item is not None]
        usable_control = [item for item in control_nets if item is not None]
        points.append({
            "multiplier": multiplier,
            "extra_slippage_bps": float(extra_slippage_bps),
            "treatment_mean_net_pct": (sum(usable) / len(usable) if usable else None),
            "control_mean_net_pct": (sum(usable_control) / len(usable_control)
                                     if usable_control else None),
            "paired_mean_diff_pct": (sum(diffs) / len(diffs) if diffs else None),
            "paired_interval": bootstrap_interval(diffs, confidence=confidence,
                                                  resamples=resamples, seed=seed,
                                                  assumption="iid_pairs"),
            "n": len(usable),
            "unprofitable": (bool(usable) and (sum(usable) / len(usable)) <= 0.0),
        })
    return {
        "points": points,
        "break_even_multiplier": _break_even_multiplier(points),
        "sign_flip_multiplier": _sign_flip_multiplier(points),
        "cost_assumption_note": ("费用/滑点/资金费均为**研究假设**，不是真实成交成本"
                                 "（legacy v1 同口径）；压力倍数是敏感性分析，"
                                 "不构成成本预测。"),
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
    }


def _break_even_multiplier(points: Sequence[Mapping[str, Any]]) -> float | None:
    """均值净值穿过 0 的倍数（相邻点线性插值）；未跨越返回 ``None``。"""
    ordered = [item for item in points if item.get("treatment_mean_net_pct") is not None]
    for left, right in zip(ordered, ordered[1:]):
        first = float(left["treatment_mean_net_pct"])
        second = float(right["treatment_mean_net_pct"])
        if first == 0.0:
            return float(left["multiplier"])
        if (first > 0.0) != (second > 0.0):
            span = abs(first) + abs(second)
            weight = abs(first) / span if span else 0.0
            return float(left["multiplier"]) + weight * (
                float(right["multiplier"]) - float(left["multiplier"]))
    if ordered and float(ordered[-1]["treatment_mean_net_pct"]) == 0.0:
        return float(ordered[-1]["multiplier"])
    return None


def _sign_flip_multiplier(points: Sequence[Mapping[str, Any]]) -> float | None:
    """配对差均值首次转负的倍数（不做插值，直接取该压力点）。"""
    for item in points:
        value = item.get("paired_mean_diff_pct")
        if value is not None and float(value) < 0.0:
            return float(item["multiplier"])
    return None


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StatisticsReport:
    """一份完整的配对统计报告（研究参考；**不含**任何 GO/promotion 结论）。"""

    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """返回可 JSON 序列化的报告副本。"""
        return json.loads(json.dumps(self.payload, ensure_ascii=False, sort_keys=True,
                                     default=str))

    def digest(self) -> str:
        """整份报告的规范摘要；同输入必得同摘要（可重放自证）。"""
        return report_digest(self.to_dict())

    def to_rows(self) -> list[dict[str, Any]]:
        """返回可直接交给共享适配层落库的**只读研究**行。"""
        return [{
            "experiment_key": str(self.payload.get("experiment_key") or ""),
            "report_kind": str(self.payload.get("report_kind") or ""),
            "config_fingerprint": str(self.payload.get("config_fingerprint") or ""),
            "digest": self.digest(),
            "schema": SCHEMA_VERSION,
            "caliber": STATISTICS_CALIBER_ID,
            "payload": self.to_dict(),
            "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
            "execution_weight": _ss.EXECUTION_WEIGHT,
            "auto_promotion": False,
        }]


def report_digest(payload: Mapping[str, Any]) -> str:
    """对任意可序列化载荷取规范 JSON 的 sha256（键排序、UTF-8、无空白抖动）。"""
    canonical = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_statistics_report(treatment: Iterable[LabelResult], control: Iterable[LabelResult], *,
                            report_kind: str = "paired_experiment",
                            config_fingerprint: str = "",
                            require_matching_entry: bool = True,
                            entry_tolerance_bps: float = DEFAULT_ENTRY_TOLERANCE_BPS,
                            confidence: float = DEFAULT_CONFIDENCE,
                            multipliers: Sequence[float] = DEFAULT_FEE_STRESS_MULTIPLIERS,
                            extra_slippage_bps: float = 0.0,
                            resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
                            seed: int = DEFAULT_BOOTSTRAP_SEED,
                            gap_seconds: float = 0.0) -> StatisticsReport:
    """一步产出「配对 → 独立块 → 区间 → 费用压力 → 覆盖率」的研究报告。

    Raises:
        ExperimentMixError: 样本混版本。
        GateLeakError: 样本带执行/门禁语义（研究侧最后一道闸）。
    """
    t_rows = _sorted_rows(treatment)
    c_rows = _sorted_rows(control)
    assert_gate_neutral(t_rows + c_rows)
    experiment_key = assert_single_experiment(t_rows + c_rows)
    pairing = pair_experiment(t_rows, c_rows, require_matching_entry=require_matching_entry,
                              entry_tolerance_bps=entry_tolerance_bps)
    return StatisticsReport(payload={
        "schema": SCHEMA_VERSION,
        "report_kind": str(report_kind),
        "caliber": STATISTICS_CALIBER_ID,
        "caliber_label": STATISTICS_CALIBER_LABEL,
        "experiment_key": experiment_key,
        "config_fingerprint": str(config_fingerprint),
        "arm_roles": {"treatment": ARM_ROLE_TREATMENT, "control": ARM_ROLE_CONTROL},
        "pairing": pairing.to_dict(),
        "coverage": {
            "treatment": _coverage_report(t_rows),
            "control": _coverage_report(c_rows),
        },
        "independence": {
            "treatment": independence_report(t_rows, gap_seconds=gap_seconds),
            "control": independence_report(c_rows, gap_seconds=gap_seconds),
            "paired": {
                "pairs": len(pairing.pairs),
                "independent_blocks": len(pair_overlap_blocks(pairing.pairs,
                                                             gap_seconds=gap_seconds)),
                "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
            },
        },
        "paired": paired_statistics(pairing.pairs, confidence=confidence,
                                    resamples=resamples, seed=seed,
                                    gap_seconds=gap_seconds),
        "fee_stress": fee_stress(pairing.pairs, multipliers=multipliers,
                                 extra_slippage_bps=extra_slippage_bps,
                                 confidence=confidence, resamples=resamples, seed=seed),
        "execution": promotion_guard(),
        "gate": {
            "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
            "role_by_source": {spec.source: spec.gate_role for spec in _ss.COHORTS},
            "research_sources": list(_ss.research_sources()),
            "authority": "src/core/release_evidence.py (U09 固定并版本化)",
            "note": "统计产物不进任何门禁分母；本报告不产出 GO 结论。",
        },
        "note": PAIRED_STATISTICS_SCOPE_NOTE,
    })


def statistics_isolation_facts() -> dict[str, Any]:
    """返回研究统计与发布门禁的隔离事实（供 UI/审查引用，不含门禁判定）。"""
    return {
        "schema": SCHEMA_VERSION,
        "caliber": STATISTICS_CALIBER_ID,
        "legacy_caliber": _ss.LEGACY_CALIBER_ID,
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        "execution_weight": _ss.EXECUTION_WEIGHT,
        "auto_promotion": False,
        "research_sources": list(_ss.research_sources()),
        "gate_role_by_source": {spec.source: spec.gate_role for spec in _ss.COHORTS},
        "release_gate_authority": "src/core/release_evidence.py (U09 固定并版本化)",
        "shadow_status_v2_state": _ss.V2_LABELS_STATE,
        "research_tables": (
            "shadow_v2_statistics（U07 共享适配补丁新增的**只读研究**表；"
            "未接线时不存在，接线后也不被任何门禁查询读取）",),
        "note": PAIRED_STATISTICS_SCOPE_NOTE,
    }
