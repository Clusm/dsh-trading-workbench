"""SQLite迁移版本与幂等性回归；只使用临时数据库。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


db_path = Path(tempfile.mkdtemp()) / "legacy.sqlite"
legacy = sqlite3.connect(db_path)
legacy.execute(
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, client_order_id TEXT NOT NULL UNIQUE)"
)
legacy.commit()
legacy.close()

print("\n[1] 旧库首次升级")
db = AuditDatabase(db_path)
columns = {row["name"] for row in db.fetch_all("PRAGMA table_info(orders)")}
versions = db.fetch_all("SELECT version, name FROM schema_migrations ORDER BY version")
check("补齐order_source列", "order_source" in columns)
check("补齐margin_mode列", "margin_mode" in columns)
check("记录迁移版本1到3", [row["version"] for row in versions] == [1, 2, 3])
db.close()

print("\n[2] 重启后迁移不重复")
again = AuditDatabase(db_path)
versions_again = again.fetch_all("SELECT version FROM schema_migrations")
check("版本记录保持唯一", [row["version"] for row in versions_again] == [1, 2, 3])
again.close()

print("\n结果: 4 通过 / 0 失败")
