"""U05 真实耗时、模型降级和决策原因（K01/K05、DL01/DL03/DL04/DL06/DL07）。

本模块是 **U03 逐意图证据** 与 **K05 轨迹/计数器契约** 之间的执行侧适配层，
只解决任务卡 U05 的五件事，且**不替任何模块做决定**：

1. **请求次数与失败轮次分开统计**
   ``cycles_total``（轮次，按 ``cycle_id`` 去重）/ ``api_attempts_total``（请求次数，
   逐 span 计数）/ ``decisions_total``（决策个数）来自 K05 :func:`tally`；
   ``failed_rounds``（失败轮次）与 ``failed_requests``（失败请求）由本模块
   **另外**按轮次结局与 span 结局统计。**没有任何一个字段可以代表全部**——
   这正是把"这一轮跑了几次、失败了几次"混成一个计数器的旧口径的修复点。
2. **所有模型尝试共享同一个绝对 deadline（R22）**
   :class:`DeadlineBudget` 的 ``deadline`` 是**单调时钟绝对时刻**；``shared()``
   返回同一个绝对期限（**不**用 ``now + timeout`` 重新推导），主/备/重试/工具轮
   全部拿它。``attempt_timeout(attempts_left)`` 把剩余时间**按剩余尝试数切分**，
   使一次长调用不能吃掉后续尝试和快循环的预算（长调用不拖住快循环）。
3. **LLM 失败不阻塞保护与对账**
   :func:`run_protection_first` 固定"保护 → 对账 → 模型"的**事实优先级**：
   模型步抛错时保护步与对账步**仍然执行**，模型失败只作为原因码附着，
   不改变 ``protection_ran``/``reconciliation_ran`` 的事实，也不置 ``blocked``。
4. **原因码分类（主因/辅因共存）**
   九个冻结原因码 + :class:`DecisionReason`（``primary`` + ``secondary``）。
   分类器只读**结构化事实**（HTTP 状态码、异常类型名、U03 证据报告原因码、
   就绪阻断项、通道冲突），不猜自然语言。
5. **分阶段 span 定义 p95/p99**
   :data:`PERCENTILE_METHOD` 写死"nearest-rank（``ceil(q*n)-1``，无插值）"，
   :func:`stage_latency_stats` 按 :data:`STAGE_ORDER` 输出 ``count/p50/p95/p99/max``。
   数值阈值属 K05 README §12 L12（RC 前冻结），本模块**只冻结口径不冻结阈值**。

另外落地 §3.3 的四类路由风险（``SILENT_MOCK`` / ``EARLY_RETURN`` /
``PARAMETER_INHERITANCE`` / ``CROSS_PROCESS_QUOTA``）与 S03/S04 的预算与准入口径：

- :class:`ModelRouteGuard`：显式 Mock 声明、返回前必须真有尝试、响应模型必须
  与请求模型一致（除非调用方**显式声明**了备用路由）、跨进程准入必须带脱敏作用域。
- :class:`BackgroundRefresh` / :class:`SourceCache`：HTTP 与摘要**总预算**、
  分源 TTL 缓存、剩余预算不足以覆盖管理预留时**立即让路不阻塞**。
- :class:`LeaseRegistry` / :func:`owner_alive`：按**脱敏凭据别名**共享的跨进程
  准入租约与**崩溃回收**（属主进程已死或租约过期即回收，不靠"等锁超时"）。

设计约束（与 U03/契约同口径）：纯函数式工具 + 内存态记录器；不联网、不读写
 ``.env``/真实 ``data/``、不构造 Broker、不启动服务、不改风险档/白名单/live 开关。
唯一会碰文件系统的路径是**调用方显式指定的沙箱目录**（租约 sidecar），
缺省目录由 ``MTA_LLM_COORDINATION_DIR`` 决定，测试一律指向临时目录。

已知限制：``MODEL_*`` 只覆盖九码中的两个模型码，供应商"不可达"与"超时"在
本层**同码**（``MODEL_DEADLINE``，细节留在 ``DecisionReason.details``）；要分开
需要 K05 新增 reason code（小版本演进），本模块不越权改契约。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from core.contracts import (
    DecisionCounters,
    DecisionTraceSpan,
    SpanValidationError,
    span_violations,
    tally,
)

SCHEMA_VERSION = "u05-decision-trace-1"

#: p95/p99 的定义（冻结口径，不含阈值）：nearest-rank，索引 ``ceil(q*n)-1``，
#: 不做线性插值。样本数为 0 时返回 ``None`` 而不是 0——"没有样本"不能读成"耗时 0"。
PERCENTILE_METHOD = "nearest-rank: sorted[ceil(q*n)-1], no interpolation"


# ------------------------------------------------------------------ 原因码

REASON_NO_EDGE = "NO_EDGE"
REASON_HOLD_EXISTING = "HOLD_EXISTING"
REASON_RESEARCH_INCOMPLETE = "RESEARCH_INCOMPLETE"
REASON_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
REASON_MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
REASON_MODEL_DEADLINE = "MODEL_DEADLINE"
REASON_VALIDATION_REJECTED = "VALIDATION_REJECTED"
REASON_READINESS_BLOCKED = "READINESS_BLOCKED"
REASON_CHANNEL_CONFLICT = "CHANNEL_CONFLICT"

#: 冻结的九码，顺序 = 主因选取的**确定性优先级**（越靠前越"根因近"）。
#: 例：既有持仓 + 无新边际 = 辅因 HOLD_EXISTING 不抢主因 NO_EDGE 的位置。
REASON_ORDER: tuple[str, ...] = (
    REASON_MODEL_DEADLINE,
    REASON_MODEL_RATE_LIMITED,
    REASON_CHANNEL_CONFLICT,
    REASON_DATA_UNAVAILABLE,
    REASON_READINESS_BLOCKED,
    REASON_RESEARCH_INCOMPLETE,
    REASON_VALIDATION_REJECTED,
    REASON_NO_EDGE,
    REASON_HOLD_EXISTING,
)
REASON_CODES: frozenset[str] = frozenset(REASON_ORDER)


class UnknownReasonCode(ValueError):
    """原因码不在冻结集合内：禁止用自由文本充当主因/辅因。"""

    def __init__(self, codes: Sequence[str]) -> None:
        self.codes = tuple(codes)
        super().__init__("unclassified reason code: " + ", ".join(self.codes))


def normalise_reason_codes(codes: Iterable[str]) -> tuple[str, ...]:
    """去重保序 + 逐码 fail-closed；空码与未登记码都拒绝。"""
    out: list[str] = []
    unknown: list[str] = []
    for raw in codes:
        text = str(raw or "").strip()
        if not text:
            continue
        if text not in REASON_CODES:
            unknown.append(text)
            continue
        if text not in out:
            out.append(text)
    if unknown:
        raise UnknownReasonCode(tuple(dict.fromkeys(unknown)))
    return tuple(out)


def choose_primary(codes: Sequence[str], *, prefer: str | None = None) -> str:
    """按 :data:`REASON_ORDER` 选主因；``prefer`` 仅在确实出现时生效。"""
    known = normalise_reason_codes(codes)
    if prefer is not None and prefer in known:
        return prefer
    for code in REASON_ORDER:
        if code in known:
            return code
    return REASON_NO_EDGE


@dataclass(frozen=True)
class DecisionReason:
    """一次决策的原因：**主因唯一、辅因可多**，两者可共存且都进审计。

    ``primary`` 必须在 :data:`REASON_CODES` 内；辅因不得与主因重复。空
    辅因是合法形态（"只有一个原因"），但"一个原因都没有"由调用方显式给
    ``NO_EDGE``，不允许以空结构代表"无原因"。
    """

    primary: str
    secondary: tuple[str, ...] = ()
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        primary = normalise_reason_codes([self.primary])
        if not primary:
            raise UnknownReasonCode(("",))
        secondary = tuple(c for c in normalise_reason_codes(self.secondary)
                          if c != primary[0])
        object.__setattr__(self, "primary", primary[0])
        object.__setattr__(self, "secondary", secondary)
        object.__setattr__(self, "details", tuple(str(item) for item in self.details if item))

    @property
    def codes(self) -> tuple[str, ...]:
        return (self.primary, *self.secondary)

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "secondary": list(self.secondary),
            "codes": list(self.codes),
            "details": list(self.details),
        }

    @classmethod
    def from_codes(cls, codes: Iterable[str], *, details: Iterable[str] = (),
                   prefer: str | None = None) -> "DecisionReason":
        known = normalise_reason_codes(codes)
        if not known:
            known = (REASON_NO_EDGE,)
        primary = choose_primary(known, prefer=prefer)
        return cls(primary=primary,
                   secondary=tuple(code for code in known if code != primary),
                   details=tuple(details))

    def merged(self, other: "DecisionReason") -> "DecisionReason":
        """两个原因共存：辅因取并集，主因重选（按优先级，不按加入顺序）。"""
        return DecisionReason.from_codes(
            (*self.codes, *other.codes), details=(*self.details, *other.details),
        )


#: 异常/信号 -> 原因码的**确定性**映射（只读结构化事实，不解析文本）。
_MODEL_DEADLINE_TYPES = frozenset({
    "LLMTimeBudgetExceeded", "LLMAdmissionTimeout", "TimeoutExpired", "TimeoutError",
    "ReadTimeout", "ConnectTimeout", "PoolTimeout", "WriteTimeout",
})
_MODEL_RATE_LIMITED_STATUS = frozenset({429, 408})


def _status_code_of(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return int(status) if isinstance(status, int) else None


def classify_exception(exc: BaseException) -> DecisionReason:
    """把模型/数据调用异常映射到九码之一，并保留异常类型名作细节。

    映射（fail-closed，不透传自由文本）：

    - HTTP ``429``/``408`` 与 ``Retry-After`` 冷却 → ``MODEL_RATE_LIMITED``；
    - ``LLMTimeBudgetExceeded`` / ``LLMAdmissionTimeout`` / 任何超时类
      （含 ``subprocess.TimeoutExpired``，即 WorkBuddy CLI 超时）→ ``MODEL_DEADLINE``；
    - 供应商网络类异常（``TransportError`` 及 5xx）→ ``MODEL_DEADLINE``
      （同码，细节留类型名；把"不可达"与"超时"分开需 K05 新增码）；
    - 其余（参数/鉴权/协议/解析错误）→ ``VALIDATION_REJECTED``，
      即"这次调用被拒"而不是"模型慢"。
    """
    names: list[str] = []
    node: BaseException | None = exc
    seen: set[int] = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        names.append(type(node).__name__)
        cause = node.__cause__ or node.__context__
        node = cause if isinstance(cause, BaseException) else None
    detail = f"{names[0]}:{exc}"[:200]

    status = _status_code_of(exc)
    if status is not None and status in _MODEL_RATE_LIMITED_STATUS:
        return DecisionReason(primary=REASON_MODEL_RATE_LIMITED, details=(detail,))
    if any(name in _MODEL_DEADLINE_TYPES for name in names):
        return DecisionReason(primary=REASON_MODEL_DEADLINE, details=(detail,))
    if status is not None and 500 <= status <= 599:
        return DecisionReason(primary=REASON_MODEL_DEADLINE,
                              details=(f"HTTP {status} {detail}",))
    if any(name in {"TransportError", "ConnectError", "ReadError"} for name in names):
        return DecisionReason(primary=REASON_MODEL_DEADLINE,
                              details=(f"transport {detail}",))
    return DecisionReason(primary=REASON_VALIDATION_REJECTED, details=(detail,))


def classify_evidence_report(report: Any) -> DecisionReason:
    """把 U03 逐意图证据报告映射成原因码（**不重复实现**证据判定）。

    只消费 U03 已产出的结构化字段：``decision_gate()``、``rejected_intent_ids``、
    每个 ``IntentEvidence.reason_codes``。判定语义：

    - 非 OPEN 动作被委托给 K04 → 不产生原因（返回空 DecisionReason）；
    - 源不可用/快照缺失/身份未固定/程序基准缺触发价 → ``RESEARCH_INCOMPLETE``
      为主因，**同时**回带 ``DATA_UNAVAILABLE`` 辅因（"证据没备齐"与"数据取不到"
      是两件事，可能同时成立）；
    - 数值矛盾/几何/方向类拒绝 → ``VALIDATION_REJECTED``；
    - 全部通过 → ``NO_EDGE``（有证据但没有可下单的边际）。
    """
    intents = list(getattr(report, "intents", ()) or ())
    codes: list[str] = []
    details: list[str] = []
    data_missing = False
    research = False
    validation = False
    for item in intents:
        reasons = tuple(getattr(item, "reason_codes", ()) or ())
        if getattr(item, "accepted", False):
            continue
        if not getattr(item, "applies_to_open", False):
            # 非 OPEN：由 K04 管理契约处理，本层不产出原因码（不误伤退出）。
            continue
        for reason in reasons:
            text = str(reason)
            if text.startswith(("SOURCE_UNAVAILABLE", "SOURCE_STALE",
                                "SOURCE_TIME_UNVERIFIED", "SNAPSHOT_MISSING",
                                "SNAPSHOT_STALE", "SNAPSHOT_UNUSABLE",
                                "SNAPSHOT_FROM_FUTURE", "MISSING_TRIGGER_PRICE",
                                "EVIDENCE_TIME_MISMATCH", "INTERVAL_")):
                research = True
                if text.startswith(("SOURCE_UNAVAILABLE", "SOURCE_STALE",
                                    "SNAPSHOT_MISSING", "SNAPSHOT_STALE")):
                    data_missing = True
            elif text.startswith(("RR_CONTRADICTION", "GEOMETRY_INVALID",
                                  "STOP_DIRECTION", "TARGET_DIRECTION",
                                  "PRICE_NON_FINITE", "K02:", "CLAIM_",
                                  "MODEL_FACT_OVERRIDE")):
                validation = True
            elif text.startswith("IDENTITY_UNPINNED"):
                research = True
            else:
                research = True
            details.append(text)
    if research:
        codes.append(REASON_RESEARCH_INCOMPLETE)
    if data_missing:
        codes.append(REASON_DATA_UNAVAILABLE)
    if validation:
        codes.append(REASON_VALIDATION_REJECTED)
    if not codes:
        return DecisionReason(primary=REASON_NO_EDGE, details=("evidence_gate_ok",))
    # 证据门禁场景下"证据没备齐"是决策层原因（主因），"某个源取不到"是其中一种
    # 成因（辅因）；两者必须共存，而不是让更靠前的数据码吞掉研究结论。
    return DecisionReason.from_codes(
        codes, details=tuple(dict.fromkeys(details)),
        prefer=REASON_RESEARCH_INCOMPLETE if research else None,
    )


def reason_from_source_errors(
    errors: Sequence[Any],
    *,
    labels: Sequence[str] = (),
    degraded: bool = False,
) -> DecisionReason | None:
    """由数据适配层收集的**源级错误**派生原因码；无错误时返回 ``None``。

    返回 ``None`` 而不是 ``NO_EDGE``：``NO_EDGE`` 是"数据齐了但没有边际"的结论，
    只有执行侧能下；数据适配层只能说"我取不到哪些源"。所有错误都给
    ``DATA_UNAVAILABLE``，并保留逐源标签（不解析自由文本，只做标签留痕）；
    ``degraded=True``（如回落到缓存/部分源）时附加 ``RESEARCH_INCOMPLETE``，
    因为"用了陈旧/部分数据"与"完全取不到"是两件不同的事。
    """
    items = [str(item) for item in errors if str(item or "").strip()]
    if not items:
        return None
    codes = [REASON_DATA_UNAVAILABLE]
    if degraded:
        codes.append(REASON_RESEARCH_INCOMPLETE)
    details = tuple(items) + tuple(str(item) for item in labels if item)
    return DecisionReason.from_codes(codes, details=details)


def classify_cycle(
    *,
    intents: Sequence[Any] = (),
    hold_existing: bool = False,
    readiness_blockers: Sequence[str] = (),
    channel_conflicts: Sequence[str] = (),
    data_unavailable: Sequence[str] = (),
    evidence_report: Any | None = None,
    model_error: BaseException | None = None,
) -> DecisionReason:
    """一轮决策的原因归集：所有成立的信号都进辅因，主因按优先级选。

    调用方只需**如实**提供结构化事实；是否"有边际"由 ``intents`` 是否非空决定，
    不由本模块猜。``hold_existing`` 只表示"确有一笔应在持仓"，不改变主因优先级。
    """
    codes: list[str] = []
    details: list[str] = []
    if model_error is not None:
        model = classify_exception(model_error)
        codes.extend(model.codes)
        details.extend(model.details)
    if channel_conflicts:
        codes.append(REASON_CHANNEL_CONFLICT)
        details.extend(str(item) for item in channel_conflicts)
    if data_unavailable:
        codes.append(REASON_DATA_UNAVAILABLE)
        details.extend(str(item) for item in data_unavailable)
    if readiness_blockers:
        codes.append(REASON_READINESS_BLOCKED)
        details.extend(str(item) for item in readiness_blockers)
    if evidence_report is not None:
        evidence = classify_evidence_report(evidence_report)
        codes.extend(evidence.codes)
        details.extend(evidence.details)
    if not intents:
        codes.append(REASON_NO_EDGE)
    if hold_existing:
        codes.append(REASON_HOLD_EXISTING)
    if not codes:
        codes.append(REASON_NO_EDGE)
    return DecisionReason.from_codes(codes, details=tuple(dict.fromkeys(details)))


# ------------------------------------------------------------------ 期限

#: 单个模型尝试的最低可执行时限：切分剩余预算时不会切到"必然失败"的 0。
MIN_ATTEMPT_SECONDS = 0.25


class DeadlineExceeded(RuntimeError):
    """本轮绝对期限已耗尽（调用方必须走安全降级，不得再发起尝试）。"""


@dataclass(frozen=True)
class DeadlineBudget:
    """**单调时钟绝对期限**；所有模型尝试共享同一个 ``deadline``。

    ``shared()`` 返回自身（同一个绝对时刻），这正是"所有尝试同一个 deadline"的
    机器可检查形态：不会出现第二个用 ``now + timeout`` 重新推导的期限。
    ``slice(seconds)`` 用于**确有独立预算上限**的阶段（情报摘要/后台刷新），
    子期限 = ``min(父 deadline, now + seconds)``，**永不越过父期限**。
    """

    deadline: float
    started: float
    total_seconds: float
    clock: Callable[[], float] = time.monotonic
    label: str = "cycle"

    @classmethod
    def start(cls, timeout_seconds: float, *, clock: Callable[[], float] = time.monotonic,
              label: str = "cycle") -> "DeadlineBudget":
        seconds = float(timeout_seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("timeout_seconds 必须是正有限数")
        started = float(clock())
        return cls(deadline=started + seconds, started=started,
                   total_seconds=seconds, clock=clock, label=str(label))

    def now(self) -> float:
        return float(self.clock())

    def remaining_seconds(self) -> float:
        return self.deadline - self.now()

    def remaining_ms(self) -> int:
        return int(self.remaining_seconds() * 1000)

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def shared(self) -> "DeadlineBudget":
        """模型尝试共用的绝对期限：就是自己，不重新推导。"""
        return self

    def slice(self, seconds: float, *, label: str | None = None) -> "DeadlineBudget":
        """受父期限约束的子预算；父已经更紧时**不延长**。"""
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("seconds 必须是正有限数")
        child_deadline = min(self.deadline, self.now() + seconds)
        return DeadlineBudget(
            deadline=child_deadline, started=self.started,
            total_seconds=child_deadline - self.started, clock=self.clock,
            label=str(label or self.label),
        )

    def attempt_timeout(self, attempts_left: int,
                        *, minimum: float = MIN_ATTEMPT_SECONDS) -> float:
        """把剩余时间按**剩余尝试数**切分，长调用不拖住后续尝试（R22）。

        ``attempts_left <= 0`` 或期限已耗尽是调用方时序错误，直接抛
        :class:`DeadlineExceeded`（不返回 0 让调用方"再试一次"）。
        """
        remaining = self.remaining_seconds()
        if attempts_left <= 0:
            raise DeadlineExceeded("attempts_left must be positive")
        if remaining <= 0:
            raise DeadlineExceeded(f"{self.label} deadline exhausted")
        return max(minimum, remaining / float(attempts_left))

    def check(self) -> None:
        """调用方在发起尝试前调用；耗尽即抛，避免无谓的供应商往返。"""
        if self.expired():
            raise DeadlineExceeded(f"{self.label} deadline exhausted")

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "total_seconds": round(self.total_seconds, 3),
            "remaining_seconds": round(self.remaining_seconds(), 3),
            "remaining_ms": self.remaining_ms(),
            "expired": self.expired(),
        }


#: span 与期限不一致的违规码（K05 span 之外的**跨 span**检查，README L4 缺口）。
VIOLATION_DEADLINE_MISMATCH = "DEADLINE_MISMATCH"
VIOLATION_DEADLINE_OVERRUN = "DEADLINE_OVERRUN"

#: 绝对期限比较的容差（毫秒）：取整与浮点余量，不影响"同/不同"的判定。
DEADLINE_TOLERANCE_MS = 1


def span_absolute_deadline_ms(span: DecisionTraceSpan) -> int | None:
    """span 隐含的**绝对期限** = ``started_ms + deadline_remaining_ms``。

    K05 只冻结了"剩余期限"，没有 ``cycle_deadline_ms`` 字段（README §12 L4）。
    把两者相加即得该 span 所依据的绝对期限；同一轮内全部 span 的这个值必须一致，
    "同 deadline" 才有机器可检查的证据，而不只是文档声明。
    """
    if span.deadline_remaining_ms is None:
        return None
    return int(span.started_ms) + int(span.deadline_remaining_ms)


def deadline_violations(spans: Sequence[DecisionTraceSpan], *,
                        expected_deadline_ms: int | None = None) -> tuple[str, ...]:
    """跨 span 的一致性谓词：同一轮全部 span 必须落在**同一个绝对期限**上。

    - 同一 ``cycle_id`` 内两两绝对期限相差超过 :data:`DEADLINE_TOLERANCE_MS`
      → ``DEADLINE_MISMATCH``（说明某个尝试偷偷用了 ``now + timeout`` 重新推导的期限）；
    - 给了 ``expected_deadline_ms`` 时，任一 span 的绝对期限**晚于**它
      → ``DEADLINE_OVERRUN``（该尝试借了本轮之外的预算）。
    """
    found: list[str] = []
    by_cycle: dict[str, list[int]] = {}
    for span in spans:
        absolute = span_absolute_deadline_ms(span)
        if absolute is None:
            continue
        by_cycle.setdefault(span.cycle_id, []).append(absolute)
        if (expected_deadline_ms is not None
                and absolute > int(expected_deadline_ms) + DEADLINE_TOLERANCE_MS):
            found.append(f"{VIOLATION_DEADLINE_OVERRUN}:{span.stage}")
    for values in by_cycle.values():
        if values and (max(values) - min(values)) > DEADLINE_TOLERANCE_MS:
            found.append(f"{VIOLATION_DEADLINE_MISMATCH}:{max(values) - min(values)}ms")
    return tuple(dict.fromkeys(found))


# ------------------------------------------------------------------ 阶段与分位


class Stage(StrEnum):
    """分阶段耗时口径；``Stage`` 值即 span 的 ``stage`` 字段。"""

    INTEL_REFRESH = "intel_refresh"
    MODEL_DECISION = "model_decision"
    TOOL_LOOP = "tool_loop"
    VALIDATION = "validation"
    MANAGEMENT = "management"
    RECONCILIATION = "reconciliation"


STAGE_ORDER: tuple[str, ...] = tuple(item.value for item in Stage)


@dataclass(frozen=True)
class StageLatency:
    """单个阶段的耗时分布；``p95``/``p99`` 按 :data:`PERCENTILE_METHOD` 计算。"""

    stage: str
    count: int
    p50: int | None
    p95: int | None
    p99: int | None
    max_ms: int | None
    mean_ms: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "count": self.count,
            "p50_ms": self.p50,
            "p95_ms": self.p95,
            "p99_ms": self.p99,
            "max_ms": self.max_ms,
            "mean_ms": None if self.mean_ms is None else round(self.mean_ms, 1),
            "method": PERCENTILE_METHOD,
        }


def percentile_ms(values: Sequence[int], q: float) -> int | None:
    """nearest-rank 分位（``ceil(q*n)-1``，无插值）；空样本返回 ``None``。

    ``None`` 而不是 ``0``：没有样本时"p95=0ms"会读成"飞快"，是虚假背书。
    """
    if not values:
        return None
    q = float(q)
    if not 0.0 < q <= 1.0:
        raise ValueError("q 必须落在 (0, 1]")
    ordered = sorted(int(item) for item in values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def stage_latency_stats(spans: Sequence[DecisionTraceSpan]) -> dict[str, StageLatency]:
    """按阶段聚合 span（只统计已结束的 span；未结束不参与分位）。"""
    buckets: dict[str, list[int]] = {}
    for span in spans:
        stage = str(span.stage or "")
        if not stage:
            continue
        duration = span.duration_ms()
        if duration is None:
            continue
        buckets.setdefault(stage, []).append(int(duration))
    out: dict[str, StageLatency] = {}
    for stage, values in buckets.items():
        out[stage] = StageLatency(
            stage=stage, count=len(values),
            p50=percentile_ms(values, 0.50),
            p95=percentile_ms(values, 0.95),
            p99=percentile_ms(values, 0.99),
            max_ms=max(values), mean_ms=sum(values) / len(values),
        )
    return out


# ------------------------------------------------------------------ 计数器


@dataclass(frozen=True)
class TraceSummary:
    """**四类分开**的计数视图；刻意不提供"总计数"字段。

    ``requests_total`` 与 ``failed_rounds`` 语义正交：一轮可以发 5 次请求全部成功
    （``cycles_total=1, api_attempts_total=5, failed_rounds=0``），也可以一轮发 1 次
    请求就失败（``cycles_total=1, api_attempts_total=1, failed_rounds=1``）。
    旧的单一"总次数"计数器无法区分这两种情形——A/Q 审查据此判定"失败轮次被轮次
    计数吞掉"。
    """

    counters: DecisionCounters
    requests_total: int
    requests_failed: int
    requests_retried: int
    failed_rounds: int
    rounds_total: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycles_total": self.counters.cycles_total,
            "api_attempts_total": self.counters.api_attempts_total,
            "decisions_total": self.counters.decisions_total,
            "requests_total": self.requests_total,
            "requests_failed": self.requests_failed,
            "requests_retried": self.requests_retried,
            "rounds_total": self.rounds_total,
            "failed_rounds": self.failed_rounds,
        }


@dataclass(frozen=True)
class CycleOutcome:
    """一轮的结局；``failed`` 是"轮次"维度的事实，不是请求维度的。"""

    cycle_id: str
    failed: bool
    reason: DecisionReason | None = None
    attempts: int = 0
    failed_attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "failed": self.failed,
            "attempts": self.attempts,
            "failed_attempts": self.failed_attempts,
            "reason": None if self.reason is None else self.reason.as_dict(),
        }


class DecisionTrace:
    """一轮/多轮决策的 span 与结局登记器（内存态；持久化由 AuditDatabase 适配承担）。

    使用方式：``with trace.span(...) as handle: handle.finish(reason)``。
    span 结束时生成 :class:`DecisionTraceSpan`；K05 ``tally`` 负责三类计数器，
    轮次结局由 :meth:`finish_cycle` 单独登记。
    """

    def __init__(self, *, cycle_id: str, clock: Callable[[], float] = time.monotonic,
                 budget: DeadlineBudget | None = None) -> None:
        self.cycle_id = str(cycle_id)
        self._clock = clock
        self.budget = budget
        self._spans: list[DecisionTraceSpan] = []
        self._outcomes: dict[str, CycleOutcome] = {}
        self._failed_requests = 0
        self._retried_requests = 0
        self._sequence = 0

    # -- span ---------------------------------------------------------

    @contextmanager
    def cycle(self, *, stage: str = "cycle", cycle_id: str | None = None,
              deadline: DeadlineBudget | None = None) -> Iterator[SpanHandle]:
        """整轮 span（``purpose="cycle"``）：K05 的 ``cycles_total`` 按它去重计数。

        与模型尝试 span 分开登记，"这一轮"与"这一轮发了几次请求"从源头就是两件事。
        """
        with self.span(stage, purpose="cycle", cycle_id=cycle_id, deadline=deadline,
                       request_id=f"{cycle_id or self.cycle_id}:cycle") as handle:
            yield handle

    @contextmanager
    def span(self, stage: str, *, purpose: str = "api_attempt",
             request_id: str = "", attempt: int = 1,
             cycle_id: str | None = None,
             deadline: DeadlineBudget | None = None) -> Iterator["SpanHandle"]:
        """记录一个阶段 span；期限剩余**由本模块在单调时钟上算**（K05 口径）。"""
        cycle = str(cycle_id or self.cycle_id)
        active = deadline if deadline is not None else self.budget
        started_ms = int(self._clock() * 1000)
        remaining = None if active is None else int(active.remaining_seconds() * 1000)
        handle = SpanHandle(
            cycle_id=cycle, stage=str(stage), purpose=str(purpose),
            request_id=str(request_id or f"{cycle}:{stage}:{attempt}"),
            attempt=int(attempt), started_ms=started_ms,
            deadline_remaining_ms=remaining,
        )
        try:
            yield handle
        finally:
            ended_ms = int(self._clock() * 1000)
            reason = handle.reason_code
            if reason == "" and handle.failed:
                reason = REASON_MODEL_DEADLINE
            self._append(DecisionTraceSpan(
                cycle_id=cycle, stage=handle.stage, request_id=handle.request_id,
                attempt=handle.attempt, purpose=handle.purpose,
                started_ms=handle.started_ms,
                ended_ms=handle.ended_ms if handle.ended_ms is not None else ended_ms,
                deadline_remaining_ms=handle.deadline_remaining_ms,
                reason_code=reason,
            ))
            if handle.purpose in ("api_attempt", "tool_call"):
                if handle.failed:
                    self._failed_requests += 1
                if handle.attempt > 1:
                    self._retried_requests += 1

    def _append(self, span: DecisionTraceSpan) -> None:
        violations = span_violations(span)
        if violations:
            raise SpanValidationError(violations)
        self._spans.append(span)
        self._sequence += 1

    def record_span(self, span: DecisionTraceSpan) -> None:
        """直接登记外部构造的 span（执行侧适配层回填用）；违规 span 一律拒绝。"""
        self._append(span)

    # -- 轮次结局 -----------------------------------------------------

    def finish_cycle(self, *, failed: bool, reason: DecisionReason | None = None,
                     cycle_id: str | None = None) -> CycleOutcome:
        """登记一轮的结局；``failed`` 只影响 ``failed_rounds``，不影响请求计数。"""
        cycle = str(cycle_id or self.cycle_id)
        counted = [item for item in self._spans
                   if item.cycle_id == cycle and item.purpose in ("api_attempt", "tool_call")]
        outcome = CycleOutcome(
            cycle_id=cycle, failed=bool(failed),
            reason=reason if reason is not None else (
                DecisionReason(primary=REASON_NO_EDGE) if not failed else None),
            attempts=len(counted),
            failed_attempts=sum(1 for item in counted if item.reason_code),
        )
        self._outcomes[cycle] = outcome
        return outcome

    # -- 视图 ---------------------------------------------------------

    @property
    def spans(self) -> tuple[DecisionTraceSpan, ...]:
        return tuple(self._spans)

    @property
    def outcomes(self) -> tuple[CycleOutcome, ...]:
        return tuple(self._outcomes.values())

    def counters(self) -> DecisionCounters:
        return tally(self._spans)

    def summary(self) -> TraceSummary:
        counters = self.counters()
        return TraceSummary(
            counters=counters,
            requests_total=counters.api_attempts_total,
            requests_failed=self._failed_requests,
            requests_retried=self._retried_requests,
            failed_rounds=sum(1 for item in self._outcomes.values() if item.failed),
            rounds_total=len(self._outcomes) or counters.cycles_total,
        )

    def latency(self) -> dict[str, StageLatency]:
        return stage_latency_stats(self._spans)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cycle_id": self.cycle_id,
            "summary": self.summary().as_dict(),
            "latency": {name: item.as_dict() for name, item in self.latency().items()},
            "spans": [
                {
                    "cycle_id": item.cycle_id, "stage": item.stage,
                    "request_id": item.request_id, "attempt": item.attempt,
                    "purpose": item.purpose, "duration_ms": item.duration_ms(),
                    "deadline_remaining_ms": item.deadline_remaining_ms,
                    "reason_code": item.reason_code,
                }
                for item in self._spans
            ],
            "outcomes": [item.as_dict() for item in self._outcomes.values()],
        }


@dataclass
class SpanHandle:
    """``with trace.span(...)`` 的句柄；``fail()`` 记录失败与原因码。"""

    cycle_id: str
    stage: str
    purpose: str
    request_id: str
    attempt: int
    started_ms: int
    deadline_remaining_ms: int | None = None
    ended_ms: int | None = None
    reason_code: str = ""
    failed: bool = False

    def finish(self, *, reason_code: str = "", failed: bool = False) -> None:
        self.reason_code = str(reason_code or "")
        self.failed = bool(failed)

    def fail(self, exc_or_reason: Any, *, reason_code: str = "") -> str:
        """把异常/显式原因码记成失败；返回实际使用的码（便于回填审计）。"""
        code = str(reason_code or "")
        if not code and isinstance(exc_or_reason, BaseException):
            code = classify_exception(exc_or_reason).primary
        if not code:
            code = REASON_MODEL_DEADLINE
        self.reason_code = code
        self.failed = True
        return code


# ------------------------------------------------------------------ 保护优先


@dataclass(frozen=True)
class StepOutcome:
    name: str
    ran: bool
    ok: bool
    reason: DecisionReason | None = None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ran": self.ran,
            "ok": self.ok,
            "reason": None if self.reason is None else self.reason.as_dict(),
            "error": self.error,
        }


@dataclass(frozen=True)
class CycleRunResult:
    """一轮执行的"谁真的跑了"事实；模型失败**不改变**保护/对账已执行的事实。"""

    model: StepOutcome
    protection: StepOutcome
    reconciliation: StepOutcome
    reason: DecisionReason
    blocked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.as_dict(),
            "protection": self.protection.as_dict(),
            "reconciliation": self.reconciliation.as_dict(),
            "reason": self.reason.as_dict(),
            "blocked": self.blocked,
        }


def run_protection_first(
    *,
    model_step: Callable[[], Any],
    protection_step: Callable[[], Any],
    reconciliation_step: Callable[[], Any],
    trace: DecisionTrace | None = None,
    model_purpose: str = "api_attempt",
    protection_purpose: str = "decision",
    reconciliation_purpose: str = "validation",
) -> CycleRunResult:
    """固定优先级执行：**模型失败不阻塞保护与对账**（任务卡必达要点 3）。

    执行顺序是"先保护/对账、后模型"，并且三步都**各自**隔离异常：
    任何一步抛错只把它自己标成 ``ok=False``，不会短路后面的步骤，也不会
    ``blocked=True`` 去阻止保护。模型的失败原因码进入 ``reason``。

    三个 purpose 缺省与 K05 计数口径对齐：模型步 = ``api_attempt``（进请求次数），
    保护步 = ``decision``（保护本身就是一个决策），对账步 = ``validation``
    （K05 显式不计数：对账是校验，不是请求也不是决策）。三者都进**分阶段耗时**。
    """
    def _run(name: str, step: Callable[[], Any], stage: str,
             purpose: str) -> tuple[StepOutcome, Any]:
        if trace is None:
            try:
                return StepOutcome(name=name, ran=True, ok=True), step()
            except BaseException as exc:  # noqa: BLE001 - 隔离是**契约行为**，不是兜底
                code = classify_exception(exc)
                return StepOutcome(name=name, ran=True, ok=False, reason=code,
                                   error=f"{type(exc).__name__}: {exc}"[:200]), None
        with trace.span(stage, purpose=purpose) as handle:
            try:
                value = step()
            except BaseException as exc:  # noqa: BLE001 - 同上
                code = classify_exception(exc)
                handle.fail(exc)
                return StepOutcome(name=name, ran=True, ok=False, reason=code,
                                   error=f"{type(exc).__name__}: {exc}"[:200]), None
            handle.finish()
            return StepOutcome(name=name, ran=True, ok=True), value

    protection, _ = _run("protection", protection_step, Stage.MANAGEMENT.value,
                         protection_purpose)
    reconciliation, _ = _run("reconciliation", reconciliation_step,
                             Stage.RECONCILIATION.value, reconciliation_purpose)
    model, _ = _run("model", model_step, Stage.MODEL_DECISION.value, model_purpose)
    codes: list[str] = []
    details: list[str] = []
    for step in (model, protection, reconciliation):
        if step.reason is not None:
            codes.extend(step.reason.codes)
            details.extend(step.reason.details)
    reason = DecisionReason.from_codes(codes or [REASON_NO_EDGE], details=tuple(details))
    return CycleRunResult(
        model=model, protection=protection, reconciliation=reconciliation,
        reason=reason, blocked=False,
    )


# ------------------------------------------------------------------ 路由风险


class RouteRisk(StrEnum):
    """§3.3 四类路由风险；每条都必须**显式**判定，不允许沉默通过。"""

    SILENT_MOCK = "SILENT_MOCK"
    EARLY_RETURN = "EARLY_RETURN"
    PARAMETER_INHERITANCE = "PARAMETER_INHERITANCE"
    CROSS_PROCESS_QUOTA = "CROSS_PROCESS_QUOTA"


@dataclass(frozen=True)
class RouteViolation:
    risk: RouteRisk
    detail: str
    request_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"risk": self.risk.value, "detail": self.detail,
                "request_id": self.request_id}


@dataclass(frozen=True)
class RouteExpectation:
    """调用方对本轮模型路由的**显式期望**；缺省保守（不许静默 Mock）。"""

    model: str
    effort: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    mock_allowed: bool = False
    require_cross_process: bool = False
    declared_fallbacks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.model or "").strip():
            raise ValueError("RouteExpectation.model 不能为空（否则无法判定参数继承）")


class ModelRouteGuard:
    """把"这一轮真的走了哪条路由"变成可判定事实。

    三类判定都需要调用方**先声明期望**再观察结果；没有声明的字段不猜：

    - ``SILENT_MOCK``：期望真实模型（``mock_allowed=False``）却拿到 mock 客户端/
      mock 模型/mock 响应体 → 违规。反向也成立：调用方**显式** ``mock_allowed=True``
      时才允许 mock。
    - ``EARLY_RETURN``：调用方 ``expect_attempt()`` 之后调用返回，但**没有任何**对该
      ``request_id`` 的尝试 span → "函数早返回了、请求从未发出"。这是静默降级最常见的
      形态（无 key/无工具/早 return 都长这样）。
    - ``PARAMETER_INHERITANCE``：响应 ``model`` 与期望不一致且**未声明**为备用路由 →
      备用链路静默继承了主路由参数（或主链路被换模型）。备用路由必须显式声明。
    - ``CROSS_PROCESS_QUOTA``：期望跨进程共享准入，但尝试没有带脱敏作用域/准入凭据 →
      该请求绕过了跨进程额度（多进程会各自打满同一凭据的额度）。
    """

    def __init__(self, expectation: RouteExpectation) -> None:
        self.expectation = expectation
        self._pending: dict[str, int] = {}
        self._attempts: set[str] = set()
        self._violations: list[RouteViolation] = []
        self._cross_process_seen: set[str] = set()

    # -- 期望登记 -----------------------------------------------------

    def expect_attempt(self, request_id: str) -> str:
        """在**发起调用之前**登记"这一轮应当发出一次请求"。"""
        key = str(request_id)
        self._pending[key] = self._pending.get(key, 0) + 1
        return key

    # -- 观察 ---------------------------------------------------------

    def observe_admission(self, *, request_id: str, provider_scope: str | None) -> None:
        """记录一次准入；``require_cross_process`` 时空作用域即违规。"""
        if self.expectation.require_cross_process and not str(provider_scope or "").strip():
            self._violations.append(RouteViolation(
                risk=RouteRisk.CROSS_PROCESS_QUOTA,
                detail="admission without desensitised provider scope",
                request_id=str(request_id),
            ))
            return
        self._cross_process_seen.add(str(request_id))

    def observe_attempt(self, *, request_id: str, client_name: str = "",
                        span: DecisionTraceSpan | None = None) -> None:
        """记录一次**真实发出**的尝试（有 span/有客户端名即算发出）。"""
        key = str(request_id)
        self._attempts.add(key)
        if span is not None:
            self.observe_admission(request_id=key, provider_scope=key)
        if not self.expectation.mock_allowed and str(client_name) == "mock":
            self._violations.append(RouteViolation(
                risk=RouteRisk.SILENT_MOCK,
                detail=f"mock client used without explicit allowance: {client_name}",
                request_id=key,
            ))

    def observe_response(self, *, request_id: str, model: str | None = None,
                         provider: str | None = None,
                         raw: Mapping[str, Any] | None = None) -> None:
        """观察一次响应：判静默 Mock 与参数继承。"""
        key = str(request_id)
        self._attempts.add(key)
        payload = dict(raw or {})
        provider_name = str(provider or payload.get("provider") or "").strip()
        model_name = str(model or "").strip()
        expected = str(self.expectation.model)
        mta = payload.get("_mta")
        mocked = (
            provider_name == "mock"
            or model_name == "mock"
            or bool(payload.get("mock"))
            or (isinstance(mta, Mapping) and bool(mta.get("mock")))
        )
        if mocked and not self.expectation.mock_allowed:
            self._violations.append(RouteViolation(
                risk=RouteRisk.SILENT_MOCK,
                detail=f"mock response without explicit allowance: provider={provider_name or 'NA'}",
                request_id=key,
            ))
            return
        if not mocked and model_name and model_name != expected:
            if model_name not in self.expectation.declared_fallbacks:
                self._violations.append(RouteViolation(
                    risk=RouteRisk.PARAMETER_INHERITANCE,
                    detail=f"response model {model_name} != expected {expected} "
                           f"and not declared as fallback",
                    request_id=key,
                ))

    # -- 结算 ---------------------------------------------------------

    def finish_call(self, *, request_id: str, returned: bool) -> tuple[RouteViolation, ...]:
        """调用**返回**后结算该 ``request_id``：返回了但没发出请求 = 早返回。"""
        key = str(request_id)
        pending = self._pending.pop(key, 0)
        if returned and pending and key not in self._attempts:
            self._violations.append(RouteViolation(
                risk=RouteRisk.EARLY_RETURN,
                detail="call returned without issuing any model attempt",
                request_id=key,
            ))
        return self.violations()

    def violations(self) -> tuple[RouteViolation, ...]:
        return tuple(self._violations)

    def violation_risks(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.risk.value for item in self._violations))

    def clean(self) -> bool:
        return not self._violations

    def assert_clean(self) -> None:
        if self._violations:
            raise RouteViolationError(self._violations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "expected_model": self.expectation.model,
            "mock_allowed": self.expectation.mock_allowed,
            "require_cross_process": self.expectation.require_cross_process,
            "attempts": sorted(self._attempts),
            "violations": [item.as_dict() for item in self._violations],
            "clean": self.clean(),
        }


class RouteViolationError(RuntimeError):
    """路由风险已成立；调用方必须降级并如实上报，不得静默继续。"""

    def __init__(self, violations: Sequence[RouteViolation]) -> None:
        self.violations = tuple(violations)
        super().__init__("route risk: "
                         + ", ".join(item.risk.value for item in self.violations))


# ------------------------------------------------------------------ 跨进程准入


def credential_alias(api_key: str | None) -> str:
    """凭据的**脱敏别名**：单向前缀哈希，不落原文、不可逆推。"""
    return hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:16]


def shared_admission_scope(base_url: str | None, api_key: str | None) -> str:
    """provider/脱敏凭据别名二元组的共享准入键（与 request_coordinator 同口径）。"""
    return f"{str(base_url or '').rstrip('/')}#key:{credential_alias(api_key)}"


def coordination_dir() -> Path:
    """跨进程协调目录；测试必须用 ``MTA_LLM_COORDINATION_DIR`` 指向沙箱。"""
    configured = os.getenv("MTA_LLM_COORDINATION_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data" / "shared" / "llm_coordination"


def lock_path_for(scope: str) -> Path:
    digest = hashlib.sha256(str(scope).encode("utf-8")).hexdigest()[:24]
    return coordination_dir() / f"provider-{digest}.lock"


def owner_alive(pid: int) -> bool:
    """属主进程是否仍存活；**只用于回收判定**，异常一律按"未知→存活"保守处理。

    Windows 用 ``OpenProcess(SYNCHRONIZE)`` + 零超时 ``WaitForSingleObject``：
    句柄可用且未 signaled 即仍在运行；POSIX 用 ``os.kill(pid, 0)``。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return True
    if pid <= 0:
        return True
    if os.name == "nt":  # pragma: no cover - 生产环境为 Windows，CI 分支保留
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            process = kernel32.OpenProcess(0x00100000, False, pid)
            if not process:
                return False
            try:
                return kernel32.WaitForSingleObject(process, 0) == 0x00000102
            finally:
                kernel32.CloseHandle(process)
        except Exception:  # noqa: BLE001 - 判定失败不得误回收
            return True
    try:
        os.kill(pid, 0)  # pragma: no cover - POSIX 分支
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


@dataclass(frozen=True)
class Lease:
    """跨进程准入租约（sidecar 记录，用于崩溃回收与可观测）。"""

    scope: str
    owner_token: str
    pid: int
    acquired_at_ms: int
    expires_at_ms: int

    def expired(self, now_ms: int) -> bool:
        return int(now_ms) >= int(self.expires_at_ms)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "owner_token": self.owner_token,
            "pid": self.pid,
            "acquired_at_ms": self.acquired_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }


class LeaseRegistry:
    """按作用域登记的跨进程租约；**崩溃即回收**，不靠"等锁超时"。

    回收条件（任一成立即回收）：租约已过 ``expires_at_ms``，或属主 pid 已不存在。
    过期上限保证"持有者活着但卡死"也有界；pid 判定保证"持有者已崩溃"时**立即**
    回收，新进程不必等到过期（旧口径下这段等待会被读成"额度被别人占着"）。
    """

    def __init__(self, *, directory: Path | str | None = None,
                 ttl_ms: int = 180_000, clock_ms: Callable[[], int] | None = None) -> None:
        self.directory = Path(directory) if directory is not None else coordination_dir()
        self.ttl_ms = max(1, int(ttl_ms))
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))

    def _path(self, scope: str) -> Path:
        digest = hashlib.sha256(str(scope).encode("utf-8")).hexdigest()[:24]
        return self.directory / f"lease-{digest}.json"

    def read(self, scope: str) -> Lease | None:
        path = self._path(scope)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        try:
            return Lease(
                scope=str(payload["scope"]), owner_token=str(payload["owner_token"]),
                pid=int(payload["pid"]), acquired_at_ms=int(payload["acquired_at_ms"]),
                expires_at_ms=int(payload["expires_at_ms"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def acquire(self, scope: str, *, owner_token: str | None = None,
                pid: int | None = None, now_ms: int | None = None) -> Lease:
        """登记租约；**先回收**该作用域上的死租约，再登记自己的。"""
        self.reap(scope=scope, now_ms=now_ms)
        now = int(now_ms if now_ms is not None else self._clock_ms())
        lease = Lease(
            scope=str(scope),
            owner_token=str(owner_token or hashlib.sha256(
                f"{os.getpid()}:{now}:{time.perf_counter_ns()}".encode()).hexdigest()[:16]),
            pid=int(pid if pid is not None else os.getpid()),
            acquired_at_ms=now, expires_at_ms=now + self.ttl_ms,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(scope).write_text(
            json.dumps(lease.as_dict(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return lease

    def release(self, scope: str, *, owner_token: str | None = None) -> bool:
        """释放；令牌不匹配时不删别人的租约（返回 False）。"""
        current = self.read(scope)
        if current is None:
            return False
        if owner_token is not None and current.owner_token != str(owner_token):
            return False
        try:
            self._path(scope).unlink()
        except OSError:
            return False
        return True

    def reap(self, *, scope: str | None = None,
             now_ms: int | None = None) -> tuple[Lease, ...]:
        """回收死租约；返回被回收的租约（可审计）。"""
        now = int(now_ms if now_ms is not None else self._clock_ms())
        if scope is not None:
            paths = [self._path(scope)]
        else:
            paths = sorted(self.directory.glob("lease-*.json")) \
                if self.directory.exists() else []
        reaped: list[Lease] = []
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                lease = Lease(
                    scope=str(payload["scope"]), owner_token=str(payload["owner_token"]),
                    pid=int(payload["pid"]), acquired_at_ms=int(payload["acquired_at_ms"]),
                    expires_at_ms=int(payload["expires_at_ms"]),
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if lease.expired(now) or not owner_alive(lease.pid):
                try:
                    path.unlink()
                except OSError:
                    continue
                reaped.append(lease)
        return tuple(reaped)

    def status(self, scope: str | None = None) -> dict[str, Any]:
        """只读视图（**不回收**：读操作不得有副作用，回收显式走 :meth:`reap`）。"""
        if scope is not None:
            lease = self.read(scope)
            alive = None if lease is None else owner_alive(lease.pid)
            return {"scope": scope,
                    "lease": None if lease is None else lease.as_dict(),
                    "owner_alive": alive}
        return {
            "directory": str(self.directory),
            "leases": [item.as_dict() for item in self._all()],
        }

    def _all(self) -> tuple[Lease, ...]:
        if not self.directory.exists():
            return ()
        out: list[Lease] = []
        for path in sorted(self.directory.glob("lease-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                out.append(Lease(
                    scope=str(payload["scope"]), owner_token=str(payload["owner_token"]),
                    pid=int(payload["pid"]), acquired_at_ms=int(payload["acquired_at_ms"]),
                    expires_at_ms=int(payload["expires_at_ms"]),
                ))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return tuple(out)


@contextmanager
def shared_quota_lease(scope: str, *, registry: LeaseRegistry | None = None,
                       owner_token: str | None = None) -> Iterator[Lease]:
    """共享准入租约的作用域管理器；异常/退出都释放（崩溃由 :meth:`LeaseRegistry.reap`）。"""
    active = registry or LeaseRegistry()
    lease = active.acquire(scope, owner_token=owner_token)
    try:
        yield lease
    finally:
        active.release(scope, owner_token=lease.owner_token)


# ------------------------------------------------------------------ 后台刷新预算


class SourceName(StrEnum):
    """S03 分源缓存口径（与 U03 ``SOURCE_NAMES`` 同名，便于直接对表）。"""

    TICKER = "ticker"
    MARK_FUNDING = "mark_funding"
    OPEN_INTEREST = "open_interest"


SOURCE_NAMES: tuple[str, ...] = tuple(item.value for item in SourceName)
#: 分源 TTL：ticker 变动最快，标记价/资金费与 OI 允许更长复用。
SOURCE_TTL_MS: dict[str, int] = {
    "ticker": 30_000,
    "mark_funding": 60_000,
    "open_interest": 60_000,
}


@dataclass(frozen=True)
class RefreshBudget:
    """HTTP 与摘要的**总预算**：两段之和不得超过 ``total_ms``。

    ``reserve_for_management_ms`` 是给管理/保护路径预留的部分：后台刷新在
    剩余预算不足以覆盖该预留时**立即让路**（不发起 HTTP、不调摘要），
    这样长刷新不会拖住快循环（R22/S03）。
    """

    total_ms: int
    http_ms: int
    summary_ms: int
    reserve_for_management_ms: int = 0
    clock: Callable[[], float] = time.monotonic
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        if self.total_ms <= 0 or self.http_ms < 0 or self.summary_ms < 0:
            raise ValueError("刷新预算必须为正")
        if self.http_ms + self.summary_ms > self.total_ms:
            raise ValueError("http_ms + summary_ms 不得超过 total_ms")

    def elapsed_ms(self) -> int:
        return int((self.clock() - self.started) * 1000)

    def remaining_ms(self) -> int:
        return max(0, int(self.total_ms) - self.elapsed_ms())

    def http_remaining_ms(self) -> int:
        return max(0, min(int(self.http_ms) - self.elapsed_ms(), self.remaining_ms()))

    def summary_remaining_ms(self) -> int:
        """摘要段可用预算：不超过 ``summary_ms``，且不超过总预算的剩余部分。"""
        return max(0, min(int(self.summary_ms), int(self.total_ms) - self.elapsed_ms()))

    def has_room_for_management(self) -> bool:
        return self.remaining_ms() > int(self.reserve_for_management_ms)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_ms": self.total_ms,
            "http_ms": self.http_ms,
            "summary_ms": self.summary_ms,
            "reserve_for_management_ms": self.reserve_for_management_ms,
            "elapsed_ms": self.elapsed_ms(),
            "remaining_ms": self.remaining_ms(),
        }


class SourceCache:
    """**分源** TTL 缓存；一个源过期不影响其它源继续命中。

    旧口径（``IntelCache`` 单条 TTL）把整包情报当一个条目：ticker 过期就要整包
    重抓，资金费/OI 的可用缓存被连带丢弃，HTTP 预算被无谓打穿。
    """

    def __init__(self, *, ttl_ms: Mapping[str, int] | None = None,
                 clock_ms: Callable[[], int] | None = None) -> None:
        self.ttl_ms = {name: int(value) for name, value in
                       (ttl_ms if ttl_ms is not None else SOURCE_TTL_MS).items()}
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._items: dict[str, dict[str, Any]] = {}

    def put(self, source: str, value: Any, *, observed_at_ms: int | None = None) -> None:
        now = int(observed_at_ms if observed_at_ms is not None
                  else self._clock_ms())
        self._items[str(source)] = {
            "observed_at_ms": now,
            "expires_at_ms": now + self.ttl_ms.get(str(source), 60_000),
            "value": value,
        }

    def get(self, source: str, *, now_ms: int | None = None) -> tuple[bool, Any]:
        """返回 ``(fresh, value)``；``fresh=False`` 时 value 仍是**可用的陈旧值**。"""
        item = self._items.get(str(source))
        if item is None:
            return False, None
        now = int(now_ms if now_ms is not None else self._clock_ms())
        return now < int(item["expires_at_ms"]), item["value"]

    def stale_sources(self, sources: Sequence[str], *,
                      now_ms: int | None = None) -> tuple[str, ...]:
        return tuple(name for name in sources if not self.get(name, now_ms=now_ms)[0])

    def age_ms(self, source: str, *, now_ms: int | None = None) -> int | None:
        item = self._items.get(str(source))
        if item is None:
            return None
        now = int(now_ms if now_ms is not None else self._clock_ms())
        return max(0, now - int(item["observed_at_ms"]))

    def status(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = int(now_ms if now_ms is not None else self._clock_ms())
        return {
            name: {"fresh": now < int(item["expires_at_ms"]),
                   "age_ms": max(0, now - int(item["observed_at_ms"])),
                   "ttl_ms": int(item["expires_at_ms"]) - int(item["observed_at_ms"])}
            for name, item in self._items.items()
        }


@dataclass(frozen=True)
class BackgroundRefreshOutcome:
    """一次后台刷新的结论；``deferred`` 表示**为管理预留让路**（不算失败）。"""

    deferred: bool
    refreshed: tuple[str, ...]
    cached: tuple[str, ...]
    failed: tuple[str, ...]
    elapsed_ms: int
    budget_ms: int
    summary_done: bool
    reason: DecisionReason | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "deferred": self.deferred,
            "refreshed": list(self.refreshed),
            "cached": list(self.cached),
            "failed": list(self.failed),
            "elapsed_ms": self.elapsed_ms,
            "budget_ms": self.budget_ms,
            "summary_done": self.summary_done,
            "reason": None if self.reason is None else self.reason.as_dict(),
        }


def background_refresh(
    *,
    sources: Sequence[str],
    fetch: Callable[[str], Any],
    cache: SourceCache | None = None,
    budget: RefreshBudget,
    summarize: Callable[[Mapping[str, Any]], Any] | None = None,
) -> BackgroundRefreshOutcome:
    """有界后台刷新：先让路、再分源缓存命中、再逐源抓取、最后摘要。

    顺序即语义：

    1. 剩余预算不足以覆盖 ``reserve_for_management_ms`` → **立即 deferred**，
       一次 ``fetch`` 都不发（后台刷新不阻塞管理路径）；
    2. 每源先查缓存（TTL 内直接命中，不占 HTTP 预算）；
    3. 逐源抓取，每一步前检查 ``http_remaining_ms()``；预算耗尽即停止并在
       ``reason`` 回带 ``DATA_UNAVAILABLE``（诚实标注"没取到"，不假装成功）；
    4. 摘要只在 ``summary_remaining_ms()`` 足够时执行，否则 ``summary_done=False``。

    **口径边界（如实声明）**：预算约束的是"**是否发起**下一次调用"，不是"中断
    已发起的调用"（本层无法打断正在进行的 HTTP/subprocess）。因此一轮刷新的
    ``elapsed_ms`` 可能略越 ``total_ms``（最多一次调用的耗时），``failed`` 与
    ``summary_done`` 才是"有没有按预算做事"的权威结论。要精确截断必须在
    ``fetch`` 内部接入本层期限（由共享适配补丁负责）。
    """
    active_cache = cache if cache is not None else SourceCache()
    requested = tuple(str(name) for name in sources)

    if not budget.has_room_for_management():
        return BackgroundRefreshOutcome(
            deferred=True, refreshed=(), cached=(), failed=(), elapsed_ms=budget.elapsed_ms(),
            budget_ms=budget.total_ms, summary_done=False,
            reason=DecisionReason(primary=REASON_DATA_UNAVAILABLE,
                                  details=("refresh deferred to protect management budget",)),
        )

    refreshed: list[str] = []
    cached: list[str] = []
    failed: list[str] = []
    values: dict[str, Any] = {}
    for name in requested:
        fresh, value = active_cache.get(name)
        if fresh:
            cached.append(name)
            values[name] = value
            continue
        if budget.http_remaining_ms() <= 0:
            failed.append(name)
            continue
        try:
            value = fetch(name)
        except BaseException:  # noqa: BLE001 - 单源失败不得中断整轮刷新
            failed.append(name)
            continue
        active_cache.put(name, value)
        refreshed.append(name)
        values[name] = value

    summary_done = False
    if summarize is not None and budget.summary_remaining_ms() > 0:
        try:
            summarize(values)
            summary_done = True
        except BaseException:  # noqa: BLE001 - 摘要失败不影响已取得的数据
            summary_done = False

    reason = None
    if failed:
        reason = DecisionReason(primary=REASON_DATA_UNAVAILABLE,
                                details=tuple(f"source_failed:{name}" for name in failed))
    return BackgroundRefreshOutcome(
        deferred=False, refreshed=tuple(refreshed), cached=tuple(cached),
        failed=tuple(failed), elapsed_ms=budget.elapsed_ms(),
        budget_ms=budget.total_ms, summary_done=summary_done, reason=reason,
    )


# ------------------------------------------------------------------ 证据关联


@dataclass(frozen=True)
class EvidenceLink:
    """span → intent/evidence 关联（K05 README §12 C#12 的下游落地）。

    U03 的 ``ResearchEvidenceReport.bindings()`` 提供 ``(intent_id, evidence_id,
    binding_id)``；本结构把它与 K05 span 的 ``request_id`` 对齐：模型尝试 span 的
    ``request_id`` 即本轮的 ``binding_id``，于是"这一单用的哪次模型尝试/哪份证据"
    可以双向检索，而**不需要**给冻结的 ``DecisionTraceSpan`` 加字段。
    """

    intent_id: str
    evidence_id: str
    binding_id: str
    request_id: str
    cycle_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "evidence_id": self.evidence_id,
            "binding_id": self.binding_id,
            "request_id": self.request_id,
            "cycle_id": self.cycle_id,
        }


def evidence_links(report: Any, *, cycle_id: str = "") -> tuple[EvidenceLink, ...]:
    """由 U03 报告派生关联表；只读 U03 已产出的绑定，不重算证据。"""
    cycle = str(cycle_id or getattr(report, "cycle_id", "") or "")
    links: list[EvidenceLink] = []
    for item in tuple(getattr(report, "bindings", lambda: ())() or ()):
        binding_id = str(item.get("binding_id") or "")
        links.append(EvidenceLink(
            intent_id=str(item.get("intent_id") or ""),
            evidence_id=str(item.get("evidence_id") or ""),
            binding_id=binding_id,
            request_id=binding_id or f"{cycle}:model_decision",
            cycle_id=str(item.get("cycle_id") or cycle),
        ))
    return tuple(links)


__all__ = [
    "BackgroundRefreshOutcome",
    "CycleOutcome",
    "CycleRunResult",
    "DeadlineBudget",
    "DeadlineExceeded",
    "DecisionReason",
    "DecisionTrace",
    "EvidenceLink",
    "Lease",
    "LeaseRegistry",
    "MIN_ATTEMPT_SECONDS",
    "ModelRouteGuard",
    "PERCENTILE_METHOD",
    "REASON_CHANNEL_CONFLICT",
    "REASON_CODES",
    "REASON_DATA_UNAVAILABLE",
    "REASON_HOLD_EXISTING",
    "REASON_MODEL_DEADLINE",
    "REASON_MODEL_RATE_LIMITED",
    "REASON_NO_EDGE",
    "REASON_ORDER",
    "REASON_READINESS_BLOCKED",
    "REASON_RESEARCH_INCOMPLETE",
    "REASON_VALIDATION_REJECTED",
    "RefreshBudget",
    "RouteExpectation",
    "RouteRisk",
    "RouteViolation",
    "RouteViolationError",
    "SCHEMA_VERSION",
    "SOURCE_NAMES",
    "SOURCE_TTL_MS",
    "STAGE_ORDER",
    "SpanHandle",
    "SourceCache",
    "SourceName",
    "Stage",
    "StageLatency",
    "StepOutcome",
    "TraceSummary",
    "VIOLATION_DEADLINE_MISMATCH",
    "VIOLATION_DEADLINE_OVERRUN",
    "UnknownReasonCode",
    "background_refresh",
    "choose_primary",
    "classify_cycle",
    "classify_evidence_report",
    "classify_exception",
    "coordination_dir",
    "credential_alias",
    "deadline_violations",
    "evidence_links",
    "lock_path_for",
    "normalise_reason_codes",
    "owner_alive",
    "percentile_ms",
    "reason_from_source_errors",
    "run_protection_first",
    "shared_admission_scope",
    "shared_quota_lease",
    "stage_latency_stats",
]
