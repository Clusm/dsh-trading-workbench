"""SQLite 向前迁移的事务执行器。"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable


def apply_migration(
    connection: sqlite3.Connection,
    version: int,
    name: str,
    action: Callable[[], None],
    *,
    applied_at: str,
) -> bool:
    """幂等执行单个迁移；成功返回 True，已执行返回 False，失败完整回滚。"""
    exists = connection.execute(
        "SELECT version FROM schema_migrations WHERE version=?", (version,)
    ).fetchone()
    if exists:
        return False
    try:
        action()
        connection.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
            (version, name, applied_at),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
