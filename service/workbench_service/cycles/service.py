"""Cycle / evidence / AI audit facade (M1.3c, demo-only, no signed trading)."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import Settings
from ..errors import ServiceError
from ..market.evidence import canonical_hash, verify_hash
from ..market.transport import MarketUpstreamError
from .store import CycleStore, decode_json_fields, evidence_file_path

CYCLE_STATES = frozenset({"RUNNING", "DONE", "FAILED", "SKIPPED", "CANCELLED"})
TERMINAL_STATES = frozenset({"DONE", "FAILED", "SKIPPED", "CANCELLED"})
OUTCOMES = frozenset({"trade", "no_trade", "error"})
STATE_OUTCOMES: dict[str, frozenset[str]] = {
    "DONE": frozenset({"trade", "no_trade"}),
    "FAILED": frozenset({"error"}),
    "SKIPPED": frozenset({"no_trade"}),
    "CANCELLED": frozenset({"no_trade"}),
}
STATE_DEFAULT_OUTCOME = {
    "DONE": "no_trade",
    "FAILED": "error",
    "SKIPPED": "no_trade",
    "CANCELLED": "no_trade",
}
AGENTS = frozenset(
    {"analysis", "red", "blue", "decision", "risk", "execution", "operator", "review"}
)
AGENT_ALIASES = {"red_team": "red", "blue_team": "blue"}
_AGENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "analysis": (
        "schema_version", "cycle_id", "agent", "market_read", "symbols_analyzed",
        "candidates", "primary_symbol", "stand_aside", "confidence", "evidence_refs",
        "warnings",
    ),
    "red": (
        "schema_version", "cycle_id", "agent", "symbol", "side", "round",
        "responds_to", "rebuttal", "tradeable", "confidence", "thesis", "evidence",
        "trigger", "plan_draft", "invalidation", "opponent_steelman",
        "no_case_reason", "warnings",
    ),
    "blue": (
        "schema_version", "cycle_id", "agent", "symbol", "side", "round",
        "responds_to", "rebuttal", "tradeable", "confidence", "thesis", "evidence",
        "trigger", "plan_draft", "invalidation", "opponent_steelman",
        "no_case_reason", "warnings",
    ),
    "decision": (
        "schema_version", "cycle_id", "agent", "action", "side", "selected_case",
        "symbol", "confidence", "thesis", "case_comparison", "score_override",
        "override_rationale", "plan", "no_trade_reason", "invalidation",
        "evidence_refs", "warnings",
    ),
    "risk": ("schema_version", "cycle_id", "agent", "verdict"),
    "execution": ("schema_version", "cycle_id", "agent", "action"),
    "operator": ("schema_version", "cycle_id", "agent"),
    "review": ("schema_version", "cycle_id", "agent"),
}
#: T22 §审计产物：Host 写的是 `risk_advice/1` / `execution_entry/1`，字段名与服务端
#: 规范名不同。这里**显式记录两套命名的映射**，而不是把校验放宽成“缺字段也算通过”：
#: 每组至少要命中一个来源字段；命中之后还要过下面的取值检查（枚举/类型）。
#: 历史 `ai_outputs` 行按旧口径写入，不重写（见 evidence/T22-产物校验对齐.md）。
_ARTIFACT_FIELD_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    # risk_advice/1：recommendation ∈ APPROVE | DOWNSIZE | REJECT（服务端旧名 verdict）
    "risk": {"verdict": ("verdict", "recommendation")},
    # execution_entry/1：executable(bool) + order_plan.action("open")（服务端旧名 action）
    "execution": {"action": ("action", "executable", "order_plan.action")},
}

#: 只能取自 Host schema 的封闭枚举；出现表外取值说明产物本身不可用。
_RISK_VERDICT_VALUES = frozenset({"APPROVE", "DOWNSIZE", "REJECT"})


def _artifact_value(output: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    """按 ``order_plan.action`` 这类一层点号路径取值；缺任一环即视为缺失。"""

    node: Any = output
    for part in str(dotted).split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _resolve_artifact_field(
    output: Mapping[str, Any], names: Sequence[str]
) -> tuple[bool, str, Any]:
    """返回 (是否存在, 命中的字段名, 值)；按 ``names`` 顺序取第一个命中的。"""

    for name in names:
        present, value = _artifact_value(output, name)
        if present:
            return True, name, value
    return False, "", None


def _artifact_value_errors(
    agent: str, resolved: Mapping[str, tuple[str, Any]]
) -> list[dict[str, Any]]:
    """按 Host schema 的封闭取值检查“命中的别名是否真的是可用值”。

    只检查 Host 名词（`recommendation` / `executable`），服务端旧名（`verdict` /
    `action`）保持既有语义不变——避免把历史调用方的写法判成无效。
    """

    errors: list[dict[str, Any]] = []
    if agent == "risk":
        source, value = resolved.get("verdict", ("", None))
        if source == "recommendation" and not isinstance(value, str):
            errors.append(
                {"code": "E_SCHEMA_TYPE", "field": "recommendation", "expected": "string"}
            )
        elif source == "recommendation":
            text = value.strip().upper()
            if text not in _RISK_VERDICT_VALUES:
                errors.append(
                    {
                        "code": "E_SCHEMA_ENUM",
                        "field": "recommendation",
                        "value": value[:40],
                        "allowed": sorted(_RISK_VERDICT_VALUES),
                    }
                )
    if agent == "execution":
        source, value = resolved.get("action", ("", None))
        if source == "executable" and not isinstance(value, bool):
            errors.append(
                {"code": "E_SCHEMA_TYPE", "field": "executable", "expected": "boolean"}
            )
    return errors


_AGENT_RE = re.compile(r"^[a-z_]{2,20}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{2,160}$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
_MAX_SAFE_INT = 2**53 - 1

_LOCK = threading.Lock()


def _attempt(value: Any, default: int = 1) -> int:
    """Validate a positive, JSON-safe attempt number (422 on bad input)."""

    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServiceError(422, "E_VALIDATION", "attempt must be a positive integer")
    if value < 1 or value > _MAX_SAFE_INT:
        raise ServiceError(422, "E_VALIDATION", "attempt must be within 1..2^53-1")
    return int(value)


def _agent_key(agent: str) -> str:
    return AGENT_ALIASES.get(agent, agent)


def _cursor_parts(cursor: str | None) -> tuple[str | None, str | None]:
    if not cursor:
        return None, None
    parts = cursor.split("|", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ServiceError(422, "E_VALIDATION", "cursor must be '<created_at>|<cycle_id>'")
    return parts[0], parts[1]



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_cycle_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"cyc-{stamp}-{secrets.token_hex(3)}"


def new_ai_run_id(sequence: int = 1) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(3)}-r{sequence}"


def _json_default(value: Any) -> Any:
    """JSON-safe scalar encoding for values produced by risk/account snapshots.

    Decimal is intentionally a string (never float) so amounts keep their
    exact scale/precision in the audit row. Unknown types must still fail
    closed rather than being silently stringified.
    """

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return value


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
#: T13: bounded raw model output kept for schema/semantic failures.
_RAW_RESPONSE_MAX_CHARS = 8192


def _iso_ms_from(ms: int) -> str:
    """Millisecond-precision ISO UTC timestamp (T13: D needs ms, not seconds)."""

    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_iso_ms(value: Any) -> int | None:
    """Parse an ISO-8601 UTC timestamp into epoch milliseconds; None when invalid."""

    if not isinstance(value, str) or not _ISO_RE.match(value.strip()):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(round(parsed.timestamp() * 1000))


def _epoch_ms(value: Any) -> int | None:
    """Accept an int epoch-ms (or a numeric string) without inventing precision."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return int(value) if value > 0 else None
    if isinstance(value, float):
        return int(value) if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _positive_int(value: Any) -> int | None:
    """Positive integer or None; 0/negative are *not* valid durations/timeouts."""

    parsed = _epoch_ms(value)
    return parsed


def _decimal_text(value: Any) -> str | None:
    """Decimal TEXT for money-like values; None when it is not a finite number."""

    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (ArithmeticError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return format(parsed.normalize(), "f")




# ---------------------------------------------------------------- T13 tracing
# The four D time points come from per-call timestamps; run/event/cost facts below
# are recorded only when a real source exists, otherwise NULL + an explicit reason.
_EVENT_TYPE_KEYS = ("event_type", "event", "type", "kind")
_EVENT_ROLE_KEYS = ("role_key", "role", "agent")
_EVENT_STATUS_TO_STATE = {"ok": "DONE", "invalid": "INVALID", "error": "FAILED"}


def _run_started_facts(payload: Mapping[str, Any]) -> tuple[str | None, str, str | None]:
    """(started_at, source, reason) for a new AI run.

    The pre-T13 code wrote the audit-persist (= finalize) moment here. Now the start
    is written only when the caller reports it; otherwise NULL + reason, and
    put_call() later derives it from the first per-call timestamp.
    """

    if payload.get("started_at_ms") is not None:
        ms = _epoch_ms(payload.get("started_at_ms"))
        if ms is None:
            raise ServiceError(422, "E_VALIDATION", "started_at_ms must be a positive epoch-ms")
        return _iso_ms_from(ms), "caller", None
    if payload.get("started_at") is not None:
        parsed = _parse_iso_ms(payload.get("started_at"))
        if parsed is None:
            raise ServiceError(
                422,
                "E_VALIDATION",
                "started_at must be an ISO-8601 UTC timestamp",
            )
        return _iso_ms_from(parsed), "caller", None
    return None, "unknown", "caller_did_not_supply_started_at"


def _timeout_facts(payload: Mapping[str, Any]) -> tuple[int | None, str | None, str | None]:
    """(timeout_ms, source, reason): a real effective timeout or an explicit gap.

    The service itself never calls an LLM (the Host owns the chain budget and the
    per-role timeouts), so the only valid source today is the caller reporting the
    timeout that actually applied. A reported 0 is *not* "no timeout": it is
    invalid and is recorded as a reason instead of a fake number.
    """

    raw = payload.get("timeout_ms")
    if raw is None:
        return None, None, "no_timeout_source_configured"
    value = _positive_int(raw)
    if value is None:
        invalid_zero = str(raw).strip() in {"0", "0.0", "-0", "-0.0"}
        reason = "reported_zero_is_not_a_timeout" if invalid_zero else "invalid_timeout_ms"
        return None, None, reason
    return value, "caller", None


def _cost_facts(payload: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """(cost_estimate, cost_source, reason) — money only from a declared price source.

    The service has no model-price table, so it never derives a cost from tokens.
    A cost value without a named price source is treated as unusable (NULL +
    reason) rather than trusted: this is what keeps failure attribution honest.
    """

    raw = payload.get("cost_estimate")
    if raw is None:
        return None, None, "no_reliable_price_source"
    cost = _decimal_text(raw)
    if cost is None:
        return None, None, "invalid_cost_estimate"
    source = str(payload.get("cost_source") or "").strip()
    if not source:
        return None, None, "cost_source_missing"
    return cost, source[:120], None


def _raw_capture_facts(
    payload: Mapping[str, Any], validation_errors: Sequence[Any]
) -> tuple[str | None, int | None, str | None, str | None]:
    """(excerpt, truncated, reason, sha256) for a failed model output.

    Bounded excerpt into the existing audit row (ai_calls), never a new table. When
    the caller does not ship the raw text, the row says so explicitly instead of
    leaving a silent hole downstream readers could mistake for "nothing to see".
    """

    raw = payload.get("response_text")
    if raw is None:
        raw = payload.get("raw_response_text")
    if isinstance(raw, str) and raw.strip():
        truncated = len(raw) > _RAW_RESPONSE_MAX_CHARS
        excerpt = raw[:_RAW_RESPONSE_MAX_CHARS]
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return excerpt, 1 if truncated else 0, None, digest
    if validation_errors:
        return None, None, "caller_did_not_supply_response_text", None
    return None, None, None, None


def _normalize_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Map the Host event payload onto ai_run_events columns.

    The Host sends {event_type, role_key, at, status, call_id, ...}; the pre-T13
    reader only looked at {event, role, ts} and therefore stored the literal
    'event' with a NULL role for every row. There is intentionally **no**
    placeholder fallback now: an event without a type is rejected (422) instead of
    being written as a fake, half-usable row.
    """

    body = dict(payload or {})
    event_type: str | None = None
    event_source: str | None = None
    for key in _EVENT_TYPE_KEYS:
        raw = body.get(key)
        if isinstance(raw, str) and raw.strip():
            event_type = raw.strip()[:64]
            event_source = "event_type" if key == "event_type" else "event_field"
            break
    if not event_type:
        raise ServiceError(
            422,
            "E_VALIDATION",
            "event requires a non-empty event_type (no placeholder rows are written)",
        )

    role: str | None = None
    for key in _EVENT_ROLE_KEYS:
        raw = body.get(key)
        if isinstance(raw, str) and raw.strip():
            role = raw.strip()[:40]
            break

    ts_ms = (
        _parse_iso_ms(body.get("at"))
        or _epoch_ms(body.get("ended_at_ms"))
        or _epoch_ms(body.get("started_at_ms"))
        or _epoch_ms(body.get("ts_ms"))
        or _parse_iso_ms(body.get("ts"))
    )
    if ts_ms is None:
        # Explicit gap: ts_ms NULL means ts is the insert time, not the event time.
        ts = _now()
    else:
        ts = _iso_ms_from(ts_ms)

    from_state: str | None = None
    to_state: str | None = None
    status = str(body.get("status") or "").strip().lower()
    if event_type == "role_start":
        to_state = "RUNNING"
    elif event_type == "role_end":
        from_state = "RUNNING"
        to_state = _EVENT_STATUS_TO_STATE.get(status) if status else None

    reason_code = body.get("error_code") or body.get("reason_code")
    payload_ref = body.get("call_id") or body.get("payload_ref")
    return {
        "event": event_type,
        "role": role,
        "ts": ts,
        "ts_ms": ts_ms,
        "from_state": from_state,
        "to_state": to_state,
        "reason_code": None if reason_code is None else str(reason_code)[:120],
        "payload_ref": None if payload_ref is None else str(payload_ref)[:160],
        "event_source": event_source,
    }


class CycleService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = CycleStore(settings)
        self._cleanup_orphan_evidence()
        self._reconcile_terminal_ai_runs()

    # ------------------------------------------------------------------ cycle
    def create(self, payload: Mapping[str, Any], *, market: Any, execution: Any) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ServiceError(422, "E_VALIDATION", "request body must be an object")
        body = dict(payload or {})
        trigger = str(body.get("trigger") or "manual").strip().lower()
        if trigger not in {"auto", "manual"}:
            raise ServiceError(422, "E_VALIDATION", "trigger must be auto or manual")
        slot_id = body.get("slot_id")
        if slot_id is not None:
            slot_id = str(slot_id).strip()
            if not slot_id or len(slot_id) > 160:
                raise ServiceError(422, "E_VALIDATION", "slot_id must be 1..160 characters")
        prev_pick = body.get("prev_pick")
        if prev_pick is not None:
            prev_pick = str(prev_pick).strip().upper()[:20] or None
        symbol = body.get("symbol")
        if symbol is not None:
            symbol = str(symbol).strip().upper()
            if not _SYMBOL_RE.match(symbol):
                raise ServiceError(
                    422, "E_VALIDATION", "symbol must be an uppercase USDT symbol",
                    details={"symbol": symbol},
                )
            whitelist = getattr(getattr(market, "settings", None), "whitelist", None)
            if whitelist and symbol not in whitelist:
                raise ServiceError(
                    422, "E_SYMBOL_NOT_ALLOWED", "symbol is not in the market whitelist",
                    details={"symbol": symbol},
                )

        with _LOCK:
            running = self.store.running()
            if running is not None:
                raise ServiceError(
                    409,
                    "E_CYCLE_RUNNING",
                    "a cycle is already RUNNING",
                    details={"cycle_id": running.get("cycle_id")},
                )
            if slot_id and self.store.slot_exists(slot_id):
                raise ServiceError(
                    409,
                    "E_CYCLE_DUPLICATE_SLOT",
                    "slot_id already used",
                    details={"slot_id": slot_id},
                )

            cycle_id = new_cycle_id()
            account, positions = self._account_snapshot(execution)
            held = [str(item.get("symbol")) for item in positions if item.get("symbol")]
            started_at = _now()
            evidence_summary, evidence_payload = self._build_evidence(
                market, cycle_id=cycle_id, held=held, prev_pick=prev_pick
            )
            evidence_summary.pop("abs_path", None)
            try:
                evidence_path = evidence_file_path(
                    self.settings, str(evidence_summary.get("path") or "")
                )
                evidence_size = evidence_path.stat().st_size
            except (OSError, ValueError) as exc:
                raise ServiceError(
                    503,
                    "E_EVIDENCE_UNAVAILABLE",
                    "market evidence file is missing or outside data_dir",
                    details={"path": str(evidence_summary.get("path") or "")},
                ) from exc
            expected_size = int(evidence_summary.get("bytes") or 0)
            if not evidence_path.is_file() or evidence_size != expected_size:
                raise ServiceError(
                    503,
                    "E_EVIDENCE_UNAVAILABLE",
                    "market evidence size does not match its DB reference",
                    details={
                        "path": str(evidence_summary.get("path") or ""),
                        "bytes": evidence_size,
                        "expected_bytes": expected_size,
                    },
                )
            evidence_summary["path"] = evidence_path.relative_to(
                Path(self.settings.data_dir).resolve()
            ).as_posix()
            envelope = self._envelope(execution)
            produced_at_ms = int(evidence_payload.get("produced_at_ms") or _now_ms())
            # T13: millisecond build duration next to the millisecond produced_at_ms;
            # the second-precision produced_at string is display-only.
            build_ms = evidence_summary.get("evidence_build_ms")
            if build_ms is None and evidence_summary.get("duration_ms") is not None:
                build_ms = int(round(float(evidence_summary["duration_ms"])))
            evidence_ref = {
                "cycle_id": cycle_id,
                "schema_version": str(evidence_payload.get("schema_version") or "md-evidence/1"),
                "produced_at_ms": produced_at_ms,
                "verdict": str(evidence_summary.get("verdict") or "UNKNOWN"),
                "score": float(evidence_summary.get("score") or 0.0),
                "path": str(evidence_summary.get("path") or ""),
                "sha256": str(evidence_summary.get("sha256") or ""),
                "bytes": int(evidence_summary.get("bytes") or 0),
                "prompt_tokens": int(evidence_summary.get("prompt_token_estimate") or 0),
                "evidence_build_ms": None if build_ms is None else int(build_ms),
                "created_at": started_at,
            }
            if not evidence_ref["path"] or not evidence_ref["sha256"]:
                raise ServiceError(503, "E_EVIDENCE_UNAVAILABLE", "market evidence has no persisted reference")
            self.store.upsert_evidence(evidence_ref)
            row = {
                "cycle_id": cycle_id,
                "slot_id": slot_id,
                "state": "RUNNING",
                "outcome": None,
                "trigger": trigger,
                "symbol": symbol,
                "started_at": started_at,
                "finished_at": None,
                "reason_code": None,
                "error_code": None,
                "execution_json": None,
                "decision_json": None,
                "evidence_snapshot_id": evidence_summary.get("snapshot_id"),
                "account_json": _json(account),
                "risk_envelope_json": _json(envelope),
                "created_at": started_at,
                "updated_at": started_at,
            }
            try:
                self.store.insert_cycle(row)
            except Exception as exc:  # sqlite IntegrityError is intentionally mapped
                # Compensate the evidence reference/file: POST is all-or-nothing.
                self.store.delete_evidence(cycle_id)
                try:
                    evidence_file_path(self.settings, evidence_ref["path"]).unlink(missing_ok=True)
                except (OSError, ValueError):
                    pass
                if "UNIQUE" in str(exc).upper():
                    raise ServiceError(
                        409, "E_CYCLE_DUPLICATE_SLOT", "slot_id already used"
                    ) from exc
                raise
            return {
                "cycle_id": cycle_id,
                "state": "RUNNING",
                "trigger": trigger,
                "slot_id": slot_id,
                "started_at": started_at,
                "evidence": {
                    "cycle_id": cycle_id,
                    **evidence_summary,
                    "schema_version": evidence_ref["schema_version"],
                    "produced_at": evidence_payload.get("produced_at"),
                    # T13: D must use the millisecond facts, not the second-precision
                    # produced_at string (0–999 ms of error erodes the 15 s TTL).
                    "produced_at_ms": produced_at_ms,
                    "produced_at_iso_ms": _iso_ms_from(produced_at_ms),
                    "evidence_build_ms": evidence_ref["evidence_build_ms"],
                    "evidence": evidence_payload,
                },
                "account": account,
                "risk_envelope": envelope,
            }

    def list_cycles(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        limit = max(1, min(200, int(limit)))
        cursor_at, cursor_id = _cursor_parts(cursor)
        with self.store._connect() as conn:
            if cursor_at is not None:
                rows = conn.execute(
                    "SELECT * FROM cycle_runs "
                    "WHERE (COALESCE(created_at, started_at) < ? "
                    "OR (COALESCE(created_at, started_at) = ? AND cycle_id < ?)) "
                    "ORDER BY COALESCE(created_at, started_at) DESC, cycle_id DESC LIMIT ?",
                    (cursor_at, cursor_at, cursor_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM cycle_runs "
                    "ORDER BY COALESCE(created_at, started_at) DESC, cycle_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        cycle_rows = [dict(row) for row in rows]
        cycles = [self._cycle_view(row) for row in cycle_rows]
        next_cursor = None
        if len(cycle_rows) >= limit:
            last = cycle_rows[-1]
            next_cursor = f"{last.get('created_at') or last.get('started_at')}|{last['cycle_id']}"
        return {"cycles": cycles, "count": len(cycles), "next_cursor": next_cursor}

    def get_cycle(self, cycle_id: str) -> dict[str, Any]:
        detail = self.store.detail(cycle_id)
        if detail is None:
            raise ServiceError(404, "E_CYCLE_NOT_FOUND", "cycle not found", details={"cycle_id": cycle_id})
        evidence = detail["evidence"]
        if evidence is not None:
            evidence = dict(evidence)
            try:
                _row, payload = self._load_evidence(cycle_id)
                evidence.update(
                    verified=True,
                    snapshot_id=payload.get("snapshot_id"),
                )
            except ServiceError as exc:
                evidence.update(verified=False, verify_error=exc.code)
        return {
            "cycle": self._cycle_view(detail["cycle"]),
            "evidence": evidence,
            "agents": detail["agents"],
            # T13: views carry started_at_trusted / legacy_unusable / *_known flags.
            "ai_runs": [self._ai_run_view(row) for row in detail["ai_runs"]],
            "outputs": [
                self._output_view(row) for row in detail["outputs"]
            ],
            "calls": self._calls_for_cycle(cycle_id),
            "events": self._events_for_cycle(cycle_id),
        }

    def evidence(self, cycle_id: str, *, agent: str | None = None) -> dict[str, Any]:
        self._require_cycle(cycle_id)
        row, payload = self._load_evidence(cycle_id)
        produced_at_ms = int(row.get("produced_at_ms") or 0)
        return {
            "cycle_id": cycle_id,
            "snapshot_id": payload.get("snapshot_id"),
            "agent": agent,
            "path": row["path"],
            "sha256": row["sha256"],
            "bytes": int(row.get("bytes") or 0),
            "schema_version": row["schema_version"],
            "verdict": row["verdict"],
            # T13: millisecond facts for the four time points (NULL = legacy row).
            "produced_at_ms": produced_at_ms or None,
            "produced_at_iso_ms": _iso_ms_from(produced_at_ms) if produced_at_ms else None,
            "evidence_build_ms": row.get("evidence_build_ms"),
            "evidence": payload,
        }

    def _load_evidence(self, cycle_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        row = self.store.get_evidence(cycle_id)
        if row is None:
            raise ServiceError(404, "E_EVIDENCE_NOT_FOUND", "evidence reference not found")
        expected_bytes = int(row.get("bytes") or 0)
        try:
            path = evidence_file_path(self.settings, str(row["path"]))
            before = path.stat()
            if not path.is_file() or before.st_size != expected_bytes:
                raise ServiceError(
                    500,
                    "E_EVIDENCE_CORRUPT",
                    "evidence file size does not match the DB reference",
                    details={"bytes": before.st_size, "expected_bytes": expected_bytes},
                )
            raw = path.read_text(encoding="utf-8")
            after = path.stat()
            if after.st_size != expected_bytes or after.st_size != before.st_size:
                raise ServiceError(
                    500,
                    "E_EVIDENCE_CORRUPT",
                    "evidence file changed while being read",
                    details={"bytes": after.st_size, "expected_bytes": expected_bytes},
                )
            payload = json.loads(raw)
        except ServiceError:
            raise
        except (OSError, ValueError) as exc:
            raise ServiceError(500, "E_EVIDENCE_CORRUPT", "evidence file is unreadable") from exc
        expected = str(row["sha256"])
        actual = "sha256:" + canonical_hash(payload)
        if expected != actual or not verify_hash(payload):
            raise ServiceError(500, "E_EVIDENCE_CORRUPT", "evidence hash mismatch")
        return dict(row), dict(payload)

    def finalize(self, cycle_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(payload or {})
        state = str(body.get("state") or "").upper()
        if state not in TERMINAL_STATES:
            raise ServiceError(422, "E_VALIDATION", "state must be DONE/FAILED/SKIPPED/CANCELLED")
        raw_outcome = body.get("outcome")
        outcome = str(raw_outcome).lower() if raw_outcome is not None else STATE_DEFAULT_OUTCOME[state]
        if outcome not in OUTCOMES:
            raise ServiceError(422, "E_VALIDATION", "outcome must be trade/no_trade/error")
        if outcome not in STATE_OUTCOMES[state]:
            raise ServiceError(
                422,
                "E_VALIDATION",
                "outcome is not allowed for the requested terminal state",
                details={"state": state, "outcome": outcome, "allowed": sorted(STATE_OUTCOMES[state])},
            )
        cycle = self.store.get_cycle(cycle_id)
        if cycle is None:
            raise ServiceError(404, "E_CYCLE_NOT_FOUND", "cycle not found")
        if cycle.get("state") in TERMINAL_STATES:
            if cycle.get("state") == state and cycle.get("outcome") == outcome:
                self._finalize_ai_runs(cycle_id, state, outcome)
                return {"cycle": self._cycle_view(cycle), "idempotent": True}
            raise ServiceError(
                409,
                "E_CYCLE_FINALIZED",
                "cycle already finalized with a different result",
                details={"state": cycle.get("state"), "outcome": cycle.get("outcome")},
            )
        updated = self.store.finalize_cycle(
            cycle_id,
            state=state,
            outcome=outcome,
            reason_code=body.get("reason"),
            error_code=(body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else None,
            execution=body.get("execution"),
        )
        if not updated:
            current = self.store.get_cycle(cycle_id)
            if current is not None and current.get("state") == state and current.get("outcome") == outcome:
                self._finalize_ai_runs(cycle_id, state, outcome)
                return {"cycle": self._cycle_view(current), "idempotent": True}
            raise ServiceError(409, "E_CYCLE_STATE_CONFLICT", "cycle state changed concurrently")
        self._finalize_ai_runs(cycle_id, state, outcome)
        return {"cycle": self._cycle_view(self.store.get_cycle(cycle_id) or cycle), "idempotent": False}

    def _finalize_ai_runs(self, cycle_id: str, state: str, outcome: str) -> None:
        """Keep AI run state/ended_at/token totals/cost consistent with cycle finalize.

        T13: the finalize moment is written to its own column (finalized_at) instead
        of being written into started_at; a still-unknown run start is derived from
        the first per-call timestamp; the run cost is only summed when every call
        carries a cost from the same declared price source (otherwise NULL + reason).
        """

        run_state = "FAILED" if (state == "FAILED" or outcome == "error") else "DONE"
        ended_at = _now()
        with self.store._connect() as conn:
            runs = conn.execute(
                "SELECT ai_run_id, state, ended_at, cost_estimate, cost_source, "
                "cost_reason FROM ai_runs WHERE cycle_id=?",
                (cycle_id,),
            ).fetchall()
            for run in runs:
                if run["state"] not in ("RUNNING", "PENDING") and run["ended_at"]:
                    continue
                usage = conn.execute(
                    "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
                    "FROM ai_calls WHERE ai_run_id=?",
                    (run["ai_run_id"],),
                ).fetchone()
                cost_estimate, cost_source, cost_reason = self._run_cost(conn, dict(run))
                conn.execute(
                    "UPDATE ai_runs SET state=?, outcome=?, ended_at=?, finalized_at=?, "
                    "token_input=?, token_output=?, cost_estimate=?, cost_source=?, "
                    "cost_reason=?, updated_at=? WHERE ai_run_id=?",
                    (
                        run_state,
                        outcome,
                        ended_at,
                        ended_at,
                        int(usage[0] or 0),
                        int(usage[1] or 0),
                        cost_estimate,
                        cost_source,
                        cost_reason,
                        ended_at,
                        run["ai_run_id"],
                    ),
                )
                # Derive the real start from the per-call timestamps when unknown.
                # Must reuse this connection: a nested one would block on our own
                # write transaction (SQLite has no intra-process lock sharing).
                self.store.apply_run_started(conn, str(run["ai_run_id"]))

    @staticmethod
    def _run_cost(
        conn: sqlite3.Connection, run: Mapping[str, Any]
    ) -> tuple[str | None, str | None, str | None]:
        """(cost_estimate, cost_source, cost_reason) for one run.

        A run cost is a sum of per-call costs only when *every* call reports a cost
        from one and the same declared price source. Anything else stays NULL with
        a reason: the service never fills gaps with 0 or with invented rates.
        """

        if run.get("cost_estimate") is not None:
            return (
                run["cost_estimate"],
                run.get("cost_source"),
                run.get("cost_reason"),
            )
        rows = conn.execute(
            "SELECT cost_estimate, cost_source FROM ai_calls WHERE ai_run_id=?",
            (run["ai_run_id"],),
        ).fetchall()
        if not rows:
            return None, None, "no_calls_recorded"
        total = Decimal("0")
        sources: set[str] = set()
        missing = 0
        for row in rows:
            value = row["cost_estimate"]
            if value is None:
                missing += 1
                continue
            try:
                total += Decimal(str(value))
            except (ArithmeticError, ValueError):
                missing += 1
                continue
            sources.add(str(row["cost_source"] or ""))
        if missing:
            return None, None, "partial_cost_missing"
        if not sources or sources == {""}:
            return None, None, "cost_source_missing"
        if len(sources) > 1:
            return None, None, "mixed_cost_sources"
        return format(total.normalize(), "f"), next(iter(sources)), None

    def _calls_for_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_calls WHERE cycle_id=? ORDER BY created_at, call_id LIMIT 200",
                (cycle_id,),
            ).fetchall()
            return [self._call_view(row) for row in rows]

    def _events_for_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_run_events WHERE cycle_id=? ORDER BY seq LIMIT 200",
                (cycle_id,),
            ).fetchall()
            return [self._event_view(row) for row in rows]

    # ------------------------------------------------------------------ AI audit
    def create_ai_run(self, cycle_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        cycle = self._require_running_cycle(cycle_id)
        requested = payload.get("ai_run_id")
        if requested:
            existing = self.store.get_ai_run(str(requested))
            if existing is not None:
                if existing.get("cycle_id") != cycle_id:
                    raise ServiceError(409, "E_AI_RUN_CONFLICT", "ai_run_id belongs to another cycle")
                return {"ai_run": self._ai_run_view(existing), "idempotent": True}
        ai_run_id = str(requested or new_ai_run_id(len(self.store.ai_runs(cycle_id)) + 1))
        now = _now()
        # T13: started_at is the run start or NULL + reason — never the persist moment.
        started_at, started_source, started_reason = _run_started_facts(payload)
        cost_estimate, cost_source, cost_reason = _cost_facts(payload)
        row = {
            "ai_run_id": ai_run_id,
            "cycle_id": cycle_id,
            "mode": str(payload.get("mode") or "FULL"),
            "trigger": str(payload.get("trigger") or cycle.get("trigger") or "manual"),
            "state": str(payload.get("state") or "RUNNING"),
            "outcome": None,
            "reason_code": None,
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "prompt_set": payload.get("prompt_set"),
            "started_at": started_at,
            "ended_at": None,
            "finalized_at": None,
            "started_at_source": started_source,
            "started_at_reason": started_reason,
            "config_json": _json(payload.get("config") or {}),
            "token_input": payload.get("token_input"),
            "token_output": payload.get("token_output"),
            "cost_estimate": cost_estimate,
            "cost_source": cost_source,
            "cost_reason": cost_reason,
            "error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        try:
            self.store.insert_ai_run(row)
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise ServiceError(409, "E_AI_RUN_CONFLICT", "ai_run_id already exists") from exc
            raise
        return {"ai_run": self._ai_run_view(row), "idempotent": False}

    def put_call(self, ai_run_id: str, call_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not _CALL_ID_RE.match(call_id or ""):
            raise ServiceError(422, "E_VALIDATION", "invalid call_id")
        run = self.store.get_ai_run(ai_run_id)
        if run is None:
            raise ServiceError(404, "E_AI_RUN_NOT_FOUND", "ai run not found")
        self._require_running_cycle(str(run["cycle_id"]))
        attempt = _attempt(payload.get("attempt"))
        validation_errors = payload.get("validation_errors") or []
        if not isinstance(validation_errors, Sequence) or isinstance(validation_errors, (str, bytes)):
            raise ServiceError(422, "E_VALIDATION", "validation_errors must be an array")
        validation_errors = list(validation_errors)
        # T13: ms timestamps are the authoritative per-call facts for the four time
        # points; the ISO columns stay for compatibility/readability.
        started_ms = _epoch_ms(payload.get("started_at_ms")) or _parse_iso_ms(payload.get("started_at"))
        ended_ms = _epoch_ms(payload.get("ended_at_ms")) or _parse_iso_ms(payload.get("ended_at"))
        timeout_ms, timeout_source, timeout_reason = _timeout_facts(payload)
        cost_estimate, cost_source, cost_reason = _cost_facts(payload)
        raw_text, raw_truncated, raw_reason, raw_sha256 = _raw_capture_facts(
            payload, validation_errors
        )
        started_at = payload.get("started_at")
        if not isinstance(started_at, str) or not started_at.strip():
            started_at = None if started_ms is None else _iso_ms_from(started_ms)
        ended_at = payload.get("ended_at")
        if not isinstance(ended_at, str) or not ended_at.strip():
            ended_at = None if ended_ms is None else _iso_ms_from(ended_ms)
        response_sha256 = payload.get("response_sha256")
        if response_sha256 is None and raw_sha256 is not None:
            # Derived from the raw text we actually received, never invented.
            response_sha256 = raw_sha256
        elif raw_sha256 is not None and str(response_sha256) != raw_sha256:
            # The archived text does not match the recorded hash: keep both facts and
            # say so, instead of silently presenting a mismatched archive as verified.
            raw_reason = "response_sha256_mismatch"
        row = {
            "call_id": call_id,
            "ai_run_id": ai_run_id,
            "cycle_id": str(run["cycle_id"]),
            "role": str(payload.get("role") or "unknown"),
            "attempt": attempt,
            "phase": payload.get("phase"),
            "provider": payload.get("provider"),
            "model": payload.get("model"),
            "reasoning_effort": payload.get("reasoning_effort"),
            "temperature": payload.get("temperature"),
            "max_tokens": payload.get("max_tokens"),
            "prompt_version": payload.get("prompt_version"),
            "output_schema_version": payload.get("output_schema_version"),
            "started_at": started_at,
            "ended_at": ended_at,
            "started_at_ms": started_ms,
            "ended_at_ms": ended_ms,
            "latency_ms": payload.get("latency_ms"),
            "timeout_ms": timeout_ms,
            "timeout_source": timeout_source,
            "timeout_reason": timeout_reason,
            "status": payload.get("status"),
            "finish_reason": payload.get("finish_reason"),
            "error_code": payload.get("error_code"),
            "error_message": payload.get("error_message"),
            "input_tokens": payload.get("input_tokens"),
            "output_tokens": payload.get("output_tokens"),
            "cache_read_tokens": payload.get("cache_read_tokens"),
            "cache_write_tokens": payload.get("cache_write_tokens"),
            "total_tokens": payload.get("total_tokens"),
            "usage_estimated": int(bool(payload.get("usage_estimated"))),
            "cost_estimate": cost_estimate,
            "cost_source": cost_source,
            "cost_reason": cost_reason,
            "request_sha256": payload.get("request_sha256"),
            "response_sha256": response_sha256,
            "request_ref": payload.get("request_ref"),
            "response_ref": payload.get("response_ref"),
            "raw_response_text": raw_text,
            "raw_response_truncated": raw_truncated,
            "raw_capture_reason": raw_reason,
            "evidence_snapshot_id": payload.get("evidence_snapshot_id"),
            "session_id": payload.get("session_id"),
            "purpose": payload.get("purpose"),
            "validation_errors_json": _json(validation_errors),
            "created_at": _now(),
        }
        existing = self.store.get_call(call_id)
        if existing is not None:
            immutable = [key for key in row if key != "created_at"]
            if all(existing.get(key) == row[key] for key in immutable):
                return {"call": self._call_view(existing), "idempotent": True}
            raise ServiceError(409, "E_AI_CALL_CONFLICT", "call_id already recorded with different content")
        self.store.insert_call(row)
        # T13: the run start becomes a real measurement once the first call lands.
        self.store.refresh_run_started_from_calls(ai_run_id)
        return {"call": self._call_view(row), "idempotent": False}

    def append_event(self, ai_run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append one AI run event with its real type/role/time (T13).

        The service normalizes the payload (event_type -> event, role_key -> role,
        at/ended_at_ms -> ts/ts_ms, status -> to_state) instead of storing the old
        literal 'event' placeholder. Events without a type are rejected with 422:
        a half-usable row is worse than an explicit refusal. Rows written before T13
        keep their placeholder values (history is not rewritten) and the cycle
        detail API flags them legacy_unusable=true.
        """

        run = self.store.get_ai_run(ai_run_id)
        if run is None:
            raise ServiceError(404, "E_AI_RUN_NOT_FOUND", "ai run not found")
        self._require_running_cycle(str(run["cycle_id"]))
        event = _normalize_event(payload or {})
        seq = self.store.insert_event(
            ai_run_id=ai_run_id, cycle_id=str(run["cycle_id"]), event=event
        )
        return {"seq": seq, "ai_run_id": ai_run_id, "event": self._event_view(event)}

    def put_artifact(self, cycle_id: str, agent: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if (
            not _AGENT_RE.match(agent or "")
            or (agent not in AGENTS and agent not in AGENT_ALIASES)
        ):
            raise ServiceError(422, "E_VALIDATION", "unsupported agent")
        agent = _agent_key(agent)
        self._require_running_cycle(cycle_id)
        ai_run_id = str(payload.get("ai_run_id") or "")
        run = self.store.get_ai_run(ai_run_id)
        if run is None:
            raise ServiceError(404, "E_AI_RUN_NOT_FOUND", "ai run not found")
        if run.get("cycle_id") != cycle_id:
            raise ServiceError(409, "E_AI_RUN_CONFLICT", "ai run belongs to another cycle")
        attempt = _attempt(payload.get("attempt"))
        output = payload.get("output")
        if not isinstance(output, Mapping):
            raise ServiceError(422, "E_VALIDATION", "output must be an object")
        semantic_errors = self._artifact_semantic_errors(
            agent, cycle_id, output, payload.get("semantic_errors")
        )
        valid = bool(payload.get("valid", True)) and not semantic_errors
        output_id = str(
            payload.get("output_id")
            or f"out-{cycle_id}-{ai_run_id}-{agent}-{attempt}"
        )
        existing = self.store.get_output(output_id)
        if existing is not None:
            if (
                existing.get("cycle_id") != cycle_id
                or existing.get("agent") != agent
                or existing.get("ai_run_id") != ai_run_id
                or int(existing.get("attempt") or 0) != attempt
                or existing.get("output_json") != _json(output)
                or existing.get("semantic_errors_json") != _json(semantic_errors)
                or int(existing.get("valid") or 0) != int(valid)
            ):
                raise ServiceError(409, "E_AI_OUTPUT_CONFLICT", "output_id already recorded")
            return {
                "output": self._output_view(existing),
                "idempotent": True,
            }
        row = {
            "output_id": output_id,
            "call_id": payload.get("call_id"),
            "cycle_id": cycle_id,
            "agent": agent,
            "ai_run_id": ai_run_id,
            "attempt": attempt,
            "schema_version": payload.get("schema_version") or output.get("schema_version"),
            "valid": int(valid),
            "output_json": _json(output),
            "semantic_errors_json": _json(semantic_errors),
            "output_sha256": _hash_json(output),
            "created_at": _now(),
        }
        self.store.insert_output(row)
        self.store.upsert_agent(
            cycle_id=cycle_id,
            agent=agent,
            ai_run_id=ai_run_id,
            attempt=attempt,
            output_id=output_id,
            status="valid" if row["valid"] else "invalid",
        )
        return {
            "output": self._output_view(row),
            "idempotent": False,
        }

    # ------------------------------------------------------------------ helpers
    def _output_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        view = decode_json_fields(row, ("output_json", "semantic_errors_json"))
        view["semantic_errors"] = view.pop("semantic_errors_json", [])
        view["valid"] = bool(view.get("valid"))
        return view
    def _ai_run_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """T13: expose whether started_at is a real run start.

        Historical rows have started_at_source NULL: their started_at is the pre-T13
        finalize-time approximation and must not be used as a round start.
        """

        view = dict(row)
        source = view.get("started_at_source")
        view["started_at_trusted"] = source in {"caller", "earliest_call"}
        view["started_at_note"] = (
            None
            if view["started_at_trusted"]
            else "pre-T13 row: started_at is a finalize-time approximation; use ai_calls timestamps"
        )
        return view

    def _call_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """T13: make every optional tracing fact self-describing.

        The *_known flags tell a downstream consumer whether a value is a
        measurement or an explicit gap, so 0/NULL can no longer be read as
        "no timeout" / "zero cost". Pre-T13 rows (no timeout_source/cost_source)
        are labelled as such instead of pretending the gap was recorded.
        """

        view = dict(row)
        view["timeout_known"] = view.get("timeout_ms") is not None
        view["cost_known"] = view.get("cost_estimate") is not None
        view["timing_known"] = view.get("started_at_ms") is not None
        if view.get("timeout_source") is None and view.get("timeout_reason") is None:
            view["timeout_reason"] = "pre-T13 row: timeout was never recorded"
        if view.get("cost_source") is None and view.get("cost_reason") is None:
            view["cost_reason"] = "pre-T13 row: cost was never recorded"
        return view

    def _event_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """T13: flag deprecated pre-T13 event rows (literal placeholder event)."""

        view = dict(row)
        placeholder = (
            str(view.get("event") or "") == "event"
            and view.get("role") is None
            and view.get("event_source") is None
        )
        view["legacy_unusable"] = placeholder
        if placeholder:
            view["deprecated_note"] = (
                "pre-T13 placeholder row (event='event', role NULL): do not use for "
                "event timelines; use ai_calls timestamps"
            )
        return view
    def _require_cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.store.get_cycle(cycle_id)
        if cycle is None:
            raise ServiceError(404, "E_CYCLE_NOT_FOUND", "cycle not found")
        return cycle

    def _require_running_cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle = self._require_cycle(cycle_id)
        state = cycle.get("state")
        if state in TERMINAL_STATES:
            raise ServiceError(
                409,
                "E_CYCLE_FINALIZED",
                "cycle is terminal; audit rows are immutable",
                details={"cycle_id": cycle_id, "state": state},
            )
        if state != "RUNNING":
            raise ServiceError(409, "E_CYCLE_NOT_RUNNING", "cycle is not RUNNING")
        return cycle

    def _artifact_semantic_errors(
        self,
        agent: str,
        cycle_id: str,
        output: Mapping[str, Any],
        supplied: Any,
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        if supplied is not None:
            if not isinstance(supplied, list):
                raise ServiceError(422, "E_VALIDATION", "semantic_errors must be an array")
            for item in supplied:
                if isinstance(item, Mapping):
                    errors.append(dict(item))
                else:
                    errors.append({"code": "E_SEMANTIC", "detail": str(item)[:240]})
        required = _AGENT_REQUIRED_FIELDS.get(agent, ())
        aliases = _ARTIFACT_FIELD_ALIASES.get(agent, {})
        missing: list[str] = []
        resolved: dict[str, tuple[str, Any]] = {}
        for field in required:
            present, source, value = _resolve_artifact_field(
                output, aliases.get(field, (field,))
            )
            if present:
                resolved[field] = (source, value)
            else:
                missing.append(field)
        if missing:
            error: dict[str, Any] = {"code": "E_SCHEMA_MISSING", "fields": missing}
            if aliases:
                # 明确告诉调用方“这两套命名等价”，缺的是**语义字段**而不是某个拼写。
                error["accepted"] = {
                    field: list(names) for field, names in aliases.items()
                }
            errors.append(error)
        else:
            errors.extend(_artifact_value_errors(agent, resolved))
        if output.get("cycle_id") is not None and str(output.get("cycle_id")) != cycle_id:
            errors.append({"code": "E_SCHEMA_CYCLE_ID"})
        out_agent = output.get("agent")
        if out_agent is not None and _agent_key(str(out_agent)) != agent:
            errors.append({"code": "E_SCHEMA_AGENT"})
        return errors

    def _cycle_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return decode_json_fields(
            row, ("execution_json", "decision_json", "account_json", "risk_envelope_json")
        )

    def _cleanup_orphan_evidence(self) -> None:
        """Startup cleanup for evidence written before a cycle row committed."""

        try:
            with self.store._connect() as conn:
                rows = conn.execute(
                    "SELECT e.cycle_id, e.path FROM evidence_snapshots e "
                    "LEFT JOIN cycle_runs c ON c.cycle_id = e.cycle_id "
                    "WHERE c.cycle_id IS NULL"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "DELETE FROM evidence_snapshots WHERE cycle_id=?", (row["cycle_id"],)
                    )
                    try:
                        evidence_file_path(self.settings, str(row["path"])).unlink(
                            missing_ok=True
                        )
                    except (OSError, ValueError):
                        pass
        except sqlite3.Error:
            # Schema may not exist yet on a fresh/partially initialized DB.
            pass

    def _reconcile_terminal_ai_runs(self) -> None:
        """Backfill AI run terminal state after a service restart/upgrade."""

        try:
            with self.store._connect() as conn:
                rows = conn.execute(
                    "SELECT cycle_id, state, outcome FROM cycle_runs "
                    "WHERE state IN ('DONE','FAILED','SKIPPED','CANCELLED')"
                ).fetchall()
            for row in rows:
                self._finalize_ai_runs(
                    str(row["cycle_id"]),
                    str(row["state"]),
                    str(row["outcome"] or "error"),
                )
        except sqlite3.Error:
            pass

    def _account_snapshot(self, execution: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            status = execution.status() or {}
        except Exception as exc:  # noqa: BLE001 - no credentials/read-only remains usable
            return {
                "venue": self.settings.venue,
                "credentials_configured": False,
                "positions": [],
                "equity": {"available": False, "source": "none"},
                "readiness": {"new_entry_allowed": False, "reasons": ["E_ACCOUNT_UNAVAILABLE"]},
                "error": f"{type(exc).__name__}: {exc}",
            }, []
        positions = status.get("positions") or []
        account = {
            "venue": status.get("venue", "demo"),
            "demo_only": True,
            "credentials_configured": bool(status.get("credentials_configured")),
            "equity": status.get("equity"),
            "account": status.get("account"),
            "positions": positions,
            "protection": status.get("protection"),
            "readiness": status.get("readiness"),
            "checked_at": status.get("checked_at"),
        }
        return account, list(positions)

    def _envelope(self, execution: Any) -> dict[str, Any]:
        try:
            return execution.envelope() or {}
        except Exception as exc:  # noqa: BLE001
            return {
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
                "new_entry_allowed": False,
            }

    def _build_evidence(
        self,
        market: Any,
        *,
        cycle_id: str,
        held: Sequence[str],
        prev_pick: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            built = market.build_evidence(
                cycle_id=cycle_id, held_symbols=list(held), prev_pick=prev_pick
            )
        except MarketUpstreamError as exc:
            raise ServiceError(
                503,
                "E_MARKET_UNAVAILABLE",
                "market evidence unavailable",
                details={"upstream_code": getattr(exc, "code", None)},
            ) from exc
        except Exception as exc:  # noqa: BLE001 - fail closed, don't create a cycle
            raise ServiceError(
                503,
                "E_EVIDENCE_UNAVAILABLE",
                "market evidence build failed",
                details={"type": type(exc).__name__},
            ) from exc
        if not isinstance(built, Mapping):
            raise ServiceError(503, "E_EVIDENCE_UNAVAILABLE", "market evidence result invalid")
        payload = built.get("evidence")
        if not isinstance(payload, Mapping):
            raise ServiceError(503, "E_EVIDENCE_UNAVAILABLE", "market evidence payload missing")
        sha = str(built.get("sha256") or payload.get("provenance", {}).get("snapshot_hash") or "")
        if not sha.startswith("sha256:") or not verify_hash(payload):
            raise ServiceError(503, "E_EVIDENCE_UNAVAILABLE", "market evidence hash invalid")
        if sha != "sha256:" + canonical_hash(payload):
            raise ServiceError(503, "E_EVIDENCE_UNAVAILABLE", "market evidence hash mismatch")
        summary = dict(built)
        return summary, dict(payload)
