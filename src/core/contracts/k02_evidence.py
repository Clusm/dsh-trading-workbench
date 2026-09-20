"""K02 EvidenceBundle 与逐意图校验契约。

证据包只承载带来源、事件时间、接收时间与有效期的输入，以及由代码从原始
输入计算出的派生事实；自由文本不能作为执行依据。校验按 intent 单独进行，
verdict 必须携带 ``checked_intent_id``，两个意图不能互相借用 PASS。
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
REASON_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
REASON_CYCLE_MISMATCH = "CYCLE_MISMATCH"
REASON_SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
REASON_MISSING_REQUIRED_INTERVAL = "MISSING_REQUIRED_INTERVAL"
REASON_BUNDLE_NOT_CLOSED = "BUNDLE_NOT_CLOSED"
REASON_BUNDLE_EXPIRED = "BUNDLE_EXPIRED"
REASON_UNCLOSED_BAR = "UNCLOSED_BAR"
REASON_FUTURE_BAR = "FUTURE_BAR"
REASON_FUTURE_INPUT = "FUTURE_INPUT"
REASON_INPUT_STALE = "INPUT_STALE"
REASON_NON_FINITE = "NON_FINITE_VALUE"
REASON_CLAIM_FALSE = "CLAIM_FALSE"
REASON_CLAIM_UNRESOLVED = "CLAIM_UNRESOLVED"
REASON_MISSING_CLAIMS = "MISSING_CLAIMS"
REASON_MISSING_QUOTE = "MISSING_EXECUTION_QUOTE"
REASON_QUOTE_SYMBOL = "QUOTE_SYMBOL_MISMATCH"
REASON_QUOTE_NON_FINITE = "QUOTE_NON_FINITE"
REASON_QUOTE_EXPIRED = "QUOTE_EXPIRED"

_CLAIM_PATTERN = re.compile(
    r"^\s*(?P<lhs>[^\s<>=!]+)\s*(?P<op>>=|<=|==|!=|>|<)\s*(?P<rhs>[^\s<>=!]+)\s*$"
)


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
    claim: Mapping[str, Any],
) -> tuple[str, str, bool | None, str, str, str] | None:
    expr = str(claim.get("expr") or "")
    match = _CLAIM_PATTERN.match(expr)
    if not match:
        return None
    expected = claim.get("expected", True)
    return (
        str(claim.get("claim_id") or ""),
        expr,
        expected if isinstance(expected, bool) else None,
        match.group("lhs"),
        match.group("op"),
        match.group("rhs"),
    )


def _referenced_input_issues(
    refs: Sequence[str],
    bundle: EvidenceBundle,
    now_ms: int,
) -> dict[str, tuple[str, ...]]:
    """返回 输入ID -> reason_code 元组；未闭合、未来、过期、缺失都在此暴露。"""
    issues: dict[str, tuple[str, ...]] = {}
    stale_marked = set(bundle.quality.get("stale_inputs") or ())
    for input_id in refs:
        codes: list[str] = []
        item = bundle.input_by_id(input_id)
        if item is None:
            issues[input_id] = (REASON_CLAIM_UNRESOLVED,)
            continue
        if item.finality != "closed":
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


def build_bundle(
    snapshot: Mapping[str, Any],
    closed_bars: Sequence[Mapping[str, Any]],
    rules: Mapping[str, Any],
    *,
    cycle_id: str | None = None,
    identity: RuntimeIdentity | Mapping[str, Any] | None = None,
) -> EvidenceBundle:
    """由显式快照、K 线与规则构建证据包；纯函数，无 IO。

    只从已收盘K线生成派生事实；未闭合/未来的 bar 保留为输入供审计，
    但不会进入事实计算。evidence_id 在未显式给出时是确定性的。
    """
    snap = dict(snapshot)
    if "symbol" not in snap or "as_of_ms" not in snap:
        raise ValueError("snapshot requires symbol and as_of_ms")
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
    seen: set[str] = set()
    for index, bar in enumerate(closed_bars):
        input_id = str(bar.get("input_id") or f"bar-{index}")
        close_time = int(bar.get("close_time_ms") or bar.get("bar_close_time_ms") or 0)
        explicit_finality = bar.get("finality")
        if explicit_finality:
            finality = str(explicit_finality)
        else:
            finality = "closed" if close_time <= as_of_ms else "live"
        if close_time > as_of_ms:
            finality = "live"
        values = {str(key): value for key, value in (bar.get("values") or {}).items()}
        dedup_key = canonical_hash({
            "symbol": bar.get("symbol") or symbol,
            "interval": bar.get("interval"),
            "close_time_ms": close_time,
            "values": values,
        })
        if dedup_key in seen:
            dropped.append(input_id)
            continue
        seen.add(dedup_key)
        max_age = rules.get("max_bar_age_ms")
        if (
            max_age is not None
            and finality == "closed"
            and as_of_ms - close_time > int(max_age)
        ):
            stale.append(input_id)
        inputs.append(EvidenceInput(
            input_id=input_id,
            source=str(bar.get("source") or "unknown"),
            event_time_ms=int(bar.get("event_time_ms") or close_time),
            received_time_ms=int(bar.get("received_time_ms") or as_of_ms),
            valid_until_ms=bar.get("valid_until_ms"),
            value_ref=str(bar.get("value_ref") or f"bar:{input_id}"),
            symbol=str(bar.get("symbol") or symbol),
            interval=bar.get("interval"),
            finality=finality,
            bar_close_time_ms=close_time or None,
            values=values,
        ))

    required_intervals = [str(item) for item in (rules.get("required_intervals") or [])]
    closed_by_interval: dict[str, list[EvidenceInput]] = {}
    for item in inputs:
        if item.interval and item.finality == "closed":
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

    if not inputs:
        bundle_finality = "unknown"
    elif required_intervals and not missing_intervals:
        bundle_finality = "closed"
    else:
        bundle_finality = "live"

    expires_at_ms = snap.get("expires_at_ms")
    if expires_at_ms is None:
        expires_at_ms = as_of_ms + int(rules.get("expires_in_ms") or 60_000)
    seed = {
        "cycle_id": cycle,
        "symbol": symbol,
        "as_of_ms": as_of_ms,
        "inputs": [item.input_id for item in inputs],
    }
    evidence_id = str(snap.get("evidence_id") or f"eb-{canonical_hash(seed)[:16]}")
    quality = {
        "missing_intervals": missing_intervals,
        "stale_inputs": stale,
        "dedup_removed": dropped,
        "unclosed_inputs": [
            item.input_id for item in inputs if item.finality != "closed"
        ],
        "as_of_ms": as_of_ms,
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
) -> EvidenceVerdict:
    """逐 intent 校验证据绑定、时效、finality、有限数值与结构化 claim。"""
    intent_id = str(intent.get("intent_id") or "")
    if not intent_id:
        raise ValueError("intent requires intent_id")
    now = int(now_ms if now_ms is not None else bundle.as_of_ms)
    intent_symbol = str(intent.get("symbol") or "")
    action = str(intent.get("action") or "OPEN").upper()
    reasons: list[str] = []
    categories: dict[str, list[str]] = {}

    def note(status: str, code: str) -> None:
        if code not in reasons:
            reasons.append(code)
        categories.setdefault(status, []).append(code)

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

    missing_intervals = bundle.quality.get("missing_intervals") or []
    if missing_intervals:
        note(STATUS_EVIDENCE_INVALID, REASON_MISSING_REQUIRED_INTERVAL)
    if bundle.finality != "closed":
        note(STATUS_EVIDENCE_INVALID, REASON_BUNDLE_NOT_CLOSED)
    if bundle.expires_at_ms and now >= bundle.expires_at_ms:
        note(STATUS_EXPIRED, REASON_BUNDLE_EXPIRED)

    claims = list(intent.get("claims") or [])
    parsed_claims = []
    for claim in claims:
        parsed = _parse_claim(claim)
        if parsed is None:
            note(STATUS_EVIDENCE_INVALID, REASON_CLAIM_UNRESOLVED)
        parsed_claims.append(parsed)

    referenced: "OrderedDict[str, None]" = OrderedDict()
    for input_id in intent.get("bound_inputs") or []:
        referenced[str(input_id)] = None
    for parsed in parsed_claims:
        if parsed is None:
            continue
        _, _, _, lhs_token, _, rhs_token = parsed
        for token in (lhs_token, rhs_token):
            _, refs, _ = resolve_operand(token, bundle)
            for ref in refs:
                referenced[ref] = None
    input_issues = _referenced_input_issues(list(referenced), bundle, now)
    for codes in input_issues.values():
        for code in codes:
            status = STATUS_STALE if code == REASON_INPUT_STALE else STATUS_EVIDENCE_INVALID
            note(status, code)

    claim_results: list[ClaimResult] = []
    for index, claim in enumerate(claims):
        parsed = parsed_claims[index]
        if parsed is None:
            claim_results.append(ClaimResult(
                claim_id=str(claim.get("claim_id") or ""),
                expr=str(claim.get("expr") or ""),
                actual=None,
                status="UNRESOLVED",
                reason_code=REASON_CLAIM_UNRESOLVED,
            ))
            continue
        claim_id, expr, expected, lhs_token, op, rhs_token = parsed
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
        passed = actual == expected if expected is not None else actual
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
            if quote_symbol and quote_symbol != intent_symbol:
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
    claim_result: ClaimResult
    execution_quote: ExecutionQuote
