"""审计数据库：阶段 0 先建核心表并记录 agent_runs / llm_calls / artifacts / 决策链路。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    role TEXT,
    model TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    model TEXT,
    input_summary TEXT,
    output_summary TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_estimate REAL DEFAULT 0,
    latency_ms INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id TEXT NOT NULL UNIQUE,
    run_id TEXT,
    agent_name TEXT NOT NULL,
    model TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost_estimate REAL DEFAULT 0,
    latency_ms INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    prompt_file TEXT,
    completion_file TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    run_id TEXT,
    agent_name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    json_path TEXT NOT NULL,
    sha256 TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    agent_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    input_summary TEXT,
    output_summary TEXT,
    latency_ms INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS trade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    run_id TEXT,
    created_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    channel TEXT DEFAULT 'ai',
    reasoning TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_intents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id TEXT NOT NULL UNIQUE,
    decision_id TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL,
    symbol TEXT,
    action TEXT,
    client_order_id TEXT,
    side TEXT,
    qty REAL,
    leverage INTEGER,
    order_type TEXT,
    stop_loss REAL,
    take_profit REAL,
    expected_holding_minutes INTEGER,
    confidence REAL,
    order_source TEXT DEFAULT 'ai',
    strategy_id TEXT,
    ticket_id TEXT,
    invalidation TEXT,
    blocked_reason TEXT,
    status TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    verdict_id TEXT NOT NULL UNIQUE,
    decision_id TEXT,
    created_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    reasons TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT NOT NULL UNIQUE,
    exchange_order_id TEXT,
    symbol TEXT,
    side TEXT,
    order_type TEXT,
    qty REAL,
    price REAL,
    status TEXT,
    order_source TEXT DEFAULT 'ai',
    strategy_id TEXT,
    ticket_id TEXT,
    margin_mode TEXT DEFAULT 'ISOLATED',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL,
    leverage INTEGER,
    stop_loss REAL,
    take_profit REAL,
    source TEXT DEFAULT 'ai',
    strategy_id TEXT,
    ticket_id TEXT,
    margin_mode TEXT DEFAULT 'ISOLATED',
    status TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    equity REAL,
    margin_balance REAL,
    available_balance REAL,
    unrealized_pnl REAL,
    margin_used REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    details TEXT,
    action_taken TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS daily_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_date TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    report_path TEXT NOT NULL,
    summary TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS agent_scorecard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score_date TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    decision_count INTEGER DEFAULT 0,
    useful_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    score REAL DEFAULT 0,
    notes TEXT,
    UNIQUE(score_date, agent_name)
);

CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL UNIQUE,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    side TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    signal_price REAL,
    bar_open_time INTEGER DEFAULT 0,
    params_json TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS signal_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL UNIQUE,
    signal_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    signal_time TEXT NOT NULL,
    signal_price REAL,
    entry_zone_low REAL,
    entry_zone_high REAL,
    expires_at TEXT NOT NULL,
    stop_loss_pct REAL,
    take_profit_pct REAL,
    time_stop_hours INTEGER DEFAULT 48,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS decision_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    decision_id TEXT,
    symbol TEXT,
    source TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stat_date TEXT NOT NULL,
    channel TEXT NOT NULL,
    start_equity REAL,
    end_equity REAL,
    realized_pnl REAL,
    fees REAL DEFAULT 0,
    gross_pnl REAL DEFAULT 0,
    closed_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    win_rate REAL DEFAULT 0,
    max_drawdown REAL DEFAULT 0,
    rebalanced_from REAL,
    rebalanced_to REAL,
    status TEXT DEFAULT 'active',
    raw_json TEXT,
    UNIQUE(stat_date, channel)
);

CREATE TABLE IF NOT EXISTS daily_rebalances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rebalance_date TEXT NOT NULL,
    channel TEXT NOT NULL,
    from_equity REAL,
    to_equity REAL,
    transfer_amount REAL,
    reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(rebalance_date, channel)
);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outcome_id TEXT NOT NULL UNIQUE,
    intent_id TEXT,
    decision_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT,
    source TEXT DEFAULT 'ai',
    strategy_id TEXT,
    ticket_id TEXT,
    confidence REAL,
    entry_price REAL,
    exit_price REAL,
    qty REAL,
    pnl REAL,
    pnl_pct REAL,
    holding_minutes INTEGER,
    expected_holding_minutes INTEGER,
    invalidation TEXT,
    exit_reason TEXT,
    status TEXT NOT NULL DEFAULT 'CLOSED',
    opened_at TEXT,
    closed_at TEXT NOT NULL
);
"""


class AuditDatabase:
    def __init__(self, db_file: Path):
        self.db_file = Path(db_file)
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_file), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        # 跨进程访问（诊断脚本/DB浏览器）撞写锁时等待而不是立刻抛 database is locked
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._write_metrics = {"count": 0, "errors": 0, "total_ms": 0.0, "last_ms": None}
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _apply_migration(self, version: int, name: str, action: Any) -> None:
        exists = self.conn.execute(
            "SELECT version FROM schema_migrations WHERE version=?", (version,)
        ).fetchone()
        if exists:
            return
        try:
            action()
            self.conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                (version, name, utc_now()),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _migrate(self) -> None:
        """按版本为旧库补列；每个迁移只执行一次并可安全重启。"""
        migrations = {
            "trade_decisions": [
                ("channel", "TEXT DEFAULT 'ai'"),
            ],
            "trade_intents": [
                ("order_source", "TEXT DEFAULT 'ai'"),
                ("strategy_id", "TEXT"),
                ("ticket_id", "TEXT"),
                ("run_id", "TEXT"),
                ("action", "TEXT"),
                ("client_order_id", "TEXT"),
                ("invalidation", "TEXT"),
                ("blocked_reason", "TEXT"),
            ],
            "orders": [
                ("order_source", "TEXT DEFAULT 'ai'"),
                ("strategy_id", "TEXT"),
                ("ticket_id", "TEXT"),
                ("margin_mode", "TEXT DEFAULT 'ISOLATED'"),
            ],
            "positions": [
                ("source", "TEXT DEFAULT 'ai'"),
                ("strategy_id", "TEXT"),
                ("ticket_id", "TEXT"),
                ("margin_mode", "TEXT DEFAULT 'ISOLATED'"),
            ],
            "trade_outcomes": [
                ("strategy_id", "TEXT"),
                ("ticket_id", "TEXT"),
                ("confidence", "REAL"),
            ],
        }
        def apply_legacy_columns() -> None:
            for table, columns in migrations.items():
                existing = {
                    r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, declaration in columns:
                    if column not in existing:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        self._apply_migration(1, "legacy_channel_and_audit_columns", apply_legacy_columns)

        def apply_exchange_event_inbox() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS exchange_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    event_time_ms INTEGER,
                    transaction_time_ms INTEGER,
                    received_at TEXT NOT NULL,
                    processed_at TEXT,
                    source TEXT NOT NULL,
                    symbol TEXT,
                    client_order_id TEXT,
                    exchange_order_id TEXT,
                    trade_id TEXT,
                    status TEXT NOT NULL DEFAULT 'RECEIVED',
                    payload_json TEXT NOT NULL,
                    error TEXT
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exchange_events_status_time "
                "ON exchange_events(status, event_time_ms)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exchange_events_order "
                "ON exchange_events(client_order_id, exchange_order_id)"
            )

        self._apply_migration(2, "exchange_event_inbox", apply_exchange_event_inbox)

        def apply_account_mirror() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS account_balances (
                    asset TEXT PRIMARY KEY,
                    wallet_balance REAL NOT NULL DEFAULT 0,
                    cross_wallet_balance REAL NOT NULL DEFAULT 0,
                    available_balance REAL,
                    unrealized_pnl REAL NOT NULL DEFAULT 0,
                    balance_change REAL,
                    event_time_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS account_positions (
                    symbol TEXT NOT NULL,
                    position_side TEXT NOT NULL,
                    position_amt REAL NOT NULL DEFAULT 0,
                    entry_price REAL NOT NULL DEFAULT 0,
                    break_even_price REAL,
                    unrealized_pnl REAL NOT NULL DEFAULT 0,
                    isolated_wallet REAL NOT NULL DEFAULT 0,
                    margin_type TEXT,
                    leverage INTEGER,
                    event_time_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(symbol, position_side)
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS mirror_reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    matched INTEGER NOT NULL,
                    missing_mirror INTEGER NOT NULL DEFAULT 0,
                    missing_rest INTEGER NOT NULL DEFAULT 0,
                    qty_mismatch INTEGER NOT NULL DEFAULT 0,
                    wallet_delta REAL,
                    details_json TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mirror_reconciliations_time "
                "ON mirror_reconciliations(checked_at)"
            )

        self._apply_migration(3, "account_shadow_mirror", apply_account_mirror)

        def apply_order_mirror() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS exchange_order_mirror (
                    environment TEXT NOT NULL,
                    exchange_order_id TEXT NOT NULL,
                    client_order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    position_side TEXT,
                    order_type TEXT,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    execution_type TEXT,
                    original_qty REAL NOT NULL DEFAULT 0,
                    cumulative_qty REAL NOT NULL DEFAULT 0,
                    avg_price REAL NOT NULL DEFAULT 0,
                    last_fill_qty REAL NOT NULL DEFAULT 0,
                    last_fill_price REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    commission REAL NOT NULL DEFAULT 0,
                    commission_asset TEXT,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    close_position INTEGER NOT NULL DEFAULT 0,
                    trade_id TEXT,
                    event_time_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(environment, exchange_order_id)
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exchange_order_mirror_client "
                "ON exchange_order_mirror(environment, client_order_id)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_exchange_order_mirror_status "
                "ON exchange_order_mirror(environment, status, symbol)"
            )

        self._apply_migration(4, "order_shadow_mirror", apply_order_mirror)

        def apply_exchange_safety_monitor() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS account_config_mirror (
                    environment TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    symbol TEXT,
                    leverage INTEGER,
                    multi_assets_mode INTEGER,
                    event_time_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(environment, scope)
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS exchange_safety_events (
                    event_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    details TEXT NOT NULL,
                    reconciled INTEGER NOT NULL DEFAULT 0,
                    reconcile_error TEXT
                )"""
            )

        self._apply_migration(5, "exchange_safety_monitor", apply_exchange_safety_monitor)

        def apply_order_mirror_reconciliation() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS order_mirror_reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    matched INTEGER NOT NULL,
                    missing_local INTEGER NOT NULL DEFAULT 0,
                    missing_exchange INTEGER NOT NULL DEFAULT 0,
                    status_mismatch INTEGER NOT NULL DEFAULT 0,
                    qty_mismatch INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_mirror_reconcile_time "
                "ON order_mirror_reconciliations(checked_at)"
            )

        self._apply_migration(
            6, "order_mirror_rest_reconciliation", apply_order_mirror_reconciliation)

        def apply_order_outbox() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS order_outbox (
                    client_order_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    position_side TEXT,
                    order_type TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    endpoint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    exchange_order_id TEXT,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    error_type TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_outbox_status "
                "ON order_outbox(environment,status,updated_at)"
            )
            self.conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_order_outbox_pending_entry
                   ON order_outbox(environment,business_key)
                   WHERE purpose='ENTRY' AND status IN
                       ('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED')"""
            )

        self._apply_migration(7, "order_outbox", apply_order_outbox)

        def apply_protection_order_mirror() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS protection_order_mirror (
                    environment TEXT NOT NULL,
                    algo_id TEXT NOT NULL,
                    client_algo_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    position_side TEXT,
                    order_type TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    actual_qty REAL NOT NULL DEFAULT 0,
                    trigger_price REAL NOT NULL,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    close_position INTEGER NOT NULL DEFAULT 0,
                    event_time_ms INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(environment,algo_id)
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_protection_mirror_symbol "
                "ON protection_order_mirror(environment,status,symbol)"
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS protection_reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checked_at TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    matched INTEGER NOT NULL,
                    positions INTEGER NOT NULL,
                    open_algos INTEGER NOT NULL,
                    orphan_orders INTEGER NOT NULL DEFAULT 0,
                    missing_protection INTEGER NOT NULL DEFAULT 0,
                    direction_mismatch INTEGER NOT NULL DEFAULT 0,
                    qty_mismatch INTEGER NOT NULL DEFAULT 0,
                    unsafe_orders INTEGER NOT NULL DEFAULT 0,
                    details_json TEXT NOT NULL
                )"""
            )

        self._apply_migration(8, "protection_order_mirror", apply_protection_order_mirror)

        def apply_readonly_intel_sidecar() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS futures_features (
                    symbol TEXT PRIMARY KEY,
                    event_time TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    fresh_until TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    source TEXT NOT NULL,
                    feature_json TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS intel_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_hash TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    chain TEXT,
                    contract TEXT,
                    candidate_symbol TEXT,
                    observed_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    quality TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_intel_events_symbol_time "
                "ON intel_events(candidate_symbol, observed_at)"
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS shadow_decisions (
                    decision_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    reference_price REAL,
                    confidence REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    execution_weight REAL NOT NULL DEFAULT 0,
                    feature_json TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS shadow_outcomes (
                    decision_id TEXT NOT NULL,
                    horizon_minutes INTEGER NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    exit_price REAL NOT NULL,
                    return_pct REAL NOT NULL,
                    correct INTEGER,
                    PRIMARY KEY(decision_id, horizon_minutes),
                    FOREIGN KEY(decision_id) REFERENCES shadow_decisions(decision_id)
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_shadow_decisions_symbol_time "
                "ON shadow_decisions(symbol, created_at)"
            )

        self._apply_migration(9, "readonly_intel_and_shadow_evaluation", apply_readonly_intel_sidecar)

        def normalize_shadow_cohort_names() -> None:
            self.conn.execute(
                "UPDATE shadow_decisions SET source='B_contract_market' "
                "WHERE source='binance_top_trader_proxy'"
            )

        self._apply_migration(10, "normalize_shadow_cohort_names", normalize_shadow_cohort_names)

        def apply_shadow_cost_and_excursion_metrics() -> None:
            existing = {
                row["name"] for row in self.conn.execute(
                    "PRAGMA table_info(shadow_outcomes)").fetchall()
            }
            columns = {
                "gross_signal_return_pct": "REAL",
                "fee_cost_pct": "REAL",
                "slippage_cost_pct": "REAL",
                "funding_cost_pct": "REAL",
                "net_return_pct": "REAL",
                "mfe_pct": "REAL",
                "mae_pct": "REAL",
                "sample_count": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in columns.items():
                if name not in existing:
                    self.conn.execute(
                        f"ALTER TABLE shadow_outcomes ADD COLUMN {name} {declaration}")
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS mark_price_samples (
                    symbol TEXT NOT NULL,
                    sampled_at TEXT NOT NULL,
                    mark_price REAL NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY(symbol, sampled_at)
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mark_price_samples_time "
                "ON mark_price_samples(sampled_at, symbol)"
            )

        self._apply_migration(
            11, "shadow_cost_excursion_and_mark_samples",
            apply_shadow_cost_and_excursion_metrics)

        def normalize_shadow_excursion_bounds() -> None:
            # 入场点收益为0，故定义上 MFE 不得小于0、MAE不得大于0。
            self.conn.execute(
                "UPDATE shadow_outcomes SET mfe_pct=MAX(0,mfe_pct) WHERE mfe_pct IS NOT NULL")
            self.conn.execute(
                "UPDATE shadow_outcomes SET mae_pct=MIN(0,mae_pct) WHERE mae_pct IS NOT NULL")

        self._apply_migration(
            12, "normalize_shadow_excursion_bounds",
            normalize_shadow_excursion_bounds)

        def apply_risk_incident_lifecycle() -> None:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS risk_incidents (
                    incident_key TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    last_details TEXT,
                    last_action_taken TEXT,
                    acknowledged_at TEXT,
                    resolved_at TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_risk_incidents_status_severity "
                "ON risk_incidents(status, severity, last_seen_at)"
            )

        self._apply_migration(13, "risk_incident_lifecycle", apply_risk_incident_lifecycle)

    def close(self) -> None:
        self.conn.close()

    def upsert_futures_feature(self, *, feature: dict[str, Any]) -> None:
        self._insert(
            """INSERT INTO futures_features(symbol,event_time,received_at,fresh_until,quality,source,feature_json)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET event_time=excluded.event_time,
               received_at=excluded.received_at,fresh_until=excluded.fresh_until,
               quality=excluded.quality,source=excluded.source,feature_json=excluded.feature_json""",
            (feature["symbol"], feature["event_time"], feature["received_at"],
             feature["fresh_until"], feature["quality"], feature["source"],
             json.dumps(feature, ensure_ascii=False)),
        )

    def log_intel_event(self, *, event: dict[str, Any]) -> None:
        self._insert(
            """INSERT OR IGNORE INTO intel_events(raw_hash,source,kind,chain,contract,
               candidate_symbol,observed_at,received_at,expires_at,confidence,quality,payload_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event["raw_hash"], event["source"], event["kind"], event.get("chain"),
             event.get("contract"), event.get("candidate_symbol"), event["observed_at"],
             event["received_at"], event["expires_at"], event["confidence"],
             event["quality"], json.dumps(event.get("payload") or {}, ensure_ascii=False)),
        )

    def log_shadow_decision(self, *, decision_id: str, symbol: str, signal: str,
                            reference_price: float | None, confidence: float, source: str,
                            execution_weight: float, feature_json: str) -> None:
        self._insert(
            """INSERT INTO shadow_decisions(decision_id,created_at,symbol,signal,reference_price,
               confidence,source,execution_weight,feature_json) VALUES(?,?,?,?,?,?,?,?,?)""",
            (decision_id, utc_now(), symbol, signal, reference_price, confidence, source,
             execution_weight, feature_json),
        )

    def record_mark_price_samples(self, *, sampled_at: str, prices: dict[str, float],
                                  source: str = "binance_usdm_premium_index",
                                  retention_days: int = 90) -> int:
        rows = [(symbol, sampled_at, float(price), source)
                for symbol, price in prices.items() if float(price) > 0]
        with self.lock:
            self.conn.executemany(
                """INSERT OR REPLACE INTO mark_price_samples(symbol,sampled_at,mark_price,source)
                   VALUES(?,?,?,?)""", rows)
            self.conn.execute(
                "DELETE FROM mark_price_samples WHERE datetime(sampled_at)<datetime('now', ?)",
                (f"-{max(7, int(retention_days))} days",),
            )
            self.conn.commit()
        return len(rows)

    def evaluate_shadow_decisions(self, *, prices: dict[str, float],
                                  horizons: tuple[int, ...] = (5, 15, 60, 240),
                                  grace_minutes: int = 15,
                                  round_trip_fee_bps: float = 10.0,
                                  slippage_bps: float = 4.0) -> int:
        """以当前标记价回看到期影子信号；不读取或修改任何订单。"""
        now = datetime.now(timezone.utc)
        inserted = 0
        with self.lock:
            for horizon in horizons:
                cutoff = (now - timedelta(minutes=horizon)).isoformat(timespec="milliseconds")
                lower = (now - timedelta(minutes=horizon + max(1, grace_minutes))).isoformat(
                    timespec="milliseconds")
                rows = self.conn.execute(
                    """SELECT d.decision_id,d.symbol,d.signal,d.reference_price,
                              d.created_at,d.feature_json
                       FROM shadow_decisions d
                       LEFT JOIN shadow_outcomes o ON o.decision_id=d.decision_id AND o.horizon_minutes=?
                       WHERE o.decision_id IS NULL AND d.created_at<=? AND d.created_at>?
                         AND d.reference_price>0""",
                    (horizon, cutoff, lower),
                ).fetchall()
                for row in rows:
                    exit_price = prices.get(str(row["symbol"]))
                    if not exit_price or float(exit_price) <= 0:
                        continue
                    ret = (float(exit_price) / float(row["reference_price"]) - 1.0) * 100.0
                    signal = str(row["signal"])
                    correct = None if signal not in {"bullish", "bearish"} else int(
                        (signal == "bullish" and ret > 0) or (signal == "bearish" and ret < 0))
                    direction = 1.0 if signal == "bullish" else (-1.0 if signal == "bearish" else 0.0)
                    gross = ret * direction if direction else None
                    fee_cost = max(0.0, float(round_trip_fee_bps)) / 100.0
                    slip_cost = max(0.0, float(slippage_bps)) / 100.0
                    funding_rate = 0.0
                    try:
                        feature = json.loads(row["feature_json"] or "{}")
                        if isinstance(feature, dict) and isinstance(feature.get("contract_market"), dict):
                            feature = feature["contract_market"]
                        funding_rate = float(feature.get("funding_rate") or 0.0)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        funding_rate = 0.0
                    funding_cost = direction * funding_rate * 100.0 * (horizon / 480.0)
                    net = (gross - fee_cost - slip_cost - funding_cost
                           if gross is not None else None)
                    samples = self.conn.execute(
                        """SELECT mark_price FROM mark_price_samples
                           WHERE symbol=? AND datetime(sampled_at)>=datetime(?)
                             AND datetime(sampled_at)<=datetime(?) ORDER BY sampled_at""",
                        (row["symbol"], row["created_at"], now.isoformat(timespec="milliseconds")),
                    ).fetchall()
                    # 路径必须包含入场点，否则单边下跌时 MFE 会错误显示为负数，
                    # 单边上涨时 MAE 也会错误显示为正数。
                    path = [float(row["reference_price"])]
                    path.extend(float(item["mark_price"]) for item in samples)
                    path.append(float(exit_price))
                    path_returns = [
                        (price / float(row["reference_price"]) - 1.0) * 100.0 * direction
                        for price in path
                    ] if direction else []
                    mfe = max(path_returns) if path_returns else None
                    mae = min(path_returns) if path_returns else None
                    self.conn.execute(
                        """INSERT OR IGNORE INTO shadow_outcomes(decision_id,horizon_minutes,
                           evaluated_at,exit_price,return_pct,correct,gross_signal_return_pct,
                           fee_cost_pct,slippage_cost_pct,funding_cost_pct,net_return_pct,
                           mfe_pct,mae_pct,sample_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (row["decision_id"], horizon, utc_now(), float(exit_price), ret, correct,
                         gross, fee_cost, slip_cost, funding_cost, net, mfe, mae, len(path)),
                    )
                    inserted += 1
            self.conn.commit()
        return inserted

    def intel_observability(self) -> dict[str, Any]:
        """情报覆盖率与影子样本进度；供看板读取，不参与风控。"""
        with self.lock:
            event_count = int(self.conn.execute(
                "SELECT COUNT(*) FROM intel_events WHERE datetime(observed_at)>=datetime('now','-1 day')"
            ).fetchone()[0])
            feature_count = int(self.conn.execute("SELECT COUNT(*) FROM futures_features").fetchone()[0])
            latest = self.conn.execute("SELECT MAX(received_at) FROM intel_events").fetchone()[0]
            rows = self.conn.execute(
                """SELECT d.source,o.horizon_minutes,COUNT(*) samples,
                          SUM(CASE WHEN o.correct=1 THEN 1 ELSE 0 END) correct,
                          SUM(CASE WHEN o.correct IS NOT NULL THEN 1 ELSE 0 END) directional,
                          COUNT(o.net_return_pct) net_samples,
                          AVG(o.net_return_pct) avg_net,
                          MIN(o.net_return_pct) worst_net,
                          AVG(CASE WHEN o.net_return_pct IS NULL THEN NULL
                                   WHEN o.net_return_pct>0 THEN 1.0 ELSE 0.0 END) net_win_rate,
                          AVG(o.mfe_pct) avg_mfe,
                          AVG(o.mae_pct) avg_mae,
                          AVG(CASE WHEN o.net_return_pct IS NOT NULL THEN o.sample_count END) avg_path_samples,
                          MAX(0, julianday(MAX(d.created_at))-julianday(MIN(d.created_at))) span_days
                   FROM shadow_outcomes o JOIN shadow_decisions d ON d.decision_id=o.decision_id
                   GROUP BY d.source,o.horizon_minutes ORDER BY d.source,o.horizon_minutes"""
            ).fetchall()
        evaluations = []
        for row in rows:
            directional = int(row["directional"] or 0)
            correct = int(row["correct"] or 0)
            span_days = round(float(row["span_days"] or 0.0), 2)
            avg_net = float(row["avg_net"]) if row["avg_net"] is not None else None
            worst_net = float(row["worst_net"]) if row["worst_net"] is not None else None
            net_samples = int(row["net_samples"] or 0)
            avg_path_samples = (float(row["avg_path_samples"])
                                if row["avg_path_samples"] is not None else 0.0)
            required_path_samples = max(2, int(row["horizon_minutes"]) // 5)
            evaluations.append({
                "source": row["source"], "horizon_minutes": int(row["horizon_minutes"]),
                "samples": int(row["samples"]), "directional_samples": directional,
                "accuracy": round(correct / directional, 4) if directional else None,
                "observation_span_days": span_days,
                "net_samples": net_samples,
                "avg_net_return_pct": round(avg_net, 5) if avg_net is not None else None,
                "worst_net_return_pct": round(worst_net, 5) if worst_net is not None else None,
                "net_win_rate": (round(float(row["net_win_rate"]), 4)
                                 if row["net_win_rate"] is not None else None),
                "avg_mfe_pct": (round(float(row["avg_mfe"]), 5)
                                if row["avg_mfe"] is not None else None),
                "avg_mae_pct": (round(float(row["avg_mae"]), 5)
                                if row["avg_mae"] is not None else None),
                "avg_path_samples": round(avg_path_samples, 2),
                "required_path_samples": required_path_samples,
                "promotion_ready": (directional >= 200 and net_samples >= 200
                                    and span_days >= 7.0 and avg_net is not None
                                    and avg_net > 0 and avg_path_samples >= required_path_samples),
            })
        return {"events_24h": event_count, "feature_coverage": feature_count,
                "last_event_at": latest, "evaluations": evaluations,
                "minimum_promotion_samples": 200, "minimum_observation_days": 7,
                "auto_promotion": False}

    def _insert(self, sql: str, params: tuple[Any, ...]) -> None:
        started = time.perf_counter()
        with self.lock:
            try:
                self.conn.execute(sql, params)
                self.conn.commit()
            except Exception:
                self._write_metrics["errors"] += 1
                raise
            finally:
                elapsed = (time.perf_counter() - started) * 1000.0
                self._write_metrics["count"] += 1
                self._write_metrics["total_ms"] += elapsed
                self._write_metrics["last_ms"] = elapsed

    def write_metrics(self) -> dict[str, Any]:
        with self.lock:
            count = int(self._write_metrics["count"])
            return {
                "writes": count,
                "write_errors": int(self._write_metrics["errors"]),
                "last_write_ms": round(float(self._write_metrics["last_ms"] or 0.0), 3),
                "avg_write_ms": round(float(self._write_metrics["total_ms"]) / count, 3) if count else 0.0,
            }

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def register_agent(self, name: str, role: str | None = None, model: str | None = None) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO agents(name, role, model, created_at) VALUES(?,?,?,?)",
                (name, role, model, utc_now()),
            )
            self.conn.commit()

    def log_agent_run(self, *, run_id: str, agent_name: str, started_at: str, ended_at: str,
                      status: str, model: str | None = None, input_summary: str = "",
                      output_summary: str = "", prompt_tokens: int = 0,
                      completion_tokens: int = 0, cost_estimate: float = 0.0,
                      latency_ms: int | None = None, error: str | None = None) -> None:
        self._insert(
            """INSERT INTO agent_runs(run_id, agent_name, started_at, ended_at, status, model,
               input_summary, output_summary, prompt_tokens, completion_tokens, cost_estimate,
               latency_ms, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, agent_name, started_at, ended_at, status, model, input_summary,
             output_summary, prompt_tokens, completion_tokens, cost_estimate, latency_ms, error),
        )

    def log_llm_call(self, *, call_id: str, run_id: str | None, agent_name: str, model: str,
                     started_at: str, ended_at: str, prompt_tokens: int, completion_tokens: int,
                     cost_estimate: float, latency_ms: int, status: str,
                     error: str | None = None, prompt_file: str | None = None,
                     completion_file: str | None = None) -> None:
        self._insert(
            """INSERT INTO llm_calls(call_id, run_id, agent_name, model, started_at, ended_at,
               prompt_tokens, completion_tokens, cost_estimate, latency_ms, status, error,
               prompt_file, completion_file) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (call_id, run_id, agent_name, model, started_at, ended_at, prompt_tokens,
             completion_tokens, cost_estimate, latency_ms, status, error, prompt_file,
             completion_file),
        )

    def log_artifact(self, *, artifact_id: str, run_id: str | None, agent_name: str,
                     artifact_type: str, created_at: str, json_path: str,
                     sha256: str | None = None) -> None:
        self._insert(
            """INSERT INTO artifacts(artifact_id, run_id, agent_name, artifact_type, created_at,
               json_path, sha256) VALUES(?,?,?,?,?,?,?)""",
            (artifact_id, run_id, agent_name, artifact_type, created_at, json_path, sha256),
        )

    def log_trade_decision(self, *, decision_id: str, run_id: str | None, decision: str,
                           reasoning: str, raw_json: str, channel: str = "ai") -> None:
        self._insert(
            """INSERT INTO trade_decisions(decision_id, run_id, created_at, decision, channel,
               reasoning, raw_json) VALUES(?,?,?,?,?,?,?)""",
            (decision_id, run_id, utc_now(), decision, channel, reasoning, raw_json),
        )

    def log_trade_intent(self, *, intent_id: str, decision_id: str | None,                         run_id: str | None, intent: Any, status: str,
                         blocked_reason: str | None = None) -> None:
        """落库单条 intent。Outcome Tracker / invalidation 归因依赖本表，缺它复盘无地基。"""
        data = intent.model_dump() if hasattr(intent, "model_dump") else dict(intent or {})
        invalidation = data.get("invalidation")
        if not invalidation:
            belief = data.get("belief")
            if isinstance(belief, dict):
                invalidation = belief.get("invalidation")
        self._insert(
            """INSERT INTO trade_intents(intent_id, decision_id, run_id, created_at, symbol,
               action, client_order_id, side, qty, leverage, order_type, stop_loss, take_profit,
               expected_holding_minutes, confidence, order_source, strategy_id, ticket_id,
               invalidation, blocked_reason, status, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                intent_id, decision_id, run_id, utc_now(),
                data.get("symbol"), data.get("action"), data.get("client_order_id"),
                data.get("side"), data.get("qty"), data.get("leverage"),
                data.get("order_type"), data.get("stop_loss"), data.get("take_profit"),
                data.get("expected_holding_minutes"), data.get("confidence"),
                data.get("order_source") or "ai", data.get("strategy_id"),
                data.get("ticket_id"), invalidation, blocked_reason, status,
                json.dumps(data, ensure_ascii=False, default=str),
            ),
        )

    def fetch_latest_open_intent(self, symbol: str, side: str,
                                 source: str = "ai") -> sqlite3.Row | None:
        """取某 symbol+side+source 最近一条 approved 的开仓 intent —— 平仓归因用。"""
        return self.fetch_one(
            """SELECT intent_id, decision_id, invalidation, client_order_id,
                      created_at, raw_json
               FROM trade_intents
               WHERE symbol=? AND side=? AND order_source=? AND action='open'
                 AND status='approved'
               ORDER BY id DESC LIMIT 1""",
            (symbol, side, source),
        )

    def log_trade_outcome(self, *, outcome_id: str, intent_id: str | None,
                          decision_id: str | None, symbol: str, side: str | None,
                          source: str = "ai", strategy_id: str | None = None,
                          ticket_id: str | None = None, confidence: float | None = None,
                          entry_price: float | None = None,
                          exit_price: float | None = None, qty: float | None = None,
                          pnl: float | None = None, pnl_pct: float | None = None,
                          holding_minutes: int | None = None,
                          expected_holding_minutes: int | None = None,
                          invalidation: str | None = None, exit_reason: str | None = None,
                          status: str = "CLOSED", opened_at: str | None = None,
                          closed_at: str | None = None) -> None:
        """平仓结果落库：把最终盈亏连回当初的 thesis（intent + invalidation）。

        这是复盘的核心地基：没有它，复盘只能看"赚/亏"，
        无法知道"当初的 thesis 是不是被行情证伪"。
        """
        self._insert(
            """INSERT INTO trade_outcomes(outcome_id, intent_id, decision_id, symbol, side,
               source, strategy_id, ticket_id, confidence, entry_price, exit_price, qty, pnl,
               pnl_pct, holding_minutes, expected_holding_minutes, invalidation, exit_reason,
               status, opened_at, closed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (outcome_id, intent_id, decision_id, symbol, side, source,
             strategy_id, ticket_id, confidence,
             entry_price, exit_price, qty, pnl, pnl_pct, holding_minutes,
             expected_holding_minutes, invalidation, exit_reason, status,
             opened_at, closed_at or utc_now()),
        )

    def log_risk_verdict(self, *, verdict_id: str, decision_id: str | None, verdict: str,
                         reasons: str, raw_json: str) -> None:
        self._insert(
            """INSERT INTO risk_verdicts(verdict_id, decision_id, created_at, verdict, reasons,
               raw_json) VALUES(?,?,?,?,?,?)""",
            (verdict_id, decision_id, utc_now(), verdict, reasons, raw_json),
        )

    def log_risk_event(self, *, event_type: str, severity: str, details: str,
                       action_taken: str = "", raw: dict[str, Any] | None = None) -> None:
        now = utc_now()
        raw_data = raw if isinstance(raw, dict) else {}
        raw_json = json.dumps(raw_data, ensure_ascii=False)
        with self.lock:
            self.conn.execute(
                """INSERT INTO risk_events(event_id, created_at, event_type, severity, details,
                   action_taken, raw_json) VALUES(?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), now, event_type, severity, details, action_taken, raw_json),
            )
            # info 只是审计流水；只有需处置的风险才进入 incident 生命周期。
            if severity in {"warn", "error", "critical"}:
                scope = str(raw_data.get("symbol") or raw_data.get("environment") or "")[:80]
                incident_key = hashlib.sha256(
                    f"{event_type}|{scope}".encode("utf-8")).hexdigest()
                row = self.conn.execute(
                    "SELECT severity,status FROM risk_incidents WHERE incident_key=?", (incident_key,)
                ).fetchone()
                ranks = {"warn": 1, "error": 2, "critical": 3}
                if row is None:
                    self.conn.execute(
                        """INSERT INTO risk_incidents(incident_key,event_type,scope,severity,status,
                           first_seen_at,last_seen_at,occurrence_count,last_details,last_action_taken,raw_json)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (incident_key, event_type, scope, severity, "OPEN", now, now, 1,
                         details, action_taken, raw_json),
                    )
                else:
                    next_severity = (severity if ranks.get(severity, 0) >=
                                     ranks.get(str(row["severity"]), 0)
                                     else str(row["severity"]))
                    next_status = "OPEN" if str(row["status"]) == "RESOLVED" else str(row["status"])
                    self.conn.execute(
                        """UPDATE risk_incidents SET severity=?,status=?,last_seen_at=?,
                           occurrence_count=occurrence_count+1,last_details=?,last_action_taken=?,
                           resolved_at=CASE WHEN ?='OPEN' THEN NULL ELSE resolved_at END,raw_json=?
                           WHERE incident_key=?""",
                        (next_severity, next_status, now, details, action_taken, next_status,
                         raw_json, incident_key),
                    )
            self.conn.commit()

    def risk_incident_summary(self) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT status,severity,COUNT(*) n FROM risk_incidents GROUP BY status,severity"
            ).fetchall()
        summary = {"open": 0, "acknowledged": 0, "resolved": 0, "critical_open": 0}
        for row in rows:
            status = str(row["status"]).lower()
            count = int(row["n"])
            if status == "open":
                summary["open"] += count
                if row["severity"] == "critical":
                    summary["critical_open"] += count
            elif status == "acknowledged":
                summary["acknowledged"] += count
            elif status == "resolved":
                summary["resolved"] += count
        return summary

    def list_risk_incidents(self, *, status: str | None = None,
                            limit: int = 100) -> list[sqlite3.Row]:
        allowed = {"OPEN", "ACKNOWLEDGED", "RESOLVED"}
        with self.lock:
            if status and status.upper() in allowed:
                return self.conn.execute(
                    "SELECT * FROM risk_incidents WHERE status=? ORDER BY last_seen_at DESC LIMIT ?",
                    (status.upper(), max(1, min(int(limit), 500))),
                ).fetchall()
            return self.conn.execute(
                "SELECT * FROM risk_incidents ORDER BY last_seen_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()

    def set_risk_incident_status(self, *, incident_key: str, status: str) -> bool:
        status = status.upper()
        if status not in {"ACKNOWLEDGED", "RESOLVED"}:
            return False
        now = utc_now()
        with self.lock:
            cursor = self.conn.execute(
                """UPDATE risk_incidents SET status=?,
                   acknowledged_at=CASE WHEN ?='ACKNOWLEDGED' THEN ? ELSE acknowledged_at END,
                   resolved_at=CASE WHEN ?='RESOLVED' THEN ? ELSE resolved_at END
                   WHERE incident_key=?""",
                (status, status, now, status, now, incident_key),
            )
            self.conn.commit()
        return cursor.rowcount > 0

    def log_order(self, *, client_order_id: str, exchange_order_id: str | None,
                  symbol: str, side: str, order_type: str, qty: float, price: float | None,
                  status: str, raw: dict[str, Any] | None = None,
                  order_source: str = "ai", strategy_id: str | None = None,
                  ticket_id: str | None = None, margin_mode: str = "ISOLATED") -> None:
        self._insert(
            """INSERT INTO orders(client_order_id, exchange_order_id, symbol, side, order_type,
               qty, price, status, order_source, strategy_id, ticket_id, margin_mode,
               created_at, updated_at, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (client_order_id, exchange_order_id, symbol, side, order_type, qty, price,
             status, order_source, strategy_id, ticket_id, margin_mode,
             utc_now(), utc_now(), json.dumps(raw or {}, ensure_ascii=False)),
        )

    def update_signal_ticket(self, *, ticket_id: str, status: str) -> None:
        self._insert(
            "UPDATE signal_tickets SET status=?, updated_at=? WHERE ticket_id=?",
            (status, utc_now(), ticket_id),
        )

    def update_order(self, *, client_order_id: str, status: str,
                     price: float | None = None) -> None:
        self._insert(
            "UPDATE orders SET status=?, price=COALESCE(?, price), updated_at=? WHERE client_order_id=?",
            (status, price, utc_now(), client_order_id),
        )

    def cancel_order_by_exchange_id(self, *, exchange_order_id: str,
                                    status: str = "CANCELED") -> None:
        """按交易所订单号（algoId/orderId）把保护单标记为已撤/已失效。

        保护单走 Algo Order API，挂单时 exchange_order_id 存的是 algoId，
        撤单/触发时只有 algoId 可用来定位本地流水。此方法让撤单动作在流水表
        里留下真实生命周期（NEW -> CANCELED），而不是留下一条永远 NEW 的幽灵单。
        """
        self._insert(
            "UPDATE orders SET status=?, updated_at=? WHERE exchange_order_id=? AND status='NEW'",
            (status, utc_now(), exchange_order_id),
        )

    @staticmethod
    def _clean_price(value: float | None) -> float | None:
        """去掉浮点尾巴（102.10000000000001 -> 102.1），保留 8 位有效数字不损失真实精度。"""
        if value is None:
            return None
        try:
            f = float(value)
            if f == 0:
                return 0.0
            return float(f"{f:.8g}")
        except (TypeError, ValueError):
            return value

    def log_position(self, *, symbol: str, side: str, qty: float,
                     entry_price: float | None, leverage: int, stop_loss: float | None,
                     take_profit: float | None, status: str = "OPEN",
                     source: str = "ai", strategy_id: str | None = None,
                     ticket_id: str | None = None, margin_mode: str = "ISOLATED") -> None:
        self._insert(
            """INSERT INTO positions(symbol, side, qty, entry_price, leverage,
               stop_loss, take_profit, source, strategy_id, ticket_id, margin_mode,
               status, opened_at, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, side, float(f"{float(qty):.8g}") if qty is not None else qty,
             self._clean_price(entry_price), leverage,
             self._clean_price(stop_loss), self._clean_price(take_profit),
             source, strategy_id, ticket_id, margin_mode,
             status, utc_now(), json.dumps({"symbol": symbol, "side": side, "qty": qty}, ensure_ascii=False)),
        )

    def close_open_position(self, *, symbol: str, side: str,
                            realized_pnl: float | None = None,
                            note: str = "") -> int:
        """精确收口单个 (symbol, side) 的所有 OPEN 流水行为 CLOSED。

        与 close_open_positions（全局）不同，本方法只作用于指定交易对的指定方向，
        供 sync_positions 在仓位变化时收口旧快照，避免误关其他仓位。

        同样只改状态、不删行，审计链完整。
        """
        now = utc_now()
        rows = self.fetch_all(
            "SELECT id, raw_json FROM positions WHERE symbol=? AND side=? AND status='OPEN'",
            (symbol, side))
        if not rows:
            return 0
        with self.lock:
            for r in rows:
                pid = int(r["id"])
                raw = r["raw_json"]
                if note:
                    try:
                        data = json.loads(raw) if raw else {}
                    except (TypeError, ValueError):
                        data = {}
                    if not isinstance(data, dict):
                        data = {"_prev": data}
                    data["_close_note"] = note
                    raw = json.dumps(data, ensure_ascii=False)
                if realized_pnl is None:
                    self.conn.execute(
                        "UPDATE positions SET status='CLOSED', closed_at=?, raw_json=? WHERE id=?",
                        (now, raw, pid))
                else:
                    self.conn.execute(
                        "UPDATE positions SET status='CLOSED', closed_at=?, realized_pnl=?, "
                        "raw_json=? WHERE id=?",
                        (now, float(realized_pnl), raw, pid))
            self.conn.commit()
        return len(rows)

    def close_open_positions(self, *, exclude_ids: Iterable[int] = (),
                             realized_pnl: float | None = None,
                             note: str = "") -> int:
        """批量把 positions 表里仍为 OPEN 的行置为 CLOSED（保留行本身，不删数据）。

        为什么需要这个方法：
        positions 是一张**流水表**而不是状态表——仓位每次变化就插一行，
        但 log_position 只有 INSERT，全工程没有任何地方能把一行收口成 CLOSED。
        正常路径下无所谓（去重判据命中就不插新行，旧行一直留着当历史），
        可一旦去重判据失效（见 sync_positions 的 GROUP BY 缺陷），
        这些 OPEN 行就再也没人关掉，永久滞留成幽灵台账。
        这个方法是清理历史数据的补救入口：**只改状态、不删行**，审计链完整。

        :param exclude_ids: 这些 id 保持 OPEN（通常是对账后确认在交易所还活着的仓位）
        :param realized_pnl: 写入 realized_pnl；None 表示保持原值不动
        :param note: 写进 raw_json 的 _close_note，记录这一行为什么被关掉
        :return: 实际被关闭的行数
        """
        keep = {int(i) for i in exclude_ids}
        now = utc_now()
        rows = self.fetch_all("SELECT id, raw_json FROM positions WHERE status = 'OPEN'")
        targets = [int(r["id"]) for r in rows if int(r["id"]) not in keep]
        if not targets:
            return 0
        with self.lock:
            for r in rows:
                pid = int(r["id"])
                if pid in keep:
                    continue
                raw = r["raw_json"]
                if note:
                    try:
                        data = json.loads(raw) if raw else {}
                    except (TypeError, ValueError):
                        data = {}
                    if not isinstance(data, dict):
                        data = {"_prev": data}
                    data["_close_note"] = note
                    raw = json.dumps(data, ensure_ascii=False)
                if realized_pnl is None:
                    self.conn.execute(
                        "UPDATE positions SET status='CLOSED', closed_at=?, raw_json=? WHERE id=?",
                        (now, raw, pid))
                else:
                    self.conn.execute(
                        "UPDATE positions SET status='CLOSED', closed_at=?, realized_pnl=?, "
                        "raw_json=? WHERE id=?",
                        (now, float(realized_pnl), raw, pid))
            self.conn.commit()
        return len(targets)

    def log_signal_event(self, *, signal_id: str, strategy_id: str, symbol: str,
                         timeframe: str, side: str, signal_time: str,
                         signal_price: float | None, bar_open_time: int = 0,
                         params: dict[str, Any] | None = None,
                         status: str = "triggered",
                         raw: dict[str, Any] | None = None) -> None:
        self._insert(
            """INSERT INTO signal_events(signal_id, strategy_id, symbol, timeframe, side,
               signal_time, signal_price, bar_open_time, params_json, status, created_at, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, strategy_id, symbol, timeframe, side, signal_time,
             signal_price, bar_open_time, json.dumps(params or {}, ensure_ascii=False),
             status, utc_now(), json.dumps(raw or {}, ensure_ascii=False)),
        )

    def log_signal_ticket(self, *, ticket_id: str, signal_id: str, strategy_id: str,
                          symbol: str, side: str, signal_time: str, signal_price: float,
                          entry_zone_low: float, entry_zone_high: float, expires_at: str,
                          stop_loss_pct: float, take_profit_pct: float,
                          time_stop_hours: int = 48, status: str = "open",
                          raw: dict[str, Any] | None = None) -> None:
        self._insert(
            """INSERT INTO signal_tickets(ticket_id, signal_id, strategy_id, symbol, side,
               signal_time, signal_price, entry_zone_low, entry_zone_high, expires_at,
               stop_loss_pct, take_profit_pct, time_stop_hours, status, created_at, updated_at,
               raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticket_id, signal_id, strategy_id, symbol, side, signal_time, signal_price,
             entry_zone_low, entry_zone_high, expires_at, stop_loss_pct, take_profit_pct,
             time_stop_hours, status, utc_now(), utc_now(),
             json.dumps(raw or {}, ensure_ascii=False)),
        )

    def log_decision_conflict(self, *, run_id: str | None, decision_id: str | None,
                               symbol: str, source: str, reason: str) -> None:
        self._insert(
            """INSERT INTO decision_conflicts(run_id, decision_id, symbol, source, reason, created_at)
               VALUES(?,?,?,?,?,?)""",
            (run_id, decision_id, symbol, source, reason, utc_now()),
        )

    def log_channel_daily_stats(self, *, stat_date: str, channel: str,
                                start_equity: float, end_equity: float,
                                realized_pnl: float = 0.0, fees: float = 0.0,
                                closed_trades: int = 0, wins: int = 0, losses: int = 0,
                                win_rate: float = 0.0, max_drawdown: float = 0.0,
                                rebalanced_from: float | None = None,
                                rebalanced_to: float | None = None,
                                status: str = "active",
                                raw: dict[str, Any] | None = None) -> None:
        self._insert(
            """INSERT INTO channel_daily_stats(stat_date, channel, start_equity, end_equity,
               realized_pnl, fees, closed_trades, wins, losses, win_rate, max_drawdown,
               rebalanced_from, rebalanced_to, status, raw_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (stat_date, channel, start_equity, end_equity, realized_pnl, fees,
             closed_trades, wins, losses, win_rate, max_drawdown,
             rebalanced_from, rebalanced_to, status,
             json.dumps(raw or {}, ensure_ascii=False)),
        )

    def log_daily_rebalance(self, *, rebalance_date: str, channel: str,
                            from_equity: float, to_equity: float,
                            transfer_amount: float, reason: str = "") -> None:
        self._insert(
            """INSERT INTO daily_rebalances(rebalance_date, channel, from_equity, to_equity,
               transfer_amount, reason, created_at) VALUES(?,?,?,?,?,?,?)""",
            (rebalance_date, channel, from_equity, to_equity, transfer_amount,
             reason, utc_now()),
        )

    def log_portfolio_snapshot(self, *, equity: float, margin_balance: float,
                               available_balance: float, unrealized_pnl: float,
                               margin_used: float, raw: dict[str, Any] | None = None) -> None:
        self._insert(
            """INSERT INTO portfolio_snapshots(created_at, equity, margin_balance, available_balance,
               unrealized_pnl, margin_used, raw_json) VALUES(?,?,?,?,?,?,?)""",
            (utc_now(), equity, margin_balance, available_balance, unrealized_pnl, margin_used,
             json.dumps(raw or {}, ensure_ascii=False)),
        )

    def table_counts(self) -> dict[str, int]:
        tables = ["agents", "agent_runs", "llm_calls", "artifacts",
                  "trade_decisions", "trade_intents", "risk_verdicts",
                  "orders", "positions", "portfolio_snapshots", "risk_events",
                  "signal_events", "signal_tickets", "channel_daily_stats",
                  "daily_rebalances"]
        with self.lock:
            return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in tables}
