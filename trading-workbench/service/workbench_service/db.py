"""SQLite 初始化与迁移（新库 ``data/workbench-v1.sqlite``）。

M1.0 建 ``schema_migrations`` / ``meta`` / ``service_boots`` 三张最小表；
M1.3c 通过 ``cycles.schema.ensure_cycle_schema`` 幂等追加
``cycle_runs`` / ``cycle_agents`` / ``ai_runs`` / ``ai_calls`` / ``ai_outputs`` /
``ai_run_events``，并给 M1.2 旧版 ``cycle_runs`` 补列。该追加 schema 不写
``schema_migrations``，因此服务 schema version 仍为 1。

与旧项目 ``agent_trader.sqlite`` **无任何 attach/import/读写**。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SCHEMA_VERSION

logger = logging.getLogger("workbench.db")

DB_FILENAME = "workbench-v1.sqlite"

_MIGRATION_0001 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_boots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    version    TEXT NOT NULL,
    pid        INTEGER,
    note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_service_boots_started_at
    ON service_boots (started_at DESC);
"""

# (version, name, sql) —— 只向前；一次发布只追加，不修改历史项。
MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "0001_init", _MIGRATION_0001),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DbStatus:
    ok: bool
    path: str
    exists: bool
    size_bytes: int
    schema_version: int
    journal_mode: str | None
    ledger_id: str | None
    boot_count: int | None
    last_boot_at: str | None
    migrations_applied: int
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "schema_version": self.schema_version,
            "journal_mode": self.journal_mode,
            "ledger_id": self.ledger_id,
            "boot_count": self.boot_count,
            "last_boot_at": self.last_boot_at,
            "migrations_applied": self.migrations_applied,
            "error": self.error,
        }


def db_path_for(data_dir: Path) -> Path:
    return Path(data_dir) / DB_FILENAME


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count_migrations(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_migrations"):
        return 0
    row = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    return int(row[0])


def _current_schema_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_migrations"):
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()
    return int(row["v"] if isinstance(row, sqlite3.Row) else row[0])


def _get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    if not _table_exists(conn, "meta"):
        return None
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, utc_now_iso()),
    )


def _backup_before_migration(db_path: Path, backups_dir: Path) -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backups_dir / f"workbench-v1-{stamp}.sqlite"
    shutil.copy2(db_path, target)
    return target


def _apply_pending(conn: sqlite3.Connection, pending: list[tuple[int, str, str]]) -> None:
    for version, name, sql in pending:
        applied_at = utc_now_iso()
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql.strip()}\n"
            "INSERT INTO schema_migrations (version, name, applied_at) "
            f"VALUES ({version}, '{name}', '{applied_at}');\n"
            "COMMIT;\n"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            conn.rollback()
            raise


def init_db(data_dir: Path, *, pid: int | None = None, version: str = "") -> DbStatus:
    """打开/迁移数据库并登记一次启动；失败返回 ``ok=False`` 的结构化状态。"""

    path = db_path_for(data_dir)
    existed = path.exists() and path.stat().st_size > 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = _connect(path)
    except (OSError, sqlite3.Error) as exc:
        return DbStatus(
            ok=False,
            path=str(path),
            exists=path.exists(),
            size_bytes=path.stat().st_size if path.exists() else 0,
            schema_version=0,
            journal_mode=None,
            ledger_id=None,
            boot_count=None,
            last_boot_at=None,
            migrations_applied=0,
            error=f"{type(exc).__name__}: {exc}",
        )

    applied = 0
    try:
        current = _current_schema_version(conn)
        pending = [item for item in MIGRATIONS if item[0] > current]
        if pending and existed:
            _backup_before_migration(path, Path(data_dir) / "backups")
        if pending:
            _apply_pending(conn, pending)
            applied = len(pending)
            conn.commit()

        # M1.3c additive local schema: cycle/evidence/AI audit tables. This is
        # intentionally not a schema_migrations row so the service schema
        # version stays 1 and existing M1.0/M1.2 databases upgrade in place.
        from .cycles.schema import ensure_cycle_schema

        ensure_cycle_schema(conn)

        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"quick_check failed: {check}")

        ledger_id = _get_meta(conn, "ledger_id") or uuid.uuid4().hex
        boot_count = int(_get_meta(conn, "boot_count") or 0) + 1
        started_at = utc_now_iso()

        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
        _set_meta(conn, "ledger_id", ledger_id)
        _set_meta(conn, "boot_count", str(boot_count))
        _set_meta(conn, "last_boot_at", started_at)
        _set_meta(conn, "last_version", version or "unknown")
        if _get_meta(conn, "created_at") is None:
            _set_meta(conn, "created_at", started_at)
        conn.execute(
            "INSERT INTO service_boots (started_at, version, pid, note) VALUES (?, ?, ?, ?)",
            (started_at, version or "unknown", pid, "m1.0 skeleton"),
        )
        conn.commit()

        row = conn.execute("SELECT value FROM meta WHERE key='boot_count'").fetchone()
        boot_count = int(row["value"]) if row is not None else boot_count
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]

        return DbStatus(
            ok=True,
            path=str(path),
            exists=True,
            size_bytes=path.stat().st_size,
            schema_version=_current_schema_version(conn),
            journal_mode=str(journal),
            ledger_id=ledger_id,
            boot_count=boot_count,
            last_boot_at=started_at,
            migrations_applied=_count_migrations(conn),
        )
    except (OSError, sqlite3.Error) as exc:
        logger.error("database init failed: %s", exc)
        return DbStatus(
            ok=False,
            path=str(path),
            exists=path.exists(),
            size_bytes=path.stat().st_size if path.exists() else 0,
            schema_version=0,
            journal_mode=None,
            ledger_id=None,
            boot_count=None,
            last_boot_at=None,
            migrations_applied=applied,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()


def db_status(data_dir: Path) -> DbStatus:
    """只读探测当前库状态（供 ``/health`` 每次调用，低成本）。"""

    path = db_path_for(data_dir)
    if not path.exists():
        return DbStatus(
            ok=False,
            path=str(path),
            exists=False,
            size_bytes=0,
            schema_version=0,
            journal_mode=None,
            ledger_id=None,
            boot_count=None,
            last_boot_at=None,
            migrations_applied=0,
            error="DB_NOT_INITIALIZED",
        )
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            version = _current_schema_version(conn)
            ledger_id = _get_meta(conn, "ledger_id")
            boot_count_raw = _get_meta(conn, "boot_count")
            last_boot_at = _get_meta(conn, "last_boot_at")
            journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
            applied_total = _count_migrations(conn)
        finally:
            conn.close()
        return DbStatus(
            ok=True,
            path=str(path),
            exists=True,
            size_bytes=path.stat().st_size,
            schema_version=version,
            journal_mode=str(journal),
            ledger_id=ledger_id,
            boot_count=int(boot_count_raw) if boot_count_raw is not None else None,
            last_boot_at=last_boot_at,
            migrations_applied=applied_total,
        )
    except (OSError, sqlite3.Error) as exc:
        return DbStatus(
            ok=False,
            path=str(path),
            exists=True,
            size_bytes=path.stat().st_size,
            schema_version=0,
            journal_mode=None,
            ledger_id=None,
            boot_count=None,
            last_boot_at=None,
            migrations_applied=0,
            error=f"{type(exc).__name__}: {exc}",
        )
