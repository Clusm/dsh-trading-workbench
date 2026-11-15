"""SQLite persistence for cycles, evidence references, and AI audit rows."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from ..config import Settings
from ..db import db_path_for, utc_now_iso


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return value


class CycleStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = db_path_for(settings.data_dir)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ cycles
    def running(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cycle_runs WHERE state='RUNNING' "
                "ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            return None if row is None else dict(row)

    def slot_exists(self, slot_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM cycle_runs WHERE slot_id=? LIMIT 1", (slot_id,)
            ).fetchone()
            return row is not None

    def insert_cycle(self, row: Mapping[str, Any]) -> None:
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO cycle_runs ({', '.join(columns)}) "
            f"VALUES ({placeholders})"
        )
        with self._connect() as conn:
            conn.execute(sql, tuple(row[column] for column in columns))

    def get_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cycle_runs WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            return None if row is None else dict(row)

    def list_cycles(self, *, limit: int = 50, cursor: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(200, int(limit)))
        params: list[Any] = []
        where = ""
        if cursor:
            where = "WHERE started_at < ?"
            params.append(cursor)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM cycle_runs {where} "
                "ORDER BY started_at DESC, cycle_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]

    def finalize_cycle(
        self,
        cycle_id: str,
        *,
        state: str,
        outcome: str,
        reason_code: str | None,
        error_code: str | None,
        execution: Any,
        finished_at: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE cycle_runs SET state=?, outcome=?, reason_code=?, error_code=?, "
                "execution_json=?, finished_at=?, updated_at=? "
                "WHERE cycle_id=? AND state='RUNNING'",
                (
                    state,
                    outcome,
                    reason_code,
                    error_code,
                    None if execution is None else _json(execution),
                    finished_at or utc_now_iso(),
                    utc_now_iso(),
                    cycle_id,
                ),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------ evidence
    def upsert_evidence(self, record: Mapping[str, Any]) -> None:
        build_ms = record.get("evidence_build_ms")
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO evidence_snapshots "
                "(cycle_id, schema_version, produced_at_ms, verdict, score, path, sha256, "
                "bytes, prompt_tokens, evidence_build_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["cycle_id"],
                    record["schema_version"],
                    int(record["produced_at_ms"]),
                    record["verdict"],
                    float(record["score"]),
                    record["path"],
                    record["sha256"],
                    int(record.get("bytes") or 0),
                    int(record.get("prompt_tokens") or 0),
                    # T13: NULL = build duration unknown (legacy rows stay NULL).
                    None if build_ms is None else int(build_ms),
                    record.get("created_at") or utc_now_iso(),
                ),
            )

    def get_evidence(self, cycle_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM evidence_snapshots WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
            return None if row is None else dict(row)

    def delete_evidence(self, cycle_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM evidence_snapshots WHERE cycle_id=?", (cycle_id,))

    # ------------------------------------------------------------------ agents
    def upsert_agent(
        self,
        *,
        cycle_id: str,
        agent: str,
        ai_run_id: str | None,
        attempt: int,
        output_id: str | None,
        status: str = "accepted",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cycle_agents "
                "(cycle_id, agent, ai_run_id, attempt, output_id, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cycle_id, agent) DO UPDATE SET "
                "ai_run_id=excluded.ai_run_id, attempt=excluded.attempt, "
                "output_id=excluded.output_id, status=excluded.status, "
                "updated_at=excluded.updated_at",
                (cycle_id, agent, ai_run_id, int(attempt), output_id, status, utc_now_iso()),
            )

    def agents(self, cycle_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM cycle_agents WHERE cycle_id=? ORDER BY agent", (cycle_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------ AI runs
    def insert_ai_run(self, row: Mapping[str, Any]) -> None:
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO ai_runs ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )

    def get_ai_run(self, ai_run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_runs WHERE ai_run_id=?", (ai_run_id,)
            ).fetchone()
            return None if row is None else dict(row)

    def ai_runs(self, cycle_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_runs WHERE cycle_id=? ORDER BY started_at", (cycle_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------ calls
    _CALL_COLUMNS = (
        "call_id", "ai_run_id", "cycle_id", "role", "attempt", "phase",
        "provider", "model", "reasoning_effort", "temperature", "max_tokens",
        "prompt_version", "output_schema_version", "started_at", "ended_at",
        # T13: millisecond timestamps for the D four time points.
        "started_at_ms", "ended_at_ms",
        "latency_ms", "timeout_ms", "timeout_source", "timeout_reason",
        "status", "finish_reason", "error_code", "error_message",
        "input_tokens", "output_tokens", "cache_read_tokens",
        "cache_write_tokens", "total_tokens", "usage_estimated",
        "cost_estimate", "cost_source", "cost_reason",
        "request_sha256", "response_sha256", "request_ref", "response_ref",
        "raw_response_text", "raw_response_truncated", "raw_capture_reason",
        "evidence_snapshot_id", "session_id", "purpose", "validation_errors_json",
        "created_at",
    )

    def insert_call(self, row: Mapping[str, Any]) -> None:
        columns = [name for name in self._CALL_COLUMNS if name in row]
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO ai_calls ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_calls WHERE call_id=?", (call_id,)
            ).fetchone()
            return None if row is None else dict(row)

    def calls(self, ai_run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_calls WHERE ai_run_id=? ORDER BY created_at", (ai_run_id,)
            ).fetchall()
            return [dict(row) for row in rows]
    #: T13: single-statement derivation of ai_runs.started_at from the first call.
    #: One statement (no read-then-write) keeps it safe both on a fresh connection
    #: and inside the finalize transaction (nested connections would deadlock).
    _START_FROM_CALLS_SQL = (
        "UPDATE ai_runs SET started_at = ("
        "        SELECT c.started_at FROM ai_calls c "
        "        WHERE c.ai_run_id = ai_runs.ai_run_id AND c.started_at IS NOT NULL "
        "        ORDER BY (c.started_at_ms IS NULL), c.started_at_ms, c.started_at LIMIT 1 "
        "    ),"
        "    started_at_source = 'earliest_call',"
        "    started_at_reason = 'derived_from_first_call',"
        "    updated_at = ? "
        "WHERE ai_run_id = ? "
        "  AND started_at_source IN ('unknown', 'earliest_call') "
        "  AND EXISTS (SELECT 1 FROM ai_calls c "
        "              WHERE c.ai_run_id = ai_runs.ai_run_id AND c.started_at IS NOT NULL)"
    )

    def apply_run_started(self, conn: sqlite3.Connection, ai_run_id: str) -> dict[str, Any] | None:
        """T13: derive the run start on an already-open connection.

        Used by CycleService._finalize_ai_runs, which holds the write transaction,
        and by refresh_run_started_from_calls(). A caller-supplied start and any
        historical row (started_at_source NULL) are never touched.
        """

        conn.execute(self._START_FROM_CALLS_SQL, (utc_now_iso(), ai_run_id))
        row = conn.execute(
            "SELECT started_at, started_at_source, started_at_reason FROM ai_runs "
            "WHERE ai_run_id=?",
            (ai_run_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def refresh_run_started_from_calls(self, ai_run_id: str) -> dict[str, Any] | None:
        """T13: derive ai_runs.started_at from the earliest per-call timestamp.

        Called after a call row lands. Uses one short connection and starts with the
        UPDATE (never a deferred read that would have to be upgraded: in WAL mode
        that upgrade fails immediately when a background writer just committed).
        """

        with self._connect() as conn:
            return self.apply_run_started(conn, ai_run_id)

    # ------------------------------------------------------------------ events
    def insert_event(
        self,
        *,
        ai_run_id: str,
        cycle_id: str,
        event: Mapping[str, Any],
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO ai_run_events "
                "(ai_run_id, cycle_id, ts, ts_ms, from_state, to_state, event, reason_code, "
                "role, payload_ref, event_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ai_run_id,
                    cycle_id,
                    event.get("ts") or utc_now_iso(),
                    event.get("ts_ms"),
                    event.get("from_state"),
                    event.get("to_state"),
                    # The service normalizes the event first; there is deliberately no
                    # placeholder fallback (a literal 'event' misled downstream readers).
                    str(event["event"]),
                    event.get("reason_code"),
                    event.get("role"),
                    event.get("payload_ref"),
                    event.get("event_source"),
                ),
            )
            return int(cur.lastrowid or 0)

    def events(self, ai_run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_run_events WHERE ai_run_id=? ORDER BY seq", (ai_run_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------ outputs
    def insert_output(self, row: Mapping[str, Any]) -> None:
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO ai_outputs ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )

    def get_output(self, output_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_outputs WHERE output_id=?", (output_id,)
            ).fetchone()
            return None if row is None else dict(row)

    def outputs(self, cycle_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_outputs WHERE cycle_id=? ORDER BY created_at", (cycle_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------ detail
    def detail(self, cycle_id: str) -> dict[str, Any] | None:
        cycle = self.get_cycle(cycle_id)
        if cycle is None:
            return None
        return {
            "cycle": cycle,
            "evidence": self.get_evidence(cycle_id),
            "agents": self.agents(cycle_id),
            "ai_runs": self.ai_runs(cycle_id),
            "outputs": self.outputs(cycle_id),
        }


def decode_json_fields(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Convert selected JSON TEXT columns to objects for API responses."""

    result = dict(row)
    for field in fields:
        if field in result:
            result[field] = _loads(result[field])
    return result


def evidence_file_path(settings: Settings, relative_path: str) -> Path:
    """Resolve a DB evidence path while keeping it inside ``data_dir``."""

    root = Path(settings.data_dir).resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("evidence path escapes data_dir")
    return candidate
