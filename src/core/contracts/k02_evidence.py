"""K02 EvidenceBundle 与逐意图校验契约。

证据包只承载带来源、事件时间、接收时间与有效期的输入，以及由代码从原始
输入计算出的派生事实；自由文本不能作为执行依据。``validate_intent`` 先过
``validate_bundle``（content_hash 重算、事实溯源、质量字段自洽），再按
intent 单独校验必要证据绑定与结构化 claim；verdict 携带 ``checked_intent_id``，
两个意图不能互相借用 PASS。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    canonical_hash,
    canonical_json,
    decimal_text,
    model_content_hash,
)
from .k01_identity import RuntimeIdentity, identity_mismatches

STATUS_VALID = "VALID"
STATUS_EVIDENCE_INVALID = "EVIDENCE_INVALID"
STATUS_EXPIRED = "EXPIRED"
STATUS_STALE = "STALE"
STATUS_SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
VERDICT_STATUS_PRIORITY = (
    STATUS_SYMBOL_MISMATCH,
    STATUS_EXPIRED,
    STATUS_STALE,
    STATUS_EVIDENCE_INVALID,
    STATUS_VALID,
)

REASON_BUNDLE_MISMATCH = "EVIDENCE_BUNDLE_MISMATCH"
REASON_BUNDLE_INVALID = "EVIDENCE_BUNDLE_INVALID"
REASON_CONTENT_HASH_MISMATCH = "CONTENT_HASH_MISMATCH"
REASON_CONTENT_HASH_MISSING = "CONTENT_HASH_MISSING"
REASON_DERIVED_FACT_MISMATCH = "DERIVED_FACT_MISMATCH"
REASON_DERIVED_FACT_INVALID = "DERIVED_FACT_INVALID"
REASON_CROSS_SYMBOL_FACT = "CROSS_SYMBOL_FACT"
REASON_DUPLICATE_INPUT_ID = "DUPLICATE_INPUT_ID"
REASON_QUALITY_MISMATCH = "QUALITY_MISMATCH"
REASON_FINALITY_MISMATCH = "FINALITY_MISMATCH"
REASON_INPUT_TIME_ORDER = "INPUT_TIME_ORDER"
REASON_BAR_RECEIVED_BEFORE_CLOSE = "BAR_RECEIVED_BEFORE_CLOSE"
REASON_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
REASON_CYCLE_MISMATCH = "CYCLE_MISMATCH"
REASON_SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
REASON_INPUT_SYMBOL_MISMATCH = "INPUT_SYMBOL_MISMATCH"
REASON_UNKNOWN_ACTION = "UNKNOWN_ACTION"
REASON_MISSING_SIDE = "MISSING_SIDE"
REASON_INVALID_SIDE = "INVALID_SIDE"
REASON_MISSING_EVIDENCE_BINDING = "MISSING_EVIDENCE_BINDING"
REASON_MISSING_REQUIRED_INTERVAL = "MISSING_REQUIRED_INTERVAL"
REASON_BUNDLE_NOT_CLOSED = "BUNDLE_NOT_CLOSED"
REASON_BUNDLE_EXPIRED = "BUNDLE_EXPIRED"
REASON_UNCLOSED_BAR = "UNCLOSED_BAR"
REASON_UNDATED_BAR = "UNDATED_BAR"
REASON_FUTURE_BAR = "FUTURE_BAR"
REASON_FUTURE_INPUT = "FUTURE_INPUT"
REASON_INPUT_STALE = "INPUT_STALE"
REASON_NON_FINITE = "NON_FINITE_VALUE"
REASON_CLAIM_FALSE = "CLAIM_FALSE"
REASON_CLAIM_UNRESOLVED = "CLAIM_UNRESOLVED"
REASON_CLAIM_WITHOUT_EVIDENCE = "CLAIM_WITHOUT_EVIDENCE"
REASON_MISSING_REQUIRED_CLAIM = "MISSING_REQUIRED_CLAIM"
REASON_MISSING_CLAIMS = "MISSING_CLAIMS"
REASON_MISSING_QUOTE = "MISSING_EXECUTION_QUOTE"
REASON_QUOTE_SYMBOL = "QUOTE_SYMBOL_MISMATCH"
REASON_QUOTE_NON_FINITE = "QUOTE_NON_FINITE"
REASON_QUOTE_EXPIRED = "QUOTE_EXPIRED"
REASON_NEEDS_AUTH = "NEEDS_AUTH"
REASON_CLAIM_MALFORMED = "CLAIM_MALFORMED"
REASON_CLAIM_EXPECTED_INVALID = "CLAIM_EXPECTED_INVALID"
REASON_CLAIM_KIND_UNKNOWN = "CLAIM_KIND_UNKNOWN"
REASON_CLAIM_KIND_CONTRADICTION = "CLAIM_KIND_CONTRADICTION"
REASON_CLAIM_OUTSIDE_BOUND_INPUTS = "CLAIM_OUTSIDE_BOUND_INPUTS"
REASON_DUPLICATE_FACT_ID = "DUPLICATE_FACT_ID"
REASON_QUALITY_MISSING_FIELD = "QUALITY_MISSING_FIELD"
REASON_RULES_MISMATCH = "RULES_MISMATCH"
REASON_RULES_MISSING = "RULES_MISSING"

SUPPORTED_ACTIONS = ("OPEN", "CLOSE", "REDUCE", "ADJUST")
AUTHORIZED_ACTIONS = ("CLOSE", "REDUCE", "ADJUST")
SUPPORTED_SIDES = ("LONG", "SHORT")
FACT_KINDS = ("closed_close", "prior_high", "bar_close_time_ms")

#: 有限结构化 claim 集合：kind -> (操作符, 左操作数语义, 右操作数语义)。
#: U03 只能从该注册表选择 kind；不得用自由标签冒充必需检查。
CLAIM_KIND_REGISTRY: dict[str, tuple[str, str, str]] = {
    "close_above_prior_high": (">", "closed_close", "prior_high"),
    "close_above_price": (">", "closed_close", "price"),
    "close_below_price": ("<", "closed_close", "price"),
}
QUALITY_REQUIRED_KEYS = (
    "missing_intervals",
    "stale_inputs",
    "dedup_removed",
    "unclosed_inputs",
    "undated_inputs",
    "symbol_mismatch_inputs",
    "required_intervals",
    "required_claim_kinds",
    "max_bar_age_ms",
    "as_of_ms",
    "rules_hash",
)

_CLAIM_PATTERN = re.compile(
    r"^\s*(?P<lhs>[^\s<>=!]+)\s*(?P<op>>=|<=|==|!=|>|<)\s*(?P<rhs>[^\s<>=!]+)\s*$"
)
_DECIMAL_LITERAL = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class EvidenceInput(BaseModel):
    """单个证据输入；value_ref 指向可复核的原始载荷。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_id: str
    source: str
    event_time_ms: int
    received_time_ms: int
    valid_until_ms: int | None = None
    value_ref: str
    symbol: str | None = None
    interval: str | None = None
    finality: Literal["closed", "live", "unknown"] = "unknown"
    bar_close_time_ms: int | None = None
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class DerivedFact(BaseModel):
    """由代码计算的派生事实；必须引用至少一个输入 ID。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    kind: str
    inputs: tuple[str, ...] = ()
    value: str
    computed_by: Literal["code"] = "code"


class EvidenceBundle(BaseModel):
    """一次周期内单币种的证据快照；content_hash 自绑定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    cycle_id: str
    identity: RuntimeIdentity
    symbol: str
    as_of_ms: int
    bar_times: dict[str, int] = Field(default_factory=dict)
    finality: Literal["closed", "live", "unknown"] = "unknown"
    inputs: tuple[EvidenceInput, ...] = ()
    dropped_input_ids: tuple[str, ...] = ()
    derived_facts: tuple[DerivedFact, ...] = ()
    quality: dict[str, Any] = Field(default_factory=dict)
    expires_at_ms: int = 0
    content_hash: str = ""

    def input_by_id(self, input_id: str) -> EvidenceInput | None:
        for item in self.inputs:
            if item.input_id == input_id:
                return item
        return None

    def fact_by_id(self, fact_id: str) -> DerivedFact | None:
        for item in self.derived_facts:
            if item.fact_id == fact_id:
                return item
        return None


class ExecutionQuote(BaseModel):
    """执行前候选入场报价；OPEN 意图必须携带且有限、未过期。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    price: str
    quote_time_ms: int
    expires_at_ms: int | None = None


class ClaimResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    expr: str
    expected: bool | None = None
    actual: bool | None = None
    status: Literal["PASS", "FAIL", "UNRESOLVED"]
    reason_code: str = ""


class BundleVerdict(BaseModel):
    """证据包完整性结论；content_hash 与事实溯源的可校验入口。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    valid: bool
    reason_codes: tuple[str, ...] = ()


class EvidenceVerdict(BaseModel):
    """单个 intent 的校验结论；绑定 intent/symbol/evidence 三元组。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict_id: str = ""
    checked_intent_id: str
    intent_symbol: str
    evidence_id: str
    status: Literal[
        "VALID", "EVIDENCE_INVALID", "EXPIRED", "STALE", "SYMBOL_MISMATCH"
    ]
    reason_codes: tuple[str, ...] = ()
    claim_results: tuple[ClaimResult, ...] = ()
    checked_at_ms: int


def _value_to_decimal(value: Any) -> tuple[Decimal | None, str | None]:
    if value is None:
        return None, REASON_CLAIM_UNRESOLVED
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        return None, REASON_CLAIM_UNRESOLVED
    if not parsed.is_finite():
        return parsed, REASON_NON_FINITE
    return parsed, None


def _fact_text(value: Any) -> str:
    if isinstance(value, Decimal):
        return decimal_text(value)
    try:
        return decimal_text(Decimal(str(value).strip()))
    except InvalidOperation:
        return str(value)


def _is_literal(token: str) -> bool:
    return bool(_DECIMAL_LITERAL.match(token))


def resolve_operand(
    token: str,
    bundle: EvidenceBundle,
) -> tuple[Decimal | None, tuple[str, ...], str | None]:
    """解析 claim 操作数；返回 (值, 引用输入ID, 错误码)。"""
    try:
        literal = Decimal(token)
    except InvalidOperation:
        pass
    else:
        if not literal.is_finite():
            return literal, (), REASON_NON_FINITE
        return literal, (), None
    if ":" in token:
        fact = bundle.fact_by_id(token)
        if fact is None:
            return None, (), REASON_CLAIM_UNRESOLVED
        value, error = _value_to_decimal(fact.value)
        return value, fact.inputs, error
    if "." in token:
        input_id, _, key = token.rpartition(".")
        item = bundle.input_by_id(input_id)
        if item is None or key not in item.values:
            return None, (), REASON_CLAIM_UNRESOLVED
        value, error = _value_to_decimal(item.values[key])
        return value, (input_id,), error
    matches = [fact for fact in bundle.derived_facts if fact.kind == token]
    if len(matches) == 1:
        fact = matches[0]
        value, error = _value_to_decimal(fact.value)
        return value, fact.inputs, error
    return None, (), REASON_CLAIM_UNRESOLVED


def _compare(op: str, lhs: Decimal, rhs: Decimal) -> bool:
    if op == ">":
        return lhs > rhs
    if op == ">=":
        return lhs >= rhs
    if op == "<":
        return lhs < rhs
    if op == "<=":
        return lhs <= rhs
    if op == "==":
        return lhs == rhs
    if op == "!=":
        return lhs != rhs
    raise ValueError(f"unsupported operator: {op}")


def _parse_claim(
    claim: Any,
) -> tuple[str, str, Any, str, str, str] | None:
    """解析 claim 元素；非 Mapping 或缺少合法表达式返回 None（结构化拒绝）。"""
    if not isinstance(claim, Mapping):
        return None
    expr = str(claim.get("expr") or "")
    match = _CLAIM_PATTERN.match(expr)
    if not match:
        return None
    return (
        str(claim.get("claim_id") or ""),
        expr,
        claim.get("expected", True),
        match.group("lhs"),
        match.group("op"),
        match.group("rhs"),
    )


def _kind_operand_role(bundle: EvidenceBundle, token: str) -> str | None:
    """把操作数归类为 closed_close/prior_high/price；其他一律 None。"""
    if _is_literal(token):
        return "price"
    if ":" in token:
        fact = bundle.fact_by_id(token)
        return fact.kind if fact is not None else None
    if "." in token:
        return None
    matches = [fact for fact in bundle.derived_facts if fact.kind == token]
    if len(matches) == 1:
        return matches[0].kind
    return None


def _kind_interval(bundle: EvidenceBundle, token: str) -> str | None:
    if ":" in token:
        fact = bundle.fact_by_id(token)
        if fact is not None:
            return fact.fact_id.partition(":")[0]
        return None
    matches = [fact for fact in bundle.derived_facts if fact.kind == token]
    if len(matches) == 1:
        return matches[0].fact_id.partition(":")[0]
    return None


def claim_kind_consistent(
    bundle: EvidenceBundle,
    kind: str,
    op: str,
    lhs_token: str,
    rhs_token: str,
) -> bool:
    """校验 claim kind 与实际谓词一致；未知 kind 或语义矛盾返回 False。"""
    spec = CLAIM_KIND_REGISTRY.get(kind)
    if spec is None:
        return False
    want_op, lhs_role, rhs_role = spec
    if op != want_op:
        return False
    if _kind_operand_role(bundle, lhs_token) != lhs_role:
        return False
    if rhs_role == "price":
        return _is_literal(rhs_token)
    if _kind_operand_role(bundle, rhs_token) != rhs_role:
        return False
    return _kind_interval(bundle, lhs_token) == _kind_interval(bundle, rhs_token)


def _referenced_input_issues(
    refs: Sequence[str],
    bundle: EvidenceBundle,
    now_ms: int,
) -> dict[str, tuple[str, ...]]:
    """返回 输入ID -> reason_code 元组；未闭合、无时间、未来、过期、跨币种都在此暴露。"""
    issues: dict[str, tuple[str, ...]] = {}
    stale_marked = set(bundle.quality.get("stale_inputs") or ())
    for input_id in refs:
        codes: list[str] = []
        item = bundle.input_by_id(input_id)
        if item is None:
            issues[input_id] = (REASON_CLAIM_UNRESOLVED,)
            continue
        if item.symbol != bundle.symbol:
            codes.append(REASON_INPUT_SYMBOL_MISMATCH)
        if item.finality == "closed" and item.bar_close_time_ms is None:
            codes.append(REASON_UNDATED_BAR)
        elif item.finality != "closed":
            if item.bar_close_time_ms is None:
                codes.append(REASON_UNDATED_BAR)
            else:
                codes.append(REASON_UNCLOSED_BAR)
        if item.bar_close_time_ms is not None and item.bar_close_time_ms > bundle.as_of_ms:
            codes.append(REASON_FUTURE_BAR)
        if item.event_time_ms > bundle.as_of_ms:
            codes.append(REASON_FUTURE_INPUT)
        if item.valid_until_ms is not None and now_ms >= item.valid_until_ms:
            codes.append(REASON_INPUT_STALE)
        if input_id in stale_marked:
            codes.append(REASON_INPUT_STALE)
        if codes:
            issues[input_id] = tuple(dict.fromkeys(codes))
    return issues


def _closed_inputs_by_interval(bundle: EvidenceBundle) -> dict[str, list[EvidenceInput]]:
    closed: dict[str, list[EvidenceInput]] = {}
    for item in bundle.inputs:
        if (
            item.interval
            and item.finality == "closed"
            and item.symbol == bundle.symbol
            and item.bar_close_time_ms is not None
        ):
            closed.setdefault(item.interval, []).append(item)
    return closed


def _expected_facts(bundle: EvidenceBundle) -> dict[str, DerivedFact]:
    """按 build_bundle 的选取规则（最新/次新 bar）重建期望事实集合。"""
    facts: dict[str, DerivedFact] = {}
    closed = _closed_inputs_by_interval(bundle)
    for interval in sorted(closed):
        bars = sorted(
            closed[interval],
            key=lambda item: (item.bar_close_time_ms or 0, item.input_id),
        )
        latest = bars[-1]
        close_value = latest.values.get("close")
        if close_value is not None:
            facts[f"{interval}:closed_close"] = DerivedFact(
                fact_id=f"{interval}:closed_close",
                kind="closed_close",
                inputs=(latest.input_id,),
                value=_fact_text(close_value),
            )
        if len(bars) >= 2:
            prior = bars[-2]
            high = prior.values.get("high")
            if high is None:
                high = prior.values.get("prior_high")
            if high is not None:
                facts[f"{interval}:prior_high"] = DerivedFact(
                    fact_id=f"{interval}:prior_high",
                    kind="prior_high",
                    inputs=(prior.input_id,),
                    value=_fact_text(high),
                )
        bar_close = int(latest.bar_close_time_ms or latest.event_time_ms)
        facts[f"{interval}:bar_close_time_ms"] = DerivedFact(
            fact_id=f"{interval}:bar_close_time_ms",
            kind="bar_close_time_ms",
            inputs=(latest.input_id,),
            value=str(bar_close),
        )
    return facts


def _expected_quality(
    bundle: EvidenceBundle,
    max_bar_age_ms: int | None,
) -> dict[str, Any]:
    """从 inputs 与 max_bar_age 重建全部可推导的 quality 字段。"""
    required_intervals = [
        str(item) for item in (bundle.quality.get("required_intervals") or [])
    ]
    closed = _closed_inputs_by_interval(bundle)
    missing_intervals = [item for item in required_intervals if item not in closed]
    stale: list[str] = []
    undated: list[str] = []
    unclosed: list[str] = []
    symbol_mismatch: list[str] = []
    for item in bundle.inputs:
        if item.symbol != bundle.symbol:
            symbol_mismatch.append(item.input_id)
        if item.bar_close_time_ms is None and item.finality in ("closed", "unknown"):
            undated.append(item.input_id)
        if item.finality != "closed":
            unclosed.append(item.input_id)
        if (
            max_bar_age_ms is not None
            and item.finality == "closed"
            and item.bar_close_time_ms is not None
            and bundle.as_of_ms - item.bar_close_time_ms > max_bar_age_ms
        ):
            stale.append(item.input_id)
    bar_times = {
        interval: int(max((bar.bar_close_time_ms or 0) for bar in bars))
        for interval, bars in closed.items()
    }
    return {
        "missing_intervals": missing_intervals,
        "stale_inputs": stale,
        "dedup_removed": [],
        "unclosed_inputs": unclosed,
        "undated_inputs": undated,
        "symbol_mismatch_inputs": symbol_mismatch,
        "bar_times": bar_times,
        "finality": _expected_finality(
            required_intervals, closed, len(bundle.inputs),
        ),
    }


def _expected_finality(
    required_intervals: Sequence[str],
    closed_by_interval: Mapping[str, Sequence[EvidenceInput]],
    input_count: int,
) -> str:
    if not input_count:
        return "unknown"
    missing = [item for item in required_intervals if item not in closed_by_interval]
    if required_intervals and not missing:
        return "closed"
    return "live"


class RulesInputError(ValueError):
    """``rules`` 载荷不可规范化时的结构化拒绝（非 Mapping / 非 JSON 值 / 混合键型）。

    ``build_bundle`` 无法返回 verdict，因此以本异常做结构化拒绝；``validate_bundle``
    则把同一情形折算成 ``RULES_MISMATCH``。
    """


def _rules_payload(rules: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``rules`` 规范化为可哈希字典；不可规范化一律结构化拒绝。

    ``isinstance(rules, Mapping)`` 守卫只覆盖外层形态：Mapping **内容**里出现
    ``set``/``bytes``/任意对象/``datetime`` 等非 JSON 值，或 ``{1: 'x'}`` 这类
    混合键型，都会让 ``json.dumps(sort_keys=True)`` 抛裸 ``TypeError``。这里
    统一转成 ``RulesInputError``，绝不把畸形 rules 当成合法哈希输入。
    """
    if not isinstance(rules, Mapping):
        raise RulesInputError(f"rules must be a mapping, got {type(rules).__name__}")
    try:
        payload = dict(rules)
        canonical_json(payload)
    except (TypeError, ValueError) as error:
        raise RulesInputError(f"rules payload is not canonical JSON: {error}") from error
    return payload


def _rules_hash(rules: Mapping[str, Any]) -> str:
    return canonical_hash(_rules_payload(rules))


def validate_bundle(
    bundle: EvidenceBundle,
    *,
    rules: Mapping[str, Any] | None = None,
    identity: RuntimeIdentity | None = None,
) -> BundleVerdict:
    """证据包完整性校验：content_hash、事实重建、输入时间与 quality 自洽。

    与 ``build_bundle`` 的输出逐项对照：事实集合（id/kind/inputs/value）、
    missing/stale/dedup/unclosed/undated/symbol 等可推导 quality 字段、
    finality 与 bar_times。篡改输入值、换指事实、重复 fact_id 或删改 quality
    都会在此失败；``validate_intent`` 必须先调用本函数。

    ``rules`` 必须显式传入：缺省（None）或非 Mapping → ``RULES_MISSING``/
    ``RULES_MISMATCH``；随后与 ``bundle.quality.rules_hash`` 做整体哈希比对，
    并对 required_intervals / required_claim_kinds / max_bar_age_ms 逐项比对，
    从而阻止“改 rules/quality 重哈希即 VALID”的路径。

    ``symbol=None`` 的输入是 ``build_bundle`` 不可达形态（幽灵输入），
    一律报 ``INPUT_SYMBOL_MISMATCH``；``dropped_input_ids`` 必须是与保留输入
    不相交的非空字符串序列（``tuple``/``list``，唯一）——非序列形态（如
    ``model_copy`` 绕过类型校验写入的 ``int``/``str``）同样报 ``QUALITY_MISMATCH``；
    ``quality.dedup_removed`` 不可重算，必须为空。

    ``rules`` 除必须是 Mapping 外，其**内容**还必须可规范化为 JSON：含
    ``set``/``bytes``/任意对象/``datetime`` 或混合键型时一律 ``RULES_MISMATCH``
    （不抛裸 ``TypeError``，也不把畸形载荷当作合法哈希输入）。
    """
    reasons: list[str] = []
    expected_hash = model_content_hash(bundle)
    if not bundle.content_hash:
        reasons.append(REASON_CONTENT_HASH_MISSING)
    elif bundle.content_hash != expected_hash:
        reasons.append(REASON_CONTENT_HASH_MISMATCH)

    seen: set[str] = set()
    for item in bundle.inputs:
        if item.input_id in seen:
            reasons.append(REASON_DUPLICATE_INPUT_ID)
        seen.add(item.input_id)
        if item.symbol is None:
            reasons.append(REASON_INPUT_SYMBOL_MISMATCH)
        if item.event_time_ms > item.received_time_ms:
            reasons.append(REASON_INPUT_TIME_ORDER)
        if (
            item.finality == "closed"
            and item.bar_close_time_ms is not None
            and item.bar_close_time_ms > item.received_time_ms
        ):
            reasons.append(REASON_BAR_RECEIVED_BEFORE_CLOSE)
        if item.finality == "closed" and item.bar_close_time_ms is None:
            reasons.append(REASON_UNDATED_BAR)

    seen_dropped: set[str] = set()
    for dropped_id in bundle.dropped_input_ids:
        if (
            not isinstance(dropped_id, str)
            or not dropped_id.strip()
            or dropped_id in seen_dropped
            or dropped_id in seen
        ):
            reasons.append(REASON_QUALITY_MISMATCH)
            break
        seen_dropped.add(dropped_id)

    missing_quality = [
        key for key in QUALITY_REQUIRED_KEYS if key not in bundle.quality
    ]
    if missing_quality:
        reasons.append(REASON_QUALITY_MISSING_FIELD)
    if bundle.quality.get("as_of_ms") != bundle.as_of_ms:
        reasons.append(REASON_QUALITY_MISMATCH)
    max_bar_age = bundle.quality.get("max_bar_age_ms")
    if max_bar_age is not None and (
        isinstance(max_bar_age, bool)
        or not isinstance(max_bar_age, int)
        or max_bar_age <= 0
    ):
        reasons.append(REASON_QUALITY_MISSING_FIELD)
        max_bar_age = None

    expected_quality = _expected_quality(bundle, max_bar_age)
    for key in (
        "missing_intervals",
        "stale_inputs",
        "dedup_removed",
        "unclosed_inputs",
        "undated_inputs",
        "symbol_mismatch_inputs",
    ):
        declared = [str(item) for item in (bundle.quality.get(key) or [])]
        if declared != expected_quality[key]:
            reasons.append(REASON_QUALITY_MISMATCH)
    if dict(bundle.bar_times) != expected_quality["bar_times"]:
        reasons.append(REASON_QUALITY_MISMATCH)
    if bundle.finality != expected_quality["finality"]:
        reasons.append(REASON_FINALITY_MISMATCH)

    expected_facts = _expected_facts(bundle)
    seen_fact_ids: set[str] = set()
    for fact in bundle.derived_facts:
        if fact.fact_id in seen_fact_ids:
            reasons.append(REASON_DUPLICATE_FACT_ID)
            continue
        seen_fact_ids.add(fact.fact_id)
        for input_id in fact.inputs:
            item = bundle.input_by_id(input_id)
            if item is None:
                reasons.append(REASON_DERIVED_FACT_INVALID)
                break
            if item.symbol is not None and item.symbol != bundle.symbol:
                reasons.append(REASON_CROSS_SYMBOL_FACT)
                break
        expected_fact = expected_facts.get(fact.fact_id)
        if expected_fact is None:
            reasons.append(REASON_DERIVED_FACT_INVALID)
            continue
        if (
            fact.kind != expected_fact.kind
            or fact.inputs != expected_fact.inputs
            or fact.value != expected_fact.value
            or fact.computed_by != "code"
        ):
            reasons.append(REASON_DERIVED_FACT_MISMATCH)
    if seen_fact_ids != set(expected_facts):
        reasons.append(REASON_DERIVED_FACT_MISMATCH)

    if rules is None:
        reasons.append(REASON_RULES_MISSING)
    elif not isinstance(rules, Mapping):
        reasons.append(REASON_RULES_MISMATCH)
    else:
        try:
            rules_hash = _rules_hash(rules)
        except RulesInputError:
            # Mapping 内容含非 JSON 值/混合键型：整体拒绝，不再逐字段比对
            # （畸形 rules 不具备任何“合法声明”地位）。
            rules_hash = None
            reasons.append(REASON_RULES_MISMATCH)
        if rules_hash is not None:
            if bundle.quality.get("rules_hash") != rules_hash:
                reasons.append(REASON_RULES_MISMATCH)
            rule_intervals = [
                str(item) for item in (rules.get("required_intervals") or [])
            ]
            declared_intervals = [
                str(item) for item in (bundle.quality.get("required_intervals") or [])
            ]
            if rule_intervals != declared_intervals:
                reasons.append(REASON_RULES_MISMATCH)
            rule_kinds = [str(item) for item in (rules.get("required_claim_kinds") or [])]
            declared_kinds = [
                str(item) for item in (bundle.quality.get("required_claim_kinds") or [])
            ]
            if rule_kinds != declared_kinds:
                reasons.append(REASON_RULES_MISMATCH)
            if rules.get("max_bar_age_ms") != bundle.quality.get("max_bar_age_ms"):
                reasons.append(REASON_RULES_MISMATCH)

    if identity is not None and identity_mismatches(expected=identity, actual=bundle.identity):
        reasons.append(REASON_IDENTITY_MISMATCH)

    unique = list(dict.fromkeys(reasons))
    return BundleVerdict(valid=not unique, reason_codes=tuple(unique))


def build_bundle(
    snapshot: Mapping[str, Any],
    closed_bars: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
    *,
    cycle_id: str | None = None,
    identity: RuntimeIdentity | Mapping[str, Any] | None = None,
) -> EvidenceBundle:
    """由显式快照、K 线与规则构建证据包；纯函数，无 IO。

    只从已收盘 K 线生成派生事实；未闭合/未来/无收盘时间的 bar 保留为输入
    供审计，但不会进入事实计算。跨 symbol 输入被隔离且不参与事实与最终性。
    evidence_id 在未显式给出时是确定性的（含输入值摘要）。

    重复内容按 ``(symbol, interval, close_time, values)`` 去重，被丢弃的
    input_id 记入 ``dropped_input_ids``（审计记录，纳入 content_hash）；
    ``quality.dedup_removed`` 保持为空（该字段不可重算，见 README §12）。
    ``rules`` 的整体哈希写入 ``quality.rules_hash``，供验证方比对。

    畸形 ``rules``（非 Mapping，或 Mapping 内容含非 JSON 值/混合键型）抛
    ``RulesInputError``（结构化拒绝），不会退化成裸 ``TypeError``/``AttributeError``。
    """
    snap = dict(snapshot)
    if "symbol" not in snap or "as_of_ms" not in snap:
        raise ValueError("snapshot requires symbol and as_of_ms")
    # rules 形态与内容先行校验：非 Mapping 或含非 JSON 值/混合键型 → 结构化拒绝，
    # 不允许在后面的 rules.get(...)/哈希阶段抛裸 AttributeError/TypeError。
    rules_payload = _rules_payload(rules)
    as_of_ms = int(snap["as_of_ms"])
    symbol = str(snap["symbol"])
    cycle = str(cycle_id or snap.get("cycle_id") or "")
    identity_value = identity if identity is not None else snap.get("identity")
    identity_model = (
        identity_value
        if isinstance(identity_value, RuntimeIdentity)
        else RuntimeIdentity.model_validate(identity_value)
    )

    inputs: list[EvidenceInput] = []
    dropped: list[str] = []
    stale: list[str] = []
    undated: list[str] = []
    cross_symbol: list[str] = []
    seen: set[str] = set()
    max_age = rules.get("max_bar_age_ms")
    for index, bar in enumerate(closed_bars):
        input_id = str(bar.get("input_id") or f"bar-{index}")
        raw_close = bar.get("close_time_ms", bar.get("bar_close_time_ms"))
        close_time: int | None = None if raw_close is None else int(raw_close)
        explicit_finality = bar.get("finality")
        if close_time is None:
            finality = "unknown"
            undated.append(input_id)
        elif close_time > as_of_ms:
            finality = "live"
        elif explicit_finality:
            finality = str(explicit_finality)
        else:
            finality = "closed"
        bar_symbol = str(bar.get("symbol") or symbol)
        values = {str(key): value for key, value in (bar.get("values") or {}).items()}
        dedup_key = canonical_hash({
            "symbol": bar_symbol,
            "interval": bar.get("interval"),
            "close_time_ms": close_time,
            "values": values,
        })
        if dedup_key in seen:
            dropped.append(input_id)
            continue
        seen.add(dedup_key)
        if bar_symbol != symbol:
            cross_symbol.append(input_id)
        if (
            max_age is not None
            and finality == "closed"
            and close_time is not None
            and as_of_ms - close_time > int(max_age)
        ):
            stale.append(input_id)
        inputs.append(EvidenceInput(
            input_id=input_id,
            source=str(bar.get("source") or "unknown"),
            event_time_ms=int(bar.get("event_time_ms") or close_time or as_of_ms),
            received_time_ms=int(bar.get("received_time_ms") or as_of_ms),
            valid_until_ms=bar.get("valid_until_ms"),
            value_ref=str(bar.get("value_ref") or f"bar:{input_id}"),
            symbol=bar_symbol,
            interval=bar.get("interval"),
            finality=finality,
            bar_close_time_ms=close_time,
            values=values,
        ))

    required_intervals = [str(item) for item in (rules.get("required_intervals") or [])]
    required_claim_kinds = [str(item) for item in (rules.get("required_claim_kinds") or [])]
    closed_by_interval: dict[str, list[EvidenceInput]] = {}
    for item in inputs:
        if (
            item.interval
            and item.finality == "closed"
            and item.symbol == symbol
            and item.bar_close_time_ms is not None
        ):
            closed_by_interval.setdefault(item.interval, []).append(item)
    missing_intervals = [item for item in required_intervals if item not in closed_by_interval]

    facts: list[DerivedFact] = []
    bar_times: dict[str, int] = {}
    for interval in sorted(closed_by_interval):
        bars = sorted(
            closed_by_interval[interval],
            key=lambda item: (item.bar_close_time_ms or 0, item.input_id),
        )
        latest = bars[-1]
        bar_times[interval] = int(latest.bar_close_time_ms or latest.event_time_ms)
        latest_close = latest.values.get("close")
        if latest_close is not None:
            facts.append(DerivedFact(
                fact_id=f"{interval}:closed_close",
                kind="closed_close",
                inputs=(latest.input_id,),
                value=_fact_text(latest_close),
            ))
        if len(bars) >= 2:
            prior = bars[-2]
            prior_high = prior.values.get("high")
            if prior_high is None:
                prior_high = prior.values.get("prior_high")
            if prior_high is not None:
                facts.append(DerivedFact(
                    fact_id=f"{interval}:prior_high",
                    kind="prior_high",
                    inputs=(prior.input_id,),
                    value=_fact_text(prior_high),
                ))
        facts.append(DerivedFact(
            fact_id=f"{interval}:bar_close_time_ms",
            kind="bar_close_time_ms",
            inputs=(latest.input_id,),
            value=str(bar_times[interval]),
        ))

    bundle_finality = _expected_finality(
        required_intervals, closed_by_interval, len(inputs),
    )

    expires_at_ms = snap.get("expires_at_ms")
    if expires_at_ms is None:
        expires_at_ms = as_of_ms + int(rules.get("expires_in_ms") or 60_000)
    seed = {
        "cycle_id": cycle,
        "symbol": symbol,
        "as_of_ms": as_of_ms,
        "inputs": [
            {
                "input_id": item.input_id,
                "finality": item.finality,
                "bar_close_time_ms": item.bar_close_time_ms,
                "values": item.values,
            }
            for item in inputs
        ],
    }
    evidence_id = str(snap.get("evidence_id") or f"eb-{canonical_hash(seed)[:16]}")
    quality = {
        "missing_intervals": missing_intervals,
        "stale_inputs": stale,
        "dedup_removed": [],
        "unclosed_inputs": [
            item.input_id for item in inputs if item.finality != "closed"
        ],
        "undated_inputs": undated,
        "symbol_mismatch_inputs": cross_symbol,
        "required_intervals": required_intervals,
        "required_claim_kinds": required_claim_kinds,
        "max_bar_age_ms": int(max_age) if max_age is not None else None,
        "as_of_ms": as_of_ms,
        "rules_hash": canonical_hash(rules_payload),
    }
    bundle = EvidenceBundle(
        evidence_id=evidence_id,
        cycle_id=cycle,
        identity=identity_model,
        symbol=symbol,
        as_of_ms=as_of_ms,
        bar_times=bar_times,
        finality=bundle_finality,
        inputs=tuple(inputs),
        dropped_input_ids=tuple(dropped),
        derived_facts=tuple(facts),
        quality=quality,
        expires_at_ms=int(expires_at_ms),
        content_hash="",
    )
    return bundle.model_copy(update={"content_hash": model_content_hash(bundle)})


def _normalise_quote(
    execution_quote: Mapping[str, Any] | ExecutionQuote | None,
) -> dict[str, Any] | None:
    if execution_quote is None:
        return None
    if isinstance(execution_quote, ExecutionQuote):
        return execution_quote.model_dump(mode="json")
    return dict(execution_quote)


def validate_intent(
    intent: Mapping[str, Any],
    bundle: EvidenceBundle,
    execution_quote: Mapping[str, Any] | ExecutionQuote | None = None,
    *,
    now_ms: int | None = None,
    rules: Mapping[str, Any] | None = None,
) -> EvidenceVerdict:
    """逐 intent 校验：bundle 完整性、action/side 枚举、证据绑定、时效与 claim。

    非 OPEN 动作（CLOSE/REDUCE/ADJUST）必须携带 ``management_authorization_id``
    指向 K04 授权，否则 ``NEEDS_AUTH``；claim kind 必须来自有限注册表并与实际
    谓词一致；claim 实际引用的输入必须 ⊆ ``bound_inputs``：非 OPEN 空绑定下
    任何引用必然越界（``CLAIM_OUTSIDE_BOUND_INPUTS``），OPEN 缺绑定由
    ``MISSING_EVIDENCE_BINDING`` 承担。``rules`` 必须显式传入并比对哈希。
    """
    intent_id = str(intent.get("intent_id") or "")
    if not intent_id:
        raise ValueError("intent requires intent_id")
    now = int(now_ms if now_ms is not None else bundle.as_of_ms)
    intent_symbol = str(intent.get("symbol") or "")
    action = str(intent.get("action") or "").upper()
    side = str(intent.get("side") or "").upper()
    reasons: list[str] = []
    categories: dict[str, list[str]] = {}

    def note(status: str, code: str) -> None:
        if code not in reasons:
            reasons.append(code)
        categories.setdefault(status, []).append(code)

    bundle_check = validate_bundle(bundle, rules=rules)
    for code in bundle_check.reason_codes:
        note(STATUS_EVIDENCE_INVALID, code)

    declared_evidence = intent.get("evidence_id") or intent.get("evidence_id_shown_by_caller")
    if declared_evidence and str(declared_evidence) != bundle.evidence_id:
        note(STATUS_EVIDENCE_INVALID, REASON_BUNDLE_MISMATCH)
    if not intent_symbol or intent_symbol != bundle.symbol:
        note(STATUS_SYMBOL_MISMATCH, REASON_SYMBOL_MISMATCH)
    intent_identity = intent.get("identity")
    if intent_identity is not None:
        claimed = (
            intent_identity
            if isinstance(intent_identity, RuntimeIdentity)
            else RuntimeIdentity.model_validate(intent_identity)
        )
        if identity_mismatches(expected=bundle.identity, actual=claimed):
            note(STATUS_EVIDENCE_INVALID, REASON_IDENTITY_MISMATCH)
    declared_cycle = intent.get("cycle_id")
    if declared_cycle and str(declared_cycle) != bundle.cycle_id:
        note(STATUS_EVIDENCE_INVALID, REASON_CYCLE_MISMATCH)

    if action not in SUPPORTED_ACTIONS:
        note(STATUS_EVIDENCE_INVALID, REASON_UNKNOWN_ACTION)
    if not side:
        note(STATUS_EVIDENCE_INVALID, REASON_MISSING_SIDE)
    elif side not in SUPPORTED_SIDES:
        note(STATUS_EVIDENCE_INVALID, REASON_INVALID_SIDE)

    bound_inputs = [
        text for text in (
            str(item).strip() for item in (intent.get("bound_inputs") or [])
        ) if text
    ]
    if action == "OPEN" and not bound_inputs:
        note(STATUS_EVIDENCE_INVALID, REASON_MISSING_EVIDENCE_BINDING)
    if action in AUTHORIZED_ACTIONS and not str(
        intent.get("management_authorization_id") or ""
    ).strip():
        note(STATUS_EVIDENCE_INVALID, REASON_NEEDS_AUTH)

    missing_intervals = bundle.quality.get("missing_intervals") or []
    if missing_intervals:
        note(STATUS_EVIDENCE_INVALID, REASON_MISSING_REQUIRED_INTERVAL)
    if bundle.finality != "closed":
        note(STATUS_EVIDENCE_INVALID, REASON_BUNDLE_NOT_CLOSED)
    if bundle.expires_at_ms and now >= bundle.expires_at_ms:
        note(STATUS_EXPIRED, REASON_BUNDLE_EXPIRED)

    raw_claims = intent.get("claims")
    malformed_claims: set[int] = set()
    claims: list[Any] = []
    if raw_claims is None:
        claims = []
    elif isinstance(raw_claims, (list, tuple)):
        claims = list(raw_claims)
    else:
        note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_MALFORMED)
    parsed_claims = []
    for index, claim in enumerate(claims):
        if not isinstance(claim, Mapping):
            malformed_claims.add(index)
            note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_MALFORMED)
            parsed_claims.append(None)
            continue
        parsed = _parse_claim(claim)
        if parsed is None:
            note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_UNRESOLVED)
        parsed_claims.append(parsed)

    referenced: "OrderedDict[str, None]" = OrderedDict()
    for input_id in bound_inputs:
        referenced[input_id] = None
    literal_only: set[int] = set()
    claim_refs: list[str] = []
    for index, parsed in enumerate(parsed_claims):
        if parsed is None:
            continue
        _, _, _, lhs_token, _, rhs_token = parsed
        if _is_literal(lhs_token) and _is_literal(rhs_token):
            literal_only.add(index)
            note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_WITHOUT_EVIDENCE)
            continue
        for token in (lhs_token, rhs_token):
            _, refs, _ = resolve_operand(token, bundle)
            for ref in refs:
                referenced[ref] = None
                claim_refs.append(ref)
    bound_set = set(bound_inputs)
    if any(ref not in bound_set for ref in claim_refs) and (
        action != "OPEN" or bound_inputs
    ):
        note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_OUTSIDE_BOUND_INPUTS)
    input_issues = _referenced_input_issues(list(referenced), bundle, now)
    for codes in input_issues.values():
        for code in codes:
            status = STATUS_STALE if code == REASON_INPUT_STALE else STATUS_EVIDENCE_INVALID
            note(status, code)

    required_claim_kinds = [
        str(item) for item in (bundle.quality.get("required_claim_kinds") or [])
    ]
    declared_claim_kinds = {
        str(claim.get("kind"))
        for claim in claims
        if isinstance(claim, Mapping) and claim.get("kind")
    }
    for required in required_claim_kinds:
        if required not in CLAIM_KIND_REGISTRY:
            note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_KIND_UNKNOWN)
        if required not in declared_claim_kinds:
            note(STATUS_EVIDENCE_INVALID, REASON_MISSING_REQUIRED_CLAIM)

    claim_results: list[ClaimResult] = []
    for index, claim in enumerate(claims):
        claim_id = str(claim.get("claim_id") or "") if isinstance(claim, Mapping) else ""
        expr = str(claim.get("expr") or "") if isinstance(claim, Mapping) else ""
        if index in malformed_claims:
            claim_results.append(ClaimResult(
                claim_id=claim_id, expr=expr, actual=None,
                status="UNRESOLVED", reason_code=REASON_CLAIM_MALFORMED,
            ))
            continue
        parsed = parsed_claims[index]
        if parsed is None:
            claim_results.append(ClaimResult(
                claim_id=claim_id, expr=expr, actual=None,
                status="UNRESOLVED", reason_code=REASON_CLAIM_UNRESOLVED,
            ))
            continue
        _, _, raw_expected, lhs_token, op, rhs_token = parsed
        if not isinstance(raw_expected, bool):
            note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_EXPECTED_INVALID)
            claim_results.append(ClaimResult(
                claim_id=claim_id, expr=expr, actual=None,
                status="UNRESOLVED", reason_code=REASON_CLAIM_EXPECTED_INVALID,
            ))
            continue
        expected = raw_expected
        kind = (
            str(claim.get("kind"))
            if isinstance(claim, Mapping) and claim.get("kind")
            else ""
        )
        if kind:
            if kind not in CLAIM_KIND_REGISTRY:
                note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_KIND_UNKNOWN)
                claim_results.append(ClaimResult(
                    claim_id=claim_id, expr=expr, expected=expected, actual=None,
                    status="UNRESOLVED", reason_code=REASON_CLAIM_KIND_UNKNOWN,
                ))
                continue
            if not claim_kind_consistent(bundle, kind, op, lhs_token, rhs_token):
                note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_KIND_CONTRADICTION)
                claim_results.append(ClaimResult(
                    claim_id=claim_id, expr=expr, expected=expected, actual=None,
                    status="UNRESOLVED", reason_code=REASON_CLAIM_KIND_CONTRADICTION,
                ))
                continue
        if index in literal_only:
            claim_results.append(ClaimResult(
                claim_id=claim_id, expr=expr, expected=expected, actual=None,
                status="UNRESOLVED", reason_code=REASON_CLAIM_WITHOUT_EVIDENCE,
            ))
            continue
        lhs, lhs_refs, lhs_error = resolve_operand(lhs_token, bundle)
        rhs, rhs_refs, rhs_error = resolve_operand(rhs_token, bundle)
        refs = tuple(dict.fromkeys(lhs_refs + rhs_refs))
        claim_input_issue = next(
            (input_issues[ref] for ref in refs if ref in input_issues), None
        )
        if claim_input_issue is not None:
            claim_results.append(ClaimResult(
                claim_id=claim_id, expr=expr, expected=expected, actual=None,
                status="UNRESOLVED", reason_code=claim_input_issue[0],
            ))
            continue
        error = lhs_error or rhs_error
        if error is not None:
            claim_results.append(ClaimResult(
                claim_id=claim_id, expr=expr, expected=expected, actual=None,
                status="UNRESOLVED", reason_code=error,
            ))
            note(STATUS_EVIDENCE_INVALID, error)
            continue
        actual = bool(_compare(op, lhs, rhs))
        passed = actual == expected
        claim_results.append(ClaimResult(
            claim_id=claim_id, expr=expr, expected=expected, actual=actual,
            status="PASS" if passed else "FAIL",
            reason_code="" if passed else REASON_CLAIM_FALSE,
        ))
        if not passed:
            note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_FALSE)

    if not claims and action == "OPEN":
        note(STATUS_EVIDENCE_INVALID, REASON_MISSING_CLAIMS)

    quote = _normalise_quote(execution_quote)
    if action == "OPEN":
        if quote is None:
            note(STATUS_EVIDENCE_INVALID, REASON_MISSING_QUOTE)
        else:
            quote_symbol = str(quote.get("symbol") or "")
            if quote_symbol != intent_symbol:
                note(STATUS_EVIDENCE_INVALID, REASON_QUOTE_SYMBOL)
            price, price_error = _value_to_decimal(quote.get("price"))
            if price is None or price_error is not None:
                note(STATUS_EVIDENCE_INVALID, REASON_QUOTE_NON_FINITE)
            quote_expiry = quote.get("expires_at_ms")
            if quote_expiry is not None and now >= int(quote_expiry):
                note(STATUS_EVIDENCE_INVALID, REASON_QUOTE_EXPIRED)

    status = STATUS_VALID
    for candidate in VERDICT_STATUS_PRIORITY:
        if candidate in categories:
            status = candidate
            break
    verdict_id = "verdict-" + canonical_hash({
        "intent": intent_id,
        "evidence": bundle.evidence_id,
        "status": status,
        "reasons": reasons,
        "at": now,
    })[:16]
    return EvidenceVerdict(
        verdict_id=verdict_id,
        checked_intent_id=intent_id,
        intent_symbol=intent_symbol,
        evidence_id=bundle.evidence_id,
        status=status,
        reason_codes=tuple(reasons),
        claim_results=tuple(claim_results),
        checked_at_ms=now,
    )


class K02SchemaEnvelope(BaseModel):
    """schema 导出与冒烟校验入口；不是运行期数据结构。"""

    model_config = ConfigDict(extra="forbid")

    bundle: EvidenceBundle
    verdict: EvidenceVerdict
    bundle_verdict: BundleVerdict
    claim_result: ClaimResult
    execution_quote: ExecutionQuote
