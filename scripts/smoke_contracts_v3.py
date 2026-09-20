"""K01-K06 契约离线冒烟：执行 docs/contracts/golden/*.golden.json 的正反用例。

全程离线：不读取 data/、.env、账户或运行状态，不构造 Broker、不发网络请求、
不写运行目录。所有期望值来自 golden fixture 的 derivation（由规格/公式/
交易所语义手工推导），不是被测实现的输出回放。
"""
from __future__ import annotations

import json
import sys
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
    Incident,
    K01SchemaEnvelope,
    K02SchemaEnvelope,
    K03SchemaEnvelope,
    K04SchemaEnvelope,
    K05SchemaEnvelope,
    K06SchemaEnvelope,
    ManagementAuthorization,
    OrderQuantity,
    RELEASE_GROUPS,
    ResolutionNotAllowedError,
    SensitiveInputError,
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
    ingest_income,
    management_idempotency_key,
    plan_next_page,
    purpose_kind,
    resolve_incident,
    stable_incident_key,
    tally,
    validate_authorization,
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


def op_k01_secret_rejected(inputs: dict[str, Any]) -> dict[str, Any]:
    functions = {
        "artifact": hash_artifact,
        "dependency": hash_dependencies,
        "strategy": hash_strategy,
        "model_route": hash_model_route,
        "effective_config": hash_effective_config,
    }
    rejected: dict[str, bool] = {}
    for case in inputs["cases"]:
        try:
            functions[case["group"]](case["value"])
        except SensitiveInputError:
            rejected[case["name"]] = True
        else:
            rejected[case["name"]] = False
    control = inputs["control"]
    try:
        functions[control["group"]](control["value"])
    except SensitiveInputError:
        control_rejected = True
    else:
        control_rejected = False
    return {"rejected": rejected, "control_rejected": control_rejected}


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
    snapshot["identity"] = inputs["identity"]
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
            _intent_from(intent_spec), bundle, quote, now_ms=inputs["now_ms"],
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


def op_k03_ingest(inputs: dict[str, Any]) -> dict[str, Any]:
    result = ingest_income(inputs["page"], existing=inputs.get("existing") or None)
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
    allocations = allocate_weighted(
        inputs["source_amount"],
        inputs["weights"],
        method=inputs["method"],
        version=inputs["version"],
        evidence=inputs["evidence"],
        event_id=inputs["event_id"],
    )
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
    revision = apply_late_income(
        inputs["event"], inputs["closed_outcomes"], lifecycle_id=inputs["lifecycle_id"],
    )
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
    result = finalize_costs(
        inputs["lifecycle_id"],
        coverage=coverage,
        attribution=attribution,
        zero_fee_provable=bool(inputs.get("zero_fee_provable")),
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


def op_k04_late_fill(inputs: dict[str, Any]) -> dict[str, Any]:
    quantity = OrderQuantity.model_validate(inputs["quantity"])
    updated = apply_fill(quantity, inputs["fill_qty"])
    output = updated.to_components()
    output["violations"] = list(updated.violations())
    return output


def op_k04_authorization(inputs: dict[str, Any]) -> dict[str, Any]:
    authorization = ManagementAuthorization.model_validate(inputs["authorization"])
    verdict = validate_authorization(
        authorization,
        request=inputs["request"],
        now_ms=inputs["now_ms"],
        current_revision=inputs["current_revision"],
    )
    return {
        "authorization_id": verdict.authorization_id,
        "allowed": verdict.allowed,
        "reason_codes": list(verdict.reason_codes),
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
    except ResolutionNotAllowedError:
        output["resolve_error"] = "ResolutionNotAllowedError"
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
    return {"derived_state": derived, "violations": list(delivery_violations(delivery))}


def op_k05_counters(inputs: dict[str, Any]) -> dict[str, Any]:
    spans = [DecisionTraceSpan.model_validate(item) for item in inputs["spans"]]
    counters = tally(spans)
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
    "k01_identity_diff": op_k01_identity_diff,
    "k01_coverage": op_k01_coverage,
    "k02_validate": op_k02_validate,
    "k03_ingest": op_k03_ingest,
    "k03_pagination": op_k03_pagination,
    "k03_zero_fee": op_k03_zero_fee,
    "k03_attribution": op_k03_attribution,
    "k03_conservation": op_k03_conservation,
    "k03_allocate": op_k03_allocate,
    "k03_late_income": op_k03_late_income,
    "k03_finalize": op_k03_finalize,
    "k04_quantity": op_k04_quantity,
    "k04_late_fill": op_k04_late_fill,
    "k04_authorization": op_k04_authorization,
    "k04_idempotency": op_k04_idempotency,
    "k05_incident_recovery": op_k05_incident_recovery,
    "k05_delivery": op_k05_delivery,
    "k05_counters": op_k05_counters,
    "k05_incident_key": op_k05_incident_key,
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
