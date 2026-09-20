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
3. **程序生成有限结构化 claim + 数值矛盾确定性拒绝**
   claim 只从 ``CLAIM_KIND_REGISTRY`` 的三种 kind 生成
   （``close_above_prior_high / close_above_price / close_below_price``），比较
   对象是**真实快照数值**；``744.13 > 744.9``、``78550 < 78443`` 这类模型叙述
   与实际数值矛盾时确定性拒绝，不因自由文本解释通过。
4. **模板化事实摘要 + 模型事实覆盖拒绝**
   事实摘要由程序按固定模板渲染（缺失渲染为 ``MISSING``）；intent 里出现任何
   行情事实字段（``last_price/close/high/facts/...``）一律 ``MODEL_FACT_OVERRIDE``，
   模型解释不得覆盖行情事实。

设计约束：

- **纯函数**：不联网、不读写运行目录、不构造 Broker、不启动服务、不改风险档/
  白名单/live 开关，也不执行任何工具（注入文本无法借此改变工具权限，DC12）。
- 非 OPEN 动作（CLOSE/REDUCE/ADJUST）不属于本模块门禁，按 K04 管理授权契约单独
  处理：本模块把它们记为 ``NOT_APPLICABLE`` 且 **不阻断**（不误伤退出）。
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

SCHEMA_VERSION = "u03-research-evidence-1"
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
    declared_until = decimal_or_none(payload.get("fresh_until_ms"))
    fresh_until: int | None = None
    if declared_until is not None:
        fresh_until = int(declared_until)
    elif available and source_at is not None:
        # 源时钟不得晚于本地观测：取 max 会把未来时间洗白成"仍在有效期"。
        fresh_until = min(int(source_at), int(observed_at_ms)) + int(ttl_ms)
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
    """
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
    if isinstance(declared_missing, (list, tuple)):
        missing_fields.extend(str(item) for item in declared_missing)

    return MarketSnapshot(
        symbol=symbol,
        interval=interval,
        observed_at_ms=observed,
        as_of_ms=observed,
        bars=bars,
        sources=sources,
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        issues=tuple(dict.fromkeys(issues)),
        ok=bool(tool_ok and quality.get("ok", True)),
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
    """由显式 bar 记录构造快照（夹具/离线重放入口，不经过工具出参）。"""
    normalized: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
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
            "interval": bar.get("interval", interval),
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

    return MarketSnapshot(
        symbol=symbol,
        interval=interval,
        observed_at_ms=int(observed_at_ms),
        as_of_ms=int(observed_at_ms),
        bars=tuple(normalized),
        sources=sources,
        missing_fields=tuple(dict.fromkeys(missing_fields)),
        issues=tuple(dict.fromkeys(str(item) for item in issues)),
        ok=bool(ok),
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
    interval = raw.get("interval") or evidence.get("confirmation_interval")
    item["interval"] = str(interval).strip() if interval else None
    trigger = raw.get("trigger_price")
    if trigger is None:
        trigger = evidence.get("trigger_price")
    item["trigger_price"] = trigger
    item["reported_rr"] = evidence.get("risk_reward_ratio", raw.get("risk_reward_ratio"))
    item["evidence_at_ms"] = evidence.get("data_observed_at_ms")
    item["breakout"] = bool(raw.get("breakout") or raw.get("breakout_confirm"))
    required_sources = raw.get("required_sources")
    item["required_sources"] = tuple(str(x) for x in required_sources) \
        if isinstance(required_sources, (list, tuple)) else ()
    return item


# ---------------------------------------------------------------- 逐意图结果


@dataclass(frozen=True)
class IntentEvidence:
    """单 intent 的证据结论；绑定自身的 intent/symbol/interval/evidence_id。"""

    intent_id: str
    symbol: str
    interval: str | None
    action: str
    evidence_id: str
    status: str
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
    intents: tuple[IntentEvidence, ...] = ()
    rejected_intent_ids: tuple[str, ...] = ()
    accepted_intent_ids: tuple[str, ...] = ()

    def by_intent(self, intent_id: str) -> IntentEvidence | None:
        for item in self.intents:
            if item.intent_id == intent_id:
                return item
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": self.ok,
            "cycle_id": self.cycle_id,
            "now_ms": self.now_ms,
            "identity_hash": self.identity_hash,
            "identity_pinned": self.identity_pinned,
            "accepted_intent_ids": list(self.accepted_intent_ids),
            "rejected_intent_ids": list(self.rejected_intent_ids),
            "intents": [item.as_dict() for item in self.intents],
        }

    def decision_gate(self) -> dict[str, Any]:
        """执行前门禁视图：只丢未通过证据门禁的 OPEN，不影响退出。"""
        return {
            "ok": self.ok,
            "identity_pinned": self.identity_pinned,
            "drop_intent_ids": list(self.rejected_intent_ids),
            "reasons": {
                item.intent_id: list(item.reason_codes)
                for item in self.intents if not item.accepted
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


def _source_gate(
    snapshot: MarketSnapshot | None,
    required_sources: Sequence[str],
    now_ms: int,
) -> list[str]:
    if snapshot is None or not required_sources:
        return []
    issues: list[str] = []
    for name in required_sources:
        item = snapshot.source(name)
        if item is None or not item.available:
            issues.append(f"{REASON_SOURCE_UNAVAILABLE}:{name}")
            continue
        if item.source_at_ms is None:
            issues.append(f"{REASON_SOURCE_TIME_UNVERIFIED}:{name}")
            continue
        if not item.fresh_at(now_ms):
            issues.append(f"{REASON_SOURCE_STALE}:{name}")
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
) -> ResearchEvidenceReport:
    """逐意图校验研究证据；``now_ms`` 必传（不使用 bundle.as_of_ms 兜底）。

    每个 OPEN intent 得到自己的证据包与 verdict；非 OPEN 动作不进本门禁。
    ``identity`` 缺省使用 :data:`UNPINNED_IDENTITY` 并在报告里置
    ``identity_pinned=False``（U15 接线前不得把它当成"身份已核对"）。
    """
    identity_pinned = identity is not None
    if identity is None:
        resolution: RuntimeIdentity = UNPINNED_IDENTITY
    elif isinstance(identity, RuntimeIdentity):
        resolution = identity
    else:
        resolution = RuntimeIdentity.model_validate(dict(identity))
    snapshot_list = list(snapshots.values()) if isinstance(snapshots, Mapping) else list(snapshots)
    quote_map = dict(quotes or {})
    results: list[IntentEvidence] = []
    rejected: list[str] = []
    accepted: list[str] = []

    for raw_intent in intents:
        item = normalise_intent(raw_intent)
        intent_id = item["intent_id"]
        symbol = item["symbol"]
        interval = item["interval"]
        action = item["action"]
        overrides = [key for key in MODEL_FACT_KEYS if key in item]
        issues: list[str] = [f"{REASON_MODEL_FACT_OVERRIDE}:{key}" for key in overrides]

        if action != _OPEN_ACTION:
            reason = "NON_OPEN_ACTION_DELEGATED_TO_MANAGEMENT_CONTRACT"
            results.append(IntentEvidence(
                intent_id=intent_id, symbol=symbol, interval=interval, action=action,
                evidence_id="", status=STATUS_NOT_APPLICABLE, accepted=True,
                applies_to_open=False, reason_codes=(reason,), bundle_reasons=(),
                summary=f"[{FACT_SUMMARY_TEMPLATE_VERSION}] {symbol} {action} "
                        f"非开仓动作，由 K04 管理授权契约处理（不阻断）",
            ))
            accepted.append(intent_id)
            continue

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
            issues.extend(_source_gate(snapshot, item["required_sources"], now_ms))

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
        bundle = build_bundle(
            {
                "symbol": symbol,
                "as_of_ms": as_of_ms,
                "cycle_id": cycle_id,
                "expires_at_ms": as_of_ms + int(expires_in_ms),
            },
            list(bars) + source_inputs,
            rules,
            cycle_id=cycle_id,
            identity=resolution,
        )

        declared_claims = item.get("claims")
        claims = [dict(claim) for claim in declared_claims] \
            if isinstance(declared_claims, (list, tuple)) else []
        if not claims:
            claims = generate_claims(
                side=side, interval=str(interval or ""),
                trigger_price=item["trigger_price"], breakout=item["breakout"],
            )
        bound_inputs = item.get("bound_inputs")
        if isinstance(bound_inputs, (list, tuple)) and bound_inputs:
            bound = [str(x) for x in bound_inputs]
        else:
            bound = list(_claim_token_bindings(claims, bundle))

        intent_payload: dict[str, Any] = {
            "intent_id": intent_id,
            "symbol": symbol,
            "action": action,
            "side": side,
            "claims": claims,
        }
        if bound:
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

        quote = quote_map.get(intent_id) or quote_map.get(symbol)
        bundle_verdict = validate_bundle(bundle, rules=rules)
        if quote is None:
            quote = _quote_from_snapshot(snapshot, symbol)
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
            status=verdict.status, facts=facts, quote=quote,
            sources={k: v for k, v in sources.items() if v is not None},
            claims=claim_rows,
        )
        binding_id = "binding-" + canonical_hash({
            "intent": intent_id, "symbol": symbol, "interval": interval,
            "evidence": bundle.evidence_id, "status": verdict.status,
        })[:16]
        results.append(IntentEvidence(
            intent_id=intent_id, symbol=symbol, interval=interval, action=action,
            evidence_id=bundle.evidence_id, status=verdict.status,
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
        identity_hash=resolution.identity_hash(),
        identity_pinned=identity_pinned,
        intents=tuple(results),
        rejected_intent_ids=tuple(rejected),
        accepted_intent_ids=tuple(accepted),
    )


__all__ = [
    "FACT_SUMMARY_TEMPLATE_VERSION",
    "INTERVAL_MS",
    "IntentEvidence",
    "MarketSnapshot",
    "REASON_EVIDENCE_TIME_MISMATCH",
    "REASON_INTERVAL_MISMATCH",
    "REASON_MODEL_FACT_OVERRIDE",
    "REASON_RR_CONTRADICTION",
    "REASON_SNAPSHOT_MISSING",
    "REASON_SNAPSHOT_STALE",
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
    "evaluate_geometry",
    "evaluate_open_intents",
    "generate_claims",
    "normalise_intent",
    "render_fact_summary",
    "snapshot_from_records",
    "snapshot_from_tool_result",
]
