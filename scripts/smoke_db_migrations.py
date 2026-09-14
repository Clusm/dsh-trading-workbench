"""SQLite迁移版本与幂等性回归；只使用临时数据库。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from audit.migrations import apply_migration  # noqa: E402


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
check("记录迁移版本1到16", [row["version"] for row in versions] == list(range(1, 17)))
check("新增成交/生命周期/补账表", all(
    db.fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    is not None
    for table in ("execution_fills", "position_lifecycles",
                  "lifecycle_fill_allocations", "trade_reconciliation_jobs")))
outcome_columns = {row["name"] for row in db.fetch_all("PRAGMA table_info(trade_outcomes)")}
check("trade_outcomes含生命周期与成本状态", {
    "lifecycle_id", "attribution_status", "cost_status", "actual_closed_at",
    "gross_realized", "entry_fee", "exit_fee", "funding_cashflow",
}.issubset(outcome_columns))
check("核心审计表含发布上下文", all(
    {row["name"] for row in db.fetch_all(f"PRAGMA table_info({table})")}
    .issuperset({"release_id", "environment"})
    for table in ("trade_decisions", "trade_intents", "orders", "positions",
                  "shadow_decisions", "risk_events", "portfolio_snapshots",
                  "signal_events", "signal_tickets", "execution_fills",
                  "position_lifecycles", "trade_reconciliation_jobs", "order_outbox")))
check("发布观察表按分钟建立", db.fetch_one(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='release_observations'")
      is not None)
observation_columns = {row["name"] for row in db.fetch_all(
    "PRAGMA table_info(release_observations)")}
check("发布观察表含门禁字段", {
    "release_id", "environment", "account_ref", "observed_minute", "health_state",
    "rate_limit_json", "entry_readiness_json",
}.issubset(observation_columns))
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
check("版本记录保持唯一", [row["version"] for row in versions_again] == list(range(1, 17)))
check("旧B组名称已归一", again.fetch_one(
    "SELECT source FROM shadow_decisions WHERE decision_id='legacy-b'")["source"] == "B_contract_market")
again.close()

print("\n[3] 迁移执行器失败回滚且不写版本")
rollback_path = db_path.with_name("rollback.sqlite")
raw = sqlite3.connect(rollback_path)
raw.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
raw.execute("CREATE TABLE probe (value TEXT)")
raw.commit()

def failing_action() -> None:
    raw.execute("INSERT INTO probe(value) VALUES('must_rollback')")
    raise RuntimeError("injected")

try:
    apply_migration(raw, 99, "failing", failing_action, applied_at="2026-09-12T00:00:00Z")
    failed = False
except RuntimeError:
    failed = True
check("注入失败向上抛出", failed)
check("迁移写入完整回滚", raw.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0)
check("失败版本没有登记", raw.execute(
    "SELECT COUNT(*) FROM schema_migrations WHERE version=99").fetchone()[0] == 0)
raw.close()

print("\n结果: 13 通过 / 0 失败")
