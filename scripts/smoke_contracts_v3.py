"""K01-K06 契约离线冒烟：执行 docs/contracts/golden/*.golden.json 的正反用例。

全程离线：不读取 data/、.env、账户或运行状态，不构造 Broker、不发网络请求、
不写运行目录。所有期望值来自 golden fixture 的 derivation（由规格/公式/
交易所语义手工推导），不是被测实现的输出回放。
"""
from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.contracts import (  # noqa: E402
    AlertDelivery,
    ConservationViolation,
    CostAttribution,
    DecisionTraceSpan,
    IdentityCoverageError,
    Incident,
    K01SchemaEnvelope,
    K02SchemaEnvelope,
    K03SchemaEnvelope,
    K04SchemaEnvelope,
    K05SchemaEnvelope,
    K06SchemaEnvelope,
    ManagementAuthorization,
    ManagementIntent,
    ModelRoute,
    OrderQuantity,
    QuantityError,
    RELEASE_GROUPS,
    ResolutionNotAllowedError,
    RuntimeIdentity,
    SensitiveInputError,
    SpanValidationError,
    TransitionError,
    UnclassifiedPurposeError,
    acknowledge,
    allocate_weighted,
    apply_fill,
    apply_late_income,
    attribute_income,
    build_bundle,
    build_identity,
    build_release_report,
    check_conservation,
    check_recovery,
    confirm_cancel,
    coverage_problems,
    decimal_text,
    delivery_violations,
    derive_management_id,
    evaluate_state,
    finalize_costs,
    hash_artifact,
    hash_dependencies,
    hash_effective_config,
    hash_model_route,
    hash_strategy,
    identity_mismatches,
    income_content_hash,
    ingest_income,
    management_idempotency_key,
    model_content_hash,
    plan_next_page,
    purpose_kind,
    request_cancel,
    resolve_incident,
    stable_incident_key,
    tally,
    transition_state,
    validate_authorization,
    validate_bundle,
    validate_coverage,
    validate_intent,
    zero_fee_proof,
)

GOLDEN_DIR = PROJECT_ROOT / "docs" / "contracts" / "golden"
SCHEMA_DIR = PROJECT_ROOT / "docs" / "contracts"

SCHEMA_MODELS = {
    "k01_runtime_identity.schema.json": K01SchemaEnvelope,
    "k02_evidence_bundle.schema.json": K02SchemaEnvelope,
    "k03_income_coverage.schema.json": K03SchemaEnvelope,
    "k04_management_intent.schema.json": K04SchemaEnvelope,
    "k05_decision_trace.schema.json": K05SchemaEnvelope,
    "k06_release_evidence.schema.json": K06SchemaEnvelope,
}

SYNTH_IDENTITY = {
    "schema_version": "3.1",
    "environment": "demo",
    "account_ref": "demo:SYNTH_ACCT",
    "artifact_hash": "artifact-synth-hash",
    "git_commit": "0123456789abcdef0123456789abcdef01234567",
    "dependency_hash": "dependency-synth-hash",
    "effective_config_hash": "effective-config-synth-hash",
    "strategy_hash": "strategy-synth-hash",
    "model_route_hash": "model-route-synth-hash",
    "runtime_instance_id": "inst-synth-r1",
}

passed = 0
failed = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def op_k01_hash_scope(inputs: dict[str, Any]) -> dict[str, Any]:
    def hashes(spec: dict[str, Any]) -> dict[str, str]:
        return {
            "artifact_hash": hash_artifact(spec["artifact_files"]),
            "dependency_hash": hash_dependencies(spec["dependency_lock_files"]),
            "strategy_hash": hash_strategy(spec["strategy_cards"]),
            "model_route_hash": hash_model_route(spec["model_route"]),
            "effective_config_hash": hash_effective_config(spec["effective_config"]),
        }

    base = hashes(inputs["base"])
    variant = hashes(inputs["variant"])
    return {"changed": {key: base[key] != variant[key] for key in base}}


def _hash_functions() -> dict[str, Any]:
    return {
        "artifact": hash_artifact,
        "dependency": hash_dependencies,
        "strategy": hash_strategy,
        "model_route": hash_model_route,
        "effective_config": hash_effective_config,
    }


def _secret_rejection(values: dict[str, Any]) -> dict[str, bool]:
    functions = _hash_functions()
    rejected: dict[str, bool] = {}
    for case in values["cases"]:
        try:
            functions[case["group"]](case["value"])
        except SensitiveInputError:
            rejected[case["name"]] = True
        else:
            rejected[case["name"]] = False
    control = values["control"]
    try:
        functions[control["group"]](control["value"])
    except SensitiveInputError:
        control_rejected = True
    else:
        control_rejected = False
    return {"rejected": rejected, "control_rejected": control_rejected}


def op_k01_secret_rejected(inputs: dict[str, Any]) -> dict[str, Any]:
    return _secret_rejection(inputs)


def op_k01_secret_payload(inputs: dict[str, Any]) -> dict[str, Any]:
    return _secret_rejection(inputs)


def op_k01_build_identity_coverage(inputs: dict[str, Any]) -> dict[str, Any]:
    base = dict(inputs["base"])
    errors: dict[str, list[str]] = {}
    for case in inputs["cases"]:
        spec = {**base, **case.get("overrides", {})}
        try:
            build_identity(**spec)
        except IdentityCoverageError as error:
            errors[case["name"]] = list(error.groups)
        else:
            errors[case["name"]] = []
    return {"errors": errors}


def op_k01_model_route(inputs: dict[str, Any]) -> dict[str, Any]:
    route = ModelRoute.model_validate(inputs["route"])
    identity = build_identity(**{**inputs["base"], "model_route": route})
    return {
        "ok": True,
        "model_route_hash_present": bool(identity.model_route_hash),
        "accepts_empty_fallbacks": route.fallback_routes == (),
    }


def op_k01_identity_diff(inputs: dict[str, Any]) -> dict[str, Any]:
    base = build_identity(**inputs["base"])
    variant = build_identity(**inputs["variant"])
    return {
        "artifact_hash_equal": base.artifact_hash == variant.artifact_hash,
        "identity_hash_equal": base.identity_hash() == variant.identity_hash(),
        "mismatch_fields": identity_mismatches(base, variant),
        "mismatch_ignoring_instance": identity_mismatches(base, variant, ignore=()),
    }


def op_k01_coverage(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"missing": validate_coverage(inputs["coverage"])}


def _bundle_from(inputs: dict[str, Any], spec: dict[str, Any]):
    snapshot = {
        key: spec[key]
        for key in (
            "symbol",
            "as_of_ms",
            "expires_at_ms",
            "evidence_id",
            "cycle_id",
            "environment",
            "account_ref",
        )
        if key in spec
    }
    snapshot["identity"] = (
        SYNTH_IDENTITY if inputs.get("identity") == "SYNTH" else inputs["identity"]
    )
    return build_bundle(snapshot, spec.get("bars") or [], spec.get("rules") or {})


def _intent_from(spec: dict[str, Any]) -> dict[str, Any]:
    intent = dict(spec)
    if "identity_override" in intent:
        intent["identity"] = intent.pop("identity_override")
    return intent


def op_k02_validate(inputs: dict[str, Any]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    verdict_ids: list[str] = []
    stable = False
    for name, intent_spec in inputs["intents"].items():
        bundle_spec = inputs["bundles"][name]
        bundle = _bundle_from(inputs, bundle_spec)
        if inputs.get("repeat"):
            repeat_bundle = _bundle_from(inputs, bundle_spec)
            stable = (
                bundle.content_hash == repeat_bundle.content_hash
                and bundle.evidence_id == repeat_bundle.evidence_id
            )
        quote = (inputs.get("quotes") or {}).get(name)
        verdict = validate_intent(
            _intent_from(intent_spec), bundle, quote,
            now_ms=inputs["now_ms"], rules=inputs.get("rules"),
        )
        verdict_ids.append(verdict.verdict_id)
        results[name] = {
            "status": verdict.status,
            "reason_codes": list(verdict.reason_codes),
            "checked_intent_id": verdict.checked_intent_id,
            "evidence_id": verdict.evidence_id,
            "claims": {
                item.claim_id: {"status": item.status, "actual": item.actual}
                for item in verdict.claim_results
            },
        }
    output: dict[str, Any] = {"results": results}
    if inputs.get("repeat"):
        output["stable"] = bool(stable)
    if len(verdict_ids) > 1:
        output["distinct_verdicts"] = len(set(verdict_ids)) == len(verdict_ids)
    return output


def op_k02_bundle_tamper(inputs: dict[str, Any]) -> dict[str, Any]:
    bundle = _bundle_from(inputs, inputs["bundle"])
    tamper = dict(inputs.get("tamper") or {"kind": "none"})
    kind = tamper.get("kind", "none")
    if kind == "input_value":
        new_inputs = tuple(
            item.model_copy(update={
                "values": {**item.values, tamper["key"]: tamper["value"]},
            }) if item.input_id == tamper["input_id"] else item
            for item in bundle.inputs
        )
        bundle = bundle.model_copy(update={"inputs": new_inputs})
    elif kind == "add_undated_input":
        extra = bundle.inputs[0].model_copy(update={
            "input_id": tamper["input_id"],
            "bar_close_time_ms": None,
            "finality": "closed",
            "interval": "1h",
            "values": {"close": "744.13"},
        })
        bundle = bundle.model_copy(update={"inputs": bundle.inputs + (extra,)})
        bundle = bundle.model_copy(update={"content_hash": model_content_hash(bundle)})
    elif kind == "add_symbolless_input":
        extra = bundle.inputs[-1].model_copy(update={
            "input_id": tamper["input_id"],
            "symbol": None,
            "finality": "closed",
            "bar_close_time_ms": bundle.as_of_ms,
            "values": {"close": "9.5"},
        })
        bundle = bundle.model_copy(update={"inputs": bundle.inputs + (extra,)})
        bundle = bundle.model_copy(update={"content_hash": model_content_hash(bundle)})
    elif kind == "quality_field":
        bundle = bundle.model_copy(update={
            "quality": {**bundle.quality, tamper["key"]: tamper["value"]},
        })
        bundle = bundle.model_copy(update={"content_hash": model_content_hash(bundle)})
    elif kind in ("fact_value", "fact_value_rehash"):
        new_facts = tuple(
            fact.model_copy(update={"value": tamper["value"]})
            if fact.fact_id == tamper["fact_id"] else fact
            for fact in bundle.derived_facts
        )
        bundle = bundle.model_copy(update={"derived_facts": new_facts})
        if kind == "fact_value_rehash":
            bundle = bundle.model_copy(
                update={"content_hash": model_content_hash(bundle)}
            )
    elif kind == "duplicate_fact":
        target = bundle.fact_by_id(tamper["fact_id"])
        bundle = bundle.model_copy(update={
            "derived_facts": bundle.derived_facts + (target,),
        })
        bundle = bundle.model_copy(update={"content_hash": model_content_hash(bundle)})
    elif kind == "repoint_fact":
        target = bundle.fact_by_id(tamper["fact_id"])
        source = bundle.input_by_id(tamper["input_id"])
        new_fact = target.model_copy(update={
            "inputs": (tamper["input_id"],),
            "value": str(source.values.get(tamper.get("key", "close"))),
        })
        new_facts = tuple(
            new_fact if fact.fact_id == tamper["fact_id"] else fact
            for fact in bundle.derived_facts
        )
        bundle = bundle.model_copy(update={"derived_facts": new_facts})
        bundle = bundle.model_copy(update={"content_hash": model_content_hash(bundle)})
    elif kind == "hash_only":
        bundle = bundle.model_copy(update={"content_hash": tamper["value"]})
    expected_identity = inputs.get("expected_identity")
    identity_model = (
        RuntimeIdentity.model_validate(expected_identity) if expected_identity else None
    )
    check = validate_bundle(
        bundle, rules=inputs.get("rules"), identity=identity_model,
    )
    output: dict[str, Any] = {
        "bundle_valid": check.valid,
        "bundle_reasons": list(check.reason_codes),
        "dropped_input_ids": list(bundle.dropped_input_ids),
        "quality_dedup_removed": list(bundle.quality.get("dedup_removed") or []),
    }
    if inputs.get("intent") is not None:
        verdict = validate_intent(
            _intent_from(inputs["intent"]),
            bundle,
            inputs.get("quote"),
            now_ms=inputs["now_ms"],
            rules=inputs.get("rules"),
        )
        output["status"] = verdict.status
        output["reason_codes"] = list(verdict.reason_codes)
        output["claims"] = {
            item.claim_id: {"status": item.status, "actual": item.actual}
            for item in verdict.claim_results
        }
    return output


def op_k03_ingest(inputs: dict[str, Any]) -> dict[str, Any]:
    source: Any = inputs.get("existing")
    if inputs.get("existing_pairs"):
        existing: dict[Any, str] = {}
        for pair in inputs["existing_pairs"]:
            key: Any = tuple(pair["key"]) if "key" in pair else pair["text_key"]
            existing[key] = income_content_hash(pair["event"])
        source = existing
    result = ingest_income(inputs["page"], existing=source)
    return {
        "inserted": list(result.inserted),
        "duplicates": list(result.duplicates),
        "quarantined": [
            {"key": item.key, "reason_code": item.reason_code}
            for item in result.quarantined
        ],
        "reason_codes": list(result.reason_codes),
    }


def op_k03_pagination(inputs: dict[str, Any]) -> dict[str, Any]:
    decision = plan_next_page(
        inputs["page"],
        start_ms=inputs["start_ms"],
        end_ms=inputs["end_ms"],
        page_limit=inputs["page_limit"],
    )
    return {
        "status": decision.status,
        "next_start_ms": decision.next_start_ms,
        "overlap_recheck": decision.overlap_recheck,
        "reason_codes": list(decision.reason_codes),
    }


def op_k03_zero_fee(inputs: dict[str, Any]) -> dict[str, Any]:
    proof = zero_fee_proof(
        inputs.get("coverage"),
        source_available=inputs["source_available"],
        pages_complete=inputs["pages_complete"],
        late_recheck_done=inputs["late_recheck_done"],
    )
    return {"provable": proof.provable, "reason_codes": list(proof.reason_codes)}


def op_k03_coverage_problems(inputs: dict[str, Any]) -> dict[str, Any]:
    return {"problems": coverage_problems(inputs.get("coverage"))}


def op_k03_attribution(inputs: dict[str, Any]) -> dict[str, Any]:
    result = attribute_income(
        inputs["events"], inputs["position_history"], inputs.get("coverage"),
    )
    return {
        "allocation_status": result.allocation_status,
        "coverage_status": result.coverage_status,
        "reason_codes": list(result.reason_codes),
        "decisions": [
            {
                "event_id": item.event_id,
                "status": item.status,
                "lifecycle_id": item.lifecycle_id,
                "reason_codes": list(item.reason_codes),
            }
            for item in result.decisions
        ],
    }


def op_k03_conservation(inputs: dict[str, Any]) -> dict[str, Any]:
    allocations = [CostAttribution.model_validate(item) for item in inputs["allocations"]]
    try:
        total = check_conservation(inputs["source_amount"], allocations)
    except ConservationViolation as error:
        return {
            "ok": False,
            "error": "ConservationViolation",
            "source_amount": str(error.source_amount),
            "allocated_total": str(error.allocated_total),
        }
    return {"ok": True, "total": decimal_text(total)}


def op_k03_allocate(inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        allocations = allocate_weighted(
            inputs["source_amount"],
            inputs["weights"],
            method=inputs["method"],
            version=inputs["version"],
            evidence=inputs["evidence"],
            event_id=inputs["event_id"],
        )
    except ValueError as error:
        return {"error": type(error).__name__}
    total = sum((item.allocated_amount for item in allocations), Decimal(0))
    return {
        "allocations": [
            {
                "lifecycle_id": item.lifecycle_id,
                "allocated_amount": decimal_text(item.allocated_amount),
                "method": item.method,
                "version": item.version,
                "event_id": item.event_id,
            }
            for item in allocations
        ],
        "total": decimal_text(total),
    }


def op_k03_late_income(inputs: dict[str, Any]) -> dict[str, Any]:
    try:
        revision = apply_late_income(
            inputs["event"], inputs["closed_outcomes"], lifecycle_id=inputs["lifecycle_id"],
        )
    except LookupError as error:
        return {"error": type(error).__name__}
    return {
        "outcome_id": revision.outcome_id,
        "source_event_id": revision.source_event_id,
        "reason_code": revision.reason_code,
        "requires_recalculation": revision.requires_recalculation,
        "new_closed_outcome_created": revision.new_closed_outcome_created,
    }


def op_k03_finalize(inputs: dict[str, Any]) -> dict[str, Any]:
    coverage = inputs.get("coverage")
    attribution = None
    if inputs.get("events"):
        attribution = attribute_income(
            inputs["events"], inputs.get("position_history") or [], coverage,
        )
    zero_fee: Any = inputs.get("zero_fee")
    if isinstance(zero_fee, Mapping) and zero_fee.get("from_coverage"):
        zero_fee = zero_fee_proof(
            zero_fee.get("coverage", coverage),
            source_available=zero_fee["source_available"],
            pages_complete=zero_fee["pages_complete"],
            late_recheck_done=zero_fee["late_recheck_done"],
        )
    result = finalize_costs(
        inputs["lifecycle_id"],
        coverage=coverage,
        attribution=attribution,
        zero_fee=zero_fee,
    )
    return {
        "cost_status": result.cost_status,
        "coverage_status": result.coverage_status,
        "attribution_status": result.attribution_status,
        "reason_codes": list(result.reason_codes),
    }


def op_k04_quantity(inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "violations": [
            list(OrderQuantity.model_validate(item).violations())
            for item in inputs["quantities"]
        ]
    }


def _components(quantity: OrderQuantity) -> dict[str, Any]:
    output = quantity.to_components()
    for key in (
        "accepted_qty",
        "cumulative_filled_qty",
        "working_leaves_qty",
        "terminal_unfilled_qty",
        "requested_qty",
    ):
        value = output.get(key)
        output[key] = decimal_text(Decimal(value)) if value is not None else None
    return output


def op_k04_late_fill(inputs: dict[str, Any]) -> dict[str, Any]:
    quantity = OrderQuantity.model_validate(inputs["quantity"])
    updated = apply_fill(quantity, inputs["fill_qty"])
    output = _components(updated)
    output["violations"] = list(updated.violations())
    return output


def op_k04_authorization(inputs: dict[str, Any]) -> dict[str, Any]:
    authorization = ManagementAuthorization.model_validate(inputs["authorization"])
    verdict = validate_authorization(
        authorization,
        request=inputs["request"],
        now_ms=inputs["now_ms"],
        current_revision=inputs["current_revision"],
        consumed_nonces=inputs.get("consumed_nonces") or (),
    )
    return {
        "authorization_id": verdict.authorization_id,
        "allowed": verdict.allowed,
        "submit_allowed": verdict.submit_allowed,
        "reconcile_allowed": verdict.reconcile_allowed,
        "reason_codes": list(verdict.reason_codes),
    }


def op_k04_apply_fills(inputs: dict[str, Any]) -> dict[str, Any]:
    quantity = OrderQuantity.model_validate(inputs["quantity"])
    applied: list[str] = []
    statuses: list[str] = []
    try:
        for fill in inputs["fills"]:
            quantity = apply_fill(
                quantity,
                fill["qty"],
                fill_id=fill.get("fill_id"),
                applied_fill_ids=applied,
            )
            statuses.append(quantity.status)
            if fill.get("fill_id"):
                applied.append(fill["fill_id"])
    except QuantityError as error:
        return {"error": type(error).__name__}
    output = _components(quantity)
    output["statuses"] = statuses
    output["violations"] = list(quantity.violations())
    return output


def op_k04_quantity_roundtrip(inputs: dict[str, Any]) -> dict[str, Any]:
    quantity = OrderQuantity.model_validate(inputs["quantity"])
    restored = OrderQuantity.model_validate(quantity.to_components())
    return {
        "roundtrip_violations": list(restored.violations()),
        "cancel_requested": restored.cancel_requested,
        "cancel_confirmed": restored.cancel_confirmed,
    }


def op_k04_cancel(inputs: dict[str, Any]) -> dict[str, Any]:
    quantity = OrderQuantity.model_validate(inputs["quantity"])
    try:
        if inputs["action"] == "request":
            updated = request_cancel(quantity)
        else:
            updated = confirm_cancel(quantity)
    except QuantityError as error:
        return {"error": type(error).__name__}
    return {
        "status": updated.status,
        "cancel_requested": updated.cancel_requested,
        "cancel_confirmed": updated.cancel_confirmed,
    }


def op_k04_intent(inputs: dict[str, Any]) -> dict[str, Any]:
    spec = dict(inputs["intent"])
    derive_kwargs = {
        "lifecycle_id": spec["lifecycle_id"],
        "trigger_type": spec["trigger_type"],
        "policy_version": spec["policy_version"],
        "trigger_event_id": spec["trigger_event_id"],
    }
    if spec.get("environment") is not None:
        derive_kwargs["environment"] = spec["environment"]
    derived = derive_management_id(**derive_kwargs)
    if inputs.get("rebind"):
        spec["management_id"] = derived
    try:
        intent = ManagementIntent.model_validate(spec)
    except ValueError as error:
        return {"error": type(error).__name__}
    return {
        "error": None,
        "rebound_ok": intent.management_id == derived,
        "management_id": intent.management_id,
    }


def op_k05_ack(inputs: dict[str, Any]) -> dict[str, Any]:
    incident = Incident.model_validate(inputs["incident"])
    try:
        updated = acknowledge(incident, now_ms=inputs["now_ms"])
    except TransitionError as error:
        return {"error": type(error).__name__, "status": incident.status}
    return {
        "error": None,
        "status": updated.status,
        "ack_at_ms_present": updated.ack_at_ms is not None,
    }


def op_k04_idempotency(inputs: dict[str, Any]) -> dict[str, Any]:
    keys = {
        round_name: management_idempotency_key(**inputs[round_name])
        for round_name in ("a", "b", "c")
    }
    identifiers = {
        round_name: derive_management_id(**inputs[round_name])
        for round_name in ("a", "b", "c")
    }
    return {
        "key_a": keys["a"],
        "key_b": keys["b"],
        "stable": keys["a"] == keys["b"] and identifiers["a"] == identifiers["b"],
        "distinct_on_event_change": identifiers["a"] != identifiers["c"],
    }


def op_k05_incident_recovery(inputs: dict[str, Any]) -> dict[str, Any]:
    incident = Incident.model_validate(inputs["incident"])
    if inputs.get("ack"):
        incident = acknowledge(incident, now_ms=inputs["now_ms"])
    evidence = inputs.get("evidence")
    if inputs.get("circular_evidence"):
        circular: dict[str, Any] = {}
        circular["fresh_account_snapshot"] = {
            "observed_at_ms": inputs["now_ms"],
            "value": True,
            "self": circular,
        }
        evidence = circular
    check = check_recovery(incident, evidence, now_ms=inputs["now_ms"])
    output: dict[str, Any] = {
        "recovered": check.recovered,
        "reason_codes": list(check.reason_codes),
        "resolve_error": None,
        "status": incident.status,
        "evidence_hash_present": False,
    }
    try:
        resolved = resolve_incident(
            incident, recovery_evidence=evidence, now_ms=inputs["now_ms"],
        )
    except ResolutionNotAllowedError as error:
        output["resolve_error"] = "ResolutionNotAllowedError"
        output["resolve_reason_codes"] = list(error.reason_codes)
    else:
        output["status"] = resolved.status
        output["evidence_hash_present"] = bool(resolved.resolution_evidence_hash)
    return output


def op_k05_delivery(inputs: dict[str, Any]) -> dict[str, Any]:
    delivery = AlertDelivery.model_validate(inputs["delivery"])
    derived = evaluate_state(
        submitted_at_ms=delivery.submitted_at_ms,
        provider_receipt=delivery.provider_receipt,
        delivered_at_ms=delivery.delivered_at_ms,
        ack_at_ms=delivery.ack_at_ms,
    )
    output: dict[str, Any] = {
        "derived_state": derived,
        "violations": list(delivery_violations(delivery)),
    }
    if inputs.get("transition"):
        try:
            updated = transition_state(
                delivery, inputs["transition"], now_ms=inputs.get("now_ms", 0),
            )
        except TransitionError as error:
            output["transition_error"] = type(error).__name__
            output["state"] = delivery.state
        else:
            output["transition_error"] = None
            output["state"] = updated.state
    return output


def op_k05_counters(inputs: dict[str, Any]) -> dict[str, Any]:
    spans = [DecisionTraceSpan.model_validate(item) for item in inputs["spans"]]
    try:
        counters = tally(spans)
    except SpanValidationError:
        return {"error": "SpanValidationError"}
    unknown_error = None
    if inputs.get("unknown_purpose"):
        try:
            purpose_kind(inputs["unknown_purpose"])
        except UnclassifiedPurposeError:
            unknown_error = "UnclassifiedPurposeError"
    return {
        "cycles_total": counters.cycles_total,
        "api_attempts_total": counters.api_attempts_total,
        "decisions_total": counters.decisions_total,
        "unknown_error": unknown_error,
    }


def op_k05_incident_key(inputs: dict[str, Any]) -> dict[str, Any]:
    key_a = stable_incident_key(**inputs["a"])
    key_b = stable_incident_key(**inputs["b"])
    key_c = stable_incident_key(**inputs["c"])
    return {
        "key_a": key_a,
        "key_b": key_b,
        "stable": key_a == key_b,
        "distinct_on_object_change": key_a != key_c,
    }


def op_k06_groups(inputs: dict[str, Any]) -> dict[str, Any]:
    report = build_release_report(
        inputs["groups"], identity_ref=inputs.get("identity_ref"),
    )
    focus = inputs.get("focus_groups") or list(RELEASE_GROUPS)
    dumped = report.model_dump(mode="json")
    allowed = {"schema_version", "groups", "blocking_items", "report_hash"}
    return {
        "statuses": {group: report.group_status(group) for group in focus},
        "blocking_codes": [
            {"group": item.group, "code": item.code}
            for item in report.blocking_items
            if item.group in focus
        ],
        "extra_top_level_fields": sorted(set(dumped) - allowed),
    }


OPERATIONS = {
    "k01_hash_scope": op_k01_hash_scope,
    "k01_secret_rejected": op_k01_secret_rejected,
    "k01_secret_payload": op_k01_secret_payload,
    "k01_identity_diff": op_k01_identity_diff,
    "k01_coverage": op_k01_coverage,
    "k01_build_identity_coverage": op_k01_build_identity_coverage,
    "k01_model_route": op_k01_model_route,
    "k02_validate": op_k02_validate,
    "k02_bundle_tamper": op_k02_bundle_tamper,
    "k03_ingest": op_k03_ingest,
    "k03_pagination": op_k03_pagination,
    "k03_zero_fee": op_k03_zero_fee,
    "k03_coverage_problems": op_k03_coverage_problems,
    "k03_attribution": op_k03_attribution,
    "k03_conservation": op_k03_conservation,
    "k03_allocate": op_k03_allocate,
    "k03_late_income": op_k03_late_income,
    "k03_finalize": op_k03_finalize,
    "k04_quantity": op_k04_quantity,
    "k04_late_fill": op_k04_late_fill,
    "k04_authorization": op_k04_authorization,
    "k04_idempotency": op_k04_idempotency,
    "k04_apply_fills": op_k04_apply_fills,
    "k04_quantity_roundtrip": op_k04_quantity_roundtrip,
    "k04_cancel": op_k04_cancel,
    "k04_intent": op_k04_intent,
    "k05_incident_recovery": op_k05_incident_recovery,
    "k05_delivery": op_k05_delivery,
    "k05_counters": op_k05_counters,
    "k05_incident_key": op_k05_incident_key,
    "k05_ack": op_k05_ack,
    "k06_groups": op_k06_groups,
}


def compare(expected: Any, actual: Any, path: str = "expect") -> list[str]:
    problems: list[str] = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, got {type(actual).__name__}"]
        for key, value in expected.items():
            if key not in actual:
                problems.append(f"{path}.{key}: missing in actual")
            else:
                problems.extend(compare(value, actual[key], f"{path}.{key}"))
        return problems
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        if len(expected) != len(actual):
            return [f"{path}: length {len(actual)} != expected {len(expected)}"]
        for index, value in enumerate(expected):
            problems.extend(compare(value, actual[index], f"{path}[{index}]"))
        return problems
    if expected != actual:
        return [f"{path}: {actual!r} != expected {expected!r}"]
    return []


def run_case(case: dict[str, Any]) -> None:
    label = f"{case['case_id']} {case['title']}"
    operation = OPERATIONS.get(case.get("operation", ""))
    if operation is None:
        check(label, False, f"unknown operation {case.get('operation')!r}")
        return
    try:
        actual = operation(case["inputs"])
    except Exception as error:
        check(label, False, f"raised {type(error).__name__}: {error}")
        return
    problems = compare(case["expect"], actual)
    check(label, not problems, "; ".join(problems[:4]))


def check_schemas() -> None:
    print("\n[schema] docs/contracts/*.schema.json 与冻结模型一致")
    for name, model in SCHEMA_MODELS.items():
        path = SCHEMA_DIR / name
        if not path.is_file():
            check(f"schema {name}", False, "file missing")
            continue
        committed = json.loads(path.read_text(encoding="utf-8"))
        generated = model.model_json_schema()
        check(f"schema {name}", committed == generated,
              "regenerate via model_json_schema()")


def main() -> int:
    files = sorted(GOLDEN_DIR.glob("*.golden.json"))
    check("golden files discovered (6)", len(files) == 6, f"found {len(files)}")
    case_count = 0
    positive = 0
    negative = 0
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        print(f"\n[{payload['contract']}] {payload['title']} ({path.name})")
        kinds = sorted({case.get("kind") for case in payload["cases"]})
        check(f"{payload['contract']} 含正反用例", kinds == ["negative", "positive"],
              f"kinds={kinds}")
        for case in payload["cases"]:
            case_count += 1
            if case.get("kind") == "positive":
                positive += 1
            else:
                negative += 1
            run_case(case)
    check_schemas()
    print(
        f"\n结果：PASS {passed} / FAIL {failed}；golden cases={case_count} "
        f"(positive={positive}, negative={negative})"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
