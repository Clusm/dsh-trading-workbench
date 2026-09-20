"""U03 逐意图可验证研究证据（DL02 / DL04 / S01 分源证据质量）。

本模块是 **K02 冻结契约** 与运行时行情工具之间的适配层，只做四件事：

1. **快照规范化（有时效 + finality + 逐源质量）**
   把 ``query_symbol_detail`` 的返回体（已收盘 K 线 + ticker/mark-funding/OI 分源
   可用性与时点）规范化成可直接喂给 ``build_bundle`` 的输入记录。每个子源保留
   ``available / source_at_ms / observed_at_ms / fresh_until_ms / missing_fields``；
   **缺失的子源一律保持 ``None``，绝不折算成 0**——资金费缺失不等于"费率中性"，
   标记价/OI 缺失不等于 0。
2. **逐意图独立证据包（F09 证据污染）**
   以 ``(symbol, interval)`` 为单位为每个 intent **独立**构建 K02 证据包并独立
   出 verdict。两个意图即使同币不同周期也不共享包；缺证据的意图拿不到另一个
   意图的 VALID（verdict 绑定自己的 intent_id/symbol/evidence_id）。
3. **程序生成有限结构化 claim + 数值矛盾确定性拒绝（程序基准不可被模型替换）**
   比较基准**始终**由程序从 ``CLAIM_KIND_REGISTRY`` 的三种 kind 生成
   （``close_above_prior_high / close_above_price / close_below_price``），比较
   对象是**真实快照数值**；``744.13 > 744.9``、``78550 < 78443`` 这类模型叙述
   与实际数值矛盾时确定性拒绝，不因自由文本解释通过。

   模型自报 ``intent["claims"]`` **只能作为附加项**：程序 claim 一律并入，
   与程序生成结果同 kind 但 ``expr``/``expected`` 不一致的（即试图替换比较
   基准或删掉必需真实数值的）一律 ``CLAIM_DECLARATION_REJECTED:override:<kind>``；
   畸形声明（非序列、元素非 Mapping、``expr`` 非法）一律
   ``CLAIM_DECLARATION_REJECTED`` 结构化拒绝，不崩溃。OPEN 动作没有可解析的
   有限触发价（入场价）时判 ``MISSING_TRIGGER_PRICE``——程序无法生成比较基准
   就不放行，模型不能靠自报 claim 自证。
4. **模板化事实摘要 + 模型事实覆盖拒绝**
   事实摘要由程序按固定模板渲染（缺失渲染为 ``MISSING``）；intent 顶层或
   ``evidence`` 子字典里出现任何行情事实字段（``last_price/close/high/facts/...``）
   一律 ``MODEL_FACT_OVERRIDE``，模型解释不得覆盖行情事实。
5. **身份未固定即 fail-closed**
   ``identity`` 缺省（U15 未接线）时 ``identity_pinned=False``，且每个 OPEN
   意图判 ``IDENTITY_UNPINNED`` 进入丢单集合；只有调用方显式声明
   ``allow_unpinned=True``（仅供离线条测）才不因身份未固定丢单，且
   ``decision_gate()["ok"]`` 仍为 False。缺省拒绝，不靠调用方约定。
   **未固定不论形态**：显式传入模块自带哨兵 ``UNPINNED_IDENTITY``、它的
   ``model_copy`` 副本、等值裸 dict，或 ``environment="unpinned"`` 的手工构造，
   一律按 :func:`is_unpinned_identity` 判为未固定（见该函数 docstring）。
6. **逐意图证据绑定可追溯**
   每个 intent 得到**自己的** ``evidence_id``（种子含 intent_id/轮次/输入摘要），
   同轮同币同周期两单不再共享同一 id；``binding_id`` 额外绑定身份哈希与轮次。
   ``decision_gate()["bindings"]`` 与 :meth:`ResearchEvidenceReport.bindings`
   把 (intent_id → evidence_id/binding_id) 回传执行侧落审计。

设计约束：

- **纯函数**：不联网、不读写运行目录、不构造 Broker、不启动服务、不改风险档/
  白名单/live 开关，也不执行任何工具（注入文本无法借此改变工具权限，DC12）。
- 非 OPEN 动作（CLOSE/REDUCE/ADJUST）不属于本模块门禁，按 K04 管理授权契约单独
  处理：本模块把它们记为 ``NOT_APPLICABLE`` 且 **不阻断**（不误伤退出）。
  **本模块不为退出动作产出任何证据绑定**（``evidence_id``/``binding_id`` 为空），
  K02 README §6 要求的 ``ManagementIntent.evidence_id`` 需由 K04 侧提供。
- ``required_sources`` 支持源级（``mark_funding``）与字段级（``mark_funding.funding_rate``）
  两种写法；源级声明等价于"该源已声明字段全必需"——源"部分可用"（例如只有
  ``markPrice`` 没有 ``lastFundingRate``）同样拒绝，``missing_fields`` 不只是留痕。
  未声明时可用 ``default_required_sources`` 由策略侧给缺省集合（本模块不替策略
  猜默认值，缺省即空集合）。两者是**并集**语义：策略侧缺省是下限，意图自报只能
  加严，不能用更弱/更窄的自报集合顶掉策略口径。畸形声明（非序列、空条目、
  ``mark_funding.`` 尾点）一律 ``SOURCE_DECLARATION_REJECTED``，不静默降级。
- 新增 Decimal 一律经 ``decimal_from_value`` / ``decimal_from_float`` 适配；不改变
  既有 ``TradeIntent`` 字段与 SQLite REAL 口径，不触发全仓重构（R16）。

已知限制见 ``docs/dispatch/reviews/U03`` 提交包 ``known_limits``：本模块不承诺
全自然语言验真（R05）；K02 没有"live 源快照"输入类别（README §12 C#8），因此分源
快照只进包做审计、不作 claim 操作数，分源门禁由本模块的 ``required_sources`` 承担。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.contracts import (
    RuntimeIdentity,
    build_bundle,
    canonical_hash,
    decimal_from_value,
    decimal_text,
    resolve_operand,
    validate_bundle,
    validate_intent,
)
from core.contracts.k02_evidence import CLAIM_KIND_REGISTRY

SCHEMA_VERSION = "u03-research-evidence-2"
FACT_SUMMARY_TEMPLATE_VERSION = "u03-fact-summary-1"

#: U15 尚未接线时的未固定身份：内容哈希与逐意图绑定仍成立，但
#: "本轮身份已核对"这一条**不成立**，报告里以 ``identity_pinned=False`` 显式暴露。
#: 不允许把未固定身份当作已验证身份（U15/DL01 接线后必须传真实 RuntimeIdentity）。
UNPINNED_IDENTITY = RuntimeIdentity(
    schema_version="3.1",
    environment="unpinned",
    account_ref="unpinned",
    artifact_hash="unpinned-u03-artifact",
    git_commit="unpinned",
    dependency_hash="unpinned",
    effective_config_hash="unpinned",
    strategy_hash="unpinned",
    model_route_hash="unpinned",
    runtime_instance_id="u03-unpinned",
)


def is_unpinned_identity(identity: RuntimeIdentity) -> bool:
    """该身份是否属于"未固定身份"（哨兵本身或等值副本）。

    堵的是 B-P1-1 的同族逃逸：``identity is not None`` 会把模块自带的哨兵
    ``UNPINNED_IDENTITY``（或它的 ``model_copy`` / 等值裸 dict）判成"身份已固定"，
    报告随之输出 ``identity_pinned=True`` 的**虚假备份**、门禁整门放行。
    这里按语义判：字段全等 ⇒ 是哨兵；``environment`` 为 ``unpinned`` ⇒ 是哨兵族
    （手改 ``runtime_instance_id`` 之类的近等值副本同样不放过）。
    """
    if identity == UNPINNED_IDENTITY:
        return True
    return str(identity.environment).strip().lower() == UNPINNED_IDENTITY.environment


# ---------------------------------------------------------------- 常量与口径

#: 支持的 K 线周期与毫秒跨度（与 ``data.market_query.VALID_INTERVALS`` 同口径）。
INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

#: 分源证据质量：逐源 TTL 与可引用数值字段。
SOURCE_NAMES: tuple[str, ...] = ("ticker", "mark_funding", "open_interest")
SOURCE_TTL_MS: dict[str, int] = {
    "ticker": 30_000,
    "mark_funding": 60_000,
    "open_interest": 60_000,
}
SOURCE_VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    "ticker": ("last_price", "pct_24h", "quote_volume_24h"),
    "mark_funding": ("mark_price", "funding_rate"),
    "open_interest": ("open_interest_usd",),
}

DEFAULT_MAX_BAR_AGE_MS = 7_200_000
DEFAULT_MAX_SNAPSHOT_AGE_MS = 600_000
DEFAULT_EXPIRES_IN_MS = 60_000
DEFAULT_RR_TOLERANCE = "0.05"

#: 时效窗口的**冻结上界**（离线确定性，与默认值同族）：调用方放宽窗口不得越过，
#: 否则 10 天前的 K 线就能支撑一次新开仓。越界是调用方参数错误，直接 ValueError。
MAX_BAR_AGE_MS_CEILING = 86_400_000
MAX_SNAPSHOT_AGE_MS_CEILING = 3_600_000
MAX_EXPIRES_IN_MS_CEILING = 3_600_000

#: 模型不得提供的行情事实字段：出现即 ``MODEL_FACT_OVERRIDE``。
MODEL_FACT_KEYS: tuple[str, ...] = (
    "facts",
    "market_facts",
    "snapshot",
    "bars",
    "candles",
    "closed_close",
    "prior_high",
    "last_price",
    "last_closed_price",
    "close",
    "high",
    "low",
    "open",
)

#: U03 层确定性拒绝原因（不与 K02 reason_code 重名）。
REASON_MODEL_FACT_OVERRIDE = "MODEL_FACT_OVERRIDE"
REASON_SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
REASON_SNAPSHOT_UNUSABLE = "SNAPSHOT_UNUSABLE"
REASON_SNAPSHOT_FROM_FUTURE = "SNAPSHOT_FROM_FUTURE"
REASON_SNAPSHOT_STALE = "SNAPSHOT_STALE"
REASON_INTERVAL_MISSING = "INTERVAL_MISSING"
REASON_INTERVAL_UNSUPPORTED = "INTERVAL_UNSUPPORTED"
REASON_INTERVAL_MISMATCH = "INTERVAL_MISMATCH"
REASON_EVIDENCE_TIME_MISMATCH = "EVIDENCE_TIME_MISMATCH"
REASON_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
REASON_SOURCE_STALE = "SOURCE_STALE"
REASON_SOURCE_TIME_UNVERIFIED = "SOURCE_TIME_UNVERIFIED"
REASON_GEOMETRY_INVALID = "GEOMETRY_INVALID"
REASON_STOP_DIRECTION = "STOP_DIRECTION_INVALID"
REASON_TARGET_DIRECTION = "TARGET_DIRECTION_INVALID"
REASON_RR_CONTRADICTION = "RR_CONTRADICTION"
REASON_PRICE_NON_FINITE = "PRICE_NON_FINITE"
#: 模型自报 claim 被拒：声明形态畸形，或试图替换/删除程序生成的比较基准。
REASON_CLAIM_DECLARATION_REJECTED = "CLAIM_DECLARATION_REJECTED"
#: OPEN 无可解析的有限触发价（入场价）：程序无法生成比较基准，不放行。
REASON_MISSING_TRIGGER_PRICE = "MISSING_TRIGGER_PRICE"
#: U15 未接线：身份未固定，OPEN 缺省一律丢单（fail-closed）。
REASON_IDENTITY_UNPINNED = "IDENTITY_UNPINNED"
#: 同一批意图出现重复 intent_id：报告失去逐意图分辨力，重复项结构化拒绝。
REASON_DUPLICATE_INTENT_ID = "DUPLICATE_INTENT_ID"
#: intent 元素不是 Mapping（或无法规范化）：结构化拒绝该条，不崩溃整批。
REASON_INTENT_MALFORMED = "INTENT_MALFORMED"
#: 声明的必需源缺字段（字段级门禁），而不是只记 missing_fields。
REASON_SOURCE_FIELD_MISSING = "SOURCE_FIELD_MISSING"
#: 声明的必需字段名不在该源的已知字段表内（拼错也 fail-closed）。
REASON_SOURCE_FIELD_UNKNOWN = "SOURCE_FIELD_UNKNOWN"
#: intent 顶层 interval 与 evidence.confirmation_interval 冲突。
REASON_INTERVAL_CONFLICT = "INTERVAL_DECLARATION_CONFLICT"
#: 快照/bar 自带的周期标签与本次建包周期不一致（15m 不得冒充 4h）。
REASON_INTERVAL_MISLABELED = "INTERVAL_MISLABELED"
#: 工具出参没有周期标签：不猜，fail-closed。
REASON_INTERVAL_UNVERIFIED = "INTERVAL_UNVERIFIED"
#: 必需源声明形态畸形（非序列、空条目、``mark_funding.`` 尾点等）：拼错即 fail-closed，
#: 不再静默降级成"无门禁"（非序列）或"源级声明"（尾点）。
REASON_SOURCE_DECLARATION_REJECTED = "SOURCE_DECLARATION_REJECTED"

STATUS_VALID = "VALID"
STATUS_REJECTED = "REJECTED"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"

_CLAIM_PATTERN = re.compile(
    r"^\s*(?P<lhs>[^\s<>=!]+)\s*(?P<op>>=|<=|==|!=|>|<)\s*(?P<rhs>[^\s<>=!]+)\s*$"
)

_OPEN_ACTION = "OPEN"
_SIDES = ("LONG", "SHORT")
_CLAIM_KIND_BY_SIDE = {
    "LONG": ("close_above_price",),
    "SHORT": ("close_below_price",),
}
_BREAKOUT_KIND = "close_above_prior_high"


# ---------------------------------------------------------------- 数值适配器


def decimal_or_none(value: Any) -> Decimal | None:
    """显式数值适配：``None``/空串/非法文本/非有限值一律返回 ``None``。

    缺失**不**折算成 0：调用方拿到 ``None`` 必须自行决定拒绝还是标记未取得。
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = decimal_from_value(value)
    except Exception:
        return None
    if not parsed.is_finite():
        return None
    return parsed


def decimal_text_or_none(value: Any) -> str | None:
    parsed = decimal_or_none(value)
    return None if parsed is None else decimal_text(parsed)


def _bounded_window(value: Any, *, ceiling: int, name: str) -> int:
    """时效窗口必须落在冻结上界内；越界是调用方参数错误（非模型输入）。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数毫秒，收到 {value!r}") from exc
    if number < 0 or number > int(ceiling):
        raise ValueError(f"{name}={number} 超出冻结上界 {ceiling}")
    return number


# ---------------------------------------------------------------- 快照结构


@dataclass(frozen=True)
class SourceQuality:
    """单个子源的证据质量；缺失字段不出现在 ``values`` 中（不留 0）。"""

    source: str
    available: bool
    observed_at_ms: int
    source_at_ms: int | None = None
    fresh_until_ms: int | None = None
    missing_fields: tuple[str, ...] = ()
    values: Mapping[str, str] = field(default_factory=dict)

    def value(self, name: str) -> str | None:
        return self.values.get(name)

    def fresh_at(self, now_ms: int) -> bool:
        if self.fresh_until_ms is None:
            return False
        return int(now_ms) < int(self.fresh_until_ms)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "available": self.available,
            "observed_at_ms": self.observed_at_ms,
            "source_at_ms": self.source_at_ms,
            "fresh_until_ms": self.fresh_until_ms,
            "missing_fields": list(self.missing_fields),
            "values": dict(self.values),
        }


@dataclass(frozen=True)
class MarketSnapshot:
    """一次按需行情采集的规范化快照（单 symbol 单 interval）。"""

    symbol: str
    interval: str | None
    observed_at_ms: int
    as_of_ms: int
    bars: tuple[Mapping[str, Any], ...] = ()
    sources: Mapping[str, SourceQuality] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    #: 适配器 ``data_quality.missing_fields`` 的**交易所原名**域（只做审计留痕），
    #: 与规范名域 ``missing_fields`` 分开，避免一个缺失记两个名字。
    declared_missing_fields: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()
    ok: bool = True

    def source(self, name: str) -> SourceQuality | None:
        return self.sources.get(name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "observed_at_ms": self.observed_at_ms,
            "as_of_ms": self.as_of_ms,
            "ok": self.ok,
            "missing_fields": list(self.missing_fields),
            "declared_missing_fields": list(self.declared_missing_fields),
            "issues": list(self.issues),
            "sources": {name: item.as_dict() for name, item in self.sources.items()},
            "bar_count": len(self.bars),
        }


def bars_from_packed_candles(
    candles: Mapping[str, Any],
    *,
    interval: str,
    symbol: str,
    observed_at_ms: int,
    source: str = "exchange_public_kline",
    ttl_ms: int = DEFAULT_MAX_BAR_AGE_MS,
) -> tuple[dict[str, Any], ...]:
    """把 ``_pack_candles`` 的列式压缩还原成带收盘时间的 bar 记录。

    ``t_sec`` 是相对首根开盘时间的秒偏移，收盘时间 = 开盘时间 + 周期 - 1ms
    （币安 K 线收盘时间为包含端点）。周期未知时不猜时间，直接返回空（fail closed）。
    """
    span = INTERVAL_MS.get(str(interval))
    if span is None:
        return ()
    n = int(candles.get("n") or 0)
    if n <= 0:
        return ()
    t0 = candles.get("t0_epoch_ms")
    if t0 is None:
        return ()
    t0 = int(t0)
    ts = list(candles.get("t_sec") or [])
    opens = list(candles.get("o") or [])
    highs = list(candles.get("h") or [])
    lows = list(candles.get("l") or [])
    closes = list(candles.get("c") or [])
    volumes = list(candles.get("v") or [])
    rows: list[dict[str, Any]] = []
    for index in range(min(n, len(closes))):
        offset = int(ts[index]) if index < len(ts) else 0
        open_time_ms = t0 + offset * 1000
        close_time_ms = open_time_ms + span - 1
        values: dict[str, Any] = {}
        for name, series in (
            ("open", opens), ("high", highs), ("low", lows),
            ("close", closes), ("volume", volumes),
        ):
            if index < len(series) and series[index] is not None:
                text = decimal_text_or_none(series[index])
                if text is not None:
                    values[name] = text
        rows.append({
            "input_id": f"{symbol.lower()}-{interval}-{close_time_ms}",
            "symbol": symbol,
            "interval": interval,
            "source": source,
            "open_time_ms": open_time_ms,
            "close_time_ms": close_time_ms,
            # 事件的**发生时间**是开盘时刻：仍在形成的 bar 收盘时间在未来，
            # 若把收盘时间当作 event_time，会与接收时间冲突并触发 INPUT_TIME_ORDER。
            "event_time_ms": open_time_ms,
            "received_time_ms": int(observed_at_ms),
            "valid_until_ms": int(observed_at_ms) + int(ttl_ms),
            "finality": "closed" if close_time_ms <= int(observed_at_ms) else "live",
            "values": values,
        })
    return tuple(rows)


def _source_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    source: str,
    observed_at_ms: int,
    ttl_ms: int,
    time_keys: Sequence[str],
    value_map: Mapping[str, tuple[str, ...]],
) -> SourceQuality:
    """构造单源质量记录；缺失字段留在 ``missing_fields``，**不填 0**。

    数值字段同时接受"规范化名"（``last_price``）与"交易所原名"（``lastPrice``）；
    两者都不存在时该字段只出现在 ``missing_fields``，不出现在 ``values``。
    """
    payload = dict(payload or {})
    if payload.get("available") is False:
        payload = {}
    available = bool(payload)
    # 适配后的工具出参把数值放在 ``values`` 子字典里（含交易所原名字段）；
    # 旧口径则平铺在顶层。两种形态都按同一口径解析。
    nested = payload.get("values")
    if isinstance(nested, Mapping):
        for key, value in nested.items():
            payload.setdefault(key, value)
    source_at = None
    for key in time_keys:
        parsed = decimal_or_none(payload.get(key))
        if parsed is not None:
            source_at = int(parsed)
            break
    missing: list[str] = []
    values: dict[str, str] = {}
    for name, keys in value_map.items():
        text = None
        for key in (name, *keys):
            text = decimal_text_or_none(payload.get(key))
            if text is not None:
                break
        if text is None:
            missing.append(name)
        else:
            values[name] = text
    if not available and not missing:
        missing = list(value_map)
    # 源时钟不得晚于本地观测：取 max 会把未来时间洗白成"仍在有效期"。
    cap = (min(int(source_at), int(observed_at_ms)) if source_at is not None
           else int(observed_at_ms)) + int(ttl_ms)
    declared_until = decimal_or_none(payload.get("fresh_until_ms"))
    fresh_until: int | None = None
    if declared_until is not None:
        # 自报有效期只能**缩短**不能延长：数据生产者不得自证"仍然新鲜"。
        fresh_until = min(int(declared_until), cap)
    elif available:
        fresh_until = cap
    return SourceQuality(
        source=source,
        available=available,
        observed_at_ms=int(observed_at_ms),
        source_at_ms=source_at,
        fresh_until_ms=fresh_until,
        missing_fields=tuple(missing),
        values=values,
    )


#: 每个源的原始载荷键 -> 规范字段。缺失键保留为 missing（不折算 0）。
_SOURCE_SPECS: dict[str, tuple[tuple[str, ...], dict[str, tuple[str, ...]]]] = {
    "ticker": (
        ("source_at_ms", "closeTime", "time"),
        {
            "last_price": ("lastPrice",),
            "pct_24h": ("priceChangePercent",),
            "quote_volume_24h": ("quoteVolume",),
        },
    ),
    "mark_funding": (
        ("source_at_ms", "time"),
        {
            "mark_price": ("markPrice",),
            "funding_rate": ("lastFundingRate",),
        },
    ),
    "open_interest": (
        ("source_at_ms", "time"),
        {"open_interest_usd": ("openInterest",)},
    ),
}

#: ``data_quality.sources`` 里的键名（market_query 适配后的分源质量）。
_QUALITY_SOURCE_KEYS = {
    "ticker": "ticker",
    "mark_funding": "mark_funding",
    "open_interest": "open_interest",
}


def snapshot_from_tool_result(
    result: Mapping[str, Any],
    *,
    interval: str,
    observed_at_ms: int | None = None,
    source_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    max_bar_age_ms: int = DEFAULT_MAX_BAR_AGE_MS,
    include_candles_in_facts: bool = True,
) -> MarketSnapshot:
    """把 ``query_symbol_detail`` 返回体规范化为 :class:`MarketSnapshot`。

    ``result`` 是工具出参（含 ``candles``/``data_quality``/``funding_rate``/``mark_price``/
    ``open_interest_usd``）。缺失的子源不会变成 0：字段缺失时对应规范字段进入
    ``missing_fields``，``values`` 里根本没有该键。

    周期标签必须**自证**：出参自带的 ``interval`` 与调用方传入的 ``interval``
    不一致（或没有标签）时快照判不可用——1h 数据不得被标成 15m，缺标签也不猜。
    """
    max_bar_age_ms = _bounded_window(
        max_bar_age_ms, ceiling=MAX_BAR_AGE_MS_CEILING, name="max_bar_age_ms")
    symbol = str(result.get("symbol") or "").strip().upper()
    quality = result.get("data_quality")
    quality = dict(quality) if isinstance(quality, Mapping) else {}
    observed = observed_at_ms
    if observed is None:
        observed = quality.get("observed_at_ms")
    observed = int(observed) if observed is not None else 0

    issues: list[str] = []
    error = result.get("error")
    tool_ok = not error
    if error:
        issues.append(str(error))
    quality_issues = [str(item) for item in (quality.get("issues") or [])]
    issues.extend(quality_issues)

    # 周期标签交叉核对：出参自带标签与本次建包周期必须一致，否则 fail closed。
    declared_interval = result.get("interval")
    interval_verified = True
    if declared_interval is None or not str(declared_interval).strip():
        issues.append(f"{REASON_INTERVAL_UNVERIFIED}:{symbol}")
        interval_verified = False
    elif str(declared_interval).strip() != str(interval):
        issues.append(f"{REASON_INTERVAL_MISLABELED}:{declared_interval}!={interval}")
        interval_verified = False

    bars: tuple[Mapping[str, Any], ...] = ()
    if include_candles_in_facts and tool_ok:
        candles = result.get("candles")
        if isinstance(candles, Mapping):
            bars = bars_from_packed_candles(
                candles, interval=interval, symbol=symbol,
                observed_at_ms=observed, ttl_ms=max_bar_age_ms,
            )

    declared = quality.get("sources") if isinstance(quality.get("sources"), Mapping) else {}
    payloads = dict(source_payloads or {})
    # 工具出参的顶层同名字段是源载荷的第二来源（适配未落地时也能工作）。
    for name in SOURCE_NAMES:
        payload: dict[str, Any] = {}
        declared_item = declared.get(_QUALITY_SOURCE_KEYS[name]) if declared else None
        if isinstance(declared_item, Mapping):
            payload.update({k: v for k, v in declared_item.items()})
        if payload.get("available") is False:
            # 逐源元数据声明"不可用"是权威口径：即使工具出参里回带 0/旧值，
            # 也不得当成真实取得（这是 funding/OI 缺失被读成"费率中性"的根因）。
            payloads[name] = {}
            continue
        explicit = payloads.get(name)
        if isinstance(explicit, Mapping):
            payload.update(explicit)
        # 直接把工具出参里的规范字段补进源载荷：缺失的键不会被补 0。
        for field_name in SOURCE_VALUE_FIELDS[name]:
            if payload.get(field_name) is None and result.get(field_name) is not None:
                payload[field_name] = result.get(field_name)
        payloads[name] = payload

    sources: dict[str, SourceQuality] = {}
    missing_fields: list[str] = []
    for name in SOURCE_NAMES:
        time_keys, value_map = _SOURCE_SPECS[name]
        item = _source_from_payload(
            payloads.get(name),
            source=name,
            observed_at_ms=observed,
            ttl_ms=SOURCE_TTL_MS[name],
            time_keys=time_keys,
            value_map=value_map,
        )
        sources[name] = item
        missing_fields.extend(f"{name}.{field}" for field in item.missing_fields)
    declared_missing = quality.get("missing_fields")
    declared_missing_fields = tuple(
        dict.fromkeys(str(item) for item in declared_missing)
    ) if isinstance(declared_missing, (list, tuple)) else ()

    return MarketSnapshot(
        symbol=symbol,
        interval=interval,
        observed_at_ms=observed,
        as_of_ms=observed,
        bars=bars,
        sources=sources,
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        declared_missing_fields=declared_missing_fields,
        issues=tuple(dict.fromkeys(issues)),
        ok=bool(tool_ok and quality.get("ok", True) and interval_verified),
    )


def snapshot_from_records(
    *,
    symbol: str,
    interval: str,
    observed_at_ms: int,
    bars: Sequence[Mapping[str, Any]],
    source_payloads: Mapping[str, Mapping[str, Any]] | None = None,
    ok: bool = True,
    issues: Sequence[str] = (),
    max_bar_age_ms: int = DEFAULT_MAX_BAR_AGE_MS,
) -> MarketSnapshot:
    """由显式 bar 记录构造快照（夹具/离线重放入口，不经过工具出参）。

    bar 自带的 ``interval`` 必须与本次建包周期一致：异周期 bar（例如 4h 快照里
    夹一根 15m）既不入事实也不入包，整个快照判不可用（``INTERVAL_MISLABELED``）。
    """
    max_bar_age_ms = _bounded_window(
        max_bar_age_ms, ceiling=MAX_BAR_AGE_MS_CEILING, name="max_bar_age_ms")
    normalized: list[dict[str, Any]] = []
    mislabeled: list[str] = []
    for index, bar in enumerate(bars):
        declared_bar_interval = bar.get("interval")
        if declared_bar_interval is not None and str(declared_bar_interval) != str(interval):
            mislabeled.append(str(declared_bar_interval))
            continue
        values = {
            str(key): decimal_text_or_none(value)
            for key, value in (bar.get("values") or {}).items()
        }
        values = {key: value for key, value in values.items() if value is not None}
        close_time = bar.get("close_time_ms", bar.get("bar_close_time_ms"))
        close_time = int(close_time) if close_time is not None else None
        finality = str(bar.get("finality") or "closed")
        if close_time is None:
            finality = "unknown"
        open_time = bar.get("open_time_ms")
        if open_time is not None:
            event_time = int(open_time)
        elif close_time is not None and interval in INTERVAL_MS:
            event_time = close_time - INTERVAL_MS[interval] + 1
        elif close_time is not None:
            event_time = close_time
        else:
            event_time = int(observed_at_ms)
        item = {
            "input_id": str(bar.get("input_id") or f"{symbol.lower()}-{interval}-{index}"),
            "symbol": str(bar.get("symbol") or symbol),
            "interval": interval,
            "source": str(bar.get("source") or "exchange_public_kline"),
            "open_time_ms": open_time,
            "close_time_ms": close_time,
            "event_time_ms": int(bar.get("event_time_ms") or event_time),
            "received_time_ms": int(bar.get("received_time_ms") or observed_at_ms),
            "valid_until_ms": bar.get("valid_until_ms"),
            "finality": finality,
            "values": values,
        }
        if item["valid_until_ms"] is None and close_time is not None:
            item["valid_until_ms"] = int(observed_at_ms) + int(max_bar_age_ms)
        normalized.append(item)

    payloads = {name: dict(source_payloads.get(name) or {}) for name in SOURCE_NAMES} \
        if source_payloads else {}
    sources: dict[str, SourceQuality] = {}
    missing_fields: list[str] = []
    for name in SOURCE_NAMES:
        time_keys, value_map = _SOURCE_SPECS[name]
        item = _source_from_payload(
            payloads.get(name),
            source=name,
            observed_at_ms=observed_at_ms,
            ttl_ms=SOURCE_TTL_MS[name],
            time_keys=time_keys,
            value_map=value_map,
        )
        sources[name] = item
        missing_fields.extend(f"{name}.{field}" for field in item.missing_fields)

    all_issues = [str(item) for item in issues]
    if mislabeled:
        all_issues.append(
            f"{REASON_INTERVAL_MISLABELED}:{interval}<-{','.join(sorted(set(mislabeled)))}")
    return MarketSnapshot(
        symbol=symbol,
        interval=interval,
        observed_at_ms=int(observed_at_ms),
        as_of_ms=int(observed_at_ms),
        bars=tuple(normalized),
        sources=sources,
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        issues=tuple(dict.fromkeys(all_issues)),
        ok=bool(ok and not mislabeled),
    )


# ---------------------------------------------------------------- 规则与事实


def build_rules(
    *,
    interval: str,
    required_claim_kinds: Sequence[str],
    max_bar_age_ms: int = DEFAULT_MAX_BAR_AGE_MS,
    max_snapshot_age_ms: int = DEFAULT_MAX_SNAPSHOT_AGE_MS,
    expires_in_ms: int = DEFAULT_EXPIRES_IN_MS,
) -> dict[str, Any]:
    """构建验证方持有的 ``rules``；构建与校验必须传同一份（K02 整体哈希比对）。"""
    return {
        "required_intervals": [str(interval)],
        "required_claim_kinds": [str(item) for item in required_claim_kinds],
        "max_bar_age_ms": int(max_bar_age_ms),
        "max_snapshot_age_ms": int(max_snapshot_age_ms),
        "expires_in_ms": int(expires_in_ms),
        "source_ttls": dict(SOURCE_TTL_MS),
        "schema_version": SCHEMA_VERSION,
    }


def _source_inputs(snapshot: MarketSnapshot) -> list[dict[str, Any]]:
    """分源快照输入：可用源进包做审计（finality=live，不作 claim 操作数）。

    ``received_time_ms`` 固定为本地观测时刻：源自带时间晚于本地接收时间属于时钟
    冲突（F17），由 K02 的 ``INPUT_TIME_ORDER`` 结构化拒绝，不在这里悄悄抹平。
    """
    out: list[dict[str, Any]] = []
    for name in SOURCE_NAMES:
        item = snapshot.source(name)
        if item is None or not item.available:
            continue
        out.append({
            "input_id": f"src-{name}-{snapshot.symbol.lower()}-{snapshot.observed_at_ms}",
            "symbol": snapshot.symbol,
            "interval": None,
            "source": f"market_query.{name}",
            "close_time_ms": item.source_at_ms,
            "event_time_ms": item.source_at_ms or snapshot.observed_at_ms,
            "received_time_ms": snapshot.observed_at_ms,
            "valid_until_ms": item.fresh_until_ms,
            "finality": "live",
            "values": dict(item.values),
        })
    return out


def _quote_from_snapshot(
    snapshot: MarketSnapshot | None,
    symbol: str,
) -> dict[str, Any] | None:
    """执行报价只能来自真实 ticker 快照；取不到就不给报价（K02 会拒绝 OPEN）。"""
    if snapshot is None or snapshot.symbol != symbol:
        return None
    ticker = snapshot.source("ticker")
    if ticker is None or not ticker.available:
        return None
    price = ticker.value("last_price")
    if price is None or ticker.source_at_ms is None:
        return None
    return {
        "symbol": symbol,
        "price": price,
        "quote_time_ms": int(ticker.source_at_ms),
        "expires_at_ms": int(ticker.source_at_ms) + int(SOURCE_TTL_MS["ticker"]),
    }


def _caller_quote(
    quote_map: Mapping[str, Mapping[str, Any]],
    intent_id: str,
    symbol: str,
    now_ms: int,
) -> Mapping[str, Any] | None:
    """调用方注入的报价只作次选，且必须带新鲜时点；过期/未来时点直接丢弃。"""
    quote = quote_map.get(intent_id) or quote_map.get(symbol)
    if not isinstance(quote, Mapping):
        return None
    quote_time = decimal_or_none(quote.get("quote_time_ms"))
    if quote_time is None:
        return None
    age_ms = int(now_ms) - int(quote_time)
    if age_ms < 0 or age_ms > int(SOURCE_TTL_MS["ticker"]):
        return None
    return quote


def _interval_compatible(requested: str | None, snapshot: MarketSnapshot) -> bool:
    if requested is None:
        return False
    return str(requested) == str(snapshot.interval)


def _locate_snapshot(
    snapshots: Sequence[MarketSnapshot],
    symbol: str,
    interval: str | None,
) -> tuple[MarketSnapshot | None, list[str]]:
    """按 (symbol, interval) 精确定位快照；同币不同周期不得互相顶替。"""
    issues: list[str] = []
    if interval is None:
        return None, [REASON_INTERVAL_MISSING]
    if interval not in INTERVAL_MS:
        return None, [REASON_INTERVAL_UNSUPPORTED]
    for item in snapshots:
        if item.symbol == symbol and _interval_compatible(interval, item):
            return item, issues
    for item in snapshots:
        if item.symbol == symbol:
            return None, [REASON_INTERVAL_MISMATCH]
    return None, [REASON_SNAPSHOT_MISSING]


def _claim_token_bindings(
    claims: Sequence[Mapping[str, Any]],
    bundle: Any,
) -> tuple[str, ...]:
    refs: list[str] = []
    for claim in claims:
        expr = str(claim.get("expr") or "")
        for token in _claim_operands(expr):
            _, inputs, _ = resolve_operand(token, bundle)
            for input_id in inputs:
                if input_id not in refs:
                    refs.append(input_id)
    return tuple(refs)


def _claim_operands(expr: str) -> tuple[str, str]:
    match = _CLAIM_PATTERN.match(str(expr or ""))
    if not match:
        return (), ()
    return match.group("lhs"), match.group("rhs")


def generate_claims(
    *,
    side: str,
    interval: str,
    trigger_price: Any,
    breakout: bool,
) -> list[dict[str, Any]]:
    """由程序生成有限结构化 claim（数值来自快照事实与模型声明的触发价）。"""
    claims: list[dict[str, Any]] = []
    kind = _CLAIM_KIND_BY_SIDE.get(str(side).upper())
    if kind is None:
        return claims
    kind_name = kind[0]
    operator = CLAIM_KIND_REGISTRY[kind_name][0]
    price_text = decimal_text_or_none(trigger_price)
    if price_text is None:
        return claims
    claims.append({
        "claim_id": f"{kind_name}",
        "kind": kind_name,
        "expr": f"{interval}:closed_close {operator} {price_text}",
        "expected": True,
    })
    if breakout:
        op = CLAIM_KIND_REGISTRY[_BREAKOUT_KIND][0]
        claims.append({
            "claim_id": _BREAKOUT_KIND,
            "kind": _BREAKOUT_KIND,
            "expr": f"{interval}:closed_close {op} {interval}:prior_high",
            "expected": True,
        })
    return claims


def declared_claims_or_issues(
    value: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """模型自报 claim 的形态守卫：畸形一律结构化拒绝，不抛异常。

    返回 ``(可用的自报 claim, 拒绝理由)``。非序列声明、非 Mapping 元素、
    ``expr`` 不是合法比较表达式的元素都不进入"可用"列表，并留下 reason code。
    """
    if value is None:
        return [], []
    if not isinstance(value, (list, tuple)):
        return [], [f"{REASON_CLAIM_DECLARATION_REJECTED}:not_a_sequence"]
    out: list[dict[str, Any]] = []
    issues: list[str] = []
    for index, claim in enumerate(value):
        if not isinstance(claim, Mapping):
            issues.append(f"{REASON_CLAIM_DECLARATION_REJECTED}:element[{index}]")
            continue
        expr = claim.get("expr")
        if not isinstance(expr, str) or not _CLAIM_PATTERN.match(expr):
            issues.append(f"{REASON_CLAIM_DECLARATION_REJECTED}:expr[{index}]")
            continue
        out.append(dict(claim))
    return out, issues


def merge_claims(
    program_claims: Sequence[Mapping[str, Any]],
    declared: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """把自报 claim 合并成"程序基准 + 附加项"，程序基准不可被替换或删除。

    - 程序 claim 一律并入（模型无法用自报 claim 顶掉真实数值的比较基准）；
    - 自报 claim 与程序生成结果同 kind 时，``expr``/``expected`` 必须逐字段一致，
      否则 ``CLAIM_DECLARATION_REJECTED:override:<kind>``（试图替换对比基准）；
    - 其余自报 claim 作为**附加项**进入 K02，仍按同一套程序校验（操作数越界、
      不可解析、与事实不符一律拒绝）。
    """
    declared_claims, issues = declared_claims_or_issues(declared)
    merged: list[dict[str, Any]] = [dict(claim) for claim in program_claims]
    by_kind = {str(claim.get("kind") or ""): claim for claim in program_claims}
    added: set[tuple[str, str, Any]] = {
        (str(claim.get("kind") or ""), str(claim.get("expr") or ""),
         claim.get("expected", True)) for claim in merged
    }
    for claim in declared_claims:
        key = (str(claim.get("kind") or ""), str(claim.get("expr") or ""),
               claim.get("expected", True))
        base = by_kind.get(str(claim.get("kind") or ""))
        if base is not None:
            if key != (str(base.get("kind") or ""), str(base.get("expr") or ""),
                       base.get("expected", True)):
                issues.append(
                    f"{REASON_CLAIM_DECLARATION_REJECTED}:override:"
                    f"{claim.get('kind') or 'unknown'}")
            continue
        if key in added:
            continue
        added.add(key)
        merged.append(claim)
    return merged, issues


def evaluate_geometry(
    *,
    side: str,
    entry: Any,
    stop: Any,
    target: Any,
    reported_rr: Any,
    rr_tolerance: str | Decimal = DEFAULT_RR_TOLERANCE,
) -> tuple[dict[str, Any], list[str]]:
    """程序计算 entry/SL/TP 方向与 RR，并与模型自报 RR 比较（DL02）。"""
    resolved = {
        "side": str(side).upper(),
        "entry": decimal_text_or_none(entry),
        "stop_loss": decimal_text_or_none(stop),
        "take_profit": decimal_text_or_none(target),
        "reported_rr": decimal_text_or_none(reported_rr),
    }
    if resolved["entry"] is None or resolved["stop_loss"] is None or resolved["take_profit"] is None:
        resolved["computed_rr"] = None
        resolved["evaluated"] = False
        return resolved, []

    entry_v = Decimal(resolved["entry"])
    stop_v = Decimal(resolved["stop_loss"])
    target_v = Decimal(resolved["take_profit"])
    if not (entry_v.is_finite() and stop_v.is_finite() and target_v.is_finite()):
        resolved["evaluated"] = False
        return resolved, [REASON_PRICE_NON_FINITE]
    long_side = resolved["side"] == "LONG"
    issues: list[str] = []
    if long_side and stop_v >= entry_v:
        issues.append(REASON_STOP_DIRECTION)
    if not long_side and stop_v <= entry_v:
        issues.append(REASON_STOP_DIRECTION)
    if long_side and target_v <= entry_v:
        issues.append(REASON_TARGET_DIRECTION)
    if not long_side and target_v >= entry_v:
        issues.append(REASON_TARGET_DIRECTION)
    risk = abs(entry_v - stop_v)
    if risk <= 0:
        issues.append(REASON_GEOMETRY_INVALID)
        resolved["computed_rr"] = None
        resolved["evaluated"] = True
        return resolved, list(dict.fromkeys(issues))
    reward = abs(target_v - entry_v)
    computed_rr = reward / risk
    resolved["computed_rr"] = decimal_text(computed_rr)
    resolved["evaluated"] = True
    tolerance = decimal_from_value(rr_tolerance)
    reported = decimal_or_none(resolved["reported_rr"])
    if reported is not None and abs(computed_rr - reported) > tolerance:
        issues.append(REASON_RR_CONTRADICTION)
    return resolved, list(dict.fromkeys(issues))


# ---------------------------------------------------------------- intent 规范化


def _as_mapping(intent: Any) -> dict[str, Any]:
    if isinstance(intent, Mapping):
        return dict(intent)
    dump = getattr(intent, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    raise TypeError(f"intent must be a mapping or pydantic model, got {type(intent).__name__}")


def normalise_intent(intent: Any) -> dict[str, Any]:
    """把 TradeIntent(+DecisionEvidence) 或测试字典摊平成 U03 需要的字段。"""
    raw = _as_mapping(intent)
    evidence = raw.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    item = dict(raw)
    item["intent_id"] = str(raw.get("intent_id") or raw.get("client_order_id") or "").strip()
    item["symbol"] = str(raw.get("symbol") or "").strip().upper()
    item["action"] = str(raw.get("action") or "OPEN").upper()
    item["side"] = str(raw.get("side") or "").upper()
    declared_interval = raw.get("interval")
    evidence_interval = evidence.get("confirmation_interval")
    item["interval_conflict"] = (
        str(declared_interval), str(evidence_interval)
    ) if (
        declared_interval is not None and evidence_interval is not None
        and str(declared_interval).strip() != str(evidence_interval).strip()
    ) else None
    interval = declared_interval or evidence_interval
    item["interval"] = str(interval).strip() if interval else None
    trigger = raw.get("trigger_price")
    if trigger is None:
        trigger = evidence.get("trigger_price")
    item["trigger_price"] = trigger
    item["reported_rr"] = evidence.get("risk_reward_ratio", raw.get("risk_reward_ratio"))
    item["evidence_at_ms"] = evidence.get("data_observed_at_ms")
    item["breakout"] = bool(raw.get("breakout") or raw.get("breakout_confirm"))
    declared_sources, source_issues = required_sources_or_issues(
        raw.get("required_sources"))
    item["required_sources"] = declared_sources
    # 形态畸形的声明（非序列）不留白：理由是意图级拒绝，在主循环里并入 issues。
    item["required_sources_issues"] = tuple(source_issues)
    return item


def model_fact_overrides(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """扫描模型提供的行情事实字段（顶层 + ``evidence`` 子字典），去重保序。

    只深入 ``evidence`` 一层：它是 K02 意图里唯一被本模块当作"模型声明"读取的
    嵌套结构，``evidence.facts``/``evidence.close`` 这类夹带过去不会被发现。
    """
    found: list[str] = []
    for key in MODEL_FACT_KEYS:
        if key in raw and key not in found:
            found.append(key)
    evidence = raw.get("evidence")
    if isinstance(evidence, Mapping):
        for key in MODEL_FACT_KEYS:
            if key in evidence and key not in found:
                found.append(key)
    return tuple(found)


# ---------------------------------------------------------------- 逐意图结果


@dataclass(frozen=True)
class IntentEvidence:
    """单 intent 的证据结论；绑定自身的 intent/symbol/interval/evidence_id。"""

    intent_id: str
    symbol: str
    interval: str | None
    action: str
    evidence_id: str
    #: U03 层状态：有任一（本模块或 K02）拒绝理由即 ``REJECTED``；非 OPEN 为
    #: ``NOT_APPLICABLE``。不再出现 ``status=VALID`` 而 ``accepted=False`` 的自相矛盾。
    status: str
    #: K02 自己的 verdict 状态（原样保留，便于对照契约层结论）。
    k02_status: str
    accepted: bool
    applies_to_open: bool
    reason_codes: tuple[str, ...]
    bundle_reasons: tuple[str, ...]
    claims: tuple[Mapping[str, Any], ...] = ()
    geometry: Mapping[str, Any] = field(default_factory=dict)
    source_quality: Mapping[str, Any] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    summary: str = ""
    binding_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "interval": self.interval,
            "action": self.action,
            "evidence_id": self.evidence_id,
            "status": self.status,
            "k02_status": self.k02_status,
            "accepted": self.accepted,
            "applies_to_open": self.applies_to_open,
            "reason_codes": list(self.reason_codes),
            "bundle_reasons": list(self.bundle_reasons),
            "claims": [dict(item) for item in self.claims],
            "geometry": dict(self.geometry),
            "source_quality": dict(self.source_quality),
            "missing_fields": list(self.missing_fields),
            "summary": self.summary,
            "binding_id": self.binding_id,
        }


@dataclass(frozen=True)
class ResearchEvidenceReport:
    """一轮逐意图研究证据结论；供执行前门禁消费（不改风险档）。"""

    ok: bool
    cycle_id: str
    now_ms: int
    identity_hash: str
    identity_pinned: bool = True
    #: 调用方显式声明"允许未固定身份"（仅离线条测）；缺省 False 即 fail-closed。
    unpinned_allowed: bool = False
    intents: tuple[IntentEvidence, ...] = ()
    rejected_intent_ids: tuple[str, ...] = ()
    accepted_intent_ids: tuple[str, ...] = ()

    def by_intent(self, intent_id: str) -> IntentEvidence | None:
        for item in self.intents:
            if item.intent_id == intent_id:
                return item
        return None

    def bindings(self) -> tuple[dict[str, Any], ...]:
        """执行侧可携带/落的逐意图证据绑定（放行路径不再零审计）。"""
        return tuple(
            {
                "intent_id": item.intent_id,
                "symbol": item.symbol,
                "interval": item.interval,
                "evidence_id": item.evidence_id,
                "binding_id": item.binding_id,
                "status": item.status,
                "k02_status": item.k02_status,
                "accepted": item.accepted,
                "applies_to_open": item.applies_to_open,
                "cycle_id": self.cycle_id,
                "identity_pinned": self.identity_pinned,
            }
            for item in self.intents
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": self.ok,
            "cycle_id": self.cycle_id,
            "now_ms": self.now_ms,
            "identity_hash": self.identity_hash,
            "identity_pinned": self.identity_pinned,
            "unpinned_allowed": self.unpinned_allowed,
            "accepted_intent_ids": list(self.accepted_intent_ids),
            "rejected_intent_ids": list(self.rejected_intent_ids),
            "intents": [item.as_dict() for item in self.intents],
        }

    def decision_gate(self) -> dict[str, Any]:
        """执行前门禁视图：只丢未通过证据门禁的 OPEN，不影响退出。

        ``ok`` 同时要求身份已固定（除非调用方显式 ``allow_unpinned=True``）：
        消费方不能只看丢单列表就放行未固定身份下的开仓。
        """
        return {
            "ok": bool(self.ok and (self.identity_pinned or self.unpinned_allowed)),
            "identity_pinned": self.identity_pinned,
            "unpinned_allowed": self.unpinned_allowed,
            "drop_intent_ids": list(self.rejected_intent_ids),
            "reasons": {
                item.intent_id: list(item.reason_codes)
                for item in self.intents if not item.accepted
            },
            "bindings": {
                item.intent_id: {
                    "evidence_id": item.evidence_id,
                    "binding_id": item.binding_id,
                }
                for item in self.intents if item.accepted and item.applies_to_open
            },
        }


# ---------------------------------------------------------------- 摘要模板


def render_fact_summary(
    *,
    symbol: str,
    interval: str | None,
    evidence_id: str,
    status: str,
    facts: Mapping[str, Mapping[str, Any]],
    quote: Mapping[str, Any] | None,
    sources: Mapping[str, SourceQuality],
    claims: Sequence[Mapping[str, Any]],
) -> str:
    """模板化事实摘要：数值全部来自程序，缺失渲染 ``MISSING``，不含模型文本。"""
    lines = [
        f"[{FACT_SUMMARY_TEMPLATE_VERSION}] {symbol} {interval or 'NA'} "
        f"evidence={evidence_id} status={status}"
    ]
    if facts:
        rendered = "; ".join(
            f"{fact_id}={item.get('value')}@{item.get('bar_close_time_ms')}"
            for fact_id, item in sorted(facts.items())
        )
    else:
        rendered = "MISSING"
    lines.append(f"facts: {rendered}")
    if quote:
        lines.append(
            f"quote: price={quote.get('price') or 'MISSING'}@{quote.get('quote_time_ms')}"
        )
    else:
        lines.append("quote: MISSING")
    source_parts = []
    for name in SOURCE_NAMES:
        item = sources.get(name)
        if item is None or not item.available:
            source_parts.append(f"{name}=MISSING")
            continue
        values = ",".join(f"{k}={v}" for k, v in sorted(item.values.items())) or "no_fields"
        source_parts.append(f"{name}={values}@{(item.source_at_ms)}")
    lines.append("sources: " + "; ".join(source_parts))
    for claim in claims:
        lines.append(
            f"claim {claim.get('claim_id')}: {claim.get('expr')} -> "
            f"actual={claim.get('actual')} expected={claim.get('expected')} "
            f"status={claim.get('status')}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- 主入口


def required_sources_or_issues(value: Any) -> tuple[tuple[str, ...], list[str]]:
    """必需源声明的形态守卫（与 :func:`declared_claims_or_issues` 同口径）。

    非序列声明（例如裸字符串 ``"mark_funding"``）过去被静默摊成空元组 ⇒
    "声明了却完全无门禁"；这里改为结构化拒绝，拼错/形态错一律 fail-closed。
    ``None`` 表示"未声明"（合法），交给策略侧缺省集合。
    """
    if value is None:
        return (), []
    if not isinstance(value, (list, tuple)):
        return (), [f"{REASON_SOURCE_DECLARATION_REJECTED}:not_a_sequence"]
    return tuple(str(item) for item in value), []


def _resolve_required_sources(
    declared: Sequence[str],
    default_required_sources: Sequence[str],
) -> tuple[str, ...]:
    """意图自报与策略侧缺省取**并集**（去重保序，自报在前）。

    上一版是"自报非空即完全顶掉策略缺省"，被门禁者只要自报一个更弱集合就能降低
    策略口径（Q-NEW-4）。并集语义下策略缺省是下限，自报只能加严；两者都空 =
    该意图不声明源依赖（由 K02 的其他控制承担）。
    """
    merged = (str(item) for item in tuple(declared) + tuple(default_required_sources))
    return tuple(dict.fromkeys(merged))


def _source_gate(
    snapshot: MarketSnapshot | None,
    required_sources: Sequence[str],
    now_ms: int,
) -> list[str]:
    """字段级分源门禁：``<source>`` 或 ``<source>.<field>`` 都必须真正有值。

    - ``mark_funding``：源可用 + 有时点 + 新鲜 + **声明的字段一个不缺**；
      只有 ``markPrice`` 没有 ``lastFundingRate`` 时仍然拒绝——"声明依赖资金费"
      不能因为源对象存在就算满足。
    - ``mark_funding.funding_rate``：精确到字段，缺该字段即拒绝。
    """
    if snapshot is None or not required_sources:
        return []
    issues: list[str] = []
    for entry in required_sources:
        text = str(entry).strip()
        if not text:
            # 空条目过去被静默忽略；现在记一条理由（该意图整体拒绝）。
            issues.append(f"{REASON_SOURCE_DECLARATION_REJECTED}:empty")
            continue
        name, dot, field = text.partition(".")
        name = name.strip()
        field = field.strip()
        if not name or (dot and not field):
            # 形如 ``.funding_rate`` / ``mark_funding.``：拼错，不静默降级成源级声明。
            issues.append(f"{REASON_SOURCE_DECLARATION_REJECTED}:{text}")
            continue
        item = snapshot.source(name)
        if item is None or not item.available:
            issues.append(f"{REASON_SOURCE_UNAVAILABLE}:{name}")
            continue
        if item.source_at_ms is None:
            issues.append(f"{REASON_SOURCE_TIME_UNVERIFIED}:{name}")
            continue
        if not item.fresh_at(now_ms):
            issues.append(f"{REASON_SOURCE_STALE}:{name}")
            continue
        if field:
            if field not in SOURCE_VALUE_FIELDS.get(name, ()):
                issues.append(f"{REASON_SOURCE_FIELD_UNKNOWN}:{text}")
            elif item.value(field) is None:
                issues.append(f"{REASON_SOURCE_FIELD_MISSING}:{text}")
            continue
        for missing in item.missing_fields:
            issues.append(f"{REASON_SOURCE_FIELD_MISSING}:{name}.{missing}")
    return issues


def evaluate_open_intents(
    intents: Iterable[Any],
    snapshots: Sequence[MarketSnapshot] | Mapping[str, MarketSnapshot],
    *,
    now_ms: int,
    identity: RuntimeIdentity | Mapping[str, Any] | None = None,
    cycle_id: str = "",
    quotes: Mapping[str, Mapping[str, Any]] | None = None,
    max_bar_age_ms: int = DEFAULT_MAX_BAR_AGE_MS,
    max_snapshot_age_ms: int = DEFAULT_MAX_SNAPSHOT_AGE_MS,
    expires_in_ms: int = DEFAULT_EXPIRES_IN_MS,
    rr_tolerance: str | Decimal = DEFAULT_RR_TOLERANCE,
    allow_unpinned: bool = False,
    default_required_sources: Sequence[str] = (),
) -> ResearchEvidenceReport:
    """逐意图校验研究证据；``now_ms`` 必传（不使用 bundle.as_of_ms 兜底）。

    每个 OPEN intent 得到自己的证据包与 verdict；非 OPEN 动作不进本门禁。
    ``identity`` 缺省（U15 未接线）时报告置 ``identity_pinned=False``，且每个
    OPEN 判 ``IDENTITY_UNPINNED`` 进丢单集合（fail-closed）。只有调用方显式
    传 ``allow_unpinned=True``（离线条测）才不因身份未固定丢单，且此时
    ``decision_gate()["ok"]`` 仍为 False。
    """
    max_bar_age_ms = _bounded_window(
        max_bar_age_ms, ceiling=MAX_BAR_AGE_MS_CEILING, name="max_bar_age_ms")
    max_snapshot_age_ms = _bounded_window(
        max_snapshot_age_ms, ceiling=MAX_SNAPSHOT_AGE_MS_CEILING,
        name="max_snapshot_age_ms")
    expires_in_ms = _bounded_window(
        expires_in_ms, ceiling=MAX_EXPIRES_IN_MS_CEILING, name="expires_in_ms")
    if identity is None:
        resolution: RuntimeIdentity = UNPINNED_IDENTITY
    elif isinstance(identity, RuntimeIdentity):
        resolution = identity
    else:
        resolution = RuntimeIdentity.model_validate(dict(identity))
    # 未固定身份**不论以何形态传入**（缺省 None、模块自带哨兵 ``UNPINNED_IDENTITY``、
    # 等值裸 dict、``model_copy`` 副本）都必须判为未固定：否则 ``identity_pinned=True``
    # 就是对审计字段的虚假背书，且 ``decision_gate()['ok']`` 会整门放行。
    identity_pinned = not is_unpinned_identity(resolution)
    identity_hash = resolution.identity_hash()
    snapshot_list = list(snapshots.values()) if isinstance(snapshots, Mapping) else list(snapshots)
    quote_map = dict(quotes or {})
    results: list[IntentEvidence] = []
    rejected: list[str] = []
    accepted: list[str] = []
    raw_intents = list(intents)
    # 先数一遍绑定键：同一 id 出现两次以上时**所有**同 id 意图一起拒绝，
    # 否则 by_intent 只能返回第一条，报告失去逐意图分辨力。
    id_counts: dict[str, int] = {}
    for position, raw_intent in enumerate(raw_intents):
        try:
            probe = normalise_intent(raw_intent)
            counted_id = probe["intent_id"] or f"open[{position}]"
        except Exception:  # noqa: BLE001 - 与主循环同一口径，非 Mapping 走 invalid[position]
            counted_id = f"invalid[{position}]"
        id_counts[counted_id] = id_counts.get(counted_id, 0) + 1

    for position, raw_intent in enumerate(raw_intents):
        try:
            item = normalise_intent(raw_intent)
        except Exception as exc:  # noqa: BLE001 - 非 Mapping 元素结构化拒绝，不炸整批
            intent_id = f"invalid[{position}]"
            results.append(IntentEvidence(
                intent_id=intent_id, symbol="", interval=None, action="",
                evidence_id="", status=STATUS_REJECTED,
                k02_status="NOT_EVALUATED", accepted=False, applies_to_open=False,
                reason_codes=(f"{REASON_INTENT_MALFORMED}:{type(exc).__name__}",),
                bundle_reasons=(),
                summary=f"[{FACT_SUMMARY_TEMPLATE_VERSION}] intent[{position}] "
                        f"不是 Mapping，结构化拒绝（{type(exc).__name__}）",
            ))
            rejected.append(intent_id)
            continue
        intent_id = item["intent_id"]
        if not intent_id:
            # 模型可能不提供 intent_id/client_order_id：用本轮序号做确定性绑定，
            # 不让缺 id 直接抛错（门禁必须给出可审计的拒绝结论，而不是崩溃）。
            intent_id = f"open[{position}]"
            item["intent_id"] = intent_id
        duplicate_id = id_counts.get(intent_id, 0) > 1
        symbol = item["symbol"]
        interval = item["interval"]
        action = item["action"]
        issues: list[str] = [
            f"{REASON_MODEL_FACT_OVERRIDE}:{key}"
            for key in model_fact_overrides(item)
        ]
        if item.get("interval_conflict"):
            declared_i, evidence_i = item["interval_conflict"]
            issues.append(f"{REASON_INTERVAL_CONFLICT}:{declared_i}!={evidence_i}")

        if action != _OPEN_ACTION:
            reason = "NON_OPEN_ACTION_DELEGATED_TO_MANAGEMENT_CONTRACT"
            results.append(IntentEvidence(
                intent_id=intent_id, symbol=symbol, interval=interval, action=action,
                evidence_id="", status=STATUS_NOT_APPLICABLE,
                k02_status="NOT_EVALUATED", accepted=True,
                applies_to_open=False, reason_codes=(reason,), bundle_reasons=(),
                summary=f"[{FACT_SUMMARY_TEMPLATE_VERSION}] {symbol} {action} "
                        f"非开仓动作，由 K04 管理授权契约处理（不阻断；"
                        f"本模块不产出退出证据绑定，evidence_id 由 K04 提供）",
            ))
            accepted.append(intent_id)
            continue

        if duplicate_id:
            issues.append(f"{REASON_DUPLICATE_INTENT_ID}:{intent_id}")
        # 畸形必需源声明（非序列）：过去静默摊成空集合 ⇒ 无门禁，现在结构化拒绝。
        issues.extend(item.get("required_sources_issues") or ())
        if not identity_pinned and not allow_unpinned:
            # U15 未接线：未固定身份下的开仓一律丢单，不靠调用方约定。
            issues.append(REASON_IDENTITY_UNPINNED)

        snapshot, locate_issues = _locate_snapshot(snapshot_list, symbol, interval)
        issues.extend(locate_issues)

        if snapshot is None:
            as_of_ms = int(now_ms)
            bars: tuple[Mapping[str, Any], ...] = ()
        else:
            as_of_ms = int(snapshot.as_of_ms)
            bars = tuple(snapshot.bars) if snapshot.ok else ()
            if snapshot.as_of_ms > int(now_ms):
                issues.append(REASON_SNAPSHOT_FROM_FUTURE)
            if int(now_ms) - int(snapshot.as_of_ms) > int(max_snapshot_age_ms):
                issues.append(REASON_SNAPSHOT_STALE)
            if not snapshot.ok:
                issues.append(REASON_SNAPSHOT_UNUSABLE)
            evidence_at = item.get("evidence_at_ms")
            if evidence_at is not None and int(evidence_at) != int(snapshot.observed_at_ms):
                issues.append(REASON_EVIDENCE_TIME_MISMATCH)
            issues.extend(_source_gate(
                snapshot,
                _resolve_required_sources(item["required_sources"],
                                          default_required_sources),
                now_ms,
            ))

        side = item["side"]
        if side not in _SIDES:
            issues.append("MISSING_SIDE" if not side else "INVALID_SIDE")

        required_kinds = list(_CLAIM_KIND_BY_SIDE.get(side, ()))
        if item["breakout"]:
            required_kinds.append(_BREAKOUT_KIND)
        rules = build_rules(
            interval=str(interval or ""),
            required_claim_kinds=required_kinds,
            max_bar_age_ms=max_bar_age_ms,
            max_snapshot_age_ms=max_snapshot_age_ms,
            expires_in_ms=expires_in_ms,
        )
        source_inputs = _source_inputs(snapshot) if snapshot is not None else []
        # 逐意图 evidence_id：种子含 intent_id 与轮次，同轮同币同周期的两单不再
        # 共享同一个 id（否则执行侧无法证明"这一单的证据是哪个"）。
        input_digest = canonical_hash([
            [str(entry.get("input_id")), str(entry.get("finality")),
             dict(entry.get("values") or {})]
            for entry in list(bars) + source_inputs
        ])
        evidence_id = "eb-" + canonical_hash({
            "intent": intent_id, "symbol": symbol, "interval": interval,
            "as_of_ms": as_of_ms, "cycle_id": str(cycle_id), "inputs": input_digest,
        })[:16]
        bundle = build_bundle(
            {
                "symbol": symbol,
                "as_of_ms": as_of_ms,
                "cycle_id": cycle_id,
                "expires_at_ms": as_of_ms + int(expires_in_ms),
                "evidence_id": evidence_id,
            },
            list(bars) + source_inputs,
            rules,
            cycle_id=cycle_id,
            identity=resolution,
        )

        # 程序 claim 是权威基准：自报 claim 只能附加，不得替换/删除比较基准。
        program_claims = generate_claims(
            side=side, interval=str(interval or ""),
            trigger_price=item["trigger_price"], breakout=item["breakout"],
        )
        if not program_claims and decimal_or_none(item["trigger_price"]) is None:
            issues.append(REASON_MISSING_TRIGGER_PRICE)
        claims, claim_issues = merge_claims(program_claims, item.get("claims"))
        issues.extend(claim_issues)

        declared_bound_inputs = item.get("bound_inputs")
        if isinstance(declared_bound_inputs, (list, tuple)):
            # 显式声明（含显式空列表）一律尊重：空绑定由 K02 的
            # MISSING_EVIDENCE_BINDING 承担，本模块不替调用方补。
            bound = [str(x).strip() for x in declared_bound_inputs if str(x).strip()]
        else:
            bound = list(_claim_token_bindings(claims, bundle))

        intent_payload: dict[str, Any] = {
            "intent_id": intent_id,
            "symbol": symbol,
            "action": action,
            "side": side,
            "claims": claims,
        }
        if bound or isinstance(declared_bound_inputs, (list, tuple)):
            intent_payload["bound_inputs"] = bound
        declared_evidence = item.get("evidence_id")
        if declared_evidence:
            intent_payload["evidence_id"] = str(declared_evidence)
        declared_cycle = item.get("cycle_id")
        if declared_cycle:
            intent_payload["cycle_id"] = str(declared_cycle)
        declared_identity = item.get("identity")
        if isinstance(declared_identity, (Mapping, RuntimeIdentity)):
            # F13：意图自报运行身份时交给 K02 比对，身份不符不发单。
            intent_payload["identity"] = declared_identity

        # 执行报价优先取快照派生（权威），调用方注入的报价只作次选且必须新鲜。
        quote = _quote_from_snapshot(snapshot, symbol) or _caller_quote(
            quote_map, intent_id, symbol, now_ms)
        bundle_verdict = validate_bundle(bundle, rules=rules)
        verdict = validate_intent(
            intent_payload, bundle, quote, now_ms=int(now_ms), rules=rules,
        )

        geometry, geometry_issues = evaluate_geometry(
            side=side,
            entry=item["trigger_price"],
            stop=item.get("stop_loss"),
            target=item.get("take_profit"),
            reported_rr=item.get("reported_rr"),
            rr_tolerance=rr_tolerance,
        )
        issues.extend(geometry_issues)

        reason_codes = tuple(dict.fromkeys(
            [code for code in issues if code]
            + [f"K02:{code}" for code in verdict.reason_codes]
        ))
        accepted_intent = not reason_codes and verdict.status == STATUS_VALID
        # U03 层状态：被拒即 REJECTED，不再出现 status=VALID 而 accepted=False。
        u03_status = verdict.status if accepted_intent else STATUS_REJECTED
        facts: dict[str, Mapping[str, Any]] = {}
        for fact in bundle.derived_facts:
            close_time = None
            for input_id in fact.inputs:
                source_input = bundle.input_by_id(input_id)
                if source_input is not None:
                    close_time = source_input.bar_close_time_ms
                    break
            facts[fact.fact_id] = {
                "value": fact.value,
                "bar_close_time_ms": close_time,
            }
        claim_rows = tuple({
            "claim_id": claim.claim_id,
            "expr": claim.expr,
            "expected": claim.expected,
            "actual": claim.actual,
            "status": claim.status,
            "reason_code": claim.reason_code,
        } for claim in verdict.claim_results)
        sources = {name: snapshot.source(name) for name in SOURCE_NAMES} \
            if snapshot is not None else {}
        summary = render_fact_summary(
            symbol=symbol, interval=interval, evidence_id=bundle.evidence_id,
            status=u03_status, facts=facts, quote=quote,
            sources={k: v for k, v in sources.items() if v is not None},
            claims=claim_rows,
        )
        binding_id = "binding-" + canonical_hash({
            "intent": intent_id, "symbol": symbol, "interval": interval,
            "evidence": bundle.evidence_id, "status": u03_status,
            "k02_status": verdict.status, "identity": identity_hash,
            "cycle_id": str(cycle_id),
        })[:16]
        results.append(IntentEvidence(
            intent_id=intent_id, symbol=symbol, interval=interval, action=action,
            evidence_id=bundle.evidence_id, status=u03_status,
            k02_status=verdict.status,
            accepted=accepted_intent, applies_to_open=True,
            reason_codes=reason_codes,
            bundle_reasons=tuple(bundle_verdict.reason_codes),
            claims=claim_rows,
            geometry=geometry,
            source_quality={name: snapshot.sources[name].as_dict()
                            for name in SOURCE_NAMES} if snapshot is not None else {},
            missing_fields=snapshot.missing_fields if snapshot is not None else (),
            summary=summary,
            binding_id=binding_id,
        ))
        (accepted if accepted_intent else rejected).append(intent_id)

    return ResearchEvidenceReport(
        ok=not rejected,
        cycle_id=str(cycle_id),
        now_ms=int(now_ms),
        identity_hash=identity_hash,
        identity_pinned=identity_pinned,
        unpinned_allowed=bool(allow_unpinned),
        intents=tuple(results),
        rejected_intent_ids=tuple(rejected),
        accepted_intent_ids=tuple(accepted),
    )


__all__ = [
    "FACT_SUMMARY_TEMPLATE_VERSION",
    "INTERVAL_MS",
    "MAX_BAR_AGE_MS_CEILING",
    "MAX_EXPIRES_IN_MS_CEILING",
    "MAX_SNAPSHOT_AGE_MS_CEILING",
    "IntentEvidence",
    "MarketSnapshot",
    "REASON_CLAIM_DECLARATION_REJECTED",
    "REASON_DUPLICATE_INTENT_ID",
    "REASON_EVIDENCE_TIME_MISMATCH",
    "REASON_IDENTITY_UNPINNED",
    "REASON_INTENT_MALFORMED",
    "REASON_INTERVAL_CONFLICT",
    "REASON_INTERVAL_MISMATCH",
    "REASON_INTERVAL_MISLABELED",
    "REASON_INTERVAL_UNVERIFIED",
    "REASON_MISSING_TRIGGER_PRICE",
    "REASON_MODEL_FACT_OVERRIDE",
    "REASON_RR_CONTRADICTION",
    "REASON_SNAPSHOT_MISSING",
    "REASON_SNAPSHOT_STALE",
    "REASON_SOURCE_DECLARATION_REJECTED",
    "REASON_SOURCE_FIELD_MISSING",
    "REASON_SOURCE_FIELD_UNKNOWN",
    "REASON_SOURCE_STALE",
    "REASON_SOURCE_UNAVAILABLE",
    "ResearchEvidenceReport",
    "SCHEMA_VERSION",
    "SOURCE_NAMES",
    "SOURCE_TTL_MS",
    "SourceQuality",
    "bars_from_packed_candles",
    "build_rules",
    "decimal_or_none",
    "declared_claims_or_issues",
    "evaluate_geometry",
    "evaluate_open_intents",
    "generate_claims",
    "is_unpinned_identity",
    "merge_claims",
    "model_fact_overrides",
    "normalise_intent",
    "render_fact_summary",
    "required_sources_or_issues",
    "snapshot_from_records",
    "snapshot_from_tool_result",
    "UNPINNED_IDENTITY",
]
