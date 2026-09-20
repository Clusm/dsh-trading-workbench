"""SQLite在线备份：使用backup API覆盖WAL，并在发布前做完整性校验。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase

#: 运行期在线备份的 epoch 边车后缀。它是"这次 DB 快照对应哪些 JSON 状态"的
#: 证据——**运行中**取的快照只能标注，不能声称与停写同 epoch 恢复包等价。
EPOCH_SIDECAR_SUFFIX = ".epoch.json"
#: 关键状态文件（与 RuntimePaths.REQUIRED_STATE_FILES 同源；这里不 import 以
#: 避免 core 包内循环依赖）。
_REQUIRED_STATE_NAMES = (
    "capital_state.json",
    "channel_state.json",
    "control_state.json",
    "account_risk_state.json",
)


class DatabaseBackupManager:
    """使用 SQLite 在线备份 API 创建包含 WAL 提交内容的一致性备份。

    调度循环可频繁调用 ``maybe_backup``；管理器内部控制周期、失败重试和保留天数，
    不会用普通文件复制正在写入的数据库。

    U13 增强（F05/F20/R21）：

    - 每次成功备份额外落一个 ``*.epoch.json`` 边车，记录 DB 快照时刻、迁移版本、
      DB 哈希与**当时**关键 JSON 状态的哈希，并显式标注 ``in_service=True``：
      同盘、运行期备份**不等于**灾难恢复，停写同 epoch 恢复包见 ``core.ops.recovery``；
    - 保留策略同时受 ``keep_days`` 与 ``keep_max_files`` 上界约束（资源有界）；
    - 仍保持"暂存 → 校验 → 原子替换"：坏备份不会覆盖好备份。
    """
    def __init__(self, db: AuditDatabase, backup_dir: Path, *, enabled: bool = True,
                 interval_hours: float = 24, keep_days: int = 14,
                 retry_hours: float = 1, keep_max_files: int = 200,
                 state_dir: Path | None = None) -> None:
        self.db = db
        self.backup_dir = Path(backup_dir)
        self.enabled = bool(enabled)
        self.interval_seconds = max(300.0, float(interval_hours) * 3600)
        self.keep_seconds = max(86400.0, int(keep_days) * 86400.0)
        self.retry_seconds = max(300.0, float(retry_hours) * 3600)
        self.keep_max_files = max(1, int(keep_max_files))
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self._lock = threading.Lock()
        self._last_attempt_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error: str | None = None
        self._last_duration_ms: float | None = None
        self._last_file: str | None = None
        self._last_schema_version: int | None = None
        self._last_epoch_file: str | None = None
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
            # U13：只暴露标量，不把绝对路径写进健康状态。
            "keep_max_files": self.keep_max_files,
            "last_schema_version": self._last_schema_version,
            "last_epoch_file": self._last_epoch_file,
            "state_dir_configured": self.state_dir is not None,
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
        if final.exists():
            # 同一秒内第二次备份（force=True / 手工触发）绝不能覆盖上一份好备份。
            final = self.backup_dir / f"agent_trader_{stamp}_{uuid.uuid4().hex[:8]}.sqlite"
        temp = self.backup_dir / f".{final.name}.tmp-{os.getpid()}-{threading.get_ident()}"
        started = time.perf_counter()
        epoch: dict[str, Any] | None = None
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
                schema_version = int(migration[0])
            finally:
                destination.close()
            # 哈希必须在**暂存文件**上算：此刻 final 还没被 os.replace 进去。
            epoch = self._build_epoch(temp, final.name, schema_version)
            os.replace(temp, final)
            self._last_success_at = time.time()
            self._last_error = None
            self._last_duration_ms = round((time.perf_counter() - started) * 1000, 3)
            self._last_file = final.name
            self._last_schema_version = schema_version
            self._last_epoch_file = self._write_epoch_sidecar(final, epoch)
            self._purge_old()
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _build_epoch(self, hash_source: Path, backup_name: str,
                     schema_version: int) -> dict[str, Any]:
        """构造 epoch 边车内容：DB 快照 + **当时**关键 JSON 状态的哈希。

        诚实标注：这是在**运行期**取的快照，JSON 可能在下一秒继续变化，因此它
        只证明"备份那一刻各方状态是什么"，不构成停写同 epoch 恢复包（R21）。
        """
        state_hashes: dict[str, str | None] = {}
        state_missing: list[str] = []
        if self.state_dir is not None:
            for name in _REQUIRED_STATE_NAMES:
                candidate = self.state_dir / name
                try:
                    state_hashes[name] = self._sha256(candidate) if candidate.is_file() else None
                except OSError:
                    state_hashes[name] = None
                if state_hashes[name] is None:
                    state_missing.append(name)
        return {
            "epoch_id": uuid.uuid4().hex,
            "snapshot_at_ms": int(time.time() * 1000),
            "database": backup_name,
            "database_sha256": self._sha256(hash_source),
            "schema_version": schema_version,
            "state_hashes": state_hashes,
            "state_missing": state_missing,
            "in_service": True,
            "disaster_recovery": {
                "same_volume_only": True,
                "requires_stop_write_epoch_bundle": True,
                "bundle_module": "core.ops.recovery",
                "reason": "运行期在线备份与 JSON 状态不保证同一快照窗口，"
                          "灾难恢复必须用停写期创建的同 epoch 恢复包（R21）",
            },
        }

    def _write_epoch_sidecar(self, backup_file: Path, epoch: dict[str, Any]) -> str:
        sidecar = backup_file.with_name(f"{backup_file.name}{EPOCH_SIDECAR_SUFFIX}")
        payload = (json.dumps(epoch, ensure_ascii=False, indent=2, sort_keys=True)
                   + "\n").encode("utf-8")
        temp = sidecar.with_name(f".{sidecar.name}.tmp-{os.getpid()}")
        temp.write_bytes(payload)
        os.replace(temp, sidecar)
        return sidecar.name

    def _purge_old(self) -> None:
        cutoff = time.time() - self.keep_seconds
        backups = self._backup_files()
        # 先按保留天数清理，再按文件数上界截断（资源有界，F20）。
        for path in backups:
            try:
                if path.name != self._last_file and path.stat().st_mtime < cutoff:
                    self._remove_backup(path)
            except OSError:
                pass
        remaining = self._backup_files()
        if len(remaining) > self.keep_max_files:
            for path in remaining[self.keep_max_files:]:
                try:
                    self._remove_backup(path)
                except OSError:
                    pass

    def _backup_files(self) -> list[Path]:
        """按修改时间倒序返回备份主文件（边车不参与计数）。"""
        try:
            paths = [path for path in self.backup_dir.glob("agent_trader_*.sqlite")
                     if path.is_file()]
        except OSError:  # pragma: no cover - 取决于文件系统
            return []
        return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)

    def _remove_backup(self, path: Path) -> None:
        path.unlink()
        sidecar = path.with_name(f"{path.name}{EPOCH_SIDECAR_SUFFIX}")
        if sidecar.exists():
            sidecar.unlink()
