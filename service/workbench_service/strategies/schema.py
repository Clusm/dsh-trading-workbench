"""Additive strategy schema with a WAL-consistent pre-migration backup."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from ..db import _connect

DDL = """
CREATE TABLE IF NOT EXISTS strategy_config (
 id INTEGER PRIMARY KEY CHECK(id=1), mode TEXT NOT NULL CHECK(mode IN ('native','mounted')),
 revision INTEGER NOT NULL, updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO strategy_config VALUES (1,'native',0,'');
CREATE TABLE IF NOT EXISTS strategies (
 strategy_id TEXT PRIMARY KEY, name TEXT NOT NULL,
 current_version INTEGER, enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 generation INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_drafts (
 strategy_id TEXT PRIMARY KEY REFERENCES strategies(strategy_id), revision INTEGER NOT NULL,
 payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
 strategy_id TEXT NOT NULL REFERENCES strategies(strategy_id), version INTEGER NOT NULL,
 payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, confirmed_at TEXT NOT NULL,
 PRIMARY KEY(strategy_id, version)
);
CREATE TABLE IF NOT EXISTS strategy_mount_requests (
 request_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL, version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_events (
 event_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, action TEXT NOT NULL,
 generation INTEGER NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_signals (
 strategy_id TEXT NOT NULL, version INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
 signal_key TEXT NOT NULL, cycle_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
 context_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL,
 PRIMARY KEY(strategy_id,version,symbol,side,signal_key)
);
CREATE TABLE IF NOT EXISTS strategy_position_ownership (
 symbol TEXT NOT NULL, side TEXT NOT NULL, cycle_id TEXT NOT NULL,
 strategy_id TEXT NOT NULL, version INTEGER NOT NULL, generation INTEGER NOT NULL,
 opened_at TEXT NOT NULL, PRIMARY KEY(symbol,side,cycle_id)
);
CREATE TRIGGER IF NOT EXISTS strategy_versions_no_update
 BEFORE UPDATE ON strategy_versions BEGIN SELECT RAISE(ABORT, 'confirmed strategy version is immutable'); END;
CREATE TRIGGER IF NOT EXISTS strategy_versions_no_delete
 BEFORE DELETE ON strategy_versions BEGIN SELECT RAISE(ABORT, 'confirmed strategy version cannot be deleted'); END;
"""


def ensure_schema(path: Path) -> None:
    path = Path(path)
    existed = path.exists() and path.stat().st_size > 0
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_versions'").fetchone()
        if not present and existed:
            directory = path.parent / "backups"
            directory.mkdir(parents=True, exist_ok=True)
            backup_path = directory / f"before-strategies-{uuid.uuid4().hex}.sqlite"
            backup = sqlite3.connect(backup_path)
            try:
                conn.backup(backup)
            finally:
                backup.close()
        conn.executescript("BEGIN IMMEDIATE;\n" + DDL + "\nCOMMIT;")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
