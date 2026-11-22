"""Cycle / evidence / AI audit tables (M1.3c).

These tables live in the same ``workbench-v1.sqlite`` as the M1.2 ledger.
``ensure_cycle_schema`` is idempotent: it creates missing tables and adds
missing columns to the pre-existing ``cycle_runs`` table (the ledger schema
created an earlier, smaller version). The service's schema version stays 1;
this is an additive local schema, not a new ``schema_migrations`` row.

T13 tracing contract (why these columns exist; keep in sync with 07 §5.6
“每轮最小追溯” and the D/T06 four time points):

* ``ai_calls.timeout_ms`` is the **effective** per-call timeout. It is written only
  when the caller reports a real value (``timeout_source='caller'``); otherwise it
  stays NULL and ``timeout_reason`` names the gap. ``0`` is **never** stored as
  "no timeout" — a reported ``0`` is invalid and recorded as a reason instead.
* ``ai_calls.cost_estimate`` / ``ai_runs.cost_estimate`` are decimal TEXT and are
  only computed from a caller-declared price source (``cost_source``); without one
  the value stays NULL and ``cost_reason`` explains why. The service never invents
  rates.
* ``ai_runs.started_at`` is the run start: caller-supplied, or derived from the
  first per-call ``started_at`` (``started_at_source='earliest_call'``), otherwise
  NULL + ``started_at_reason``. ``finalized_at`` is the explicit finalize moment.
  Rows written before T13 have no ``started_at_source`` and their ``started_at``
  is a finalize-time approximation — historical rows are **not rewritten**, so
  consumers must treat ``started_at_source IS NULL`` as "not a run start".
  The four time points for D come from the per-call ``ai_calls.started_at_ms`` /
  ``ended_at_ms``, never from ``ai_runs.started_at``.
* ``ai_run_events``: new rows carry the real ``event`` type (e.g. ``role_start``),
  ``role``, the event ``ts``/``ts_ms``, derived ``from_state``/``to_state`` and
  ``event_source``. Rows written before T13 hold the placeholder literal
  ``event='event'`` with NULL ``role``/``from_state``/``to_state``/``event_source``:
  they are **deprecated and unusable** (the cycle detail API flags them
  ``legacy_unusable=true``) and are not rewritten.
* ``evidence_snapshots.evidence_build_ms`` records the evidence build duration in
  milliseconds next to the millisecond ``produced_at_ms``. The second-precision
  ``produced_at`` string is display-only: 0–999 ms of error would erode the 15 s
  entry TTL. Legacy rows keep ``evidence_build_ms`` NULL (= unknown).
"""

from __future__ import annotations

import sqlite3
from typing import Any

CYCLE_RUN_DDL = """
CREATE TABLE IF NOT EXISTS cycle_runs (
    cycle_id             TEXT PRIMARY KEY,
    slot_id              TEXT,
    state                TEXT NOT NULL,
    outcome              TEXT,
    trigger              TEXT NOT NULL DEFAULT 'manual',
    -- v3.1 P2（docs/19 R2）：research / trigger / manual，按 purpose 各允许 1 个 RUNNING。
    purpose              TEXT NOT NULL DEFAULT 'manual',
    symbol               TEXT,
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    reason_code          TEXT,
    error_code           TEXT,
    execution_json       TEXT,
    decision_json        TEXT,
    evidence_snapshot_id TEXT,
    account_json         TEXT,
    risk_envelope_json   TEXT,
    created_at           TEXT,
    updated_at           TEXT
);
"""

_EVIDENCE_DDL = """
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    cycle_id          TEXT PRIMARY KEY,
    schema_version    TEXT NOT NULL,
    produced_at_ms    INTEGER NOT NULL,
    verdict           TEXT NOT NULL,
    score             REAL NOT NULL,
    path              TEXT NOT NULL,
    sha256            TEXT NOT NULL,
    bytes             INTEGER NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    evidence_build_ms INTEGER,
    created_at        TEXT NOT NULL
);
"""

_AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS cycle_agents (
    cycle_id   TEXT NOT NULL,
    agent      TEXT NOT NULL,
    ai_run_id  TEXT,
    attempt    INTEGER NOT NULL DEFAULT 0,
    output_id  TEXT,
    status     TEXT NOT NULL DEFAULT 'accepted',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cycle_id, agent)
);
"""

_AI_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS ai_runs (
    ai_run_id        TEXT PRIMARY KEY,
    cycle_id         TEXT NOT NULL,
    mode             TEXT NOT NULL,
    trigger          TEXT NOT NULL,
    state            TEXT NOT NULL,
    outcome          TEXT,
    reason_code      TEXT,
    provider         TEXT,
    model            TEXT,
    prompt_set       TEXT,
    -- T13: nullable on purpose. NULL = start unknown; see started_at_source/reason.
    started_at       TEXT,
    ended_at         TEXT,
    -- T13: the explicit finalize moment (older rows only have ended_at).
    finalized_at     TEXT,
    started_at_source TEXT,
    started_at_reason TEXT,
    config_json      TEXT,
    token_input      INTEGER,
    token_output     INTEGER,
    cost_estimate    TEXT,
    cost_source      TEXT,
    cost_reason      TEXT,
    error_code       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_runs_cycle ON ai_runs(cycle_id, started_at DESC);
"""

_AI_CALLS_DDL = """
CREATE TABLE IF NOT EXISTS ai_calls (
    call_id                 TEXT PRIMARY KEY,
    ai_run_id               TEXT NOT NULL,
    cycle_id                TEXT NOT NULL,
    role                    TEXT NOT NULL,
    attempt                 INTEGER NOT NULL DEFAULT 1,
    phase                   TEXT,
    provider                TEXT,
    model                   TEXT,
    reasoning_effort        TEXT,
    temperature             REAL,
    max_tokens              INTEGER,
    prompt_version          TEXT,
    output_schema_version   TEXT,
    started_at              TEXT,
    ended_at                TEXT,
    -- T13: millisecond integers for D's four time points (ISO strings above stay).
    started_at_ms           INTEGER,
    ended_at_ms             INTEGER,
    latency_ms              INTEGER,
    -- T13: effective timeout, only when a real source exists; NULL + reason otherwise.
    timeout_ms              INTEGER,
    timeout_source          TEXT,
    timeout_reason          TEXT,
    status                  TEXT,
    finish_reason           TEXT,
    error_code              TEXT,
    error_message           TEXT,
    input_tokens            INTEGER,
    output_tokens           INTEGER,
    cache_read_tokens       INTEGER,
    cache_write_tokens      INTEGER,
    total_tokens            INTEGER,
    usage_estimated         INTEGER NOT NULL DEFAULT 0,
    request_sha256          TEXT,
    response_sha256         TEXT,
    request_ref             TEXT,
    response_ref            TEXT,
    evidence_snapshot_id    TEXT,
    session_id              TEXT,
    purpose                 TEXT,
    validation_errors_json  TEXT,
    -- T13: cost only from a caller-declared price source; NULL + reason otherwise.
    cost_estimate           TEXT,
    cost_source             TEXT,
    cost_reason             TEXT,
    -- T13: raw model output kept for schema/semantic failures (bounded excerpt).
    raw_response_text       TEXT,
    raw_response_truncated  INTEGER,
    raw_capture_reason      TEXT,
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_run ON ai_calls(ai_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_calls_cycle ON ai_calls(cycle_id, created_at);
"""

_AI_OUTPUTS_DDL = """
CREATE TABLE IF NOT EXISTS ai_outputs (
    output_id             TEXT PRIMARY KEY,
    call_id               TEXT,
    cycle_id              TEXT NOT NULL,
    agent                 TEXT NOT NULL,
    ai_run_id             TEXT NOT NULL,
    attempt               INTEGER NOT NULL,
    schema_version        TEXT,
    valid                 INTEGER NOT NULL DEFAULT 0,
    output_json           TEXT NOT NULL,
    semantic_errors_json  TEXT,
    output_sha256         TEXT,
    created_at            TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_output_attempt
    ON ai_outputs(cycle_id, agent, ai_run_id, attempt);
"""

_AI_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS ai_run_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ai_run_id   TEXT NOT NULL,
    cycle_id    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    ts_ms       INTEGER,
    from_state  TEXT,
    to_state    TEXT,
    event       TEXT NOT NULL,
    reason_code TEXT,
    role        TEXT,
    payload_ref TEXT,
    -- T13: where event/ts came from ('event_type' | 'event_field' | 'server_insert').
    -- NULL only for deprecated pre-T13 placeholder rows (event='event').
    event_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_events_run ON ai_run_events(ai_run_id, seq);
"""

_CYCLE_RUN_COLUMNS: dict[str, str] = {
    "slot_id": "TEXT",
    "state": "TEXT",
    "outcome": "TEXT",
    "trigger": "TEXT DEFAULT 'manual'",
    "purpose": "TEXT NOT NULL DEFAULT 'manual'",
    "symbol": "TEXT",
    "started_at": "TEXT",
    "finished_at": "TEXT",
    "reason_code": "TEXT",
    "error_code": "TEXT",
    "execution_json": "TEXT",
    "decision_json": "TEXT",
    "evidence_snapshot_id": "TEXT",
    "account_json": "TEXT",
    "risk_envelope_json": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}


# T13 additions. Additive ALTERs only: nullable columns, so old rows stay readable
# and no historical value is rewritten.
_AI_RUNS_COLUMNS: dict[str, str] = {
    "finalized_at": "TEXT",
    "started_at_source": "TEXT",
    "started_at_reason": "TEXT",
    "cost_source": "TEXT",
    "cost_reason": "TEXT",
}

_AI_CALLS_COLUMNS: dict[str, str] = {
    "started_at_ms": "INTEGER",
    "ended_at_ms": "INTEGER",
    "timeout_source": "TEXT",
    "timeout_reason": "TEXT",
    "cost_estimate": "TEXT",
    "cost_source": "TEXT",
    "cost_reason": "TEXT",
    "raw_response_text": "TEXT",
    "raw_response_truncated": "INTEGER",
    "raw_capture_reason": "TEXT",
}

_AI_EVENTS_COLUMNS: dict[str, str] = {
    "ts_ms": "INTEGER",
    "event_source": "TEXT",
}

_EVIDENCE_COLUMNS: dict[str, str] = {
    "evidence_build_ms": "INTEGER",
}

#: Pre-T13 ai_runs DDL, kept verbatim for the documented down-migration.
_AI_RUNS_LEGACY_DDL = """
CREATE TABLE {table} (
    ai_run_id        TEXT PRIMARY KEY,
    cycle_id         TEXT NOT NULL,
    mode             TEXT NOT NULL,
    trigger          TEXT NOT NULL,
    state            TEXT NOT NULL,
    outcome          TEXT,
    reason_code      TEXT,
    provider         TEXT,
    model            TEXT,
    prompt_set       TEXT,
    started_at       TEXT NOT NULL,
    ended_at         TEXT,
    config_json      TEXT,
    token_input      INTEGER,
    token_output     INTEGER,
    cost_estimate    TEXT,
    error_code       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
)
"""

_AI_RUNS_T13_COLUMNS = (
    "ai_run_id", "cycle_id", "mode", "trigger", "state", "outcome", "reason_code",
    "provider", "model", "prompt_set", "started_at", "ended_at", "finalized_at",
    "started_at_source", "started_at_reason", "config_json", "token_input",
    "token_output", "cost_estimate", "cost_source", "cost_reason", "error_code",
    "created_at", "updated_at",
)

_AI_RUNS_T13_ADDED = (
    "finalized_at", "started_at_source", "started_at_reason", "cost_source", "cost_reason",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

def _column_notnull(conn: sqlite3.Connection, table: str, column: str) -> bool | None:
    """PRAGMA notnull flag for one column; None when the column/table is absent."""

    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        if str(row[1]) == column:
            return bool(row[3])
    return None


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def migrate_ai_runs_started_at_nullable(conn: sqlite3.Connection) -> bool:
    """T13 up-migration: make ai_runs.started_at nullable (SQLite table rebuild).

    Why a rebuild: pre-T13 started_at carried the audit-persist (= finalize) moment,
    so the service cannot honestly write a value when the caller reports no run
    start. Keeping NOT NULL would force the old misleading value back into the row.

    * Idempotent: rebuild runs only while the column is still NOT NULL.
    * Backward compatible: every legacy row and legacy column value is copied
      verbatim (no historical rewrite); only new nullable columns are added.
    * Atomic: DDL runs inside one transaction, so a crash leaves the old table.
    * Rollback: see rollback_ai_runs_started_at_nullable().

    Returns True when the table was rebuilt.
    """

    if not table_exists(conn, "ai_runs"):
        return False
    if _column_notnull(conn, "ai_runs", "started_at") == 0:
        return False

    existing = _columns(conn, "ai_runs")
    copy_columns = [
        name
        for name in _AI_RUNS_T13_COLUMNS
        if name in existing and name not in _AI_RUNS_T13_ADDED
    ]
    was_in_transaction = conn.in_transaction
    if not was_in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS ai_runs__t13")
        new_ddl = _AI_RUNS_DDL.split("CREATE INDEX", 1)[0].replace(
            "CREATE TABLE IF NOT EXISTS ai_runs", "CREATE TABLE ai_runs__t13"
        )
        conn.execute(new_ddl)
        columns = ", ".join(copy_columns)
        conn.execute(f"INSERT INTO ai_runs__t13 ({columns}) SELECT {columns} FROM ai_runs")
        conn.execute("DROP TABLE ai_runs")
        conn.execute("ALTER TABLE ai_runs__t13 RENAME TO ai_runs")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_runs_cycle ON ai_runs(cycle_id, started_at DESC)"
        )
        if not was_in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if not was_in_transaction:
            conn.execute("ROLLBACK")
        raise
    return True


def rollback_ai_runs_started_at_nullable(conn: sqlite3.Connection) -> bool:
    """T13 down-migration (maintenance helper, covered by tests).

    Restores the pre-T13 ai_runs shape: started_at NOT NULL and the T13 columns
    dropped. A NULL start (honest "unknown") is backfilled with created_at so the
    legacy NOT NULL contract holds; that reintroduces the finalize-time
    approximation, so this is a rollback path, not a normal one. Legacy rows keep
    their original values. Returns True when it ran.
    """

    if not table_exists(conn, "ai_runs"):
        return False
    if _column_notnull(conn, "ai_runs", "started_at") == 1:
        return False

    existing = _columns(conn, "ai_runs")
    legacy_columns = [
        name
        for name in (
            "ai_run_id", "cycle_id", "mode", "trigger", "state", "outcome",
            "reason_code", "provider", "model", "prompt_set", "started_at",
            "ended_at", "config_json", "token_input", "token_output",
            "cost_estimate", "error_code", "created_at", "updated_at",
        )
        if name in existing
    ]
    was_in_transaction = conn.in_transaction
    if not was_in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS ai_runs__legacy")
        conn.execute(_AI_RUNS_LEGACY_DDL.format(table="ai_runs__legacy"))
        select = ", ".join(
            "COALESCE(started_at, created_at, updated_at)" if name == "started_at" else name
            for name in legacy_columns
        )
        conn.execute(
            f"INSERT INTO ai_runs__legacy ({', '.join(legacy_columns)}) "
            f"SELECT {select} FROM ai_runs"
        )
        conn.execute("DROP TABLE ai_runs")
        conn.execute("ALTER TABLE ai_runs__legacy RENAME TO ai_runs")
        conn.execute("DROP INDEX IF EXISTS idx_ai_runs_cycle")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_runs_cycle ON ai_runs(cycle_id, started_at DESC)"
        )
        if not was_in_transaction:
            conn.execute("COMMIT")
    except Exception:
        if not was_in_transaction:
            conn.execute("ROLLBACK")
        raise
    return True


def ensure_cycle_schema(conn: sqlite3.Connection) -> None:
    """Create/upgrade the M1.3c cycle/audit schema (idempotent, T13-aware).

    Safe to call on every start: table creation, the T13 ai_runs rebuild (only
    while started_at is NOT NULL) and the additive column migrations all check the
    current shape first, so repeated calls are no-ops and old rows stay untouched.
    """

    conn.executescript(CYCLE_RUN_DDL)
    _add_missing_columns(conn, "cycle_runs", _CYCLE_RUN_COLUMNS)
    conn.executescript(_EVIDENCE_DDL)
    _add_missing_columns(conn, "evidence_snapshots", _EVIDENCE_COLUMNS)
    conn.executescript(_AGENTS_DDL)
    conn.executescript(_AI_RUNS_DDL)
    # T13: only a pre-T13 table needs the started_at rebuild; the rebuild also adds
    # the new columns, so the additive pass below is a no-op for rebuilt tables.
    migrate_ai_runs_started_at_nullable(conn)
    _add_missing_columns(conn, "ai_runs", _AI_RUNS_COLUMNS)
    conn.executescript(_AI_CALLS_DDL)
    _add_missing_columns(conn, "ai_calls", _AI_CALLS_COLUMNS)
    conn.executescript(_AI_OUTPUTS_DDL)
    conn.executescript(_AI_EVENTS_DDL)
    _add_missing_columns(conn, "ai_run_events", _AI_EVENTS_COLUMNS)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_cycle_slot "
        "ON cycle_runs(slot_id) WHERE slot_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cycle_runs_state "
        "ON cycle_runs(state, started_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cycle_runs_started "
        "ON cycle_runs(started_at DESC)"
    )
    # R2：按 purpose 找 RUNNING cycle（冲突判定 / 僵尸收敛都只看同一个 purpose）。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cycle_runs_purpose "
        "ON cycle_runs(purpose, state, started_at DESC)"
    )
    conn.commit()


def table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def schema_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Small diagnostic used by tests/README; no secrets.

    Includes the T13 tracing facts so a reviewer can tell whether the running
    database has the new columns and whether ai_runs.started_at is honest
    (nullable) or still the pre-T13 finalize-time approximation.
    """

    def cols(table: str) -> list[str]:
        try:
            return sorted(_columns(conn, table))
        except sqlite3.Error:
            return []

    return {
        "tables": sorted(table_names(conn)),
        "cycle_columns": cols("cycle_runs"),
        "ai_run_columns": cols("ai_runs"),
        "ai_call_columns": cols("ai_calls"),
        "ai_event_columns": cols("ai_run_events"),
        "evidence_columns": cols("evidence_snapshots"),
        "tracing": {
            "ai_runs_started_at_nullable": _column_notnull(conn, "ai_runs", "started_at") == 0,
            "ai_runs_finalized_at": "finalized_at" in cols("ai_runs"),
            "ai_calls_timeout_source": "timeout_source" in cols("ai_calls"),
            "ai_calls_cost_source": "cost_source" in cols("ai_calls"),
            "ai_call_ms_timestamps": "started_at_ms" in cols("ai_calls"),
            "ai_events_real_type": "event_source" in cols("ai_run_events"),
            "evidence_build_ms": "evidence_build_ms" in cols("evidence_snapshots"),
        },
    }
