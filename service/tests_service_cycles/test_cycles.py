"""M1.3c cycle/evidence/AI audit service tests (offline fakes, no live orders)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from unittest import mock

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from workbench_service.app import create_app  # noqa: E402
from workbench_service.config import (  # noqa: E402
    AUTH_MODE_BEARER,
    AUTH_MODE_DEV_OPEN,
    Settings,
    resolve_runtime_auth,
)
from workbench_service.errors import ServiceError  # noqa: E402
from workbench_service.execution.service import build_execution_service  # noqa: E402
from workbench_service.market.evidence import canonical_hash  # noqa: E402
from workbench_service.cycles.schema import (  # noqa: E402
    ensure_cycle_schema,
    rollback_ai_runs_started_at_nullable,
    schema_snapshot,
)
from workbench_service.cycles.service import _hash_json, _json  # noqa: E402


class FakeMarket:
    """Deterministic md-evidence/1 writer; never touches the network."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.calls: list[dict[str, Any]] = []
        self.settings = SimpleNamespace(whitelist=("BTCUSDT", "ETHUSDT", "SOLUSDT"))

    def build_evidence(
        self,
        *,
        cycle_id: str,
        held_symbols: list[str] | tuple[str, ...] = (),
        prev_pick: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {"cycle_id": cycle_id, "held": list(held_symbols), "prev_pick": prev_pick}
        )
        produced_ms = int(time.time() * 1000)
        payload: dict[str, Any] = {
            "schema_version": "md-evidence/1",
            "cycle_id": cycle_id,
            "produced_at_ms": produced_ms,
            "produced_at": "2026-11-09T04:00:00Z",
            "snapshot_id": f"ev-{cycle_id}",
            "data_health": {"verdict": "GOOD", "score": 100.0, "issues": []},
            "limits": {"prompt_token_estimate": 12, "truncated": False},
            "symbols": {},
            "provenance": {
                "service": "fake-market",
                "snapshot_hash": "sha256:" + "0" * 64,
            },
        }
        # canonical_hash excludes snapshot_id and provenance.snapshot_hash.
        digest = canonical_hash(payload)
        payload["provenance"]["snapshot_hash"] = "sha256:" + digest
        directory = self.data_dir / "evidence" / "2026-11-09" / cycle_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "evidence.json"
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        target.write_bytes(encoded)
        relative = target.relative_to(self.data_dir).as_posix()
        return {
            "cycle_id": cycle_id,
            "snapshot_id": payload["snapshot_id"],
            "verdict": "GOOD",
            "score": 100.0,
            "sha256": payload["provenance"]["snapshot_hash"],
            "path": relative,
            "abs_path": str(target),
            "bytes": len(encoded),
            "prompt_token_estimate": 12,
            "deep": ["BTCUSDT"],
            "index_count": 0,
            "duration_ms": 1.0,
            "evidence": payload,
        }


class FakeExecution:
    """Read-only execution facade; no credentials, no brokers, no orders."""

    configured = False

    def status(self) -> dict[str, Any]:
        return {
            "venue": "demo",
            "demo_only": True,
            "credentials_configured": False,
            "account": {"available": False, "source": "none"},
            "equity": {"available": False, "source": "none"},
            "positions": [],
            "protection": {"ok": None, "positions": 0, "missing": []},
            "readiness": {"new_entry_allowed": False, "reasons": ["E_CREDENTIALS_MISSING"]},
            "checked_at": "2026-11-09T04:00:00Z",
        }

    def envelope(self) -> dict[str, Any]:
        return {
            "new_entry_allowed": False,
            "blockers": ["E_CREDENTIALS_MISSING"],
            "rules_version": "test",
            "limits": {"max_open_positions": 1},
        }


def make_settings(tmp: str, *, mode: str = AUTH_MODE_DEV_OPEN) -> Settings:
    old = {key: os.environ.get(key) for key in ("WORKBENCH_AUTH_MODE", "WORKBENCH_API_TOKEN")}
    try:
        os.environ["WORKBENCH_AUTH_MODE"] = mode
        os.environ.pop("WORKBENCH_API_TOKEN", None)
        settings = Settings.from_env(data_dir=tmp, default_auth_mode=AUTH_MODE_BEARER)
        if mode == AUTH_MODE_DEV_OPEN:
            return settings
        return resolve_runtime_auth(settings)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def client_for(tmp: str, *, mode: str = AUTH_MODE_DEV_OPEN) -> Iterator[tuple[TestClient, Settings, FakeMarket]]:
    settings = make_settings(tmp, mode=mode)
    app = create_app(settings)
    market = FakeMarket(Path(tmp))
    app.state.market = market
    app.state.services = FakeExecution()
    with TestClient(app) as client:
        yield client, settings, market


def auth_headers(settings: Settings) -> dict[str, str]:
    if settings.auth_mode == AUTH_MODE_DEV_OPEN:
        return {}
    return {"Authorization": f"Bearer {settings.api_token}"}


def create_cycle(client: TestClient, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = client.post("/api/cycles", headers=headers, json=body or None)
    assert response.status_code == 200, response.text
    return response.json()


class CycleCreateTests(unittest.TestCase):
    def test_json_encoder_handles_decimal_temporal_and_rejects_unknown(self) -> None:
        encoded = _json(
            {
                "amount": Decimal("12345.6700"),
                "fraction": Decimal("0.0100"),
                "when": datetime(2026, 11, 9, 1, 2, 3, tzinfo=timezone.utc),
                "day": date(2026, 11, 9),
            }
        )
        decoded = json.loads(encoded)
        self.assertEqual(decoded["amount"], "12345.6700")
        self.assertEqual(decoded["fraction"], "0.0100")
        self.assertEqual(decoded["when"], "2026-11-09T01:02:03+00:00")
        self.assertEqual(decoded["day"], "2026-11-09")
        with self.assertRaises(TypeError):
            _json({"unknown": object()})

    def test_create_with_decimal_envelope_persists_and_serializes(self) -> None:
        envelope = FakeExecution().envelope()
        envelope = {
            **envelope,
            "account": {"equity": Decimal("12345.6700"), "available": True},
            "daily_pnl": Decimal("-0.00"),
            "limits": {**envelope.get("limits", {}), "max_notional": Decimal("10000.50")},
        }
        with mock.patch.object(FakeExecution, "envelope", return_value=envelope):
            with tempfile.TemporaryDirectory() as tmp:
                with client_for(tmp) as (client, _settings, _market):
                    created = create_cycle(client, {})
                    self.assertEqual(created["state"], "RUNNING")
                    self.assertEqual(created["risk_envelope"]["daily_pnl"], "-0.00")
                    db = Path(tmp) / "workbench-v1.sqlite"
                    conn = sqlite3.connect(db)
                    try:
                        row = conn.execute(
                            "SELECT risk_envelope_json FROM cycle_runs WHERE cycle_id=?",
                            (created["cycle_id"],),
                        ).fetchone()
                        evidence = conn.execute(
                            "SELECT sha256, bytes FROM evidence_snapshots WHERE cycle_id=?",
                            (created["cycle_id"],),
                        ).fetchone()
                    finally:
                        conn.close()
                    self.assertIsNotNone(row)
                    self.assertIsNotNone(evidence)
                    persisted = json.loads(row[0])
                    self.assertEqual(persisted["account"]["equity"], "12345.6700")
                    self.assertEqual(persisted["daily_pnl"], "-0.00")
                    self.assertEqual(persisted["limits"]["max_notional"], "10000.50")

    def test_create_without_body_uses_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                response = client.post("/api/cycles")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["state"], "RUNNING")
                self.assertIn("evidence", response.json())
                self.assertIn("account", response.json())
                self.assertIn("risk_envelope", response.json())

    def test_create_returns_evidence_account_envelope_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, market):
                created = create_cycle(client, {})
                self.assertTrue(created["cycle_id"].startswith("cyc-"))
                self.assertEqual(created["state"], "RUNNING")
                self.assertEqual(created["evidence"]["sha256"], created["evidence"]["evidence"]["provenance"]["snapshot_hash"])
                self.assertFalse(created["account"]["credentials_configured"])
                self.assertEqual(created["account"]["positions"], [])
                self.assertFalse(created["risk_envelope"]["new_entry_allowed"])
                self.assertEqual(market.calls[0]["cycle_id"], created["cycle_id"])

                # The evidence reference is persisted in the same DB as the cycle.
                db = Path(tmp) / "workbench-v1.sqlite"
                conn = sqlite3.connect(db)
                try:
                    row = conn.execute(
                        "SELECT path, sha256, bytes FROM evidence_snapshots WHERE cycle_id=?",
                        (created["cycle_id"],),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertIsNotNone(row)
                evidence_path = Path(tmp) / row[0]
                self.assertTrue(evidence_path.is_file())
                disk = json.loads(evidence_path.read_text(encoding="utf-8"))
                self.assertEqual(row[1], disk["provenance"]["snapshot_hash"])
                self.assertEqual(row[1], "sha256:" + canonical_hash(disk))

    def test_bearer_required_for_all_cycle_mutations_and_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp, mode=AUTH_MODE_BEARER) as (client, settings, _market):
                headers = auth_headers(settings)
                self.assertEqual(client.get("/api/cycles").status_code, 401)
                self.assertEqual(client.post("/api/cycles").status_code, 401)
                created = create_cycle(client, headers)
                cycle_id = created["cycle_id"]
                self.assertEqual(client.get(f"/api/cycles/{cycle_id}").status_code, 401)
                self.assertEqual(client.get(f"/api/cycles/{cycle_id}/evidence").status_code, 401)
                self.assertEqual(client.post(f"/api/cycles/{cycle_id}/finalize", json={"state": "DONE"}).status_code, 401)
                self.assertEqual(client.post(f"/api/cycles/{cycle_id}/ai-runs", json={}).status_code, 401)
                self.assertEqual(
                    client.put("/api/ai-runs/run-x/calls/call-x", json={"role": "analysis"}).status_code,
                    401,
                )
                self.assertEqual(
                    client.post("/api/ai-runs/run-x/events", json={"event": "x"}).status_code,
                    401,
                )
                self.assertEqual(
                    client.put(f"/api/cycles/{cycle_id}/artifacts/analysis", json={"output": {}}).status_code,
                    401,
                )

    def test_running_and_slot_conflicts_are_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                _ = create_cycle(client, {}, slot_id="slot-a")
                again = client.post("/api/cycles", json={"slot_id": "slot-b"})
                self.assertEqual(again.status_code, 409)
                self.assertEqual(again.json()["error"]["code"], "E_CYCLE_RUNNING")

                # Finalize the first cycle, then the same slot must still be rejected.
                first_id = client.get("/api/cycles").json()["cycles"][0]["cycle_id"]
                done = client.post(
                    f"/api/cycles/{first_id}/finalize",
                    json={"state": "DONE", "outcome": "no_trade"},
                )
                self.assertEqual(done.status_code, 200)
                duplicate = client.post("/api/cycles", json={"slot_id": "slot-a"})
                self.assertEqual(duplicate.status_code, 409)
                self.assertEqual(duplicate.json()["error"]["code"], "E_CYCLE_DUPLICATE_SLOT")

                # A different slot is allowed after the running cycle ended.
                second = create_cycle(client, {}, slot_id="slot-b")
                self.assertNotEqual(second["cycle_id"], first_id)

    def test_get_list_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {}, slot_id="slot-list", trigger="auto")
                listing = client.get("/api/cycles?limit=10")
                self.assertEqual(listing.status_code, 200)
                self.assertEqual(listing.json()["count"], 1)
                self.assertEqual(listing.json()["cycles"][0]["cycle_id"], created["cycle_id"])
                detail = client.get(f"/api/cycles/{created['cycle_id']}")
                self.assertEqual(detail.status_code, 200)
                body = detail.json()
                self.assertEqual(body["cycle"]["trigger"], "auto")
                self.assertEqual(body["agents"], [])
                self.assertEqual(body["outputs"], [])
                evidence = client.get(f"/api/cycles/{created['cycle_id']}/evidence?agent=analysis")
                self.assertEqual(evidence.status_code, 200)
                self.assertEqual(evidence.json()["agent"], "analysis")
                self.assertEqual(evidence.json()["snapshot_id"], created["evidence"]["snapshot_id"])

    def test_cycle_id_hits_p1_server_evidence_without_crossing_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                execution = build_execution_service(settings, use_env_credentials=False)
                try:
                    evidence = execution._server_evidence(cycle_id)
                    self.assertEqual(evidence["evidence_id"], cycle_id)
                    self.assertEqual(evidence["sha256"], created["evidence"]["sha256"])
                    self.assertTrue(evidence["path"].endswith("/evidence.json"))
                    with self.assertRaises(ServiceError) as other:
                        execution._server_evidence("cyc-other")
                    self.assertEqual(other.exception.status_code, 422)
                    self.assertEqual(other.exception.code, "E_EVIDENCE_UNAVAILABLE")

                    # Literal P1 entry point: only the missing demo account and
                    # market snapshot are patched; the evidence lookup itself is real.
                    execution.broker = SimpleNamespace(mmr_for=lambda _symbol, leverage=None: None)
                    equity = SimpleNamespace(
                        session=Decimal("5000"),
                        exchange=Decimal("5000"),
                        available=True,
                        source="test",
                        age_ms=0,
                    )
                    plan = {
                        "symbol": "BTCUSDT",
                        "side": "LONG",
                        "action": "open",
                        "entry_type": "MARKET",
                        "leverage": 8,
                        "qty": "0.02",
                        "sl": "99000.0",
                        "tp": "102800.0",
                        "confidence": 0.50,
                        "reasoning_ref": "decision:test",
                    }
                    with mock.patch.object(
                        execution,
                        "_heartbeat",
                        return_value=(None, equity, {}, {"state": "normal"}),
                    ), mock.patch.object(
                        execution,
                        "_server_market_facts",
                        return_value={
                            "mark": "100000",
                            "snapshot_age_ms": 1000,
                            "mark_move_60s_pct": 0.001,
                            "mark_index_deviation_pct": 0.0,
                            "spread_bps": 2.0,
                        },
                    ), mock.patch.object(
                        execution, "_server_evidence", wraps=execution._server_evidence
                    ) as spy:
                        with self.assertRaises(ServiceError) as p1_error:
                            execution.evaluate({"cycle_id": cycle_id, "plan": plan})
                        self.assertEqual(spy.call_args.args[0], cycle_id)
                        self.assertNotEqual(p1_error.exception.code, "E_EVIDENCE_UNAVAILABLE")
                        with self.assertRaises(ServiceError) as cross_error:
                            execution.evaluate({"cycle_id": "cyc-other", "plan": plan})
                        self.assertEqual(cross_error.exception.code, "E_EVIDENCE_UNAVAILABLE")
                finally:
                    execution.ledger.close()

    def test_old_ledger_cycle_runs_table_is_upgraded_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "workbench-v1.sqlite"
            conn = sqlite3.connect(db)
            try:
                conn.executescript(
                    """
                    CREATE TABLE cycle_runs (
                        cycle_id TEXT PRIMARY KEY,
                        slot_id TEXT,
                        state TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        outcome TEXT,
                        error_code TEXT,
                        decision_json TEXT
                    );
                    """
                )
                ensure_cycle_schema(conn)
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(cycle_runs)").fetchall()
                }
                self.assertTrue(
                    {
                        "trigger",
                        "evidence_snapshot_id",
                        "account_json",
                        "risk_envelope_json",
                        "created_at",
                        "updated_at",
                    }.issubset(columns)
                )
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertTrue({"cycle_agents", "ai_runs", "ai_calls", "ai_outputs"}.issubset(tables))
            finally:
                conn.close()

    def test_service_generates_authoritative_cycle_id_for_host_to_adopt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                requested = "cyc-20261109T000000Z-abcdef"
                created = create_cycle(client, {}, cycle_id=requested)
                self.assertNotEqual(created["cycle_id"], requested)
                self.assertTrue(created["cycle_id"].startswith("cyc-"))
                self.assertEqual(created["evidence"]["cycle_id"], created["cycle_id"])
                self.assertEqual(
                    client.get(f"/api/cycles/{created['cycle_id']}").status_code, 200
                )

    def test_evidence_bytes_are_checked_before_and_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                expected_bytes = int(created["evidence"]["bytes"])
                evidence_path = Path(tmp) / created["evidence"]["path"]
                self.assertEqual(evidence_path.stat().st_size, expected_bytes)
                self.assertEqual(
                    client.get(f"/api/cycles/{cycle_id}/evidence").json()["bytes"],
                    expected_bytes,
                )
                # Same canonical JSON/hash, different on-disk bytes (whitespace).
                evidence_path.write_text(
                    evidence_path.read_text(encoding="utf-8") + " ",
                    encoding="utf-8",
                )
                self.assertNotEqual(evidence_path.stat().st_size, expected_bytes)
                corrupt = client.get(f"/api/cycles/{cycle_id}/evidence")
                self.assertEqual(corrupt.status_code, 500)
                self.assertEqual(corrupt.json()["error"]["code"], "E_EVIDENCE_CORRUPT")
                detail = client.get(f"/api/cycles/{cycle_id}")
                self.assertEqual(detail.status_code, 200)
                self.assertFalse(detail.json()["evidence"]["verified"])
                self.assertEqual(detail.json()["evidence"]["verify_error"], "E_EVIDENCE_CORRUPT")

                execution = build_execution_service(settings, use_env_credentials=False)
                try:
                    with self.assertRaises(ServiceError) as p1:
                        execution._server_evidence(cycle_id)
                    self.assertEqual(p1.exception.status_code, 422)
                    self.assertEqual(p1.exception.code, "E_EVIDENCE_UNVERIFIED")
                finally:
                    execution.ledger.close()

    def test_evidence_path_is_normalized_before_cycle_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, market):
                original = market.build_evidence

                def bad_path(**kwargs: Any) -> dict[str, Any]:
                    result = original(**kwargs)
                    result["path"] = "../../outside/evidence.json"
                    return result

                market.build_evidence = bad_path  # type: ignore[method-assign]
                response = client.post("/api/cycles")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "E_EVIDENCE_UNAVAILABLE")
                self.assertEqual(client.get("/api/cycles").json()["count"], 0)

    def test_symbol_is_validated_against_market_whitelist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                invalid = client.post("/api/cycles", json={"symbol": "bad"})
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(invalid.json()["error"]["code"], "E_VALIDATION")
                not_listed = client.post("/api/cycles", json={"symbol": "DOGEUSDT"})
                self.assertEqual(not_listed.status_code, 422)
                self.assertEqual(not_listed.json()["error"]["code"], "E_SYMBOL_NOT_ALLOWED")
                created = create_cycle(client, {}, symbol="BTCUSDT")
                self.assertEqual(created["cycle_id"], created["cycle_id"])

    def test_orphan_evidence_is_cleaned_when_cycle_service_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                db = Path(tmp) / "workbench-v1.sqlite"
                orphan_path = Path(tmp) / "evidence" / "2026-11-09" / "cyc-orphan" / "evidence.json"
                orphan_path.parent.mkdir(parents=True, exist_ok=True)
                orphan_path.write_text("{}", encoding="utf-8")
                conn = sqlite3.connect(db)
                try:
                    conn.execute(
                        "INSERT INTO evidence_snapshots "
                        "(cycle_id, schema_version, produced_at_ms, verdict, score, path, sha256, "
                        "bytes, prompt_tokens, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            "cyc-orphan",
                            "md-evidence/1",
                            1,
                            "GOOD",
                            1.0,
                            orphan_path.relative_to(Path(tmp)).as_posix(),
                            "sha256:" + "0" * 64,
                            2,
                            1,
                            "2026-11-09T00:00:00Z",
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                self.assertEqual(client.get("/api/cycles").status_code, 200)
                conn = sqlite3.connect(db)
                try:
                    self.assertIsNone(
                        conn.execute(
                            "SELECT 1 FROM evidence_snapshots WHERE cycle_id='cyc-orphan'"
                        ).fetchone()
                    )
                finally:
                    conn.close()
                self.assertFalse(orphan_path.exists())

    def test_detail_exposes_calls_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={"mode": "FULL"}
                ).json()["ai_run"]["ai_run_id"]
                call = client.put(
                    f"/api/ai-runs/{run_id}/calls/call-read",
                    json={"role": "analysis", "attempt": 1, "status": "ok"},
                )
                self.assertEqual(call.status_code, 200)
                event = client.post(
                    f"/api/ai-runs/{run_id}/events", json={"event": "call_completed"}
                )
                self.assertEqual(event.status_code, 200)
                detail = client.get(f"/api/cycles/{cycle_id}").json()
                self.assertEqual(detail["calls"][0]["call_id"], "call-read")
                self.assertEqual(detail["events"][0]["event"], "call_completed")

    def test_calls_are_immutable_and_attempt_must_be_positive_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]
                base = {"role": "analysis", "attempt": 1, "status": "ok"}
                self.assertEqual(
                    client.put(f"/api/ai-runs/{run_id}/calls/call-a", json=base).status_code,
                    200,
                )
                repeat = client.put(f"/api/ai-runs/{run_id}/calls/call-a", json=base)
                self.assertTrue(repeat.json()["idempotent"])
                for changed in (
                    {**base, "role": "red"},
                    {**base, "attempt": 2},
                    {**base, "status": "error"},
                ):
                    conflict = client.put(f"/api/ai-runs/{run_id}/calls/call-a", json=changed)
                    self.assertEqual(conflict.status_code, 409)
                for bad_attempt in (0, -1, 1.5, "1", True):
                    bad = client.put(
                        f"/api/ai-runs/{run_id}/calls/call-bad",
                        json={"role": "analysis", "attempt": bad_attempt},
                    )
                    self.assertEqual(bad.status_code, 422, bad.text)
                    self.assertEqual(bad.json()["error"]["code"], "E_VALIDATION")
                bad_artifact = client.put(
                    f"/api/cycles/{cycle_id}/artifacts/analysis",
                    json={"ai_run_id": run_id, "attempt": 0, "output": {}},
                )
                self.assertEqual(bad_artifact.status_code, 422)

    def test_two_ai_runs_same_attempt_get_distinct_default_output_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                run1 = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]
                run2 = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]
                out = {"bias": "neutral"}
                a = client.put(
                    f"/api/cycles/{cycle_id}/artifacts/analysis",
                    json={"ai_run_id": run1, "attempt": 1, "output": out},
                )
                b = client.put(
                    f"/api/cycles/{cycle_id}/artifacts/analysis",
                    json={"ai_run_id": run2, "attempt": 1, "output": out},
                )
                self.assertEqual(a.status_code, 200)
                self.assertEqual(b.status_code, 200)
                self.assertNotEqual(
                    a.json()["output"]["output_id"], b.json()["output"]["output_id"]
                )
                detail = client.get(f"/api/cycles/{cycle_id}").json()
                self.assertEqual(len(detail["outputs"]), 2)

    def test_artifact_semantics_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]
                invalid = client.put(
                    f"/api/cycles/{cycle_id}/artifacts/analysis",
                    json={"ai_run_id": run_id, "attempt": 1, "output": {}},
                )
                self.assertEqual(invalid.status_code, 200)
                self.assertFalse(invalid.json()["output"]["valid"])
                self.assertTrue(invalid.json()["output"]["semantic_errors"])
                valid_output = {
                    "schema_version": "analysis/1",
                    "cycle_id": cycle_id,
                    "agent": "analysis",
                    "market_read": {},
                    "symbols_analyzed": [],
                    "candidates": [],
                    "primary_symbol": "BTCUSDT",
                    "stand_aside": True,
                    "confidence": 0.5,
                    "evidence_refs": [],
                    "warnings": [],
                }
                valid = client.put(
                    f"/api/cycles/{cycle_id}/artifacts/analysis",
                    json={"ai_run_id": run_id, "attempt": 2, "output": valid_output},
                )
                self.assertEqual(valid.status_code, 200)
                self.assertTrue(valid.json()["output"]["valid"])
                self.assertEqual(valid.json()["output"]["semantic_errors"], [])




class ArtifactSchemaAlignmentTests(unittest.TestCase):
    """T22：Host 产物（risk_advice/1 / execution_entry/1）字段名与服务端规范名不同。

    修复前基线（真实账本副本只读实测，见 evidence/T22-产物校验对齐.md §2）：
    risk 72 行 + execution 57 行**全部** valid=0，semantic_errors 恒为
    `E_SCHEMA_MISSING fields=["verdict"]` / `["action"]`；analysis/red/blue/decision
    125+234+234+103 行全部 valid=1，说明只有这两个角色与 Host 命名不一致。
    """

    def _host_risk(
        self, cycle_id: str, *, recommendation: Any = "DOWNSIZE", **over: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "risk_advice/1",
            "cycle_id": cycle_id,
            "agent": "risk",
            "recommendation": recommendation,
            "size_multiplier": 0.5,
            "max_risk_pct": 1.0,
            "max_leverage": 10,
            "concerns": [],
            "conditions": [],
            "reasoning": "consistency check for host artifact",
            "confidence": 0.62,
            "warnings": [],
        }
        payload.update(over)
        return payload

    def _host_execution(
        self, cycle_id: str, *, executable: Any = False, **over: Any
    ) -> dict[str, Any]:
        order_plan = None
        if executable is True:
            order_plan = {
                "symbol": "BTCUSDT",
                "side": "SHORT",
                "action": "open",
                "entry_type": "MARKET",
                "leverage": 8,
                "qty": "0.02",
                "sl": "99000.0",
                "tp": "102800.0",
                "confidence": 0.55,
                "reasoning_ref": "decision:test",
                "execution_policy": {
                    "max_slippage_bps": 8,
                    "on_protection_failure": "CLOSE_POSITION",
                    "protection_retry_max": 1,
                },
            }
        payload: dict[str, Any] = {
            "schema_version": "execution_entry/1",
            "cycle_id": cycle_id,
            "agent": "execution",
            "mode": "ENTRY",
            "executable": executable,
            "blocked_reason": None if executable is True else "PRICE_MOVED",
            "symbol": "BTCUSDT",
            "side": "SHORT",
            "order_plan": order_plan,
            "preflight": None,
            "rationale": "market price already past the planned entry",
            "warnings": [],
        }
        payload.update(over)
        return payload

    def _put(
        self,
        client: TestClient,
        cycle_id: str,
        run_id: str,
        agent: str,
        output: dict[str, Any],
        *,
        attempt: int = 1,
    ) -> Any:
        return client.put(
            f"/api/cycles/{cycle_id}/artifacts/{agent}",
            json={"ai_run_id": run_id, "attempt": attempt, "output": output},
        )

    def test_host_shapes_are_valid_after_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id = create_cycle(client, {})["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]

                risk = self._put(client, cycle_id, run_id, "risk", self._host_risk(cycle_id))
                self.assertEqual(risk.status_code, 200, risk.text)
                self.assertTrue(risk.json()["output"]["valid"], risk.json()["output"])
                self.assertEqual(risk.json()["output"]["semantic_errors"], [])
                self.assertEqual(risk.json()["output"]["schema_version"], "risk_advice/1")

                no_trade = self._put(
                    client,
                    cycle_id,
                    run_id,
                    "execution",
                    self._host_execution(cycle_id, executable=False),
                    attempt=1,
                )
                self.assertEqual(no_trade.status_code, 200, no_trade.text)
                self.assertTrue(
                    no_trade.json()["output"]["valid"], no_trade.json()["output"]
                )
                self.assertEqual(no_trade.json()["output"]["semantic_errors"], [])

                entry = self._put(
                    client,
                    cycle_id,
                    run_id,
                    "execution",
                    self._host_execution(cycle_id, executable=True),
                    attempt=2,
                )
                self.assertEqual(entry.status_code, 200, entry.text)
                self.assertTrue(entry.json()["output"]["valid"], entry.json()["output"])

    def test_value_semantics_are_checked_not_just_presence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id = create_cycle(client, {})["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]

                # 有 recommendation 字段但取值不在 Host 枚举里：不能因为“有字段”就算通过。
                bad_enum = self._put(
                    client,
                    cycle_id,
                    run_id,
                    "risk",
                    self._host_risk(cycle_id, recommendation="MAYBE"),
                )
                self.assertEqual(bad_enum.status_code, 200, bad_enum.text)
                self.assertFalse(bad_enum.json()["output"]["valid"])
                codes = [
                    item["code"] for item in bad_enum.json()["output"]["semantic_errors"]
                ]
                self.assertIn("E_SCHEMA_ENUM", codes)

                bad_type = self._put(
                    client,
                    cycle_id,
                    run_id,
                    "execution",
                    self._host_execution(cycle_id, executable="false"),
                    attempt=2,
                )
                self.assertEqual(bad_type.status_code, 200, bad_type.text)
                self.assertFalse(bad_type.json()["output"]["valid"])
                codes = [
                    item["code"] for item in bad_type.json()["output"]["semantic_errors"]
                ]
                self.assertIn("E_SCHEMA_TYPE", codes)

    def test_missing_key_field_is_still_invalid_with_explicit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id = create_cycle(client, {})["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]

                risk = self._host_risk(cycle_id)
                risk.pop("recommendation")  # 两套命名都没有：必须仍然无效
                out = self._put(client, cycle_id, run_id, "risk", risk)
                self.assertEqual(out.status_code, 200, out.text)
                body = out.json()["output"]
                self.assertFalse(body["valid"])
                errors = body["semantic_errors"]
                self.assertEqual(errors[0]["code"], "E_SCHEMA_MISSING")
                self.assertEqual(errors[0]["fields"], ["verdict"])
                self.assertEqual(
                    errors[0]["accepted"]["verdict"], ["verdict", "recommendation"]
                )

                execution = self._host_execution(cycle_id)
                execution.pop("executable")
                execution.pop("order_plan")
                out2 = self._put(
                    client, cycle_id, run_id, "execution", execution, attempt=2
                )
                self.assertEqual(out2.status_code, 200, out2.text)
                body2 = out2.json()["output"]
                self.assertFalse(body2["valid"])
                self.assertEqual(body2["semantic_errors"][0]["fields"], ["action"])
                self.assertEqual(
                    body2["semantic_errors"][0]["accepted"]["action"],
                    ["action", "executable", "order_plan.action"],
                )

    def test_service_native_names_remain_accepted(self) -> None:
        """向后兼容：服务端旧名（verdict/action）继续可用，不因对齐而失效。"""

        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id = create_cycle(client, {})["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]

                risk = self._host_risk(cycle_id)
                risk.pop("recommendation")
                risk["verdict"] = "APPROVE"
                out = self._put(client, cycle_id, run_id, "risk", risk)
                self.assertTrue(out.json()["output"]["valid"], out.json()["output"])

                execution = self._host_execution(cycle_id, executable=False)
                execution.pop("executable")
                execution["action"] = "no_trade"
                out2 = self._put(
                    client, cycle_id, run_id, "execution", execution, attempt=2
                )
                self.assertTrue(out2.json()["output"]["valid"], out2.json()["output"])

    def test_historical_rows_are_not_rewritten(self) -> None:
        """历史 valid=0 行保留原样；只有新写入的行按新口径判定。"""

        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id = create_cycle(client, {})["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]
                service = client.app.state.cycles

                # 复刻“旧口径”写下的历史行：Host 形状 + 旧校验结论（valid=0）。
                legacy_output = self._host_risk(cycle_id)
                legacy_errors = [{"code": "E_SCHEMA_MISSING", "fields": ["verdict"]}]
                service.store.insert_output(
                    {
                        "output_id": "out-legacy-risk-1",
                        "call_id": None,
                        "cycle_id": cycle_id,
                        "agent": "risk",
                        "ai_run_id": run_id,
                        "attempt": 9,
                        "schema_version": "risk_advice/1",
                        "valid": 0,
                        "output_json": _json(legacy_output),
                        "semantic_errors_json": _json(legacy_errors),
                        "output_sha256": _hash_json(legacy_output),
                        "created_at": "2026-11-14T05:00:00Z",
                    }
                )

                fresh = self._put(
                    client, cycle_id, run_id, "risk", self._host_risk(cycle_id)
                )
                self.assertTrue(fresh.json()["output"]["valid"], fresh.json()["output"])

                legacy = service.store.get_output("out-legacy-risk-1")
                self.assertIsNotNone(legacy)
                self.assertEqual(int(legacy["valid"]), 0)
                self.assertEqual(json.loads(legacy["semantic_errors_json"]), legacy_errors)
                self.assertEqual(json.loads(legacy["output_json"]), legacy_output)
                self.assertEqual(legacy["created_at"], "2026-11-14T05:00:00Z")

                stated = client.get(f"/api/cycles/{cycle_id}").json()["outputs"]
                by_id = {row["output_id"]: row for row in stated}
                self.assertIn("out-legacy-risk-1", by_id)
                self.assertFalse(by_id["out-legacy-risk-1"]["valid"])
                self.assertTrue(by_id[fresh.json()["output"]["output_id"]]["valid"])


class CycleFinalizeTests(unittest.TestCase):
    def test_same_second_cycles_use_composite_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                fixed = "2026-11-09T05:00:00Z"
                with mock.patch(
                    "workbench_service.cycles.service._now", return_value=fixed
                ):
                    first = create_cycle(client, {})
                    self.assertEqual(
                        client.post(
                            f"/api/cycles/{first['cycle_id']}/finalize",
                            json={"state": "DONE", "outcome": "no_trade"},
                        ).status_code,
                        200,
                    )
                    second = create_cycle(client, {})
                page1 = client.get("/api/cycles", params={"limit": 1})
                self.assertEqual(page1.status_code, 200)
                self.assertIsNotNone(page1.json()["next_cursor"])
                page2 = client.get(
                    "/api/cycles",
                    params={"limit": 1, "cursor": page1.json()["next_cursor"]},
                )
                self.assertEqual(page2.status_code, 200)
                ids = {
                    page1.json()["cycles"][0]["cycle_id"],
                    page2.json()["cycles"][0]["cycle_id"],
                }
                self.assertEqual(ids, {first["cycle_id"], second["cycle_id"]})

    def test_missing_cycle_is_404(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                self.assertEqual(client.get("/api/cycles/cyc-missing").status_code, 404)
                self.assertEqual(client.get("/api/cycles/cyc-missing/evidence").status_code, 404)

    def test_state_outcome_matrix_and_terminal_audit_writes_are_409(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                for bad in (
                    {"state": "DONE", "outcome": "error"},
                    {"state": "FAILED", "outcome": "no_trade"},
                    {"state": "SKIPPED", "outcome": "trade"},
                    {"state": "CANCELLED", "outcome": "error"},
                ):
                    response = client.post(
                        f"/api/cycles/{cycle_id}/finalize", json=bad
                    )
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(response.json()["error"]["code"], "E_VALIDATION")

                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]
                self.assertEqual(
                    client.put(
                        f"/api/ai-runs/{run_id}/calls/call-before",
                        json={"role": "analysis", "attempt": 1},
                    ).status_code,
                    200,
                )
                done = client.post(
                    f"/api/cycles/{cycle_id}/finalize",
                    json={"state": "DONE", "outcome": "no_trade"},
                )
                self.assertEqual(done.status_code, 200)
                self.assertEqual(
                    client.post(f"/api/cycles/{cycle_id}/ai-runs", json={}).status_code,
                    409,
                )
                self.assertEqual(
                    client.put(
                        f"/api/ai-runs/{run_id}/calls/call-after",
                        json={"role": "analysis", "attempt": 1},
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.post(
                        f"/api/ai-runs/{run_id}/events", json={"event": "late"}
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.put(
                        f"/api/cycles/{cycle_id}/artifacts/analysis",
                        json={"ai_run_id": run_id, "attempt": 1, "output": {}},
                    ).status_code,
                    409,
                )
                self.assertEqual(
                    client.get(f"/api/cycles/{cycle_id}").status_code, 200
                )

    def test_terminal_state_outcomes_match_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                for state, outcome in (("SKIPPED", "no_trade"), ("CANCELLED", "no_trade")):
                    created = create_cycle(client, {})
                    response = client.post(
                        f"/api/cycles/{created['cycle_id']}/finalize",
                        json={"state": state, "outcome": outcome},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["cycle"]["state"], state)
                    self.assertEqual(response.json()["cycle"]["outcome"], outcome)

    def test_finalize_is_idempotent_and_blocks_different_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                body = {"state": "DONE", "outcome": "no_trade", "reason": "NO_CANDIDATE"}
                first = client.post(f"/api/cycles/{cycle_id}/finalize", json=body)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["cycle"]["state"], "DONE")
                self.assertFalse(first.json()["idempotent"])
                second = client.post(f"/api/cycles/{cycle_id}/finalize", json=body)
                self.assertEqual(second.status_code, 200)
                self.assertTrue(second.json()["idempotent"])
                conflict = client.post(
                    f"/api/cycles/{cycle_id}/finalize",
                    json={"state": "FAILED", "outcome": "error"},
                )
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.json()["error"]["code"], "E_CYCLE_FINALIZED")

    def test_no_credentials_cycle_can_no_trade_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                self.assertFalse(created["account"]["credentials_configured"])
                finalized = client.post(
                    f"/api/cycles/{created['cycle_id']}/finalize",
                    json={
                        "state": "DONE",
                        "outcome": "no_trade",
                        "execution": {"submitted": False, "reason": "NO_CANDIDATE"},
                    },
                )
                self.assertEqual(finalized.status_code, 200)
                self.assertEqual(finalized.json()["cycle"]["outcome"], "no_trade")
                self.assertEqual(
                    finalized.json()["cycle"]["execution_json"]["submitted"], False
                )




class T13TracingTests(unittest.TestCase):
    """T13: ai_calls timeout/cost/raw capture, ai_runs start vs finalize, events, evidence ms.

    Contract: a missing tracing fact must be NULL + an explicit reason; 0 must never
    stand in for "no timeout"/"zero cost"; history is never rewritten.
    """

    def _cycle_and_run(self, client: TestClient) -> tuple[str, str]:
        created = create_cycle(client, {})
        cycle_id = created["cycle_id"]
        run = client.post(f"/api/cycles/{cycle_id}/ai-runs", json={"mode": "FULL"})
        self.assertEqual(run.status_code, 200, run.text)
        return cycle_id, run.json()["ai_run"]["ai_run_id"]

    def _db(self, tmp: str) -> sqlite3.Connection:
        conn = sqlite3.connect(Path(tmp) / "workbench-v1.sqlite")
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ ai_runs
    def test_ai_run_without_reported_start_is_null_not_finalize_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id, run_id = self._cycle_and_run(client)
                conn = self._db(tmp)
                try:
                    row = conn.execute(
                        "SELECT started_at, finalized_at, started_at_source, "
                        "started_at_reason, created_at FROM ai_runs WHERE ai_run_id=?",
                        (run_id,),
                    ).fetchone()
                finally:
                    conn.close()
                # No caller start: NULL + reason, never the persist/finalize moment.
                self.assertIsNone(row["started_at"])
                self.assertIsNone(row["finalized_at"])
                self.assertEqual(row["started_at_source"], "unknown")
                self.assertEqual(row["started_at_reason"], "caller_did_not_supply_started_at")
                detail = client.get(f"/api/cycles/{cycle_id}").json()
                self.assertFalse(detail["ai_runs"][0]["started_at_trusted"])
                self.assertIn("finalize-time", detail["ai_runs"][0]["started_at_note"])

    def test_ai_run_start_is_derived_from_first_call_and_finalize_is_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id, run_id = self._cycle_and_run(client)
                call_start = "2026-11-13T18:20:21.390Z"
                first = client.put(
                    f"/api/ai-runs/{run_id}/calls/call-1",
                    json={
                        "role": "analysis",
                        "attempt": 1,
                        "status": "ok",
                        "started_at": call_start,
                        "ended_at": "2026-11-13T18:20:47.491Z",
                        "started_at_ms": 1794594021390,
                        "ended_at_ms": 1794594047491,
                    },
                )
                self.assertEqual(first.status_code, 200, first.text)
                conn = self._db(tmp)
                try:
                    run = conn.execute(
                        "SELECT started_at, started_at_source, started_at_reason "
                        "FROM ai_runs WHERE ai_run_id=?",
                        (run_id,),
                    ).fetchone()
                    call = conn.execute(
                        "SELECT started_at, started_at_ms, ended_at_ms FROM ai_calls "
                        "WHERE call_id='call-1'",
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(run["started_at"], call_start)
                self.assertEqual(run["started_at_source"], "earliest_call")
                self.assertEqual(run["started_at_reason"], "derived_from_first_call")
                self.assertEqual(call["started_at_ms"], 1794594021390)
                self.assertEqual(call["ended_at_ms"], 1794594047491)
                final = client.post(
                    f"/api/cycles/{cycle_id}/finalize",
                    json={"state": "DONE", "outcome": "no_trade"},
                )
                self.assertEqual(final.status_code, 200, final.text)
                conn = self._db(tmp)
                try:
                    row = conn.execute(
                        "SELECT started_at, finalized_at, ended_at FROM ai_runs "
                        "WHERE ai_run_id=?",
                        (run_id,),
                    ).fetchone()
                finally:
                    conn.close()
                # finalize writes finalized_at; started_at keeps the measured call start.
                self.assertEqual(row["started_at"], call_start)
                self.assertIsNotNone(row["finalized_at"])
                self.assertEqual(row["finalized_at"], row["ended_at"])
                self.assertNotEqual(row["finalized_at"], row["started_at"])

    def test_caller_reported_run_start_is_kept_and_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                run = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs",
                    json={"started_at": "2026-11-13T18:00:00.000Z"},
                ).json()["ai_run"]
                self.assertTrue(run["started_at_trusted"])
                self.assertEqual(run["started_at_source"], "caller")
                run_id = run["ai_run_id"]
                client.put(
                    f"/api/ai-runs/{run_id}/calls/call-1",
                    json={
                        "role": "analysis",
                        "attempt": 1,
                        "started_at": "2026-11-13T18:20:21.390Z",
                    },
                )
                conn = self._db(tmp)
                try:
                    row = conn.execute(
                        "SELECT started_at, started_at_source FROM ai_runs WHERE ai_run_id=?",
                        (run_id,),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(row["started_at"], "2026-11-13T18:00:00.000Z")
                self.assertEqual(row["started_at_source"], "caller")

    # ------------------------------------------------------------------ timeout
    def test_call_timeout_is_null_with_reason_and_zero_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                _cycle_id, run_id = self._cycle_and_run(client)
                for call_id, extra in (
                    ("no-timeout", {}),
                    ("zero-timeout", {"timeout_ms": 0}),
                    ("real-timeout", {"timeout_ms": 120000}),
                ):
                    response = client.put(
                        f"/api/ai-runs/{run_id}/calls/{call_id}",
                        json={"role": "analysis", "attempt": 1, **extra},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                conn = self._db(tmp)
                try:
                    rows = {
                        row["call_id"]: row
                        for row in conn.execute(
                            "SELECT call_id, timeout_ms, timeout_source, timeout_reason "
                            "FROM ai_calls WHERE ai_run_id=?",
                            (run_id,),
                        ).fetchall()
                    }
                finally:
                    conn.close()
                self.assertIsNone(rows["no-timeout"]["timeout_ms"])
                self.assertEqual(
                    rows["no-timeout"]["timeout_reason"], "no_timeout_source_configured"
                )
                # 0 must never be stored as "no timeout".
                self.assertIsNone(rows["zero-timeout"]["timeout_ms"])
                self.assertEqual(
                    rows["zero-timeout"]["timeout_reason"],
                    "reported_zero_is_not_a_timeout",
                )
                self.assertEqual(rows["real-timeout"]["timeout_ms"], 120000)
                self.assertEqual(rows["real-timeout"]["timeout_source"], "caller")
                self.assertIsNone(rows["real-timeout"]["timeout_reason"])

    # ------------------------------------------------------------------ cost
    def test_call_cost_requires_a_declared_price_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                _cycle_id, run_id = self._cycle_and_run(client)
                cases = {
                    "cost-none": {},
                    "cost-no-source": {"cost_estimate": "0.42"},
                    "cost-sourced": {
                        "cost_estimate": "0.42",
                        "cost_source": "llm-catalog:rev-2",
                    },
                }
                for call_id, extra in cases.items():
                    response = client.put(
                        f"/api/ai-runs/{run_id}/calls/{call_id}",
                        json={"role": "analysis", "attempt": 1, **extra},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                conn = self._db(tmp)
                try:
                    rows = {
                        row["call_id"]: row
                        for row in conn.execute(
                            "SELECT call_id, cost_estimate, cost_source, cost_reason "
                            "FROM ai_calls WHERE ai_run_id=?",
                            (run_id,),
                        ).fetchall()
                    }
                finally:
                    conn.close()
                self.assertIsNone(rows["cost-none"]["cost_estimate"])
                self.assertEqual(
                    rows["cost-none"]["cost_reason"], "no_reliable_price_source"
                )
                self.assertIsNone(rows["cost-no-source"]["cost_estimate"])
                self.assertEqual(rows["cost-no-source"]["cost_reason"], "cost_source_missing")
                self.assertEqual(rows["cost-sourced"]["cost_estimate"], "0.42")
                self.assertEqual(rows["cost-sourced"]["cost_source"], "llm-catalog:rev-2")
                self.assertIsNone(rows["cost-sourced"]["cost_reason"])

    def test_run_cost_is_null_with_reason_when_calls_are_mixed_or_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id, run_id = self._cycle_and_run(client)
                client.put(
                    f"/api/ai-runs/{run_id}/calls/call-1",
                    json={
                        "role": "analysis", "attempt": 1,
                        "cost_estimate": "0.10", "cost_source": "catalog:a",
                    },
                )
                client.put(
                    f"/api/ai-runs/{run_id}/calls/call-2",
                    json={"role": "red", "attempt": 1},
                )
                final = client.post(
                    f"/api/cycles/{cycle_id}/finalize",
                    json={"state": "DONE", "outcome": "no_trade"},
                )
                self.assertEqual(final.status_code, 200, final.text)
                conn = self._db(tmp)
                try:
                    run = conn.execute(
                        "SELECT cost_estimate, cost_source, cost_reason FROM ai_runs "
                        "WHERE ai_run_id=?",
                        (run_id,),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertIsNone(run["cost_estimate"])
                self.assertIsNone(run["cost_source"])
                self.assertEqual(run["cost_reason"], "partial_cost_missing")

    def test_run_cost_sums_calls_from_one_declared_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id, run_id = self._cycle_and_run(client)
                for index, cost in enumerate(("0.10", "0.25"), start=1):
                    client.put(
                        f"/api/ai-runs/{run_id}/calls/call-{index}",
                        json={
                            "role": "analysis", "attempt": index,
                            "cost_estimate": cost, "cost_source": "catalog:rev-7",
                        },
                    )
                client.post(
                    f"/api/cycles/{cycle_id}/finalize",
                    json={"state": "DONE", "outcome": "no_trade"},
                )
                conn = self._db(tmp)
                try:
                    run = conn.execute(
                        "SELECT cost_estimate, cost_source, cost_reason FROM ai_runs "
                        "WHERE ai_run_id=?",
                        (run_id,),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(run["cost_estimate"], "0.35")
                self.assertEqual(run["cost_source"], "catalog:rev-7")
                self.assertIsNone(run["cost_reason"])



    # ------------------------------------------------------------------ raw output
    def test_failed_output_raw_text_is_archived_or_explicitly_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                _cycle_id, run_id = self._cycle_and_run(client)
                raw = '{"bias": "long", "oops": '  # invalid JSON from the model
                response = client.put(
                    f"/api/ai-runs/{run_id}/calls/call-raw",
                    json={
                        "role": "analysis",
                        "attempt": 1,
                        "status": "invalid",
                        "response_text": raw,
                        "validation_errors": [{"keyword": "json", "path": "$"}],
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                absent = client.put(
                    f"/api/ai-runs/{run_id}/calls/call-noraw",
                    json={
                        "role": "analysis",
                        "attempt": 2,
                        "status": "invalid",
                        "validation_errors": [{"keyword": "schema", "path": "$"}],
                    },
                )
                self.assertEqual(absent.status_code, 200, absent.text)
                conn = self._db(tmp)
                try:
                    rows = {
                        row["call_id"]: row
                        for row in conn.execute(
                            "SELECT call_id, raw_response_text, raw_response_truncated, "
                            "raw_capture_reason, response_sha256 FROM ai_calls "
                            "WHERE ai_run_id=?",
                            (run_id,),
                        ).fetchall()
                    }
                finally:
                    conn.close()
                self.assertEqual(rows["call-raw"]["raw_response_text"], raw)
                self.assertEqual(rows["call-raw"]["raw_response_truncated"], 0)
                self.assertIsNone(rows["call-raw"]["raw_capture_reason"])
                # sha is derived from the real text when the caller does not send one
                self.assertEqual(
                    rows["call-raw"]["response_sha256"],
                    hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                )
                # A failed validation without raw text is an explicit gap, not silence.
                self.assertIsNone(rows["call-noraw"]["raw_response_text"])
                self.assertEqual(
                    rows["call-noraw"]["raw_capture_reason"],
                    "caller_did_not_supply_response_text",
                )

    def test_raw_text_hash_mismatch_with_recorded_sha_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                _cycle_id, run_id = self._cycle_and_run(client)
                client.put(
                    f"/api/ai-runs/{run_id}/calls/call-mismatch",
                    json={
                        "role": "analysis",
                        "attempt": 1,
                        "status": "invalid",
                        "response_text": "{\"bias\": \"short\"}",
                        "response_sha256": "b" * 64,
                        "validation_errors": [{"keyword": "schema"}],
                    },
                )
                conn = self._db(tmp)
                try:
                    row = conn.execute(
                        "SELECT response_sha256, raw_capture_reason FROM ai_calls "
                        "WHERE call_id='call-mismatch'",
                    ).fetchone()
                finally:
                    conn.close()
                # The caller-provided hash is immutable; the mismatch is explicit.
                self.assertEqual(row["response_sha256"], "b" * 64)
                self.assertEqual(row["raw_capture_reason"], "response_sha256_mismatch")

    def test_long_raw_text_is_truncated_with_a_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                _cycle_id, run_id = self._cycle_and_run(client)
                long_text = "x" * 9000
                client.put(
                    f"/api/ai-runs/{run_id}/calls/call-long",
                    json={
                        "role": "analysis", "attempt": 1, "status": "invalid",
                        "response_text": long_text,
                        "validation_errors": [{"keyword": "json"}],
                    },
                )
                conn = self._db(tmp)
                try:
                    row = conn.execute(
                        "SELECT raw_response_text, raw_response_truncated FROM ai_calls "
                        "WHERE call_id='call-long'",
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(len(row["raw_response_text"]), 8192)
                self.assertEqual(row["raw_response_truncated"], 1)

    # ------------------------------------------------------------------ events
    def test_events_store_real_type_role_time_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id, run_id = self._cycle_and_run(client)
                start_event = client.post(
                    f"/api/ai-runs/{run_id}/events",
                    json={
                        "event_type": "role_start",
                        "role_key": "red_team",
                        "call_id": "call-1",
                        "attempt": 1,
                        "at": "2026-11-13T18:20:21.390Z",
                    },
                )
                self.assertEqual(start_event.status_code, 200, start_event.text)
                end_event = client.post(
                    f"/api/ai-runs/{run_id}/events",
                    json={
                        "event_type": "role_end",
                        "role_key": "red_team",
                        "call_id": "call-1",
                        "status": "invalid",
                        "at": "2026-11-13T18:20:47.491Z",
                        "error_code": "SCHEMA_INVALID",
                    },
                )
                self.assertEqual(end_event.status_code, 200, end_event.text)
                conn = self._db(tmp)
                try:
                    rows = conn.execute(
                        "SELECT event, role, ts, ts_ms, from_state, to_state, reason_code, "
                        "payload_ref, event_source FROM ai_run_events "
                        "WHERE ai_run_id=? ORDER BY seq",
                        (run_id,),
                    ).fetchall()
                finally:
                    conn.close()
                self.assertEqual([row["event"] for row in rows], ["role_start", "role_end"])
                self.assertEqual([row["role"] for row in rows], ["red_team", "red_team"])
                # ts is the event time (ms precision), not the insert time.
                self.assertEqual(rows[0]["ts"], "2026-11-13T18:20:21.390Z")
                self.assertEqual(rows[0]["ts_ms"], 1794594021390)
                self.assertEqual(rows[0]["from_state"], None)
                self.assertEqual(rows[0]["to_state"], "RUNNING")
                self.assertEqual(rows[1]["from_state"], "RUNNING")
                self.assertEqual(rows[1]["to_state"], "INVALID")
                self.assertEqual(rows[1]["reason_code"], "SCHEMA_INVALID")
                self.assertEqual([row["payload_ref"] for row in rows], ["call-1", "call-1"])
                self.assertEqual([row["event_source"] for row in rows], ["event_type", "event_type"])
                detail = client.get(f"/api/cycles/{cycle_id}").json()
                self.assertTrue(all(not row["legacy_unusable"] for row in detail["events"]))

    def test_event_without_type_is_rejected_and_no_placeholder_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id, run_id = self._cycle_and_run(client)
                response = client.post(f"/api/ai-runs/{run_id}/events", json={"at": "2026-11-13T18:20:21.390Z"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "E_VALIDATION")
                conn = self._db(tmp)
                try:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM ai_run_events WHERE ai_run_id=?", (run_id,)
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(count, 0)

    def test_legacy_placeholder_event_row_is_flagged_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                cycle_id, run_id = self._cycle_and_run(client)
                conn = self._db(tmp)
                try:
                    conn.execute(
                        "INSERT INTO ai_run_events (ai_run_id, cycle_id, ts, event) "
                        "VALUES (?, ?, '2026-11-13T18:22:37Z', 'event')",
                        (run_id, cycle_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
                detail = client.get(f"/api/cycles/{cycle_id}").json()
                legacy = detail["events"][0]
                # History is not rewritten: the placeholder stays, but is labelled.
                self.assertEqual(legacy["event"], "event")
                self.assertIsNone(legacy["role"])
                self.assertTrue(legacy["legacy_unusable"])
                self.assertIn("pre-T13 placeholder", legacy["deprecated_note"])



    # ------------------------------------------------------------------ evidence ms
    def test_evidence_records_millisecond_build_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                evidence = created["evidence"]
                self.assertIsInstance(evidence["produced_at_ms"], int)
                self.assertIsInstance(evidence["evidence_build_ms"], int)
                self.assertGreaterEqual(evidence["evidence_build_ms"], 0)
                # ms-precision ISO for the four time points (not the second string).
                self.assertRegex(
                    evidence["produced_at_iso_ms"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
                )
                conn = self._db(tmp)
                try:
                    row = conn.execute(
                        "SELECT produced_at_ms, evidence_build_ms FROM evidence_snapshots "
                        "WHERE cycle_id=?",
                        (cycle_id,),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(row["produced_at_ms"], evidence["produced_at_ms"])
                self.assertEqual(row["evidence_build_ms"], evidence["evidence_build_ms"])
                detail = client.get(f"/api/cycles/{cycle_id}").json()
                self.assertEqual(detail["evidence"]["evidence_build_ms"], row["evidence_build_ms"])
                read = client.get(f"/api/cycles/{cycle_id}/evidence").json()
                self.assertEqual(read["produced_at_ms"], row["produced_at_ms"])
                self.assertEqual(read["evidence_build_ms"], row["evidence_build_ms"])
                self.assertRegex(read["produced_at_iso_ms"], r"\.\d{3}Z$")

    def test_legacy_evidence_row_without_build_ms_stays_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                conn = self._db(tmp)
                try:
                    conn.execute(
                        "UPDATE evidence_snapshots SET evidence_build_ms=NULL WHERE cycle_id=?",
                        (cycle_id,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                read = client.get(f"/api/cycles/{cycle_id}/evidence").json()
                self.assertIsNone(read["evidence_build_ms"])
                self.assertIsInstance(read["produced_at_ms"], int)



class T13MigrationTests(unittest.TestCase):
    """T13 up/down migration: ai_runs.started_at NOT NULL -> nullable, history kept."""

    LEGACY_AI_RUNS = """
    CREATE TABLE ai_runs (
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
    );
    CREATE INDEX idx_ai_runs_cycle ON ai_runs(cycle_id, started_at DESC);
    """

    def _legacy_db(self, tmp: str) -> sqlite3.Connection:
        conn = sqlite3.connect(Path(tmp) / "workbench-v1.sqlite")
        conn.row_factory = sqlite3.Row
        conn.executescript(self.LEGACY_AI_RUNS)
        conn.execute(
            "INSERT INTO ai_runs (ai_run_id, cycle_id, mode, trigger, state, provider, "
            "model, started_at, ended_at, cost_estimate, created_at, updated_at) "
            "VALUES ('legacy-1', 'cyc-legacy', 'FULL', 'manual', 'DONE', 'p', 'm', "
            "'2026-11-13T18:22:36Z', '2026-11-13T18:22:37Z', NULL, "
            "'2026-11-13T18:22:36Z', '2026-11-13T18:22:37Z')",
        )
        conn.commit()
        return conn

    def test_up_migration_makes_started_at_nullable_and_keeps_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._legacy_db(tmp)
            try:
                ensure_cycle_schema(conn)
                info = {
                    row[1]: row[3]
                    for row in conn.execute("PRAGMA table_info(ai_runs)").fetchall()
                }
                self.assertEqual(info["started_at"], 0, "started_at must be nullable")
                self.assertIn("finalized_at", info)
                self.assertIn("started_at_source", info)
                row = conn.execute(
                    "SELECT * FROM ai_runs WHERE ai_run_id='legacy-1'"
                ).fetchone()
                # History is preserved verbatim and marked as untrusted by source NULL.
                self.assertEqual(row["started_at"], "2026-11-13T18:22:36Z")
                self.assertEqual(row["ended_at"], "2026-11-13T18:22:37Z")
                self.assertIsNone(row["started_at_source"])
                self.assertIsNone(row["finalized_at"])
                self.assertIsNone(row["cost_source"])
                snapshot = schema_snapshot(conn)
                self.assertTrue(snapshot["tracing"]["ai_runs_started_at_nullable"])
                # New rows can now honestly record an unknown start.
                conn.execute(
                    "INSERT INTO ai_runs (ai_run_id, cycle_id, mode, trigger, state, "
                    "started_at, started_at_source, created_at, updated_at) "
                    "VALUES ('new-1', 'cyc-new', 'FULL', 'manual', 'RUNNING', NULL, "
                    "'unknown', '2026-11-13T19:00:00Z', '2026-11-13T19:00:00Z')",
                )
                # Idempotent: a second ensure must not rebuild or lose rows.
                ensure_cycle_schema(conn)
                count = conn.execute("SELECT COUNT(*) FROM ai_runs").fetchone()[0]
                self.assertEqual(count, 2)
            finally:
                conn.close()

    def test_down_migration_restores_not_null_and_backfills_created_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = self._legacy_db(tmp)
            try:
                ensure_cycle_schema(conn)
                conn.execute(
                    "INSERT INTO ai_runs (ai_run_id, cycle_id, mode, trigger, state, "
                    "started_at, started_at_source, created_at, updated_at) "
                    "VALUES ('new-1', 'cyc-new', 'FULL', 'manual', 'RUNNING', NULL, "
                    "'unknown', '2026-11-13T19:00:00Z', '2026-11-13T19:00:00Z')",
                )
                conn.commit()
                self.assertTrue(rollback_ai_runs_started_at_nullable(conn))
                info = {
                    row[1]: row[3]
                    for row in conn.execute("PRAGMA table_info(ai_runs)").fetchall()
                }
                self.assertEqual(info["started_at"], 1, "rollback must restore NOT NULL")
                self.assertNotIn("finalized_at", info)
                rows = {
                    row["ai_run_id"]: row
                    for row in conn.execute("SELECT * FROM ai_runs").fetchall()
                }
                self.assertEqual(rows["legacy-1"]["started_at"], "2026-11-13T18:22:36Z")
                # An unknown start is backfilled with created_at to satisfy the old
                # NOT NULL contract; the approximation is documented as rollback-only.
                self.assertEqual(rows["new-1"]["started_at"], "2026-11-13T19:00:00Z")
                # Rolling back twice is a no-op.
                self.assertFalse(rollback_ai_runs_started_at_nullable(conn))
            finally:
                conn.close()


class AiAuditTests(unittest.TestCase):
    def test_ai_runs_calls_events_and_artifacts_are_per_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                run_resp = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs",
                    json={"mode": "FULL", "provider": "fake", "model": "fake-1"},
                )
                self.assertEqual(run_resp.status_code, 200)
                run_id = run_resp.json()["ai_run"]["ai_run_id"]
                run_repeat = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs",
                    json={"ai_run_id": run_id},
                )
                self.assertEqual(run_repeat.status_code, 200)
                self.assertTrue(run_repeat.json()["idempotent"])

                call = {
                    "role": "analysis",
                    "attempt": 1,
                    "status": "ok",
                    "request_sha256": "a" * 64,
                    "response_sha256": "b" * 64,
                }
                first = client.put(f"/api/ai-runs/{run_id}/calls/call-1", json=call)
                self.assertEqual(first.status_code, 200)
                self.assertFalse(first.json()["idempotent"])
                repeat = client.put(f"/api/ai-runs/{run_id}/calls/call-1", json=call)
                self.assertEqual(repeat.status_code, 200)
                self.assertTrue(repeat.json()["idempotent"])
                changed = dict(call, response_sha256="c" * 64)
                conflict = client.put(f"/api/ai-runs/{run_id}/calls/call-1", json=changed)
                self.assertEqual(conflict.status_code, 409)

                event = client.post(
                    f"/api/ai-runs/{run_id}/events",
                    json={"event": "call_started", "role": "analysis"},
                )
                self.assertEqual(event.status_code, 200)

                artifact = {
                    "ai_run_id": run_id,
                    "call_id": "call-1",
                    "attempt": 1,
                    "schema_version": "analysis/1",
                    "valid": True,
                    "output": {"bias": "neutral"},
                }
                art1 = client.put(f"/api/cycles/{cycle_id}/artifacts/analysis", json=artifact)
                self.assertEqual(art1.status_code, 200)
                art1_repeat = client.put(f"/api/cycles/{cycle_id}/artifacts/analysis", json=artifact)
                self.assertEqual(art1_repeat.status_code, 200)
                self.assertTrue(art1_repeat.json()["idempotent"])

                artifact2 = dict(artifact, attempt=2, output={"bias": "long"})
                art2 = client.put(f"/api/cycles/{cycle_id}/artifacts/analysis", json=artifact2)
                self.assertEqual(art2.status_code, 200)

                detail = client.get(f"/api/cycles/{cycle_id}")
                self.assertEqual(detail.status_code, 200)
                body = detail.json()
                self.assertEqual(len(body["outputs"]), 2)
                self.assertEqual(body["agents"][0]["attempt"], 2)
                self.assertEqual(body["agents"][0]["output_id"], art2.json()["output"]["output_id"])
                self.assertEqual(len(body["ai_runs"]), 1)
                db = Path(tmp) / "workbench-v1.sqlite"
                conn = sqlite3.connect(db)
                try:
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM ai_calls WHERE ai_run_id=?", (run_id,)
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT COUNT(*) FROM ai_outputs WHERE cycle_id=? AND agent='analysis'",
                            (cycle_id,),
                        ).fetchone()[0],
                        2,
                    )
                finally:
                    conn.close()

    def test_cycle_finalize_closes_ai_run_with_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                run_id = client.post(
                    f"/api/cycles/{cycle_id}/ai-runs", json={}
                ).json()["ai_run"]["ai_run_id"]
                self.assertEqual(
                    client.put(
                        f"/api/ai-runs/{run_id}/calls/call-usage",
                        json={
                            "role": "analysis",
                            "attempt": 1,
                            "status": "ok",
                            "input_tokens": 11,
                            "output_tokens": 22,
                        },
                    ).status_code,
                    200,
                )
                final = client.post(
                    f"/api/cycles/{cycle_id}/finalize",
                    json={"state": "DONE", "outcome": "no_trade"},
                )
                self.assertEqual(final.status_code, 200)
                db = Path(tmp) / "workbench-v1.sqlite"
                conn = sqlite3.connect(db)
                try:
                    row = conn.execute(
                        "SELECT state, outcome, ended_at, token_input, token_output "
                        "FROM ai_runs WHERE ai_run_id=?",
                        (run_id,),
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual(row[0], "DONE")
                self.assertEqual(row[1], "no_trade")
                self.assertIsNotNone(row[2])
                self.assertEqual(row[3], 11)
                self.assertEqual(row[4], 22)
                late = client.post(
                    f"/api/cycles/{cycle_id}/finalize",
                    json={"state": "DONE", "outcome": "no_trade"},
                )
                self.assertEqual(late.status_code, 200)
                self.assertTrue(late.json()["idempotent"])

    def test_ai_cycle_auth_and_missing_run_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                created = create_cycle(client, {})
                cycle_id = created["cycle_id"]
                missing = client.post("/api/cycles/cyc-missing/ai-runs", json={})
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(
                    client.put(
                        "/api/ai-runs/run-missing/calls/call-1", json={"role": "analysis"}
                    ).status_code,
                    404,
                )
                self.assertEqual(
                    client.put(
                        f"/api/cycles/{cycle_id}/artifacts/nope",
                        json={"ai_run_id": "run-missing", "output": {}},
                    ).status_code,
                    422,
                )


if __name__ == "__main__":
    unittest.main()
