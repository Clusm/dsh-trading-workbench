"""SQLite在线备份：使用backup API覆盖WAL，并在发布前做完整性校验。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase


class DatabaseBackupManager:
    """使用 SQLite 在线备份 API 创建包含 WAL 提交内容的一致性备份。

    调度循环可频繁调用 ``maybe_backup``；管理器内部控制周期、失败重试和保留天数，
    不会用普通文件复制正在写入的数据库。
    """
    def __init__(self, db: AuditDatabase, backup_dir: Path, *, enabled: bool = True,
                 interval_hours: float = 24, keep_days: int = 14,
                 retry_hours: float = 1) -> None:
        self.db = db
        self.backup_dir = Path(backup_dir)
        self.enabled = bool(enabled)
        self.interval_seconds = max(300.0, float(interval_hours) * 3600)
        self.keep_seconds = max(86400.0, int(keep_days) * 86400.0)
        self.retry_seconds = max(300.0, float(retry_hours) * 3600)
        self._lock = threading.Lock()
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error: str | None = None
        self._last_duration_ms: float | None = None
        self._last_file: str | None = None
        self._load_latest()

    def _load_latest(self) -> None:
        try:
            files = list(self.backup_dir.glob("agent_trader_*.sqlite"))
            if files:
                newest = max(files, key=lambda p: p.stat().st_mtime)
                self._last_success_at = newest.stat().st_mtime
                self._last_file = newest.name
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "last_attempt_at": self._last_attempt_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "last_duration_ms": self._last_duration_ms,
            "last_file": self._last_file,
            "interval_hours": self.interval_seconds / 3600,
            "keep_days": int(self.keep_seconds / 86400),
        }

    def maybe_backup(self, *, force: bool = False) -> dict[str, Any]:
        if not self.enabled:
            return self.status()
        now = time.time()
        if not force:
            if self._last_success_at and now - self._last_success_at < self.interval_seconds:
                return self.status()
            if (self._last_error and self._last_attempt_at and
                    now - self._last_attempt_at < self.retry_seconds):
                return self.status()
        if not self._lock.acquire(blocking=False):
            return self.status()
        try:
            self._last_attempt_at = now
            self._create_backup()
        except Exception as exc:
            self._last_error = type(exc).__name__
        finally:
            self._lock.release()
        return self.status()

    def _create_backup(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        final = self.backup_dir / f"agent_trader_{stamp}.sqlite"
        temp = self.backup_dir / f".{final.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        started = time.perf_counter()
        try:
            destination = sqlite3.connect(str(temp))
            try:
                with self.db.lock:
                    self.db.conn.backup(destination)
                check = destination.execute("PRAGMA quick_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise RuntimeError("SQLite备份完整性校验失败")
                migration = destination.execute(
                    "SELECT MAX(version) FROM schema_migrations").fetchone()
                if not migration or migration[0] is None:
                    raise RuntimeError("SQLite备份缺少迁移版本")
            finally:
                destination.close()
            os.replace(temp, final)
            self._last_success_at = time.time()
            self._last_error = None
            self._last_duration_ms = round((time.perf_counter() - started) * 1000, 3)
            self._last_file = final.name
            self._purge_old()
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass

    def _purge_old(self) -> None:
        cutoff = time.time() - self.keep_seconds
        for path in self.backup_dir.glob("agent_trader_*.sqlite"):
            try:
                if path.name != self._last_file and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
