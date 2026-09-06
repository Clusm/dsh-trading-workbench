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
check("记录迁移版本1到10", [row["version"] for row in versions] == list(range(1, 11)))
db.log_shadow_decision(
    decision_id="legacy-b", symbol="BTCUSDT", signal="bullish", reference_price=100.0,
    confidence=0.5, source="binance_top_trader_proxy", execution_weight=0.0,
    feature_json="{}",
)
with db.lock:
    # 模拟从仅完成v9的旧版本升级，验证v10的数据迁移。
    db.conn.execute("DELETE FROM schema_migrations WHERE version=10")
    db.conn.commit()
db.close()

print("\n[2] 重启后迁移不重复")
again = AuditDatabase(db_path)
versions_again = again.fetch_all("SELECT version FROM schema_migrations")
check("版本记录保持唯一", [row["version"] for row in versions_again] == list(range(1, 11)))
check("旧B组名称已归一", again.fetch_one(
    "SELECT source FROM shadow_decisions WHERE decision_id='legacy-b'")["source"] == "B_contract_market")
again.close()

print("\n结果: 5 通过 / 0 失败")
