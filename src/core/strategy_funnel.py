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

import contextlib
import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - 平台分支（Windows）
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]
try:  # pragma: no cover - 平台分支（POSIX）
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

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
    reason_from_source_errors,
)

#: 仓位归属枚举**与 U02 同源**（P2-1）：直接引用 ``strategy_position_manager``
#: 的 ``PositionClass``，不在本地复制字面量——U02 改枚举值时这里跟着变，
#: 不会静默漂移成"看起来对但其实是巧合"。
from core.strategy_position_manager import PositionClass

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

# -- 轮级故障（B-r1 P0-4：轮级前置失败原先完全没有表达，被压成 no_edge/no_candidates） --
#: 策略卡册不存在/读不到（``knowledge_dir/strategy_cards.json`` 缺失）。
DETAIL_CARDS_UNAVAILABLE = "CARDS_UNAVAILABLE"
#: 策略卡册存在但不可解析（JSON 损坏 / 根形态非法）。
DETAIL_CARDS_INVALID = "CARDS_INVALID"
#: 策略运行时不可用（``strategy_runtime.REGISTRY`` 缺失 / ``build_features`` 缺失）。
DETAIL_STRATEGY_LAB_UNAVAILABLE = "STRATEGY_LAB_UNAVAILABLE"
#: 策略卡与运行时注册表**漂移**（卡所需 ``signal_fn`` 不在注册表里）。
DETAIL_STRATEGY_LAB_DRIFT = "STRATEGY_LAB_DRIFT"
#: 票据源读取失败（审计库 ``fetch_open_signal_tickets`` 抛错）——与"本轮没有票据"
#: （:data:`DETAIL_NO_TICKETS_AVAILABLE`）是两件事。
DETAIL_TICKET_SOURCE_UNAVAILABLE = "TICKET_SOURCE_UNAVAILABLE"
#: 轮级未捕获异常（整轮中断时的兜底留痕）。
DETAIL_ROUND_EXCEPTION = "ROUND_EXCEPTION"
#: 研究预算总额为 0/负（"没有预算可谈"与"预算被管理占用"是两件事，B-r1 P2-3）。
DETAIL_RESEARCH_BUDGET_ZERO = "RESEARCH_BUDGET_ZERO"
#: 执行器收尾的**合并落盘**没做成（B-r3 N3）：取不到跨进程锁，或盘上账本已被判定损坏。
#: 注意它**不是**"某张票没被消费"——每张票的消费事实在 :meth:`TicketLedger.consume`
#: 返回前就已单独落盘；这里推迟的是"本轮消费/释放事实的合并写"。之所以仍登记为
#: **轮级故障**：本轮之后盘上账本与内存不再一致，跨进程可见性被推迟到下一次成功写入，
#: 必须让"为什么这轮的状态文件看起来不对"有结构化的答案，而不是只剩一句异常堆栈。
DETAIL_LEDGER_SAVE_DEFERRED = "LEDGER_SAVE_DEFERRED"

#: 细分码 -> 九码之一。**这是本模块唯一的分类轴**；任何细分码都必须在此登记，
#: 未登记码在 :func:`detail_reason` 处 fail-closed（:class:`UnknownDetailCode`）。
DETAIL_REASON: dict[str, str] = {
    # -- 扫描侧 -------------------------------------------------------- #
    # 卡片未指标化 = 该卡未通过"是信号来源"这道配置校验（B-r1 P0-4a：原先映射到
    # ``NO_EDGE``，会把"卡册配置缺陷"读成"没有信号"）。
    DETAIL_CARD_NOT_INDICATORIZED: REASON_VALIDATION_REJECTED,
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
    # -- 轮级故障 ------------------------------------------------------ #
    DETAIL_CARDS_UNAVAILABLE: REASON_READINESS_BLOCKED,
    DETAIL_CARDS_INVALID: REASON_VALIDATION_REJECTED,
    DETAIL_STRATEGY_LAB_UNAVAILABLE: REASON_READINESS_BLOCKED,
    DETAIL_STRATEGY_LAB_DRIFT: REASON_VALIDATION_REJECTED,
    DETAIL_TICKET_SOURCE_UNAVAILABLE: REASON_DATA_UNAVAILABLE,
    DETAIL_ROUND_EXCEPTION: REASON_VALIDATION_REJECTED,
    DETAIL_RESEARCH_BUDGET_ZERO: REASON_MODEL_DEADLINE,
    DETAIL_LEDGER_SAVE_DEFERRED: REASON_READINESS_BLOCKED,
}

DETAIL_CODES: frozenset[str] = frozenset(DETAIL_REASON)

# --------------------------------------------------------------------------- #
# 二之二、细分码的**性质**（B-r1 P0-4：故障不得被读成"没有信号"）
# --------------------------------------------------------------------------- #
#: 故障：本轮**某项能力受损**（数据取不到 / 校验失败 / 就绪阻断 / 模型类）。
#: 只要出现一个故障，本轮 `verdict` 就是 ``blocked``——故障永远**不**降级成
#: "没有信号"。
NATURE_FAULT = "fault"
#: 真的没有边际（候选**评估过**了，就是没有可下单边际）。
NATURE_EDGE = "edge"
#: 已在处理（已持仓 / 已消费 / 同 symbol 冲突）：不是"没有信号"，也**不是**故障。
NATURE_HANDLED = "handled"
#: 范围界定 / 配置态（标的不在白名单、卡未指标化、通道只扫描不下单）：正常跳过，
#: 既不是故障也不是"没有边际"。它们**不**让整轮变成 blocked——否则真实轮次里
#: "有一张范围外的卡"是常态，verdict 会永久失去区分力（B-r1 §2.0 反向误标）。
NATURE_SCOPE = "scope"

NATURES: frozenset[str] = frozenset({NATURE_FAULT, NATURE_EDGE,
                                     NATURE_HANDLED, NATURE_SCOPE})

#: 细分码 -> 性质。**每个**细分码都必须登记，未登记在
#: :func:`validate_detail_table` 处 fail-closed。
DETAIL_NATURE: dict[str, str] = {
    # 扫描侧：前置筛里的配置/范围界定
    DETAIL_CARD_NOT_INDICATORIZED: NATURE_SCOPE,
    DETAIL_SYMBOL_NOT_USDT: NATURE_SCOPE,
    DETAIL_SYMBOL_NOT_IN_SCOPE: NATURE_SCOPE,
    DETAIL_TICKET_SYMBOL_NOT_IN_SCOPE: NATURE_SCOPE,
    DETAIL_SYMBOL_NOT_ALLOWED: NATURE_SCOPE,
    DETAIL_CHANNEL_BUDGET_ZERO: NATURE_SCOPE,
    # 扫描侧：故障
    DETAIL_SIGNAL_FN_MISSING: NATURE_FAULT,
    DETAIL_SIGNAL_FN_OUTPUT_INVALID: NATURE_FAULT,
    DETAIL_TIMEFRAME_UNSUPPORTED: NATURE_FAULT,
    DETAIL_MARKET_TRANSPORT_OUTAGE: NATURE_FAULT,
    DETAIL_MARKET_BUDGET_DEFERRED: NATURE_FAULT,
    DETAIL_KLINE_FETCH_FAILED: NATURE_FAULT,
    DETAIL_KLINE_QUALITY_REJECTED: NATURE_FAULT,
    DETAIL_KLINE_ROWS_INSUFFICIENT: NATURE_FAULT,
    DETAIL_FEATURES_INCOMPLETE: NATURE_FAULT,
    DETAIL_SCAN_EXCEPTION: NATURE_FAULT,
    DETAIL_PROVIDER_RATE_LIMITED: NATURE_FAULT,
    DETAIL_SIDE_NOT_ALLOWED_BY_CARD: NATURE_FAULT,
    # 扫描侧：真的没有边际
    DETAIL_NO_SIGNAL_ON_CLOSED_BAR: NATURE_EDGE,
    DETAIL_BAR_ALREADY_SEEN: NATURE_EDGE,
    DETAIL_SIGNAL_ALREADY_PERSISTED: NATURE_EDGE,
    # 票据形状/时效
    DETAIL_TICKET_SHAPE_INVALID: NATURE_FAULT,
    DETAIL_TICKET_EXPIRED: NATURE_FAULT,
    DETAIL_TICKET_EXPIRING_BEFORE_USE: NATURE_FAULT,
    DETAIL_TICKET_LEDGER_UNAVAILABLE: NATURE_FAULT,
    DETAIL_ENTRY_ZONE_INVERTED: NATURE_FAULT,
    DETAIL_TICKET_ALREADY_CONSUMED: NATURE_HANDLED,
    # 执行侧
    DETAIL_SYMBOL_ALREADY_HELD: NATURE_HANDLED,
    DETAIL_STRATEGY_ALREADY_HELD: NATURE_HANDLED,
    DETAIL_NO_LATEST_PRICE: NATURE_FAULT,
    DETAIL_STRATEGY_TRADER_EXCEPTION: NATURE_FAULT,
    DETAIL_PRICE_OUTSIDE_ENTRY_ZONE: NATURE_EDGE,
    DETAIL_MANAGEMENT_PRIORITY_BUDGET: NATURE_FAULT,
    # 研究证据侧
    DETAIL_EVIDENCE_GATE_FAILED: NATURE_FAULT,
    DETAIL_EVIDENCE_DATA_UNAVAILABLE: NATURE_FAULT,
    DETAIL_EVIDENCE_VALIDATION_REJECTED: NATURE_FAULT,
    # 轮级
    DETAIL_NO_TICKETS_AVAILABLE: NATURE_EDGE,
    DETAIL_CARDS_UNAVAILABLE: NATURE_FAULT,
    DETAIL_CARDS_INVALID: NATURE_FAULT,
    DETAIL_STRATEGY_LAB_UNAVAILABLE: NATURE_FAULT,
    DETAIL_STRATEGY_LAB_DRIFT: NATURE_FAULT,
    DETAIL_TICKET_SOURCE_UNAVAILABLE: NATURE_FAULT,
    DETAIL_ROUND_EXCEPTION: NATURE_FAULT,
    DETAIL_RESEARCH_BUDGET_ZERO: NATURE_FAULT,
    DETAIL_LEDGER_SAVE_DEFERRED: NATURE_FAULT,
}

#: 九码 -> 性质（外部码没有细分码可查时用；同码冲突时以 `FAULT` 为准）。
REASON_NATURE: dict[str, str] = {
    REASON_MODEL_DEADLINE: NATURE_FAULT,
    REASON_MODEL_RATE_LIMITED: NATURE_FAULT,
    REASON_DATA_UNAVAILABLE: NATURE_FAULT,
    REASON_READINESS_BLOCKED: NATURE_FAULT,
    REASON_RESEARCH_INCOMPLETE: NATURE_FAULT,
    REASON_VALIDATION_REJECTED: NATURE_FAULT,
    REASON_CHANNEL_CONFLICT: NATURE_HANDLED,
    REASON_NO_EDGE: NATURE_EDGE,
    REASON_HOLD_EXISTING: NATURE_HANDLED,
}

#: 细分码 -> 允许登记的**闸门**（空元组 = 只能作为轮级故障登记，不能挂在候选上）。
#: 这张表让"17 闸门 × N 码"不再是两张各自自洽的表：挂错闸门在
#: :class:`FunnelItem` 处直接 ``ValueError``（B-r1 P3-5）。
DETAIL_STAGES: dict[str, tuple[str, ...]] = {
    DETAIL_CARD_NOT_INDICATORIZED: (Stage.CARD_INDICATORIZED.value,),
    DETAIL_SIGNAL_FN_MISSING: (Stage.SIGNAL_FN_REGISTERED.value,),
    DETAIL_SYMBOL_NOT_USDT: (Stage.SYMBOL_IN_SCOPE.value,),
    DETAIL_SYMBOL_NOT_IN_SCOPE: (Stage.SYMBOL_IN_SCOPE.value,),
    DETAIL_TICKET_SYMBOL_NOT_IN_SCOPE: (Stage.SYMBOL_IN_SCOPE.value,),
    DETAIL_TIMEFRAME_UNSUPPORTED: (Stage.TIMEFRAME_SUPPORTED.value,),
    DETAIL_MARKET_TRANSPORT_OUTAGE: (Stage.MARKET_DATA_READY.value,),
    DETAIL_MARKET_BUDGET_DEFERRED: (Stage.MARKET_DATA_READY.value,),
    DETAIL_KLINE_FETCH_FAILED: (Stage.MARKET_DATA_READY.value,),
    DETAIL_KLINE_QUALITY_REJECTED: (Stage.MARKET_DATA_READY.value,),
    DETAIL_KLINE_ROWS_INSUFFICIENT: (Stage.MARKET_DATA_READY.value,),
    DETAIL_PROVIDER_RATE_LIMITED: (Stage.MARKET_DATA_READY.value,
                                   Stage.SIGNAL_TRIGGERED.value),
    DETAIL_SIGNAL_FN_OUTPUT_INVALID: (Stage.SIGNAL_TRIGGERED.value,),
    DETAIL_NO_SIGNAL_ON_CLOSED_BAR: (Stage.SIGNAL_TRIGGERED.value,),
    DETAIL_SCAN_EXCEPTION: (Stage.SIGNAL_TRIGGERED.value,),
    DETAIL_SIDE_NOT_ALLOWED_BY_CARD: (Stage.SIDE_ALLOWED.value,),
    DETAIL_FEATURES_INCOMPLETE: (Stage.FEATURES_COMPLETE.value,),
    DETAIL_BAR_ALREADY_SEEN: (Stage.BAR_UNSEEN.value,),
    DETAIL_SIGNAL_ALREADY_PERSISTED: (Stage.BAR_UNSEEN.value,),
    DETAIL_TICKET_SHAPE_INVALID: (Stage.TICKET_FRESH.value,),
    DETAIL_TICKET_EXPIRED: (Stage.TICKET_FRESH.value,),
    DETAIL_TICKET_EXPIRING_BEFORE_USE: (Stage.TICKET_FRESH.value,),
    DETAIL_TICKET_ALREADY_CONSUMED: (Stage.TICKET_FRESH.value,),
    DETAIL_TICKET_LEDGER_UNAVAILABLE: (Stage.TICKET_FRESH.value,),
    DETAIL_ENTRY_ZONE_INVERTED: (Stage.TICKET_FRESH.value,),
    DETAIL_SYMBOL_NOT_ALLOWED: (Stage.NO_POSITION_CONFLICT.value,),
    DETAIL_SYMBOL_ALREADY_HELD: (Stage.NO_POSITION_CONFLICT.value,),
    DETAIL_STRATEGY_ALREADY_HELD: (Stage.NO_POSITION_CONFLICT.value,),
    DETAIL_NO_LATEST_PRICE: (Stage.PRICE_AVAILABLE.value,),
    DETAIL_STRATEGY_TRADER_EXCEPTION: (Stage.PRICE_AVAILABLE.value,),
    DETAIL_PRICE_OUTSIDE_ENTRY_ZONE: (Stage.IN_ENTRY_ZONE.value,),
    # 轮级：只能经 :meth:`ScanFunnel.record_round_fault` 登记
    DETAIL_NO_TICKETS_AVAILABLE: (),
    DETAIL_CHANNEL_BUDGET_ZERO: (),
    DETAIL_MANAGEMENT_PRIORITY_BUDGET: (),
    DETAIL_RESEARCH_BUDGET_ZERO: (),
    DETAIL_CARDS_UNAVAILABLE: (),
    DETAIL_CARDS_INVALID: (),
    DETAIL_STRATEGY_LAB_UNAVAILABLE: (),
    DETAIL_STRATEGY_LAB_DRIFT: (),
    DETAIL_TICKET_SOURCE_UNAVAILABLE: (),
    DETAIL_ROUND_EXCEPTION: (),
    DETAIL_LEDGER_SAVE_DEFERRED: (),
    DETAIL_EVIDENCE_GATE_FAILED: (),
    DETAIL_EVIDENCE_DATA_UNAVAILABLE: (),
    DETAIL_EVIDENCE_VALIDATION_REJECTED: (),
}

#: 轮级专用细分码（``DETAIL_STAGES`` 为空元组的那些）。
ROUND_ONLY_DETAILS: frozenset[str] = frozenset(
    code for code, stages in DETAIL_STAGES.items() if not stages
)

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
    """自检注册表：每个细分码都映射到九码、都有性质、都挂对了闸门；九码全部可达。

    ``RESEARCH_INCOMPLETE``/``MODEL_RATE_LIMITED`` 等码由 U05 的
    ``classify_evidence_report`` / ``classify_exception`` 直接产出，这里只要求
    **至少一个**细分码可达，避免注册表与九码漂移。

    负例（抛 :class:`UnknownDetailCode`/``ValueError``）：
    - 细分码映射到非九码；
    - 某个九码没有任何细分码可达；
    - 细分码没有登记性质（:data:`DETAIL_NATURE`）；
    - 细分码没有登记闸门（:data:`DETAIL_STAGES`）或挂了未知闸门。
    """
    unknown = sorted(code for code, reason in DETAIL_REASON.items()
                     if reason not in REASON_ALL)
    if unknown:
        raise UnknownDetailCode(unknown)
    missing = sorted(REASON_ALL - {*DETAIL_REASON.values()})
    if missing:
        raise UnknownDetailCode(missing)
    bad_nature = sorted(code for code, nature in DETAIL_NATURE.items()
                        if nature not in NATURES)
    if bad_nature:
        raise ValueError(f"细分码性质非法/未登记: {bad_nature}")
    not_in_nature = sorted(DETAIL_CODES - set(DETAIL_NATURE))
    if not_in_nature:
        raise ValueError(f"细分码没有登记性质: {not_in_nature}")
    not_in_stages = sorted(DETAIL_CODES - set(DETAIL_STAGES))
    if not_in_stages:
        raise ValueError(f"细分码没有登记闸门: {not_in_stages}")
    bad_stage = sorted(f"{code}:{stage}" for code, stages in DETAIL_STAGES.items()
                       for stage in stages if stage not in STAGE_ORDER)
    if bad_stage:
        raise ValueError(f"细分码挂了未知闸门: {bad_stage}")


# --------------------------------------------------------------------------- #
# 二之三、一轮"没开仓"的判定（verdict + 主因）——**唯一**判定入口
# --------------------------------------------------------------------------- #
VERDICT_OPENED = "opened"
VERDICT_NO_EDGE = "no_edge"
VERDICT_BLOCKED = "blocked"
VERDICT_NO_CANDIDATES = "no_candidates"
VERDICT_HANDLED = "handled"
VERDICT_UNAVAILABLE = "unavailable"

VERDICTS: frozenset[str] = frozenset({
    VERDICT_OPENED, VERDICT_NO_EDGE, VERDICT_BLOCKED,
    VERDICT_NO_CANDIDATES, VERDICT_HANDLED, VERDICT_UNAVAILABLE,
})


def bucket_reason_codes(*, details: Iterable[str] = (),
                        codes: Iterable[str] = ()) -> dict[str, set[str]]:
    """把细分码与九码按**性质**分桶（故障优先：同码冲突时按故障处理，保守）。

    返回 ``{fault|edge|handled|scope: {九码, ...}}``。未登记的细分码被忽略
    （读侧可能拿到被篡改的状态文件，这里不抛异常，由调用方显式标注不可用）。
    """
    buckets: dict[str, set[str]] = {NATURE_FAULT: set(), NATURE_EDGE: set(),
                                    NATURE_HANDLED: set(), NATURE_SCOPE: set()}
    from_details: set[str] = set()
    for detail in details:
        text = str(detail or "").strip()
        nature = DETAIL_NATURE.get(text)
        reason = DETAIL_REASON.get(text)
        if nature and reason:
            buckets[nature].add(reason)
            from_details.add(reason)
    # 九码**已有细分码来源**时以细分码的性质为准（``READINESS_BLOCKED`` 既可能是
    # "标的不在白名单"（范围界定），也可能是外部传入的就绪阻断（故障）——
    # 只有后者才走 :data:`REASON_NATURE` 的默认性质）。
    for code in codes:
        text = str(code or "").strip()
        if not text or text in from_details:
            continue
        nature = REASON_NATURE.get(text)
        if nature:
            buckets[nature].add(text)
    return buckets


def classify_no_open(*, details: Iterable[str] = (), codes: Iterable[str] = (),
                     opened: int = 0, evaluated: int = 0) -> tuple[str, str]:
    """返回 ``(verdict, primary_reason)``。**故障绝不降级成"没有信号"。**

    判定顺序（每一步都是**结构化事实**，不做文本推断）：

    1. 有开仓意图 → ``opened`` / ``HOLD_EXISTING``；
    2. 出现任一**故障**（数据取不到 / 校验失败 / 就绪阻断 / 模型类）→ ``blocked``，
       主因取故障码里优先级最高者；
    3. 一个候选都没**评估**过（全部停在前置筛，或本轮根本没有候选）→
       ``no_candidates``，主因取当前码里优先级最高者（**不**硬塞 ``NO_EDGE``）；
       **已知留白（B-r3 N5，P3）**：九码集合冻结在 U05，"候选集为空"**没有**自己的
       九码，所以"本轮一张票都没有"（轮级 ``NO_TICKETS_AVAILABLE``）只能落到它
       自己登记的九码 ``NO_EDGE`` 上 → ``no_candidates`` + 主因字面像"没信号"。
       区分方式是**结构化的**、不在主因字符串里：``verdict=no_candidates``（不是
       ``no_edge``）＋ ``round_faults`` 里的事实＋ ``details`` 计数。此处**不改**
       注册表：把 ``NO_TICKETS_AVAILABLE`` 改挂别的九码只会把"没有票"说成别的
       事实。文档同款说明见 ``docs/策略扫描原因码.md`` §4；
    4. 有"真的没有边际"的观测 → ``no_edge``，主因取边际类码（范围界定类码只做辅因，
       否则"有一张范围外的卡"会让整轮常态变成 ``blocked``，verdict 失去区分力）；
    5. 只有"已在处理"的事实 → ``handled``（已持仓/已消费，**不是**没有信号）；
    6. 只剩下范围界定类码 → ``blocked``（本轮的处理被范围/配置挡住，且没有任何
       边际观测）；
    7. 其余 → ``no_edge``（有评估过）或 ``no_candidates``（没有）。
    """
    buckets = bucket_reason_codes(details=details, codes=codes)
    if opened:
        return VERDICT_OPENED, REASON_HOLD_EXISTING
    if buckets[NATURE_FAULT]:
        return VERDICT_BLOCKED, choose_primary(sorted(buckets[NATURE_FAULT]))
    if int(evaluated) <= 0:
        # 主因取"当前所有码"里优先级最高者，而不是硬塞 NO_EDGE。唯一例外是
        # `everything` 里**只有** NO_EDGE 时（典型：本轮连票都没有，轮级
        # NO_TICKETS_AVAILABLE 登记的九码就是 NO_EDGE）——九码集合冻结在 U05，
        # "候选集为空"没有专属九码，此时靠 `verdict=no_candidates` + `round_faults`
        # 区分，不靠主因字面（B-r3 N5）。
        everything = sorted(set().union(*buckets.values()))
        return VERDICT_NO_CANDIDATES, choose_primary(everything or [REASON_NO_EDGE])
    if buckets[NATURE_EDGE]:
        return VERDICT_NO_EDGE, choose_primary(sorted(buckets[NATURE_EDGE]))
    if buckets[NATURE_HANDLED]:
        return VERDICT_HANDLED, choose_primary(sorted(buckets[NATURE_HANDLED]))
    if buckets[NATURE_SCOPE]:
        return VERDICT_BLOCKED, choose_primary(sorted(buckets[NATURE_SCOPE]))
    return VERDICT_NO_EDGE, REASON_NO_EDGE


# --------------------------------------------------------------------------- #
# 三、漏斗本身
# --------------------------------------------------------------------------- #
#: 明细来源：扫描候选 / 跨轮恢复的票据 / 执行侧 / 轮级结论。
#: 只有 ``scan`` 来源参与"漏斗收窄"的前缀计数，避免把恢复出来的票据算成新候选。
ORIGIN_SCAN = "scan"
ORIGIN_RECOVER = "recover"
ORIGIN_EXECUTE = "execute"
ORIGIN_ROUND = "round"

#: **评估型**阶段：到达这些闸门才代表"这个候选真的被评估过"（拉过行情 / 跑过策略函数 /
#: 检查过票据）。前面的 5 道是**前置筛**（卡册属性与范围界定），停在那里说明候选
#: 从未进入评估——所以"一个候选都没评估"与"评估过但没有边际"必须可区分。
PRE_GATE_STAGES: tuple[str, ...] = (
    Stage.CARD_LOADED.value,
    Stage.CARD_INDICATORIZED.value,
    Stage.SIGNAL_FN_REGISTERED.value,
    Stage.SYMBOL_IN_SCOPE.value,
    Stage.TIMEFRAME_SUPPORTED.value,
)
EVALUATION_STAGES: tuple[str, ...] = tuple(
    name for name in STAGE_ORDER if name not in PRE_GATE_STAGES
)


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
        # 反向：**终态/通过型**阶段不得带细分码。"到达终态却带原因"会被
        # :attr:`passed` 静默算成"被拒绝"（不计入 opened），是真实的漏计通路
        # （B-r1 P3-4/P2-1）。宁可报错也不让终态被算错。
        if self.detail and stage in SUCCESS_STAGES:
            raise ValueError(
                f"通过型阶段 {stage!r} 不得带细分码（{self.detail!r} 应挂在对应的"
                f"拒绝型闸门上）")
        # 细分码必须挂在**允许的闸门**上：让"17 闸门 × N 码"成为一张有约束的表
        # （B-r1 P3-5），而不是两张各自自洽的表。
        if self.detail:
            allowed = DETAIL_STAGES.get(self.detail)
            if allowed is None:
                raise UnknownDetailCode((self.detail,))
            if not allowed:
                raise ValueError(
                    f"细分码 {self.detail!r} 是**轮级**码，只能经 "
                    f"record_round_fault() 登记，不能挂在候选闸门 {stage!r} 上")
            if self.origin != ORIGIN_ROUND and stage not in allowed:
                raise ValueError(
                    f"细分码 {self.detail!r} 不允许挂在闸门 {stage!r}（允许:"
                    f"{list(allowed)}）")

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


@dataclass(frozen=True)
class RoundFault:
    """**轮级故障**：不是某个候选的结局，而是"这一轮根本没跑成/没跑全"。

    B-r1 P0-4②：轮级前置失败（卡册读不到、运行时注册表不可用/漂移、通道预算为 0、
    票据源读取失败、轮级异常）原先**完全没有表达**，被压成 ``no_candidates`` 甚至
    ``no_edge``。轮级故障有独立的登记通道，因此它既不会被算成候选，也不会丢。
    """

    detail: str
    reason: str
    note: str = ""
    round_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"detail": self.detail, "reason": self.reason, "note": self.note,
                "round_id": self.round_id, "nature": DETAIL_NATURE.get(self.detail, "")}


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
    #: 轮级故障（不是候选结局；不计入候选数、不计入闸门直方图）。
    round_faults: list[RoundFault] = field(default_factory=list)
    round_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    truncated: int = 0
    #: 读侧不可信标记（状态文件被篡改/写入未登记码时置位）。置位后
    #: :meth:`no_open_explanation` 返回 ``verdict=unavailable``，**不抛异常**——
    #: 一个坏状态文件不得把展示层打成 500（B-r1 P3-2）。
    report_corrupt_reason: str = ""

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

    def record_round_fault(self, detail: str, *, note: str = "",
                           round_id: str = "") -> RoundFault:
        """登记一个**轮级故障**（卡册读不到 / 运行时不可用 / 通道预算为 0 / ...）。

        与 :meth:`record` 的区别：这不是"某个候选停在哪道闸门"，而是"这一轮本身的
        能力受损"。因此它**不**进候选计数、**不**进闸门直方图，但**进**原因码与细分码
        计数（否则"为什么没开"又会只剩一个 verdict）。``detail`` 必须是登记为
        轮级专用的细分码（:data:`ROUND_ONLY_DETAILS`），否则 ``ValueError``。
        """
        code = str(detail or "").strip()
        reason = detail_reason(code)
        if code not in ROUND_ONLY_DETAILS:
            raise ValueError(
                f"细分码 {code!r} 不是轮级码（它挂在闸门 "
                f"{list(DETAIL_STAGES.get(code, ()))} 上），请用 record() 登记")
        rid = str(round_id or self.round_id)
        if rid not in self.round_ids:
            self.round_ids.append(rid)
        fault = RoundFault(detail=code, reason=reason, note=str(note or "")[:200],
                           round_id=rid)
        self.round_faults.append(fault)
        self.detail_counts[code] += 1
        self.reason_counts[reason] += 1
        return fault

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
    def evaluated(self) -> int:
        """**真的被评估过**的项数（到达评估型闸门；轮级条目不参与）。

        这是"no_edge 与 no_candidates 的分界线"：全部候选都停在前置筛（卡未指标化 /
        标的不在白名单）时，本轮**没有评估过任何东西**，此时不允许报 ``no_edge``。
        """
        return sum(int(self.throughput.get(stage, 0)) for stage in EVALUATION_STAGES)

    @property
    def round_fault_details(self) -> tuple[str, ...]:
        return tuple(fault.detail for fault in self.round_faults)

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
                    details: Iterable[str] = (),
                    primary: str | None = None) -> DecisionReason:
        """本轮"为什么没开/开了什么"的原因：主因唯一、辅因可多。

        ``extra_codes`` 供调用方补充管理面事实（如渠道冲突、就绪阻断）；
        仍走 U05 的 :class:`DecisionReason`，因此码域与九码完全一致。

        主因由 :func:`classify_no_open` **按性质**决定（故障 > 边际 > 已处理 >
        范围界定），不按出现次数投票，也不用 U05 的固定优先级直接压——否则
        "一张范围外的卡"（``READINESS_BLOCKED``）会把真实的 ``NO_EDGE`` 挤成主因
        （B-r1 P0-4 反向误标）。
        """
        codes = list(self.reason_counts)
        codes.extend(str(code) for code in extra_codes if str(code or "").strip())
        code_list = list(dict.fromkeys(codes)) or [REASON_NO_EDGE]
        chosen = primary or self.verdict_pair(extra_codes=extra_codes)[1]
        payload = tuple(self.detail_counts.most_common())
        return DecisionReason(
            primary=chosen,
            secondary=tuple(code for code in code_list if code != chosen),
            details=tuple([f"{code}x{count}" for code, count in payload][:20])
            + tuple(str(item) for item in details if str(item or "").strip()),
        )

    def verdict_pair(self, *, extra_codes: Iterable[str] = ()) -> tuple[str, str]:
        """``(verdict, primary_reason)``；顶层结论与内嵌 ``no_open`` **共用**它，
        因此同一份工件不可能给出两个互相打脸的结论（B-r1 P0-4c）。"""
        return classify_no_open(
            details=self.detail_counts,
            codes=(*self.reason_counts, *[str(code) for code in extra_codes
                                          if str(code or "").strip()]),
            opened=self.opened,
            evaluated=self.evaluated,
        )

    def no_open_explanation(self, *, extra_codes: Iterable[str] = ()) -> dict[str, Any]:
        """任意一轮的"为什么没开"答复（F09：扫描异常不得报全成功）。

        ``verdict``：
        - ``opened``：本轮有开仓意图，``reasons`` 只是"其余候选为什么没开"；
        - ``no_edge``：候选**评估过**了，就是没有可下单边际；
        - ``handled``：本轮的动作都在"已在处理"（已持仓/已消费），不是没有信号；
        - ``blocked``：本轮有**故障**（数据/校验/就绪/模型/配置），
          **不得**读成"没有信号"；
        - ``no_candidates``：一个候选都没评估过（全部停在前置筛或本轮没有候选）；
        - ``unavailable``：读侧数据不可信（被篡改/无法解析），**不猜**结论。

        ``extra_codes`` 是漏斗之外的九码（源级错误 / 预算 / 证据门禁 / 模型异常）；
        给了就让判定看**整轮**，而不是只看漏斗内部。
        """
        if self.report_corrupt_reason:
            return {
                "round_id": self.round_id,
                "verdict": VERDICT_UNAVAILABLE,
                "opened": self.opened,
                "candidates": self.total_candidates,
                "evaluated": self.evaluated,
                "primary_reason": "",
                "reason_codes": [],
                "reasons": [],
                "details": [],
                "stopped_at": {},
                "reached": {},
                "round_faults": [],
                "error": self.report_corrupt_reason,
            }
        verdict, primary = self.verdict_pair(extra_codes=extra_codes)
        return {
            "round_id": self.round_id,
            "verdict": verdict,
            "opened": self.opened,
            "candidates": self.total_candidates,
            "evaluated": self.evaluated,
            "primary_reason": primary,
            "reason_codes": list(self.explanation(extra_codes=extra_codes,
                                                  primary=primary).codes),
            # 按 U05 的九码优先级排序（同一份数据每次呈现同一顺序，便于比对）。
            "reasons": [
                {"reason": code, "count": self.reason_counts[code]}
                for code in REASON_ORDER if self.reason_counts.get(code)
            ],
            "details": [
                {"detail": code, "reason": DETAIL_REASON.get(code, ""),
                 "nature": DETAIL_NATURE.get(code, ""), "count": count}
                for code, count in self.detail_counts.most_common()
            ],
            "stopped_at": {stage: self.stopped_at[stage] for stage in GATE_STAGES
                           if self.stopped_at.get(stage)},
            "reached": {stage: self.reached[stage] for stage in STAGE_ORDER
                        if self.reached.get(stage)},
            "round_faults": [fault.as_dict() for fault in self.round_faults],
        }

    def as_dict(self) -> dict[str, Any]:
        """状态文件/看板用的紧凑摘要（计数精确，明细截断并标注）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "round_id": self.round_id,
            "rounds": list(self.round_ids[-8:]),
            "opened": self.opened,
            "candidates": self.total_candidates,
            "evaluated": self.evaluated,
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
                         "nature": DETAIL_NATURE.get(code, ""), "count": count}
                        for code, count in self.detail_counts.most_common()],
            "round_faults": [fault.as_dict() for fault in self.round_faults],
            "report_corrupt_reason": self.report_corrupt_reason,
            "no_open": self.no_open_explanation(),
            "notes": list(self.notes[:20]),
            "items": [item.as_dict() for item in self.items],
            "items_truncated": int(self.truncated),
        }


def funnel_from_report(payload: Mapping[str, Any] | None) -> ScanFunnel:
    """从状态文件里的紧凑摘要**重建**漏斗（跨轮/重启读侧）。

    重建只承诺计数与原因码（明细在写入侧已按 :data:`MAX_ITEMS_IN_REPORT` 截断），
    因此看板/回归在"进程重启后"仍能回答"上一轮为什么没开"，而不是只剩 ``ok``。

    **不可信输入 fail-closed 但不抛异常**（B-r1 P3-2）：状态文件被手工篡改、
    塞进未登记的原因码/细分码时，这些条目被**丢弃**并置
    :attr:`ScanFunnel.report_corrupt_reason`；此后 :meth:`ScanFunnel.no_open_explanation`
    返回 ``verdict=unavailable``，**不猜**结论，也不会把展示路径打成 500。
    """
    data = dict(payload or {})
    funnel = ScanFunnel(round_id=str(data.get("round_id") or ""))
    dropped: list[str] = []

    def _keep_reason(value: Any) -> bool:
        text = str(value or "").strip()
        if text in REASON_ALL:
            return True
        dropped.append(f"reason:{text}"[:60])
        return False

    def _keep_detail(value: Any) -> bool:
        text = str(value or "").strip()
        if text in DETAIL_CODES:
            return True
        dropped.append(f"detail:{text}"[:60])
        return False

    funnel.reason_counts.update({
        str(entry.get("reason")): int(entry.get("count") or 0)
        for entry in data.get("reason_codes") or []
        if entry.get("reason") and _keep_reason(entry.get("reason"))
    })
    funnel.detail_counts.update({
        str(entry.get("detail")): int(entry.get("count") or 0)
        for entry in data.get("details") or []
        if entry.get("detail") and _keep_detail(entry.get("detail"))
    })
    funnel.stopped_at.update({
        str(stage): int(count or 0) for stage, count in (data.get("stopped_at") or {}).items()
        if str(stage) in STAGE_ORDER
    })
    funnel.reached.update({
        str(stage): int(count or 0) for stage, count in (data.get("reached") or {}).items()
        if str(stage) in STAGE_ORDER
    })
    funnel.passed_stages.update({
        str(stage): int(count or 0)
        for stage, count in (data.get("passed_stages") or {}).items()
        if str(stage) in STAGE_ORDER
    })
    funnel.throughput.update({
        str(stage): int(count or 0) for stage, count in (data.get("throughput") or {}).items()
        if str(stage) in STAGE_ORDER
    })
    for entry in data.get("round_faults") or []:
        if not isinstance(entry, Mapping):
            continue
        detail = str(entry.get("detail") or "").strip()
        if detail in ROUND_ONLY_DETAILS:
            funnel.round_faults.append(RoundFault(
                detail=detail, reason=DETAIL_REASON[detail],
                note=str(entry.get("note") or "")[:200],
                round_id=str(entry.get("round_id") or "")))
        else:
            dropped.append(f"round_fault:{detail}"[:60])
    funnel.round_ids.extend(str(item) for item in data.get("rounds") or [])
    funnel.notes.extend(str(item) for item in data.get("notes") or [])
    funnel.truncated = int(data.get("items_truncated") or 0)
    if dropped:
        reason = "状态文件含未登记条目，已丢弃: " + ", ".join(sorted(set(dropped))[:5])
        funnel.report_corrupt_reason = reason[:200]
        funnel.note(reason)
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
#: 取不到跨进程独占锁（另一进程正在写）：fail-closed，**不**消费。
LEDGER_BUSY = "LEDGER_BUSY"
#: 账本**写盘**本身失败（B-r4 N6）：磁盘满/只读挂载（``OSError``）、
#: ``os.replace`` 目标被占用（``PermissionError``）、存储层 ``OperationalError`` 等。
#: 与 :data:`LEDGER_BUSY` 同等待遇（fail-closed，**不**消费、不穿出执行器），
#: 区别只在"为什么写不进去"——底层异常名回带在
#: :attr:`TicketConsumption.error` 里，诊断不丢。
LEDGER_WRITE_FAILED = "LEDGER_WRITE_FAILED"
UNSEEN = "UNSEEN"

#: 账本保留窗口：超过这个时长仍未结算的 old 条目会被修剪（防止文件无限增长）。
DEFAULT_RETENTION_MS = 14 * 24 * 3600 * 1000

#: 跨进程独占锁的默认等待上限（秒）。U13 单实例锁在生产上限制了多写者，
#: 这里兜住"崩溃后人工重启与旧进程重叠""双实例误启"这两种真金白银的场景。
DEFAULT_LOCK_TIMEOUT_S = 10.0


class LedgerCorrupt(RuntimeError):
    """账本文件不可解析：拒绝新消费（fail-closed，宁可不追单也不重复消费）。"""


class LedgerLockTimeout(RuntimeError):
    """跨进程独占锁等待超时（另一个进程长时间持有账本）。"""


@contextlib.contextmanager
def exclusive_file_lock(path: str | Path, *, timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
                        poll_s: float = 0.02):
    """**跨进程**独占锁（Windows ``msvcrt.locking`` / POSIX ``fcntl.flock``）。

    锁文件是账本旁的 ``<name>.lock``（sidecar）。``self._lock``（RLock）只覆盖
    进程内线程，**不能**代替本函数——B-r1 P0-3 的核心就是这两者的区别。
    取不到锁时抛 :class:`LedgerLockTimeout`，由调用方决定是 fail-closed 还是报错。
    """
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if msvcrt is not None:  # pragma: no cover - Windows
            # **不要**往锁文件里写字节：先把文件撑到 1 字节再抢锁的写法本身有竞态
            # （A 已锁住字节 0，B 在 size==0 的窗口里写同一字节 → PermissionError，
            # 实测在统一离线回归的并发用例里稳定命中）。Windows 允许锁定越过 EOF
            # 的字节区间，所以直接锁空文件的字节 0。
            deadline = time.monotonic() + float(timeout_s)
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LedgerLockTimeout(
                            f"取不到跨进程锁（{timeout_s}s 超时）: {lock_path}") from None
                    time.sleep(poll_s)
        elif fcntl is not None:  # pragma: no cover - POSIX
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        try:
            if locked:
                if msvcrt is not None:  # pragma: no cover - Windows
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:  # pragma: no cover - POSIX
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass(frozen=True)
class TicketConsumption:
    """一次消费尝试的结果；``consumed=False`` 时 ``state`` 说明为什么。

    ``error`` 只在"账本写盘本身失败"（``state == LEDGER_WRITE_FAILED``）时非空，
    带上底层异常**类名**（如 ``OSError``/``PermissionError``）——调用方把它写进
    note/日志，运维不必再从一句泛泛的异常里猜原因（B-r4 N6）。
    """

    ticket_id: str
    state: str
    round_id: str = ""
    first_round_id: str = ""
    first_at_ms: int = 0
    error: str = ""

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
                "duplicate": self.duplicate, "error": self.error}


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
    #: 撤销留痕（B-r1 P1-2：``release()`` 原先"没有审计留痕"）。
    released_at_ms: int = 0
    release_round_id: str = ""
    release_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id, "signal_id": self.signal_id,
            "strategy_id": self.strategy_id, "symbol": self.symbol,
            "expires_at": self.expires_at, "emitted_at_ms": self.emitted_at_ms,
            "emitted_round_id": self.emitted_round_id,
            "consumed": bool(self.consumed),
            "consumed_at_ms": self.consumed_at_ms,
            "consumed_round_id": self.consumed_round_id,
            "released_at_ms": self.released_at_ms,
            "release_round_id": self.release_round_id,
            "release_count": self.release_count,
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
            released_at_ms=int(payload.get("released_at_ms") or 0),
            release_round_id=str(payload.get("release_round_id") or ""),
            release_count=int(payload.get("release_count") or 0),
        )


class TicketLedger:
    """``ticket_id`` 维度的**消费一次**账本；落盘可在重启后恢复。

    - :meth:`register`：扫描器发射 ticket 时登记（幂等；已是 consumed 的不会被复活）。
    - :meth:`consume`：执行器**真正要下单前**调用。首次返回 ``CONSUMED``，
      之后任何一轮/任何进程都返回 ``ALREADY_CONSUMED``（不回带 intent）。
    - :meth:`recover`：重启后列出"仍然有效且尚未消费"的 ticket —— 跨轮恢复。
    - 文件损坏（含被截断/被手工改写）时 :meth:`consume` 一律返回 ``LEDGER_CORRUPT``
      并**拒绝**新消费；**写路径也不洗白**：:meth:`register` / :meth:`save` 在损坏态
      直接抛 :class:`LedgerCorrupt`（B-r1 P1-1：原先"损坏 → 重新登记 → 落盘"会把
      损坏文件覆盖成一份只有新票的合法账本，旧消费事实一次性丢失）。
      真的要在损坏后继续，必须显式 :meth:`reset_corrupt`（会记 ``reset_at_ms`` 留痕）。

    线程安全：所有读写走 ``self._lock``（RLock，进程内）。
    **进程安全**（B-r1 P0-3，必达②原文要求）：跨进程写走
    :func:`exclusive_file_lock`（Windows ``msvcrt.locking`` / POSIX ``fcntl.flock``），
    ``consume`` 是"取锁 → 重读文件 → 判定 → 落盘"的**读-改-写原子**操作，
    因此两个进程不会同时拿到 ``CONSUMED``。落盘用 **pid+uuid 命名的临时文件** +
    ``os.replace``（原子替换；原先确定性 tmp 名在并发下直接 ``PermissionError``）。
    """

    def __init__(self, path: str | Path, *, clock: Any = None,
                 retention_ms: int = DEFAULT_RETENTION_MS,
                 lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S) -> None:
        self.path = Path(path)
        # 缺省钟用**墙钟毫秒**（与 ticket 的 ``expires_at`` 同一时基）；测试与
        # 离线重放一律显式传 ``at_ms``/``now_ms``，不读墙钟。
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.retention_ms = int(retention_ms)
        self.lock_timeout_s = float(lock_timeout_s)
        self._lock = threading.RLock()
        self._records: dict[str, TicketRecord] = {}
        self._corrupt: str = ""
        self._reset_at_ms: int = 0
        self._release_counts: Counter[str] = Counter()
        self.load()

    # -- 持久化 -------------------------------------------------------- #

    def _read_file_records(self) -> tuple[dict[str, TicketRecord], str, int]:
        """读盘（**不加锁**，必须在 :func:`exclusive_file_lock` 内或只读场景调用）。

        返回 ``(records, corrupt_reason, reset_at_ms)``；损坏时 ``records`` 为空且
        ``corrupt_reason`` 非空——调用方必须先看 corrupt 再决定怎么用。
        """
        if not self.path.exists():
            return {}, "", 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or int(raw.get("schema_version_ms", 0)) < 0:
                raise ValueError("ledger root 形态非法")
            records = raw.get("records")
            if not isinstance(records, list):
                raise ValueError("records 非列表")
            parsed = {
                str(item["ticket_id"]): TicketRecord.from_dict(item)
                for item in records if isinstance(item, Mapping) and item.get("ticket_id")
            }
            return parsed, "", int(raw.get("reset_at_ms") or 0)
        except Exception as exc:  # noqa: BLE001 - 损坏必须被兜住并可见
            return {}, f"{type(exc).__name__}: {exc}"[:200], 0

    def load(self) -> bool:
        """读盘；返回是否可解析。损坏时置 :attr:`corrupt_reason` 且**不**清空。"""
        with self._lock:
            records, corrupt, reset_at = self._read_file_records()
            if corrupt:
                self._corrupt = corrupt
                return False
            self._records = records
            self._reset_at_ms = reset_at
            self._corrupt = ""
            return True

    def _reload_locked(self) -> None:
        """跨进程锁内重读：文件里的**已消费事实**是权威（合并，不丢本进程新登记）。

        一旦判定损坏就**不再**自动复活——要恢复必须显式 :meth:`reset_corrupt`。
        """
        records, corrupt, reset_at = self._read_file_records()
        if corrupt:
            self._corrupt = corrupt
            return
        if self._corrupt:
            return
        for ticket_id, record in records.items():
            mine = self._records.get(ticket_id)
            if mine is None:
                self._records[ticket_id] = record
            elif record.consumed and not mine.consumed:
                # 另一个进程已经消费过：采纳它的事实（这才是"跨进程只消费一次"）。
                self._records[ticket_id] = record
        if reset_at > self._reset_at_ms:
            self._reset_at_ms = reset_at

    def _dump(self) -> dict[str, Any]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "schema_version_ms": 1,
            "reset_at_ms": int(self._reset_at_ms),
            "records": [record.as_dict() for record in self._records.values()],
        }

    def _save_locked(self) -> None:
        """原子落盘（必须在跨进程锁内）。临时文件名带 pid+uuid，避免并发互踩。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(self._dump(), ensure_ascii=False, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()

    def _locked(self):
        return exclusive_file_lock(self.path.with_name(self.path.name + ".lock"),
                                   timeout_s=self.lock_timeout_s)

    def save(self) -> None:
        """合并盘上事实后原子落盘。**损坏态拒绝写**（B-r1 P1-1）。"""
        with self._lock:
            if self._corrupt:
                raise LedgerCorrupt(
                    f"账本已判定损坏，拒绝落盘（需显式 reset_corrupt()）: {self._corrupt}")
            with self._locked():
                self._reload_locked()
                if self._corrupt:
                    raise LedgerCorrupt(
                        f"账本已判定损坏，拒绝落盘（需显式 reset_corrupt()）: {self._corrupt}")
                self._save_locked()

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
        """登记"这张 ticket 已被发射"。已存在的记录只补空字段，**不**重置消费事实。

        损坏态直接抛 :class:`LedgerCorrupt`：不允许用"重新登记本轮新票"把损坏文件
        洗成一份只有新票的合法账本（那会让旧消费事实静默丢失 → 同一 ticket 再消费）。
        """
        ticket_id = str(ticket.get("ticket_id") or "")
        if not ticket_id:
            raise ValueError("ticket_id 不能为空")
        stamp = int(self._clock() if at_ms is None else at_ms)
        with self._lock:
            if self._corrupt:
                raise LedgerCorrupt(
                    f"账本已判定损坏，拒绝登记新票据（需显式 reset_corrupt()）: {self._corrupt}")
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
                released_at_ms=int(existing.released_at_ms if existing else 0),
                release_round_id=str(existing.release_round_id if existing else ""),
                release_count=int(existing.release_count if existing else 0),
            )
            self._records[ticket_id] = record
            return record

    def consume(self, ticket_id: str, *, round_id: str = "",
                at_ms: int | None = None) -> TicketConsumption:
        """消费（下单前）这张 ticket；幂等且跨轮/**跨进程**。

        实现是"取跨进程锁 → 重读文件 → 判定 → 落盘"的读-改-写原子操作：
        两个进程同时消费同一张票时，**恰好一个**拿到 ``CONSUMED``，另一个拿到
        ``ALREADY_CONSUMED``（B-r1 P0-3）。取不到锁返回 ``LEDGER_BUSY``（fail-closed，
        不消费），损坏返回 ``LEDGER_CORRUPT``，**写盘本身失败**返回
        ``LEDGER_WRITE_FAILED``（同样 fail-closed，并把底层异常名回带在 ``error``）。

        **本方法不抛异常**（B-r4 N6）：``OSError``/``PermissionError`` 等写盘异常原先会
        一路穿出调用方（执行器），后果是"已经消费落盘的票据被丢掉、本轮已构造的
        intents 全部作废、调用方只得到一句泛泛的异常"——与 B-r3 N3 完全同形。
        """
        key = str(ticket_id or "")
        stamp = int(self._clock() if at_ms is None else at_ms)
        with self._lock:
            if self._corrupt:
                return TicketConsumption(ticket_id=key, state=LEDGER_CORRUPT,
                                         round_id=round_id)
            if not key:
                return TicketConsumption(ticket_id="", state=UNSEEN, round_id=round_id)
            try:
                with self._locked():
                    return self._consume_locked(key, stamp, round_id)
            except LedgerLockTimeout:
                return TicketConsumption(ticket_id=key, state=LEDGER_BUSY,
                                         round_id=round_id)
            except Exception as exc:  # noqa: BLE001 - 写盘异常不得穿出执行器
                # 磁盘满/只读挂载/``os.replace`` 目标被占用/存储层报错……一律与"取不到锁"
                # 同等待遇：fail-closed（本轮**不**下单这张票），异常类名进 ``error``。
                # 内存回滚已由 :meth:`_consume_locked` 在落盘处完成。
                return TicketConsumption(ticket_id=key, state=LEDGER_WRITE_FAILED,
                                         round_id=round_id, error=type(exc).__name__)

    def _consume_locked(self, key: str, stamp: int, round_id: str) -> TicketConsumption:
        self._reload_locked()
        if self._corrupt:
            return TicketConsumption(ticket_id=key, state=LEDGER_CORRUPT,
                                     round_id=round_id)
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
            released_at_ms=int(existing.released_at_ms if existing else 0),
            release_round_id=str(existing.release_round_id if existing else ""),
            release_count=int(existing.release_count if existing else 0),
        )
        # 消费事实必须在下单前**落盘**，否则崩溃窗口里"已消费"只活在内存里。
        try:
            self._save_locked()
        except Exception:  # noqa: BLE001 - 落盘失败先回滚内存，再交给调用方局部化
            # **没落盘就不算消费**：把上面预置的"已消费"回滚回 `existing`，否则这次的
            # 失败消费会留在内存里，被下一次成功的 `save()` 洗成"已消费"——一张从未
            # 下单的票被永久占死（B-r4 N6；与 N4 的"占死"是同一类伤害）。
            if existing is None:
                self._records.pop(key, None)
            else:
                self._records[key] = existing
            raise
        return TicketConsumption(ticket_id=key, state=CONSUMED, round_id=round_id,
                                 first_round_id=str(round_id), first_at_ms=stamp)

    def register_and_consume(self, ticket: Mapping[str, Any], *, round_id: str = "",
                             at_ms: int | None = None) -> TicketConsumption:
        """执行器入口：先登记再消费，保证"第一次消费"也带得出 ticket 事实。"""
        self.register(ticket, round_id=round_id, at_ms=at_ms)
        return self.consume(str(ticket.get("ticket_id") or ""), round_id=round_id,
                            at_ms=at_ms)

    def release(self, ticket_id: str, *, round_id: str = "", at_ms: int | None = None,
                force: bool = False) -> bool:
        """撤销一次**尚未下单**的消费（建意图失败等）。返回是否真的撤销了。

        门禁（B-r1 P1-2，原先"没有任何前置校验"，任何拿到账本对象的调用方都能撤销
        一次已落库的消费 → 二次下单）：

        - 损坏态一律 ``False``（fail-closed）；
        - 未消费 → ``False``；
        - **必须**给出 ``round_id``，且必须与 ``consumed_round_id`` 一致——
          跨轮/跨进程的撤销被拒绝。真的要在崩溃后人工撤销，必须显式
          ``force=True``（并会留下 ``released_at_ms``/``release_count`` 留痕）。

        ``consumed_round_id`` 为空（消费时没给轮次）时只要求给出 ``round_id``，
        因为"当次"已无法从账本自证；审计以留痕为准。
        """
        key = str(ticket_id or "")
        stamp = int(self._clock() if at_ms is None else at_ms)
        with self._lock:
            if self._corrupt:
                return False
            existing = self._records.get(key)
            if existing is None or not existing.consumed:
                return False
            if not force:
                if not str(round_id or "").strip():
                    return False
                if (existing.consumed_round_id
                        and str(round_id) != existing.consumed_round_id):
                    return False
            self._records[key] = TicketRecord(
                ticket_id=existing.ticket_id, signal_id=existing.signal_id,
                strategy_id=existing.strategy_id, symbol=existing.symbol,
                expires_at=existing.expires_at, emitted_at_ms=existing.emitted_at_ms,
                emitted_round_id=existing.emitted_round_id,
                consumed=False, consumed_at_ms=0, consumed_round_id="",
                released_at_ms=stamp, release_round_id=str(round_id),
                release_count=int(existing.release_count) + 1,
            )
            self._release_counts[str(round_id)] += 1
            try:
                with self._locked():
                    self._save_locked()
            except Exception:  # noqa: BLE001 - 锁超时/写盘异常都不得穿出（B-r4 N6）
                # 内存已撤销；落盘留给下一次成功写入。写盘异常（磁盘满/目标文件被占用）
                # 原先会穿出整个执行器——后果与 N3 同形（本轮 intents 全废），同样必须
                # 局部化。撤销事实以内存为准，`release_count` 留痕已生效。
                return True
            return True

    def orphan_consumed(self, ticket_has_order: Any = None) -> list[TicketRecord]:
        """**已消费但（据权威判定）没有对应订单**的票 —— 崩溃窗口里的"待补单"集合。

        B-r1 P1-2：模块原先没有这个查询，所以"审计库是已落库的权威、只补不重发"
        在模块内**不可验证**。``ticket_has_order`` 是审计库侧的权威判定
        （``AuditDatabase.signal_ticket_has_order`` 形态：``ticket_id -> bool``）；
        给了就只返回"消费了但查不到订单"的票，没给就返回全部已消费票
        （调用方自行用审计库收敛，模块**不**假装自己能判定）。
        """
        out: list[TicketRecord] = []
        with self._lock:
            for record in self._records.values():
                if not record.consumed:
                    continue
                if callable(ticket_has_order):
                    try:
                        if bool(ticket_has_order(record.ticket_id)):
                            continue
                    except Exception:  # noqa: BLE001 - 查不动就不敢宣称"没订单"
                        continue
                out.append(record)
        return sorted(out, key=lambda item: (item.consumed_at_ms, item.ticket_id))

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
        """显式放弃损坏账本（会留痕并**立即落盘**，否则下一次读盘又会看到损坏文件）。

        调用方应先确认 DB 侧 ``signal_ticket_has_order`` 能兜住去重，否则会把同一
        ticket 消费第二次。
        """
        with self._lock:
            self._corrupt = ""
            self._records = {}
            self._reset_at_ms = int(self._clock() if at_ms is None else at_ms)
            with self._locked():
                self._save_locked()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "path": str(self.path),
                "records": len(self._records),
                "consumed": sum(1 for record in self._records.values() if record.consumed),
                "orphan_consumed": len(self.orphan_consumed()),
                "releases": int(sum(self._release_counts.values())),
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


#: 仓位归属类别，**与 U02 ``PositionClass`` 同源**：直接取枚举值，不在本地写
#: 字面量（B-r1 P2-1：原先靠"字符串字面量巧合一致"，U02 改枚举值时会静默漂移）。
#: 有 ``strategy_id`` 或 ``ticket_id`` ⇒ ATTRIBUTED，否则 LEGACY_UNATTRIBUTED。
POSITION_CLASS_ATTRIBUTED = PositionClass.ATTRIBUTED.value
POSITION_CLASS_LEGACY = PositionClass.LEGACY_UNATTRIBUTED.value
POSITION_CLASSES: frozenset[str] = frozenset(
    {POSITION_CLASS_ATTRIBUTED, POSITION_CLASS_LEGACY})


def manager_of(position: Mapping[str, Any]) -> tuple[str, str]:
    """返回 ``(manager, detail)``：**谁在执行侧负责这个仓位**。

    判定只看**既有消费者用的同一批字段**（``source``/``strategy_id``/``ticket_id``），
    不新增列、不猜语义：

    - 有 ``strategy_id``/``ticket_id`` → 策略卡在管（``strategy``），detail 记来源；
    - 否则 ``source == "ai"`` → ``ai`` 通道在管，``source == "guardian"`` → 监护人；
    - 既无归属、``source`` 也未知/为空 → ``legacy_unattributed``。

    **注意两个正交事实**：``manager``（谁在管）与 :data:`POSITION_CLASS_*`
    （是否属于某个策略生命周期）不是同一件事。``source="ai"`` 且无策略归属的仓位
    既是"AI 通道在管"，也是 U02 语义下的 ``LEGACY_UNATTRIBUTED``——两者都要能读到，
    所以 :class:`PositionAttribution` **同时**给出 ``manager`` 与 ``position_class``，
    不用一个字段冒充另一个。
    """
    source = str(position.get("source") or "").strip().lower()
    strategy_id = str(position.get("strategy_id") or "").strip()
    ticket_id = str(position.get("ticket_id") or "").strip()
    if strategy_id or ticket_id:
        detail = "order_source=strategy" if source == MANAGER_STRATEGY else (
            f"strategy_attributed(source={source or 'unset'})")
        return MANAGER_STRATEGY, detail
    if source == MANAGER_AI:
        return MANAGER_AI, "ai_channel(no_strategy_attribution)"
    if source == MANAGER_GUARDIAN:
        return MANAGER_GUARDIAN, "guardian_inferred"
    return MANAGER_LEGACY, "no_source_no_strategy_attribution"


@dataclass(frozen=True)
class PositionAttribution:
    """一个仓位的管理责任：谁在管、属于哪个策略生命周期、用哪版规则开的。"""

    lifecycle_id: str
    symbol: str
    position_side: str
    manager: str
    manager_detail: str
    position_class: str
    strategy_id: str = ""
    ticket_id: str = ""
    rule_version: str = ""
    rule_known: bool = False

    @property
    def attributed(self) -> bool:
        return self.position_class == POSITION_CLASS_ATTRIBUTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id, "symbol": self.symbol,
            "position_side": self.position_side, "manager": self.manager,
            "manager_detail": self.manager_detail,
            "position_class": self.position_class, "attributed": self.attributed,
            "strategy_id": self.strategy_id,
            "ticket_id": self.ticket_id, "rule_version": self.rule_version,
            "rule_known": self.rule_known,
        }


def attribute_position(position: Mapping[str, Any],
                       cards: Iterable[Mapping[str, Any]] | None = None,
                       *, strategy_hash: str = "") -> PositionAttribution:
    """单仓归属；``rule_known=False`` 表示策略卡已不在当前卡册（不得冒充已知版本）。"""
    strategy_id = str(position.get("strategy_id") or "").strip()
    ticket_id = str(position.get("ticket_id") or "").strip()
    manager, detail = manager_of(position)
    rule_version = rule_version_of(strategy_id, cards, strategy_hash=strategy_hash)
    return PositionAttribution(
        lifecycle_id=str(position.get("lifecycle_id")
                        or ticket_id or position.get("symbol") or ""),
        symbol=str(position.get("symbol") or ""),
        position_side=str(position.get("side") or position.get("position_side") or ""),
        manager=manager, manager_detail=detail,
        position_class=(POSITION_CLASS_ATTRIBUTED if (strategy_id or ticket_id)
                        else POSITION_CLASS_LEGACY),
        strategy_id=strategy_id,
        ticket_id=ticket_id,
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
    """按管理者聚合 + 按归属类别聚合 + 列出"规则未知"的仓位。

    ``manager``（谁在管）与 ``position_class``（有无策略归属）是两个正交事实，
    所以要**分别**聚合（B-r1 P3-6：原先只按 manager 聚合，看板上"需要迁移的旧仓"
    不能一眼看出）。``legacy_pending`` 是"存在 LEGACY 仓位"的显式提示位。
    """
    by_manager: dict[str, list[dict[str, Any]]] = {}
    by_position_class: dict[str, list[dict[str, Any]]] = {}
    unknown: list[dict[str, Any]] = []
    for item in attributions:
        by_manager.setdefault(item.manager, []).append(item.as_dict())
        by_position_class.setdefault(item.position_class, []).append(item.as_dict())
        if not item.rule_known:
            unknown.append(item.as_dict())
    legacy = [item.as_dict() for item in attributions
              if item.position_class == POSITION_CLASS_LEGACY]
    return {
        "total": len(attributions),
        "by_manager": {manager: len(rows) for manager, rows in sorted(by_manager.items())},
        "by_position_class": {cls: len(rows)
                             for cls, rows in sorted(by_position_class.items())},
        "legacy_pending": len(legacy),
        "legacy_positions": legacy,
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
    2. **预留池不被研究触碰**：研究侧**永远**碰不到管理预留那一份，
       即 ``management_used + management_available >= management_reserve`` 恒成立。
       （**注意措辞**：不是"``claim_research`` 后 ``management_available`` 不下降"——
       研究取用自然会减少全池剩余，B-r1 P2-4 指出原 docstring 的说法与实现不符。）
    3. **可见的让路**：研究池为 0 时 :meth:`research_reason` 给出
       ``MODEL_DEADLINE`` + ``MANAGEMENT_PRIORITY_BUDGET``；总预算为 0/负时给出
       ``MODEL_DEADLINE`` + ``RESEARCH_BUDGET_ZERO``（"没有预算可谈"与"预算被管理
       占用"是两件事），都不是静默的"这轮没信号"。
    """

    total_budget: int
    management_reserve: int
    label: str = "cycle"
    management_used: int = 0
    research_used: int = 0
    management_demands: int = 0
    #: 管理侧**没拿到**的累计量（>0 表示研究侧/预算上限把管理挤下去过）。
    management_shortfall: int = 0
    #: 实际生效的预留比例（非法比例退化为 0 预留时这里显式为 0，B-r1 P3-3）。
    reserve_ratio_effective: float = 0.0

    def __post_init__(self) -> None:
        self.total_budget = max(0, int(self.total_budget))
        reserve = max(0, min(int(self.management_reserve), self.total_budget))
        self.management_reserve = reserve
        self.management_used = max(0, int(self.management_used))
        self.research_used = max(0, int(self.research_used))
        self.management_shortfall = max(0, int(self.management_shortfall))
        self.reserve_ratio_effective = (
            min(1.0, reserve / self.total_budget) if self.total_budget > 0 else 0.0)

    @classmethod
    def from_total(cls, total_budget: int, *,
                   reserve_ratio: float = DEFAULT_MANAGEMENT_RESERVE_RATIO,
                   reserve_min: int = 1, reserve_max: int | None = None,
                   label: str = "cycle") -> "ManagementPriorityPlan":
        """按比例切分；比例非法（非有限/<=0/>1）时退化为"全部留给研究"的显式形态。

        退化时 ``management_reserve=0`` 是**显式可见**的：``as_dict()`` 里有
        ``reserve_ratio_effective``（实际生效比例，退化时为 0.0），不是静默把管理饿死。
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
        """研究池被**管理占用**吃光（总预算为 0/负不算：那是 :attr:`no_budget`）。"""
        return self.research_available <= 0 and self.total_budget > 0

    @property
    def no_budget(self) -> bool:
        """总预算为 0/负：**没有预算可谈**（与"预算被管理占用"是两件事）。"""
        return self.total_budget <= 0

    # -- 写 ----------------------------------------------------------- #

    def claim_management(self, amount: int) -> int:
        """管理侧取用；可取到把研究池吃光（管理优先）。返回实际取到的量。"""
        want = max(0, int(amount))
        if want:
            self.management_demands += 1
        granted = min(want, self.management_available)
        self.management_shortfall += want - granted
        self.management_used += granted
        return granted

    def claim_research(self, amount: int) -> int:
        """新仓研究取用；**永不**触碰管理预留池。返回实际取到的量。"""
        granted = min(max(0, int(amount)), self.research_available)
        self.research_used += granted
        return granted

    def meets_management_demand(self) -> bool:
        """管理侧是否**每一次请求都拿到了它要的量**（DC10：不允许研究把管理挤下去）。

        B-r1 P2-2：原先的实现是 ``management_demands == 0 or research_available >= 0``，
        而 ``research_available`` 是 ``max(0, ...)`` 恒 >= 0 ⇒ **恒真空转**。现在按
        :meth:`claim_management` 累计的"没拿到的量"判定，是一个真有约束力的谓词。
        """
        return int(self.management_shortfall) == 0

    def research_reason(self) -> DecisionReason | None:
        """研究侧拿不到预算时的原因码；有预算时返回 ``None``（不硬塞原因）。

        - 总预算为 0/负 → ``MODEL_DEADLINE`` + ``RESEARCH_BUDGET_ZERO``
          （B-r1 P2-3：原先这种情况静默返回 ``None``，上层只能读成"没有信号"）；
        - 研究池被管理占用吃光 → ``MODEL_DEADLINE`` + ``MANAGEMENT_PRIORITY_BUDGET``。
        """
        if self.no_budget:
            return DecisionReason.from_codes(
                [REASON_MODEL_DEADLINE],
                details=(DETAIL_RESEARCH_BUDGET_ZERO,
                         f"total={self.total_budget}",
                         f"management_reserve={self.management_reserve}",
                         f"management_used={self.management_used}"),
            )
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
            "reserve_ratio_effective": self.reserve_ratio_effective,
            "management_used": self.management_used,
            "research_used": self.research_used,
            "management_available": self.management_available,
            "research_available": self.research_available,
            "management_demands": self.management_demands,
            "management_shortfall": self.management_shortfall,
            "meets_management_demand": self.meets_management_demand(),
            "no_budget": self.no_budget,
            "research_starved": self.research_starved,
        }


# --------------------------------------------------------------------------- #
# 七、逐轮解释（把漏斗 + 预算 + 持仓合成一句"为什么"）
# --------------------------------------------------------------------------- #
def source_error_reason(source_errors: Sequence[Any] = (),
                        *, labels: Sequence[str] = (),
                        degraded: bool = False) -> DecisionReason | None:
    """数据适配层的**源级错误** → 原因码（U05 ``reason_from_source_errors`` 的接线点）。

    U05 的补丁包 §二.4 明确把这条链路的接线留给 U08（``market_query`` 侧适配被
    撤回，理由是该文件被 U03 的双态测试按内容锁定）。本函数是漏斗侧的入口：
    数据适配层只负责"我取不到哪些源"，是否"没有边际"仍由漏斗/执行侧决定。
    """
    return reason_from_source_errors(source_errors, labels=labels, degraded=degraded)


def round_explanation(
    *,
    funnel: ScanFunnel | None = None,
    plan: ManagementPriorityPlan | None = None,
    positions: Iterable[Mapping[str, Any]] = (),
    channel_conflicts: Sequence[str] = (),
    readiness_blockers: Sequence[str] = (),
    data_unavailable: Sequence[str] = (),
    source_errors: Sequence[Any] = (),
    source_labels: Sequence[str] = (),
    source_degraded: bool = False,
    evidence_report: Any | None = None,
    model_error: BaseException | None = None,
) -> dict[str, Any]:
    """一轮的完整原因归集：**复用** U05 的 ``classify_cycle``，不另建分类。

    ``hold_existing`` 由持仓事实决定（确有一笔在管仓位），``intents`` 由漏斗的
    开仓计数决定（漏斗到达终态才算"有边际"）——两者都是**结构化事实**，
    本函数不猜。

    ``source_errors`` 是数据适配层收集的源级错误（U05 ``reason_from_source_errors``
    的入参形态）；给了就并入原因码，与 :func:`source_error_reason` 同一条链路。
    """
    # 延迟导入：避免与 U05 的循环引用风险，同时保持"九码只有一处定义"。
    from core.decision_trace import classify_cycle

    reasons: list[str] = []
    details: list[str] = []
    if funnel is not None:
        own = funnel.explanation()
        reasons.extend(own.codes)
        details.extend(own.details)
    data_reason = source_error_reason(source_errors, labels=source_labels,
                                      degraded=source_degraded)
    if data_reason is not None:
        reasons.extend(data_reason.codes)
        details.extend(data_reason.details)
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
    # **顶层结论与内嵌 no_open 共用同一次判定**（B-r1 P0-4c）：先按性质算出
    # `(verdict, primary)`，再用它构造 DecisionReason 与 no_open——否则
    # "故障只在漏斗之外发生"（源级错误/证据门禁/模型异常）时会出现
    # "顶层 blocked、内嵌 no_edge"两个互相打脸的结论。
    merged_codes = [*reasons, *reason.codes]
    if funnel is not None:
        verdict, primary = funnel.verdict_pair(extra_codes=merged_codes)
        if funnel.opened:
            verdict, primary = VERDICT_OPENED, REASON_HOLD_EXISTING
    else:
        # 没有漏斗（调用方只给了外部事实）：按九码性质判定；有码即视为
        # "本轮确实评估过"，无码则视为没有候选。
        verdict, primary = classify_no_open(
            codes=merged_codes, opened=0, evaluated=1 if merged_codes else 0)
    merged = DecisionReason(
        primary=primary,
        secondary=tuple(code for code in dict.fromkeys(merged_codes) if code != primary),
        details=tuple(dict.fromkeys((*details, *reason.details))),
    )
    no_open = (funnel.no_open_explanation(extra_codes=[*reasons, *reason.codes])
               if funnel is not None else None)
    if no_open is not None:
        # 内嵌块与顶层逐字一致（不是各算一次）。
        no_open = {**no_open, "verdict": verdict, "primary_reason": primary}
    return {
        "reason": merged.as_dict(),
        "primary_reason": primary,
        "verdict": verdict,
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
    "DETAIL_NATURE",
    "DETAIL_REASON",
    "DETAIL_STAGES",
    "EVALUATION_STAGES",
    "FUNNEL_REPORT_KEY",
    "NATURES",
    "NATURE_EDGE",
    "NATURE_FAULT",
    "NATURE_HANDLED",
    "NATURE_SCOPE",
    "ORIGIN_ROUND",
    "POSITION_CLASS_ATTRIBUTED",
    "POSITION_CLASS_LEGACY",
    "PRE_GATE_STAGES",
    "ROUND_ONLY_DETAILS",
    "REASON_NATURE",
    "RoundFault",
    "DETAIL_LEDGER_SAVE_DEFERRED",
    "VERDICTS",
    "VERDICT_BLOCKED",
    "VERDICT_HANDLED",
    "VERDICT_NO_CANDIDATES",
    "VERDICT_NO_EDGE",
    "VERDICT_OPENED",
    "VERDICT_UNAVAILABLE",
    "bucket_reason_codes",
    "classify_no_open",
    "FunnelItem",
    "LEDGER_BUSY",
    "LEDGER_CORRUPT",
    "LEDGER_SCHEMA_VERSION",
    "LEDGER_WRITE_FAILED",
    "LedgerCorrupt",
    "LedgerLockTimeout",
    "exclusive_file_lock",
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
    "source_error_reason",
    "validate_detail_table",
]
