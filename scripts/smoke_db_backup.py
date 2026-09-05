"""SQLite在线备份、WAL一致性与保留期回归。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.db_backup import DatabaseBackupManager  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


root = Path(tempfile.mkdtemp(prefix="mta-db-backup-"))
db = AuditDatabase(root / "source.sqlite")
db.log_risk_event(event_type="backup_test", severity="info",
                  details="WAL中的记录也必须进入备份", action_taken="none")
backup_dir = root / "backups"
old = backup_dir / "agent_trader_20000101_000000.sqlite"
backup_dir.mkdir(parents=True)
old.write_bytes(b"old")
old_time = time.time() - 3 * 86400
os.utime(old, (old_time, old_time))

manager = DatabaseBackupManager(db, backup_dir, interval_hours=24, keep_days=1)
print("\n[1] 在线备份包含WAL并通过完整性校验")
status = manager.maybe_backup(force=True)
target = backup_dir / str(status["last_file"])
check("备份成功且文件存在", status["last_error"] is None and target.exists())
copy = sqlite3.connect(str(target))
try:
    row = copy.execute(
        "SELECT event_type FROM risk_events WHERE event_type='backup_test'").fetchone()
    integrity = copy.execute("PRAGMA quick_check").fetchone()
finally:
    copy.close()
check("WAL记录已包含", row and row[0] == "backup_test")
check("备份quick_check=ok", integrity and integrity[0] == "ok")

print("\n[2] 频率门禁和保留期")
same = manager.maybe_backup()
check("24小时内不重复备份", same["last_file"] == status["last_file"] and
      len(list(backup_dir.glob("agent_trader_*.sqlite"))) == 1)
check("过期备份已清理", not old.exists())
check("健康状态不暴露绝对路径", str(root) not in str(status))

db.close()
print("\n结果: 6 通过 / 0 失败")
