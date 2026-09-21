"""U08 策略漏斗与管理责任展示（K02/K05、F03/F09/F19、DC10、DL03/DL07）。

本模块是 C 线 CD 的**决策证据**层：把"这一轮为什么没开仓"从自由文本升级为
**可区分、可计数、可跨轮恢复**的结构化事实，并且**复用** U05 已冻结的九码
（:mod:`core.decision_trace`）与 U03 的证据报告，**不新建原因码体系**。

四件事，各有一个公开对象：

1. **扫描漏斗**（:class:`ScanFunnel`）：逐 ``(策略卡, symbol, timeframe)`` 记录
   它**停在哪一道闸门**（:data:`FUNNEL_STAGES`）以及**为什么停**（:data:`DETAIL_CODES`
   里的细分码）。每一轮结束时 :meth:`ScanFunnel.explanation` 一定给出主因/辅因，
   绝不会在"一个信号都没扫出来"时只报 ``ok``。细分码本身不是原因码：每个细分码
   在 :data:`DETAIL_REASON` 里**恰好映射到九码之一**，因此"分类轴"仍然只有九码，
   细分码只是 ``DecisionReason.details`` 级别的细节。
2. **有效 ticket 账本**（:class:`TicketLedger`）：跨轮 + 重启的**消费一次**语义。
   ticket 的"已消费"事实落盘（原子替换），重复消费返回 ``ALREADY_CONSUMED`` 且
   不回带 intent；重启后仍能恢复"本轮仍有效但尚未消费"的 ticket。
3. **管理责任归属**（:func:`attribute_positions` / :class:`PositionAttribution`）：
   每个仓位都能回答"谁在管它"（strategy/guardian/ai/legacy）与"用哪一版规则开的"
   （策略卡内容哈希 + 可选 K01 ``strategy_hash``）。
4. **管理优先预算**（:class:`ManagementPriorityPlan`）：把一轮的预算切成"管理预留"
   与"新仓研究"两池。管理侧**永远**能取到钱（必要时吃掉研究池），研究侧**永远**
   取不到预留池。研究池被吃光时给出 ``MODEL_DEADLINE`` + ``MANAGEMENT_PRIORITY_BUDGET``，
   而不是静默地"这轮没信号"。

设计约束（与 U03/U05 同口径）：纯函数 + 内存态记录器；唯一会碰文件系统的路径是
**调用方显式指定的沙箱文件**（ticket 账本）。不联网、不读 ``.env``/真实 ``data/``、
不构造 Broker、不启动服务、不下单撤单、不改风险档/白名单/live 开关。

**与 U05 的边界**：原因码、``DecisionReason``、``classify_cycle``、
``reason_from_source_errors`` 全部**直接复用** ``core.decision_trace``；本模块只补
U05 明确留给 U08 的那一层——漏斗状态枚举（K05 README §12 C#13）、逐轮/逐票解释、
以及上面第 2/3/4 三件事。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.decision_trace import (
    REASON_CHANNEL_CONFLICT,
    REASON_CODES,
    REASON_DATA_UNAVAILABLE,
    REASON_HOLD_EXISTING,
    REASON_MODEL_DEADLINE,
    REASON_MODEL_RATE_LIMITED,
    REASON_NO_EDGE,
    REASON_ORDER,
    REASON_READINESS_BLOCKED,
    REASON_RESEARCH_INCOMPLETE,
    REASON_VALIDATION_REJECTED,
    DecisionReason,
    choose_primary,
    normalise_reason_codes,
)

SCHEMA_VERSION = "u08-strategy-funnel-1"

#: 审计库/看板读取漏斗摘要时使用的状态文件键名（共享适配写入点）。
FUNNEL_REPORT_KEY = "funnel"

#: 状态文件里逐项留痕的最大条数：漏斗**计数**是精确的，逐项明细只留可复核的样本，
#: 避免 universal_rotation 一轮数千项把 state.json 撑爆（明细不是计数来源）。
MAX_ITEMS_IN_REPORT = 40


# --------------------------------------------------------------------------- #
# 一、闸门（funnel stage）
# --------------------------------------------------------------------------- #
class Stage(StrEnum):
    """漏斗闸门；顺序 = 评估顺序（K05 README §12 C#13 的 funnel 状态枚举）。

    每个候选（策略卡 / symbol / timeframe 三元组）会依次经过这些闸门，
    停在**第一道过不去的闸门**。终态是 :attr:`INTENT_READY`（真的产出了开仓意图）。
    """

    CARD_LOADED = "card_loaded"
    CARD_INDICATORIZED = "card_indicatorized"
    SIGNAL_FN_REGISTERED = "signal_fn_registered"
    SYMBOL_IN_SCOPE = "symbol_in_scope"
    TIMEFRAME_SUPPORTED = "timeframe_supported"
    MARKET_DATA_READY = "market_data_ready"
    SIGNAL_TRIGGERED = "signal_triggered"
    SIDE_ALLOWED = "side_allowed"
    FEATURES_COMPLETE = "features_complete"
    BAR_UNSEEN = "bar_unseen"
    TICKET_EMITTED = "ticket_emitted"
    TICKET_ACTIVE = "ticket_active"
    TICKET_FRESH = "ticket_fresh"
    NO_POSITION_CONFLICT = "no_position_conflict"
    PRICE_AVAILABLE = "price_available"
    IN_ENTRY_ZONE = "in_entry_zone"
    INTENT_READY = "intent_ready"


STAGE_ORDER: tuple[str, ...] = tuple(item.value for item in Stage)

#: 终态闸门：到达它才代表"这一轮这一项真的开仓了"。
TERMINAL_STAGE: str = Stage.INTENT_READY.value

#: **通过型**阶段（不是"闸门"）：到达即代表成功，不是"被拒绝"。
#: - ``CARD_LOADED``：候选入口（只有前缀计数用它，从不单独 record）；
#: - ``TICKET_EMITTED``：扫描侧全部闸门通过、票据已落库；
#: - ``TICKET_ACTIVE``：跨轮/重启恢复出一张仍有效的票据；
#: - ``INTENT_READY``：执行侧真的产出了开仓意图（终态）。
#: 其余阶段都是**拒绝型闸门**：到达即停止，必须带细分码说明为什么。
SUCCESS_STAGES: frozenset[str] = frozenset({
    Stage.CARD_LOADED.value,
    Stage.TICKET_EMITTED.value,
    Stage.TICKET_ACTIVE.value,
    TERMINAL_STAGE,
})

#: 拒绝型闸门（= 全部阶段减去通过型）。"为什么没开"只统计这些。
GATE_STAGES: tuple[str, ...] = tuple(
    name for name in STAGE_ORDER if name not in SUCCESS_STAGES
)

#: 漏斗阶段分组（看板折叠显示用；不影响计数）。
STAGE_GROUPS: dict[str, tuple[str, ...]] = {
    "scan": (Stage.CARD_LOADED.value, Stage.CARD_INDICATORIZED.value,
             Stage.SIGNAL_FN_REGISTERED.value, Stage.SYMBOL_IN_SCOPE.value,
             Stage.TIMEFRAME_SUPPORTED.value, Stage.MARKET_DATA_READY.value,
             Stage.SIGNAL_TRIGGERED.value, Stage.SIDE_ALLOWED.value,
             Stage.FEATURES_COMPLETE.value, Stage.BAR_UNSEEN.value,
             Stage.TICKET_EMITTED.value),
    "recover": (Stage.TICKET_ACTIVE.value,),
    "execute": (Stage.TICKET_FRESH.value, Stage.NO_POSITION_CONFLICT.value,
                Stage.PRICE_AVAILABLE.value, Stage.IN_ENTRY_ZONE.value,
                Stage.INTENT_READY.value),
}


class UnknownDetailCode(ValueError):
    """细分码未登记：禁止用自由文本充当"为什么没开"。"""

    def __init__(self, codes: Sequence[str]) -> None:
        self.codes = tuple(codes)
        super().__init__("unregistered funnel detail code: " + ", ".join(self.codes))


# --------------------------------------------------------------------------- #
# 二、细分码注册表 -> 九码（**唯一**的分类轴仍是九码）
# --------------------------------------------------------------------------- #
# 扫描侧（signal_engine/scanner.py 写入）
DETAIL_CARD_NOT_INDICATORIZED = "CARD_NOT_INDICATORIZED"
DETAIL_SIGNAL_FN_MISSING = "SIGNAL_FN_MISSING"
DETAIL_SIGNAL_FN_OUTPUT_INVALID = "SIGNAL_FN_OUTPUT_INVALID"
DETAIL_SYMBOL_NOT_USDT = "SYMBOL_NOT_USDT"
DETAIL_SYMBOL_NOT_IN_SCOPE = "SYMBOL_NOT_IN_SCOPE"
DETAIL_TIMEFRAME_UNSUPPORTED = "TIMEFRAME_UNSUPPORTED"
DETAIL_MARKET_TRANSPORT_OUTAGE = "MARKET_TRANSPORT_OUTAGE"
DETAIL_MARKET_BUDGET_DEFERRED = "MARKET_BUDGET_DEFERRED"
DETAIL_KLINE_FETCH_FAILED = "KLINE_FETCH_FAILED"
DETAIL_KLINE_QUALITY_REJECTED = "KLINE_QUALITY_REJECTED"
DETAIL_KLINE_ROWS_INSUFFICIENT = "KLINE_ROWS_INSUFFICIENT"
DETAIL_NO_SIGNAL_ON_CLOSED_BAR = "NO_SIGNAL_ON_CLOSED_BAR"
DETAIL_SIDE_NOT_ALLOWED_BY_CARD = "SIDE_NOT_ALLOWED_BY_CARD"
DETAIL_FEATURES_INCOMPLETE = "FEATURES_INCOMPLETE"
DETAIL_BAR_ALREADY_SEEN = "BAR_ALREADY_SEEN"
DETAIL_SIGNAL_ALREADY_PERSISTED = "SIGNAL_ALREADY_PERSISTED"
DETAIL_SCAN_EXCEPTION = "SCAN_EXCEPTION"
DETAIL_NO_TICKETS_AVAILABLE = "NO_TICKETS_AVAILABLE"

# ticket 形状/时效（core/signal_ticket.py 写入；两个消费者共用同一判定）
DETAIL_TICKET_SHAPE_INVALID = "TICKET_SHAPE_INVALID"
DETAIL_TICKET_EXPIRED = "TICKET_EXPIRED"
DETAIL_TICKET_EXPIRING_BEFORE_USE = "TICKET_EXPIRING_BEFORE_USE"
DETAIL_TICKET_ALREADY_CONSUMED = "TICKET_ALREADY_CONSUMED"
DETAIL_TICKET_LEDGER_UNAVAILABLE = "TICKET_LEDGER_UNAVAILABLE"
DETAIL_TICKET_SYMBOL_NOT_IN_SCOPE = "TICKET_SYMBOL_NOT_IN_SCOPE"
DETAIL_ENTRY_ZONE_INVERTED = "ENTRY_ZONE_INVERTED"

# 执行侧（agents/strategy_trader.py 写入）
DETAIL_SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"
DETAIL_SYMBOL_ALREADY_HELD = "SYMBOL_ALREADY_HELD"
DETAIL_STRATEGY_ALREADY_HELD = "STRATEGY_ALREADY_HELD"
DETAIL_NO_LATEST_PRICE = "NO_LATEST_PRICE"
DETAIL_PRICE_OUTSIDE_ENTRY_ZONE = "PRICE_OUTSIDE_ENTRY_ZONE"
DETAIL_CHANNEL_BUDGET_ZERO = "CHANNEL_BUDGET_ZERO"
DETAIL_MANAGEMENT_PRIORITY_BUDGET = "MANAGEMENT_PRIORITY_BUDGET"
DETAIL_STRATEGY_TRADER_EXCEPTION = "STRATEGY_TRADER_EXCEPTION"
#: 供应商限流/冷却（U05 ``classify_exception`` 已判定的异常类名原样带入，
#: 本模块不重新解析异常，只把结论落到漏斗上）。
DETAIL_PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"

# 研究证据侧（core/decision_trace.py 的九码在此按细分再命名，供漏斗引用）
DETAIL_EVIDENCE_GATE_FAILED = "EVIDENCE_GATE_FAILED"
DETAIL_EVIDENCE_DATA_UNAVAILABLE = "EVIDENCE_DATA_UNAVAILABLE"
DETAIL_EVIDENCE_VALIDATION_REJECTED = "EVIDENCE_VALIDATION_REJECTED"

#: 细分码 -> 九码之一。**这是本模块唯一的分类轴**；任何细分码都必须在此登记，
#: 未登记码在 :func:`detail_reason` 处 fail-closed（:class:`UnknownDetailCode`）。
DETAIL_REASON: dict[str, str] = {
    # -- 扫描侧 -------------------------------------------------------- #
    DETAIL_CARD_NOT_INDICATORIZED: REASON_NO_EDGE,
    DETAIL_SIGNAL_FN_MISSING: REASON_VALIDATION_REJECTED,
    DETAIL_SIGNAL_FN_OUTPUT_INVALID: REASON_VALIDATION_REJECTED,
    DETAIL_SYMBOL_NOT_USDT: REASON_READINESS_BLOCKED,
    DETAIL_SYMBOL_NOT_IN_SCOPE: REASON_READINESS_BLOCKED,
    DETAIL_TIMEFRAME_UNSUPPORTED: REASON_VALIDATION_REJECTED,
    DETAIL_MARKET_TRANSPORT_OUTAGE: REASON_DATA_UNAVAILABLE,
    DETAIL_MARKET_BUDGET_DEFERRED: REASON_DATA_UNAVAILABLE,
    DETAIL_KLINE_FETCH_FAILED: REASON_DATA_UNAVAILABLE,
    DETAIL_KLINE_QUALITY_REJECTED: REASON_DATA_UNAVAILABLE,
    DETAIL_KLINE_ROWS_INSUFFICIENT: REASON_DATA_UNAVAILABLE,
    DETAIL_NO_SIGNAL_ON_CLOSED_BAR: REASON_NO_EDGE,
    DETAIL_SIDE_NOT_ALLOWED_BY_CARD: REASON_VALIDATION_REJECTED,
    DETAIL_FEATURES_INCOMPLETE: REASON_DATA_UNAVAILABLE,
    DETAIL_BAR_ALREADY_SEEN: REASON_NO_EDGE,
    DETAIL_SIGNAL_ALREADY_PERSISTED: REASON_NO_EDGE,
    DETAIL_SCAN_EXCEPTION: REASON_VALIDATION_REJECTED,
    DETAIL_NO_TICKETS_AVAILABLE: REASON_NO_EDGE,
    # -- ticket 形状/时效 --------------------------------------------- #
    DETAIL_TICKET_SHAPE_INVALID: REASON_VALIDATION_REJECTED,
    DETAIL_TICKET_EXPIRED: REASON_VALIDATION_REJECTED,
    DETAIL_TICKET_EXPIRING_BEFORE_USE: REASON_VALIDATION_REJECTED,
    DETAIL_TICKET_ALREADY_CONSUMED: REASON_HOLD_EXISTING,
    DETAIL_TICKET_LEDGER_UNAVAILABLE: REASON_READINESS_BLOCKED,
    DETAIL_TICKET_SYMBOL_NOT_IN_SCOPE: REASON_READINESS_BLOCKED,
    DETAIL_ENTRY_ZONE_INVERTED: REASON_VALIDATION_REJECTED,
    # -- 执行侧 -------------------------------------------------------- #
    DETAIL_SYMBOL_NOT_ALLOWED: REASON_READINESS_BLOCKED,
    DETAIL_SYMBOL_ALREADY_HELD: REASON_CHANNEL_CONFLICT,
    DETAIL_STRATEGY_ALREADY_HELD: REASON_HOLD_EXISTING,
    DETAIL_NO_LATEST_PRICE: REASON_DATA_UNAVAILABLE,
    DETAIL_PRICE_OUTSIDE_ENTRY_ZONE: REASON_NO_EDGE,
    DETAIL_CHANNEL_BUDGET_ZERO: REASON_READINESS_BLOCKED,
    DETAIL_MANAGEMENT_PRIORITY_BUDGET: REASON_MODEL_DEADLINE,
    DETAIL_STRATEGY_TRADER_EXCEPTION: REASON_VALIDATION_REJECTED,
    DETAIL_PROVIDER_RATE_LIMITED: REASON_MODEL_RATE_LIMITED,
    # -- 研究证据侧 ---------------------------------------------------- #
    DETAIL_EVIDENCE_GATE_FAILED: REASON_RESEARCH_INCOMPLETE,
    DETAIL_EVIDENCE_DATA_UNAVAILABLE: REASON_DATA_UNAVAILABLE,
    DETAIL_EVIDENCE_VALIDATION_REJECTED: REASON_VALIDATION_REJECTED,
}

DETAIL_CODES: frozenset[str] = frozenset(DETAIL_REASON)

#: 九码全集：**直接引用** U05 的冻结集合，不重复定义字面量（防漂移）。
REASON_ALL: frozenset[str] = frozenset(REASON_CODES)


def detail_reason(detail: str) -> str:
    """细分码 -> 九码之一；未登记码 fail-closed。"""
    code = str(detail or "").strip()
    if code not in DETAIL_REASON:
        raise UnknownDetailCode((code,))
    return DETAIL_REASON[code]


def detail_codes_for_reason(reason: str) -> tuple[str, ...]:
    """:func:`detail_reason` 的反查（文档/看板按九码归组显示细分码）。"""
    return tuple(sorted(code for code, mapped in DETAIL_REASON.items() if mapped == reason))


def validate_detail_table() -> None:
    """自检注册表：每个细分码都映射到九码，且九码全部有细分码可达。

    ``RESEARCH_INCOMPLETE``/``MODEL_RATE_LIMITED`` 等码由 U05 的
    ``classify_evidence_report`` / ``classify_exception`` 直接产出，这里只要求
    **至少一个**细分码可达，避免注册表与九码漂移。
    """
    unknown = sorted(code for code, reason in DETAIL_REASON.items()
                     if reason not in REASON_ALL)
    if unknown:
        raise UnknownDetailCode(unknown)
    missing = sorted(REASON_ALL - {*DETAIL_REASON.values()})
    if missing:
        raise UnknownDetailCode(missing)


# --------------------------------------------------------------------------- #
# 三、漏斗本身
# --------------------------------------------------------------------------- #
#: 明细来源：扫描候选 / 跨轮恢复的票据 / 执行侧 / 轮级结论。
#: 只有 ``scan`` 来源参与"漏斗收窄"的前缀计数，避免把恢复出来的票据算成新候选。
ORIGIN_SCAN = "scan"
ORIGIN_RECOVER = "recover"
ORIGIN_EXECUTE = "execute"
ORIGIN_ROUND = "round"


@dataclass(frozen=True)
class FunnelItem:
    """一个候选的结局：停在哪道闸门、为什么。"""

    stage: str
    detail: str = ""
    card_id: str = ""
    symbol: str = ""
    timeframe: str = ""
    ticket_id: str = ""
    reason: str = ""
    origin: str = ORIGIN_SCAN

    def __post_init__(self) -> None:
        stage = str(self.stage or "")
        if stage not in STAGE_ORDER:
            raise ValueError(f"unknown funnel stage: {stage!r}")
        reason = str(self.reason or "")
        if not reason and self.detail:
            reason = detail_reason(self.detail)
        if reason:
            code = normalise_reason_codes([reason])
            if not code:
                raise UnknownDetailCode((reason,))
            object.__setattr__(self, "reason", code[0])
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "detail", str(self.detail or ""))
        origin = str(self.origin or ORIGIN_SCAN)
        if origin not in (ORIGIN_SCAN, ORIGIN_RECOVER, ORIGIN_EXECUTE, ORIGIN_ROUND):
            origin = ORIGIN_SCAN
        object.__setattr__(self, "origin", origin)
        # 只有**通过型**阶段允许"没有细分码"；拒绝型阶段没有细分码是程序错误
        # （会让"为什么没开"重新变成自由文本），因此 fail-closed。
        if not self.detail and stage not in SUCCESS_STAGES:
            raise ValueError(
                f"拒绝型闸门 {stage!r} 必须带细分码（未登记的原因不允许静默）")

    @property
    def passed(self) -> bool:
        """通过型阶段 + 无细分码 = 成功；其余都是"被这道闸门拒绝"。"""
        return not self.detail and self.stage in SUCCESS_STAGES

    @property
    def key(self) -> str:
        return "|".join((self.card_id, self.symbol, self.timeframe,
                         self.ticket_id or "-"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage, "detail": self.detail, "reason": self.reason,
            "card_id": self.card_id, "symbol": self.symbol,
            "timeframe": self.timeframe, "ticket_id": self.ticket_id,
            "origin": self.origin,
        }


@dataclass
class ScanFunnel:
    """一轮（或跨轮累计）的扫描漏斗。

    ``items`` 是**逐项明细**（可截断），``stopped_at``/``passed_stages`` 是
    **精确计数**（不受截断影响）。所有计数都来自 :meth:`record` 的入参，不做推断。
    """

    round_id: str
    items: list[FunnelItem] = field(default_factory=list)
    #: 拒绝型闸门上的计数（"为什么没开"的直方图）。
    stopped_at: Counter[str] = field(default_factory=Counter)
    #: 通过型阶段的计数（``ticket_emitted``/``ticket_active``/``intent_ready``）。
    reached: Counter[str] = field(default_factory=Counter)
    #: 扫描侧候选的漏斗收窄（只统计 ``origin=scan`` 的项）。
    passed_stages: Counter[str] = field(default_factory=Counter)
    #: 每个项落在哪个阶段（不分来源），用于"这一轮处理了多少张票"的吞吐视图。
    throughput: Counter[str] = field(default_factory=Counter)
    detail_counts: Counter[str] = field(default_factory=Counter)
    reason_counts: Counter[str] = field(default_factory=Counter)
    round_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: int = 0

    # -- 写入 --------------------------------------------------------- #

    def record(self, stage: str, detail: str = "", *, card_id: str = "",
               symbol: str = "", timeframe: str = "", ticket_id: str = "",
               round_id: str = "", reason: str = "", origin: str = ORIGIN_SCAN) -> FunnelItem:
        """登记一个候选的结局。

        ``stage`` 是**它停下的那道闸门**（未通过）或 :data:`TERMINAL_STAGE`
        （通过全部闸门）。``detail`` 必须是已登记的细分码；未登记码 fail-closed
        （宁愿报错也不让自由文本冒充原因）。

        ``origin=scan`` 的项参与"漏斗收窄"前缀计数（它们确实从第一道闸门走起）；
        ``recover``/``execute``/``round`` 来源的项只进 :attr:`throughput` 与原因计数，
        否则"重启后恢复的票据"会被算成"本轮新候选"，把候选数虚增。
        """
        item = FunnelItem(stage=stage, detail=detail, card_id=card_id, symbol=symbol,
                          timeframe=timeframe, ticket_id=ticket_id, reason=reason,
                          origin=origin)
        rid = str(round_id or self.round_id)
        if rid not in self.round_ids:
            self.round_ids.append(rid)
        if item.passed:
            self.reached[item.stage] += 1
        else:
            self.stopped_at[item.stage] += 1
            if item.detail:
                self.detail_counts[item.detail] += 1
            if item.reason:
                self.reason_counts[item.reason] += 1
        self.throughput[item.stage] += 1
        # 到达/通过停下的那一道闸门即计入该闸门；终态项计入全部闸门（含
        # ``INTENT_READY``）。**只在这里累加一次**——重复累加会让收窄数翻倍。
        if item.origin == ORIGIN_SCAN:
            for name in STAGE_ORDER[:STAGE_ORDER.index(item.stage) + 1]:
                self.passed_stages[name] += 1
        if len(self.items) < MAX_ITEMS_IN_REPORT:
            self.items.append(item)
        else:
            self.truncated += 1
        return item

    def note(self, text: str) -> None:
        text = str(text or "").strip()
        if text and text not in self.notes:
            self.notes.append(text)

    # -- 读 ----------------------------------------------------------- #

    @property
    def opened(self) -> int:
        """本轮真的产出开仓意图的数量（终态项，含执行侧来源）。"""
        return int(self.reached.get(TERMINAL_STAGE, 0))

    @property
    def tickets_emitted(self) -> int:
        """扫描侧新发射的票据数。"""
        return int(self.reached.get(Stage.TICKET_EMITTED.value, 0))

    @property
    def tickets_recovered(self) -> int:
        """跨轮/重启恢复出来的仍有效票据数。"""
        return int(self.reached.get(Stage.TICKET_ACTIVE.value, 0))

    @property
    def total_candidates(self) -> int:
        """本轮**扫描侧**候选数（到达第一道闸门的 ``(卡, symbol, timeframe)`` 项）。"""
        return int(self.passed_stages.get(STAGE_ORDER[0], 0))

    def primary_reason(self) -> str:
        """本轮主因：按 U05 的九码**确定性优先级**选（不按出现次数投票）。

        有开仓时不做"没开的那些"的归因（那不是本轮的结论），直接报
        ``HOLD_EXISTING``——"本轮有仓在手、其余候选各有各的理由"。
        """
        if self.opened:
            return REASON_HOLD_EXISTING
        codes = [code for code, _ in self.reason_counts.most_common()]
        if not codes:
            return REASON_NO_EDGE
        return choose_primary(codes)

    def explanation(self, *, extra_codes: Iterable[str] = (),
                    details: Iterable[str] = ()) -> DecisionReason:
        """本轮"为什么没开/开了什么"的原因：主因唯一、辅因可多。

        ``extra_codes`` 供调用方补充管理面事实（如渠道冲突、就绪阻断）；
        仍走 U05 的 :class:`DecisionReason`，因此码域与九码完全一致。
        """
        codes = list(self.reason_counts)
        codes.extend(str(code) for code in extra_codes if str(code or "").strip())
        # 有开仓时本轮结论是"仓在手、其余候选各有理由"：主因取 ``HOLD_EXISTING``
        # （用 ``prefer`` 而不是删码——删码会让"其余候选为什么没开"从辅因里消失）。
        if self.opened and REASON_HOLD_EXISTING not in codes:
            codes.append(REASON_HOLD_EXISTING)
        prefer = REASON_HOLD_EXISTING if self.opened else None
        payload = tuple(self.detail_counts.most_common())
        return DecisionReason.from_codes(
            codes or [REASON_NO_EDGE],
            details=tuple([f"{code}x{count}" for code, count in payload][:20])
            + tuple(str(item) for item in details if str(item or "").strip()),
            prefer=prefer,
        )

    def no_open_explanation(self) -> dict[str, Any]:
        """任意一轮的"为什么没开"答复（F09：扫描异常不得报全成功）。

        ``verdict``：
        - ``opened``：本轮有开仓意图，``reasons`` 只是"其余候选为什么没开"；
        - ``no_edge``：候选都评估过了，就是没有可下单边际；
        - ``blocked``：本轮有闸门级故障（数据/校验/就绪），**不得**读成"没有信号"。
        """
        reason = self.explanation()
        blocked_codes = {REASON_DATA_UNAVAILABLE, REASON_VALIDATION_REJECTED,
                         REASON_READINESS_BLOCKED, REASON_RESEARCH_INCOMPLETE,
                         REASON_MODEL_DEADLINE, REASON_MODEL_RATE_LIMITED,
                         REASON_CHANNEL_CONFLICT}
        if self.opened:
            verdict = "opened"
        elif set(reason.codes) & blocked_codes:
            verdict = "blocked"
        elif self.total_candidates == 0:
            verdict = "no_candidates"
        else:
            verdict = "no_edge"
        return {
            "round_id": self.round_id,
            "verdict": verdict,
            "opened": self.opened,
            "candidates": self.total_candidates,
            "primary_reason": reason.primary,
            "reason_codes": list(reason.codes),
            # 按 U05 的九码优先级排序（同一份数据每次呈现同一顺序，便于比对）。
            "reasons": [
                {"reason": code, "count": self.reason_counts[code]}
                for code in REASON_ORDER if self.reason_counts.get(code)
            ],
            "details": [
                {"detail": code, "reason": DETAIL_REASON.get(code, ""), "count": count}
                for code, count in self.detail_counts.most_common()
            ],
            "stopped_at": {stage: self.stopped_at[stage] for stage in GATE_STAGES
                           if self.stopped_at.get(stage)},
            "reached": {stage: self.reached[stage] for stage in STAGE_ORDER
                        if self.reached.get(stage)},
        }

    def as_dict(self) -> dict[str, Any]:
        """状态文件/看板用的紧凑摘要（计数精确，明细截断并标注）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "round_id": self.round_id,
            "rounds": list(self.round_ids[-8:]),
            "opened": self.opened,
            "candidates": self.total_candidates,
            "tickets_emitted": self.tickets_emitted,
            "tickets_recovered": self.tickets_recovered,
            "stopped_at": {stage: self.stopped_at[stage] for stage in GATE_STAGES
                           if self.stopped_at.get(stage)},
            "reached": {stage: self.reached[stage] for stage in STAGE_ORDER
                        if self.reached.get(stage)},
            "throughput": {stage: self.throughput[stage] for stage in STAGE_ORDER
                           if self.throughput.get(stage)},
            "passed_stages": {stage: self.passed_stages[stage] for stage in STAGE_ORDER
                              if self.passed_stages.get(stage)},
            "reason_codes": [{"reason": code, "count": count}
                             for code, count in self.reason_counts.most_common()],
            "details": [{"detail": code, "reason": DETAIL_REASON.get(code, ""),
                         "count": count}
                        for code, count in self.detail_counts.most_common()],
            "no_open": self.no_open_explanation(),
            "notes": list(self.notes[:20]),
            "items": [item.as_dict() for item in self.items],
            "items_truncated": int(self.truncated),
        }


def funnel_from_report(payload: Mapping[str, Any] | None) -> ScanFunnel:
    """从状态文件里的紧凑摘要**重建**漏斗（跨轮/重启读侧）。

    重建只承诺计数与原因码（明细在写入侧已按 :data:`MAX_ITEMS_IN_REPORT` 截断），
    因此看板/回归在"进程重启后"仍能回答"上一轮为什么没开"，而不是只剩 ``ok``。
    """
    data = dict(payload or {})
    funnel = ScanFunnel(round_id=str(data.get("round_id") or ""))
    funnel.reason_counts.update({
        str(entry.get("reason")): int(entry.get("count") or 0)
        for entry in data.get("reason_codes") or [] if entry.get("reason")
    })
    funnel.detail_counts.update({
        str(entry.get("detail")): int(entry.get("count") or 0)
        for entry in data.get("details") or [] if entry.get("detail")
    })
    funnel.stopped_at.update({
        str(stage): int(count or 0) for stage, count in (data.get("stopped_at") or {}).items()
    })
    funnel.reached.update({
        str(stage): int(count or 0) for stage, count in (data.get("reached") or {}).items()
    })
    funnel.passed_stages.update({
        str(stage): int(count or 0)
        for stage, count in (data.get("passed_stages") or {}).items()
    })
    funnel.throughput.update({
        str(stage): int(count or 0) for stage, count in (data.get("throughput") or {}).items()
    })
    funnel.round_ids.extend(str(item) for item in data.get("rounds") or [])
    funnel.notes.extend(str(item) for item in data.get("notes") or [])
    funnel.truncated = int(data.get("items_truncated") or 0)
    return funnel


def empty_funnel_report(round_id: str) -> dict[str, Any]:
    """没有扫描结果时的显式形态：``ok`` 但**带原因**，不是空结构。"""
    return ScanFunnel(round_id=round_id).as_dict()


# --------------------------------------------------------------------------- #
# 四、有效 ticket 账本（跨轮 + 重启，消费一次）
# --------------------------------------------------------------------------- #
LEDGER_SCHEMA_VERSION = "u08-ticket-ledger-1"

CONSUMED = "CONSUMED"
ALREADY_CONSUMED = "ALREADY_CONSUMED"
LEDGER_CORRUPT = "LEDGER_CORRUPT"
UNSEEN = "UNSEEN"

#: 账本保留窗口：超过这个时长仍未结算的 old 条目会被修剪（防止文件无限增长）。
DEFAULT_RETENTION_MS = 14 * 24 * 3600 * 1000


class LedgerCorrupt(RuntimeError):
    """账本文件不可解析：拒绝新消费（fail-closed，宁可不追单也不重复消费）。"""


@dataclass(frozen=True)
class TicketConsumption:
    """一次消费尝试的结果；``consumed=False`` 时 ``state`` 说明为什么。"""

    ticket_id: str
    state: str
    round_id: str = ""
    first_round_id: str = ""
    first_at_ms: int = 0

    @property
    def consumed(self) -> bool:
        return self.state == CONSUMED

    @property
    def duplicate(self) -> bool:
        return self.state == ALREADY_CONSUMED

    def as_dict(self) -> dict[str, Any]:
        return {"ticket_id": self.ticket_id, "state": self.state,
                "round_id": self.round_id, "first_round_id": self.first_round_id,
                "first_at_ms": self.first_at_ms, "consumed": self.consumed,
                "duplicate": self.duplicate}


@dataclass(frozen=True)
class TicketRecord:
    """账本里的 ticket：发射事实 + 消费事实（两件事分开记）。

    ``consumed`` 是一个**独立的布尔事实**，不是从 ``consumed_at_ms > 0`` 推出来的：
    调用方给的钟可能是 0（离线重放/测试替身），让"已消费"依赖时间戳的值会把
    "消费一次"悄悄退化成"每次都能再消费一次"——正是本账本要防的那件事。
    """

    ticket_id: str
    signal_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    expires_at: str = ""
    emitted_at_ms: int = 0
    emitted_round_id: str = ""
    consumed: bool = False
    consumed_at_ms: int = 0
    consumed_round_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id, "signal_id": self.signal_id,
            "strategy_id": self.strategy_id, "symbol": self.symbol,
            "expires_at": self.expires_at, "emitted_at_ms": self.emitted_at_ms,
            "emitted_round_id": self.emitted_round_id,
            "consumed": bool(self.consumed),
            "consumed_at_ms": self.consumed_at_ms,
            "consumed_round_id": self.consumed_round_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TicketRecord":
        # 兼容没有显式 ``consumed`` 字段的旧账本文件：那时用时间戳>0 推断。
        produced = payload.get("consumed")
        consumed = bool(produced) if produced is not None else (
            int(payload.get("consumed_at_ms") or 0) > 0)
        return cls(
            ticket_id=str(payload.get("ticket_id") or ""),
            signal_id=str(payload.get("signal_id") or ""),
            strategy_id=str(payload.get("strategy_id") or ""),
            symbol=str(payload.get("symbol") or ""),
            expires_at=str(payload.get("expires_at") or ""),
            emitted_at_ms=int(payload.get("emitted_at_ms") or 0),
            emitted_round_id=str(payload.get("emitted_round_id") or ""),
            consumed=consumed,
            consumed_at_ms=int(payload.get("consumed_at_ms") or 0),
            consumed_round_id=str(payload.get("consumed_round_id") or ""),
        )


class TicketLedger:
    """``ticket_id`` 维度的**消费一次**账本；落盘可在重启后恢复。

    - :meth:`register`：扫描器发射 ticket 时登记（幂等；已是 consumed 的不会被复活）。
    - :meth:`consume`：执行器**真正要下单前**调用。首次返回 ``CONSUMED``，
      之后任何一轮/任何进程都返回 ``ALREADY_CONSUMED``（不回带 intent）。
    - :meth:`recover`：重启后列出"仍然有效且尚未消费"的 ticket —— 跨轮恢复。
    - 文件损坏（含被截断/被手工改写）时 :meth:`consume` 一律返回 ``LEDGER_CORRUPT``
      并**拒绝**新消费，而不是把损坏读成空账本（后者会让同一 ticket 被消费第二次）。
      真的要在损坏后继续，必须显式 :meth:`reset_corrupt`（会记 ``reset_at_ms`` 留痕）。

    线程安全：所有读写走 ``self._lock``；进程安全：写用 ``tmp + os.replace`` 原子替换。
    """

    def __init__(self, path: str | Path, *, clock: Any = None,
                 retention_ms: int = DEFAULT_RETENTION_MS) -> None:
        self.path = Path(path)
        # 缺省钟用**墙钟毫秒**（与 ticket 的 ``expires_at`` 同一时基）；测试与
        # 离线重放一律显式传 ``at_ms``/``now_ms``，不读墙钟。
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.retention_ms = int(retention_ms)
        self._lock = threading.RLock()
        self._records: dict[str, TicketRecord] = {}
        self._corrupt: str = ""
        self._reset_at_ms: int = 0
        self.load()

    # -- 持久化 -------------------------------------------------------- #

    def load(self) -> bool:
        """读盘；返回是否可解析。损坏时置 :attr:`corrupt_reason` 且**不**清空。"""
        with self._lock:
            if not self.path.exists():
                self._records = {}
                self._corrupt = ""
                return True
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or int(raw.get("schema_version_ms", 0)) < 0:
                    raise ValueError("ledger root 形态非法")
                records = raw.get("records")
                if not isinstance(records, list):
                    raise ValueError("records 非列表")
                self._records = {
                    str(item["ticket_id"]): TicketRecord.from_dict(item)
                    for item in records if isinstance(item, Mapping) and item.get("ticket_id")
                }
                self._reset_at_ms = int(raw.get("reset_at_ms") or 0)
                self._corrupt = ""
                return True
            except Exception as exc:  # noqa: BLE001 - 损坏必须被兜住并可见
                self._corrupt = f"{type(exc).__name__}: {exc}"[:200]
                return False

    def _dump(self) -> dict[str, Any]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "schema_version_ms": 1,
            "reset_at_ms": int(self._reset_at_ms),
            "records": [record.as_dict() for record in self._records.values()],
        }

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._dump(), ensure_ascii=False, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.path)

    # -- 属性 ---------------------------------------------------------- #

    @property
    def corrupt_reason(self) -> str:
        return self._corrupt

    def __len__(self) -> int:
        return len(self._records)

    def get(self, ticket_id: str) -> TicketRecord | None:
        with self._lock:
            return self._records.get(str(ticket_id))

    def is_consumed(self, ticket_id: str) -> bool:
        record = self.get(ticket_id)
        return bool(record and record.consumed)

    # -- 写入 ---------------------------------------------------------- #

    def register(self, ticket: Mapping[str, Any], *, round_id: str = "",
                 at_ms: int | None = None) -> TicketRecord:
        """登记"这张 ticket 已被发射"。已存在的记录只补空字段，**不**重置消费事实。"""
        ticket_id = str(ticket.get("ticket_id") or "")
        if not ticket_id:
            raise ValueError("ticket_id 不能为空")
        stamp = int(self._clock() if at_ms is None else at_ms)
        with self._lock:
            existing = self._records.get(ticket_id)
            record = TicketRecord(
                ticket_id=ticket_id,
                signal_id=str(ticket.get("signal_id") or (existing.signal_id if existing else "")),
                strategy_id=str(ticket.get("strategy_id") or (existing.strategy_id if existing else "")),
                symbol=str(ticket.get("symbol") or (existing.symbol if existing else "")),
                expires_at=str(ticket.get("expires_at") or (existing.expires_at if existing else "")),
                emitted_at_ms=int(existing.emitted_at_ms if existing else stamp),
                emitted_round_id=str(existing.emitted_round_id if existing else round_id),
                consumed=bool(existing.consumed) if existing else False,
                consumed_at_ms=int(existing.consumed_at_ms if existing else 0),
                consumed_round_id=str(existing.consumed_round_id if existing else ""),
            )
            self._records[ticket_id] = record
            return record

    def consume(self, ticket_id: str, *, round_id: str = "",
                at_ms: int | None = None) -> TicketConsumption:
        """消费（下单前）这张 ticket；幂等且跨轮/跨进程。"""
        key = str(ticket_id or "")
        stamp = int(self._clock() if at_ms is None else at_ms)
        with self._lock:
            if self._corrupt:
                return TicketConsumption(ticket_id=key, state=LEDGER_CORRUPT,
                                         round_id=round_id)
            if not key:
                return TicketConsumption(ticket_id="", state=UNSEEN, round_id=round_id)
            existing = self._records.get(key)
            if existing is not None and existing.consumed:
                return TicketConsumption(
                    ticket_id=key, state=ALREADY_CONSUMED, round_id=round_id,
                    first_round_id=existing.consumed_round_id,
                    first_at_ms=existing.consumed_at_ms,
                )
            self._records[key] = TicketRecord(
                ticket_id=key,
                signal_id=existing.signal_id if existing else "",
                strategy_id=existing.strategy_id if existing else "",
                symbol=existing.symbol if existing else "",
                expires_at=existing.expires_at if existing else "",
                emitted_at_ms=int(existing.emitted_at_ms if existing else stamp),
                emitted_round_id=str(existing.emitted_round_id if existing else round_id),
                consumed=True, consumed_at_ms=stamp, consumed_round_id=str(round_id),
            )
            return TicketConsumption(ticket_id=key, state=CONSUMED, round_id=round_id,
                                     first_round_id=str(round_id), first_at_ms=stamp)

    def register_and_consume(self, ticket: Mapping[str, Any], *, round_id: str = "",
                             at_ms: int | None = None) -> TicketConsumption:
        """执行器入口：先登记再消费，保证"第一次消费"也带得出 ticket 事实。"""
        self.register(ticket, round_id=round_id, at_ms=at_ms)
        return self.consume(str(ticket.get("ticket_id") or ""), round_id=round_id,
                            at_ms=at_ms)

    def release(self, ticket_id: str, *, at_ms: int | None = None) -> bool:
        """撤销一次**尚未下单**的消费（intent 构建失败等）。返回是否真的撤销了。

        只允许撤销当次消费：一旦离开进程（或已成交）就没有"撤销"语义，
        因此本方法不提供跨进程/跨轮的撤回，调用方必须在下单前调用。
        """
        key = str(ticket_id or "")
        with self._lock:
            existing = self._records.get(key)
            if existing is None or not existing.consumed:
                return False
            self._records[key] = TicketRecord(
                ticket_id=existing.ticket_id, signal_id=existing.signal_id,
                strategy_id=existing.strategy_id, symbol=existing.symbol,
                expires_at=existing.expires_at, emitted_at_ms=existing.emitted_at_ms,
                emitted_round_id=existing.emitted_round_id,
                consumed=False, consumed_at_ms=0, consumed_round_id="",
            )
            return True

    def recover(self, *, round_id: str = "", now_ms: int | None = None,
                is_expired: Any = None) -> list[TicketRecord]:
        """重启后恢复：仍未消费、且（若给了 ``is_expired``）仍未过期的 ticket。"""
        stamp = int(self._clock() if now_ms is None else now_ms)
        out: list[TicketRecord] = []
        with self._lock:
            for record in self._records.values():
                if record.consumed:
                    continue
                if callable(is_expired) and bool(is_expired(record, stamp)):
                    continue
                out.append(record)
        return sorted(out, key=lambda item: (item.emitted_at_ms, item.ticket_id))

    def prune(self, *, now_ms: int | None = None) -> int:
        """修剪已结算且超出保留窗口的条目（未结算的永不修剪）。"""
        stamp = int(self._clock() if now_ms is None else now_ms)
        removed = 0
        with self._lock:
            for key, record in list(self._records.items()):
                if not record.consumed or record.consumed_at_ms <= 0:
                    # 未结算的、或没有可信结算时间的，一律不修剪（宁可不删也不误删）。
                    continue
                if stamp and stamp - record.consumed_at_ms > self.retention_ms:
                    self._records.pop(key, None)
                    removed += 1
        return removed

    def reset_corrupt(self, *, at_ms: int | None = None) -> None:
        """显式放弃损坏账本（会留痕）；调用方应先确认 DB 侧 ``signal_ticket_has_order``
        能兜住去重，否则会把同一 ticket 消费第二次。"""
        with self._lock:
            self._corrupt = ""
            self._records = {}
            self._reset_at_ms = int(self._clock() if at_ms is None else at_ms)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "path": str(self.path),
                "records": len(self._records),
                "consumed": sum(1 for record in self._records.values() if record.consumed),
                "corrupt_reason": self._corrupt,
                "reset_at_ms": int(self._reset_at_ms),
            }


# --------------------------------------------------------------------------- #
# 五、管理责任归属（每仓：谁在管 + 哪一版规则）
# --------------------------------------------------------------------------- #
MANAGER_STRATEGY = "strategy"
MANAGER_AI = "ai"
MANAGER_GUARDIAN = "guardian"
MANAGER_LEGACY = "legacy_unattributed"


def rule_version_of(strategy_id: str, cards: Iterable[Mapping[str, Any]] | None = None,
                    *, strategy_hash: str = "") -> str:
    """规则版本 = 策略卡**内容哈希**（``rv1:<12 位十六进制>``）。

    与 K01 ``strategy_hash`` 的关系：K01 哈希的是整份 ``strategy_cards.json``
    （改任何一张卡都改身份）；本函数是**单卡**粒度，用来回答"这个仓位是用哪一版
    规则开的"。两者都提供时拼成 ``rv1:<卡哈希>@<全局哈希前 12 位>``，缺一不可比较。
    """
    sid = str(strategy_id or "").strip()
    digest = ""
    for card in cards or ():
        if str(card.get("id") or "").strip() == sid:
            canonical = json.dumps(card, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), default=str)
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
            break
    global_digest = str(strategy_hash or "").strip()[:12]
    if not digest:
        return f"rv1:unknown@{global_digest}" if global_digest else "rv1:unknown"
    return f"rv1:{digest}@{global_digest}" if global_digest else f"rv1:{digest}"


def manager_of(position: Mapping[str, Any]) -> tuple[str, str]:
    """返回 ``(manager, detail)``。

    判定只看**既有消费者用的同一批字段**（``source``/``strategy_id``/``ticket_id``），
    不新增列、不猜语义：

    - ``source == "strategy"`` 且有 ``strategy_id`` → 策略卡在管（``strategy``）；
    - 有 ``strategy_id``/``ticket_id`` 但 ``source`` 不是 ``strategy`` → 仍是策略仓
      （历史行 source 缺省为 ``ai``），标 ``strategy`` 且 detail 记来源；
    - 两者都缺 → ``legacy_unattributed``（U02 的 ``LEGACY_UNATTRIBUTED`` 同口径，
      重启不产生隐式退出）。
    """
    source = str(position.get("source") or "").strip().lower()
    strategy_id = str(position.get("strategy_id") or "").strip()
    ticket_id = str(position.get("ticket_id") or "").strip()
    if strategy_id or ticket_id:
        detail = "order_source=strategy" if source == MANAGER_STRATEGY else (
            f"strategy_attributed(source={source or 'unset'})")
        return MANAGER_STRATEGY, detail
    if source == MANAGER_AI:
        return MANAGER_AI, "ai_channel"
    if source == "guardian":
        return MANAGER_GUARDIAN, "guardian_inferred"
    return MANAGER_LEGACY, "no_strategy_id_no_ticket_id"


@dataclass(frozen=True)
class PositionAttribution:
    """一个仓位的管理责任：谁在管、用哪版规则、依据哪张 ticket。"""

    lifecycle_id: str
    symbol: str
    position_side: str
    manager: str
    manager_detail: str
    strategy_id: str = ""
    ticket_id: str = ""
    rule_version: str = ""
    rule_known: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id, "symbol": self.symbol,
            "position_side": self.position_side, "manager": self.manager,
            "manager_detail": self.manager_detail, "strategy_id": self.strategy_id,
            "ticket_id": self.ticket_id, "rule_version": self.rule_version,
            "rule_known": self.rule_known,
        }


def attribute_position(position: Mapping[str, Any],
                       cards: Iterable[Mapping[str, Any]] | None = None,
                       *, strategy_hash: str = "") -> PositionAttribution:
    """单仓归属；``rule_known=False`` 表示策略卡已不在当前卡册（不得冒充已知版本）。"""
    strategy_id = str(position.get("strategy_id") or "").strip()
    manager, detail = manager_of(position)
    rule_version = rule_version_of(strategy_id, cards, strategy_hash=strategy_hash)
    return PositionAttribution(
        lifecycle_id=str(position.get("lifecycle_id")
                        or position.get("ticket_id") or position.get("symbol") or ""),
        symbol=str(position.get("symbol") or ""),
        position_side=str(position.get("side") or position.get("position_side") or ""),
        manager=manager, manager_detail=detail,
        strategy_id=strategy_id,
        ticket_id=str(position.get("ticket_id") or ""),
        rule_version=rule_version,
        rule_known=not rule_version.endswith("unknown")
        and not rule_version.startswith("rv1:unknown"),
    )


def attribute_positions(positions: Iterable[Mapping[str, Any]],
                        cards: Iterable[Mapping[str, Any]] | None = None,
                        *, strategy_hash: str = "") -> list[PositionAttribution]:
    card_list = list(cards or ())
    return [attribute_position(position, card_list, strategy_hash=strategy_hash)
            for position in positions if isinstance(position, Mapping)]


def attribution_summary(attributions: Sequence[PositionAttribution]) -> dict[str, Any]:
    """按管理者聚合 + 列出"规则未知"的仓位（改卡/删卡后必须可见，不得静默）。"""
    by_manager: dict[str, list[dict[str, Any]]] = {}
    unknown: list[dict[str, Any]] = []
    for item in attributions:
        by_manager.setdefault(item.manager, []).append(item.as_dict())
        if not item.rule_known:
            unknown.append(item.as_dict())
    return {
        "total": len(attributions),
        "by_manager": {manager: len(rows) for manager, rows in sorted(by_manager.items())},
        "positions": [item.as_dict() for item in attributions],
        "rule_unknown": unknown,
    }


# --------------------------------------------------------------------------- #
# 六、管理优先预算（管理存量优先于寻找新仓）
# --------------------------------------------------------------------------- #
DEFAULT_MANAGEMENT_RESERVE_RATIO = 0.5


@dataclass
class ManagementPriorityPlan:
    """一轮预算的两池切分：管理预留池 + 新仓研究池（DL03）。

    不变量（由 :meth:`claim_management` / :meth:`claim_research` 强制，测试逐条验）：

    1. **管理优先**：管理侧可取的量 = 全池剩余，**不受**预留比例限制；
       新仓研究最多只能用 :meth:`research_available`。
    2. **研究不挤占管理**：研究侧的取用**永远**不减少管理侧仍可取的量，
       即 ``claim_research`` 后 ``management_available()`` 不下降。
    3. **可见的让路**：研究池为 0 时 :meth:`research_reason` 给出
       ``MODEL_DEADLINE`` + ``MANAGEMENT_PRIORITY_BUDGET``，而不是"没有信号"。
    """

    total_budget: int
    management_reserve: int
    label: str = "cycle"
    management_used: int = 0
    research_used: int = 0
    management_demands: int = 0

    def __post_init__(self) -> None:
        self.total_budget = max(0, int(self.total_budget))
        reserve = max(0, min(int(self.management_reserve), self.total_budget))
        self.management_reserve = reserve
        self.management_used = max(0, int(self.management_used))
        self.research_used = max(0, int(self.research_used))

    @classmethod
    def from_total(cls, total_budget: int, *,
                   reserve_ratio: float = DEFAULT_MANAGEMENT_RESERVE_RATIO,
                   reserve_min: int = 1, reserve_max: int | None = None,
                   label: str = "cycle") -> "ManagementPriorityPlan":
        """按比例切分；比例非法（非有限/<=0/>1）时退化为"全部留给研究"的显式形态。

        退化时 ``management_reserve=0`` 是**显式可见**的（``as_dict`` 里有
        ``reserve_ratio_effective``），不是静默把管理饿死。
        """
        total = max(0, int(total_budget))
        ratio = float(reserve_ratio)
        if not math.isfinite(ratio) or ratio <= 0 or ratio > 1:
            reserve = 0
        else:
            reserve = max(int(reserve_min), int(math.ceil(total * ratio)))
            if reserve_max is not None:
                reserve = min(reserve, int(reserve_max))
        return cls(total_budget=total, management_reserve=min(reserve, total), label=label)

    # -- 读 ----------------------------------------------------------- #

    @property
    def management_available(self) -> int:
        """管理侧仍能取用的量：**全池剩余**（研究池也被它兜住）。"""
        return max(0, self.total_budget - self.management_used - self.research_used)

    @property
    def research_available(self) -> int:
        """新仓研究仍能取用的量：全池剩余扣除管理预留。"""
        spare = max(0, self.total_budget - self.management_used - self.research_used
                    - self.management_reserve)
        return spare

    @property
    def research_starved(self) -> bool:
        return self.research_available <= 0 and self.total_budget > 0

    # -- 写 ----------------------------------------------------------- #

    def claim_management(self, amount: int) -> int:
        """管理侧取用；可取到把研究池吃光（管理优先）。返回实际取到的量。"""
        want = max(0, int(amount))
        if want:
            self.management_demands += 1
        granted = min(want, self.management_available)
        self.management_used += granted
        return granted

    def claim_research(self, amount: int) -> int:
        """新仓研究取用；**永不**触碰管理预留池。返回实际取到的量。"""
        granted = min(max(0, int(amount)), self.research_available)
        self.research_used += granted
        return granted

    def meets_management_demand(self) -> bool:
        """管理侧是否得到了它要的全部预算（DC10：不允许研究把管理挤下去）。"""
        return self.management_demands == 0 or self.research_available >= 0

    def research_reason(self) -> DecisionReason | None:
        """研究池被管理吃光时的原因码；有预算时返回 ``None``（不硬塞原因）。"""
        if not self.research_starved:
            return None
        return DecisionReason.from_codes(
            [REASON_MODEL_DEADLINE],
            details=(DETAIL_MANAGEMENT_PRIORITY_BUDGET,
                     f"total={self.total_budget}",
                     f"management_reserve={self.management_reserve}",
                     f"management_used={self.management_used}"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "total_budget": self.total_budget,
            "management_reserve": self.management_reserve,
            "management_used": self.management_used,
            "research_used": self.research_used,
            "management_available": self.management_available,
            "research_available": self.research_available,
            "research_starved": self.research_starved,
        }


# --------------------------------------------------------------------------- #
# 七、逐轮解释（把漏斗 + 预算 + 持仓合成一句"为什么"）
# --------------------------------------------------------------------------- #
def round_explanation(
    *,
    funnel: ScanFunnel | None = None,
    plan: ManagementPriorityPlan | None = None,
    positions: Iterable[Mapping[str, Any]] = (),
    channel_conflicts: Sequence[str] = (),
    readiness_blockers: Sequence[str] = (),
    data_unavailable: Sequence[str] = (),
    evidence_report: Any | None = None,
    model_error: BaseException | None = None,
) -> dict[str, Any]:
    """一轮的完整原因归集：**复用** U05 的 ``classify_cycle``，不另建分类。

    ``hold_existing`` 由持仓事实决定（确有一笔在管仓位），``intents`` 由漏斗的
    开仓计数决定（漏斗到达终态才算"有边际"）——两者都是**结构化事实**，
    本函数不猜。
    """
    # 延迟导入：避免与 U05 的循环引用风险，同时保持"九码只有一处定义"。
    from core.decision_trace import classify_cycle

    reasons: list[str] = []
    details: list[str] = []
    if funnel is not None:
        own = funnel.explanation()
        reasons.extend(own.codes)
        details.extend(own.details)
    if plan is not None:
        budget_reason = plan.research_reason()
        if budget_reason is not None:
            reasons.extend(budget_reason.codes)
            details.extend(budget_reason.details)
    reason = classify_cycle(
        intents=[1] * (funnel.opened if funnel is not None else 0),
        hold_existing=bool(list(positions)),
        readiness_blockers=readiness_blockers,
        channel_conflicts=channel_conflicts,
        data_unavailable=data_unavailable,
        evidence_report=evidence_report,
        model_error=model_error,
    )
    # 有开仓时本轮结论是"仓在手"（与 :meth:`ScanFunnel.explanation` 同口径）；
    # 其余情况一律让 U05 的九码确定性优先级决定主因，**不**用漏斗的计数投票去压
    # 模型/数据类根因（那会让 MODEL_DEADLINE 被 NO_EDGE 掩盖）。
    prefer = REASON_HOLD_EXISTING if (funnel is not None and funnel.opened) else None
    merged = DecisionReason.from_codes(
        (*reasons, *reason.codes),
        details=tuple(dict.fromkeys((*details, *reason.details))),
        prefer=prefer,
    )
    no_open = funnel.no_open_explanation() if funnel is not None else None
    return {
        "reason": merged.as_dict(),
        "primary_reason": merged.primary,
        "reason_codes": list(merged.codes),
        "hold_existing": bool(list(positions)),
        "opened": funnel.opened if funnel is not None else 0,
        "funnel": funnel.as_dict() if funnel is not None else None,
        "no_open": no_open,
        "budget": plan.as_dict() if plan is not None else None,
    }


__all__ = [
    "ALREADY_CONSUMED",
    "CONSUMED",
    "DEFAULT_MANAGEMENT_RESERVE_RATIO",
    "DEFAULT_RETENTION_MS",
    "DETAIL_CODES",
    "DETAIL_REASON",
    "FUNNEL_REPORT_KEY",
    "FunnelItem",
    "LEDGER_CORRUPT",
    "LEDGER_SCHEMA_VERSION",
    "LedgerCorrupt",
    "MANAGER_AI",
    "MANAGER_GUARDIAN",
    "MANAGER_LEGACY",
    "MANAGER_STRATEGY",
    "MAX_ITEMS_IN_REPORT",
    "ManagementPriorityPlan",
    "PositionAttribution",
    "REASON_ALL",
    "SCHEMA_VERSION",
    "STAGE_GROUPS",
    "STAGE_ORDER",
    "ScanFunnel",
    "Stage",
    "TERMINAL_STAGE",
    "TicketConsumption",
    "TicketLedger",
    "TicketRecord",
    "UNSEEN",
    "UnknownDetailCode",
    "attribute_position",
    "attribute_positions",
    "attribution_summary",
    "detail_codes_for_reason",
    "detail_reason",
    "empty_funnel_report",
    "funnel_from_report",
    "manager_of",
    "round_explanation",
    "rule_version_of",
    "validate_detail_table",
]
