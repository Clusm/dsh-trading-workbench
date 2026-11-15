"""M1.2 账本 schema（新库 ``data/workbench-v1.sqlite``，与旧库零共享）。

与 t9 的 ``db.py`` 关系：
- ``db.py`` 负责 ``schema_migrations`` / ``meta`` / ``service_boots``（M1.0 迁移 1）；
- 本模块幂等创建执行/风控/账本表，并为 v2 增补手续费币种列；
  不写 ``schema_migrations``，因此不会改变 ``/health`` 的 ``schema_version`` 语义。
- ``meta`` 表若尚未创建（测试里先建服务实例再 init_db），这里用同形状 DDL 兜底，
  之后 ``db.init_db`` 的 ``CREATE TABLE IF NOT EXISTS`` 不会冲突。

表清单（docs/03 §10）：orders / order_outbox / fills / positions / position_events /
equity_snapshots / risk_state / risk_events / cycle_runs / control_state，外加
``approvals``（P1 一次性 approval_token 的持久化）。
"""

from __future__ import annotations

import sqlite3

# v3（§8·C）：资金流水（资金费/划转）独立成表、回填游标按标的保存、
# 风控水位带来源/日期/时区。升级只做增量列与幂等建表，不改既有行。
LEDGER_SCHEMA_VERSION = 3

_SQL = """
CREATE TABLE IF NOT EXISTS operation_history (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    target TEXT,
    reason TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT,
    summary_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_at ON operation_history(started_at DESC);
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,
    exchange_order_id TEXT,
    kind              TEXT NOT NULL DEFAULT 'STANDARD',
    purpose           TEXT NOT NULL DEFAULT 'ENTRY',
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    position_side     TEXT,
    order_type        TEXT NOT NULL,
    qty_requested     TEXT NOT NULL,
    qty_filled        TEXT NOT NULL DEFAULT '0',
    avg_price         TEXT,
    trigger_price     TEXT,
    reduce_only       INTEGER NOT NULL DEFAULT 0,
    close_position    INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    cycle_id          TEXT,
    binance_code      INTEGER,
    error_msg         TEXT,
    raw_json          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    resolved_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_time ON orders(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS order_outbox (
    client_order_id  TEXT PRIMARY KEY,
    environment      TEXT NOT NULL DEFAULT 'demo',
    business_key     TEXT NOT NULL,
    endpoint         TEXT NOT NULL,
    request_json     TEXT NOT NULL,
    status           TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    not_found_queries INTEGER NOT NULL DEFAULT 0,
    unknown_since    TEXT,
    next_retry_at    TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    resolved_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_active_entry
    ON order_outbox(environment, business_key)
    WHERE business_key LIKE 'ENTRY:%'
      AND status IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED');
CREATE INDEX IF NOT EXISTS idx_outbox_status ON order_outbox(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS fills (
    fill_id         TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    price           TEXT NOT NULL,
    qty             TEXT NOT NULL,
    realized_pnl    TEXT,
    commission      TEXT,
    trade_time_ms   INTEGER NOT NULL,
    source          TEXT NOT NULL DEFAULT 'userTrades'
);
CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(client_order_id);
CREATE INDEX IF NOT EXISTS idx_fills_time ON fills(trade_time_ms DESC);

CREATE TABLE IF NOT EXISTS positions (
    ledger_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    position_side     TEXT NOT NULL,
    qty               TEXT NOT NULL,
    entry_price       TEXT NOT NULL,
    mark_price        TEXT,
    upnl              TEXT,
    leverage          INTEGER,
    margin            TEXT,
    margin_mode       TEXT,
    liq_price         TEXT,
    sl_price          TEXT,
    tp_price          TEXT,
    protection_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    origin            TEXT NOT NULL DEFAULT 'OPENED_BY_WORKBENCH',
    opened_at         TEXT,
    deadline_at       TEXT,
    time_stop_source  TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (ledger_id, symbol, position_side)
);

CREATE TABLE IF NOT EXISTS position_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    position_side TEXT NOT NULL,
    kind          TEXT NOT NULL,
    before_json   TEXT,
    after_json    TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_position_events_at ON position_events(at DESC);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ledger_id        TEXT NOT NULL,
    ts_bucket        TEXT NOT NULL,
    equity_exchange  TEXT NOT NULL,
    equity_session   TEXT NOT NULL,
    wallet_balance   TEXT,
    available        TEXT,
    upnl             TEXT,
    margin_used      TEXT,
    day_start_equity TEXT,
    peak_equity      TEXT,
    daily_loss_ratio TEXT,
    drawdown_ratio   TEXT,
    source           TEXT NOT NULL DEFAULT 'loop',
    PRIMARY KEY (ledger_id, ts_bucket)
);
CREATE INDEX IF NOT EXISTS idx_equity_bucket ON equity_snapshots(ts_bucket DESC);

-- §8·C2: 资金费与划转必须与“策略盈亏”分开。交易所 income 流水按 ref_id 幂等落库；
-- amount 为带符号的账户变动（收入为正），划转单列为资金流，不计入策略盈利。
CREATE TABLE IF NOT EXISTS capital_flows (
    ref_id      TEXT PRIMARY KEY,
    at_ms       INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    asset       TEXT NOT NULL,
    amount      TEXT NOT NULL,
    income_type TEXT,
    symbol      TEXT,
    source      TEXT NOT NULL DEFAULT 'income',
    raw_json    TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_capital_flows_at ON capital_flows(at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_capital_flows_kind ON capital_flows(kind, at_ms DESC);

-- §8·C5: 历史回填按标的保存游标与覆盖区间；界面据此说明“统计覆盖自某时刻”，
-- 不允许以“近期若干页”冒充完整账户历史。
CREATE TABLE IF NOT EXISTS backfill_state (
    ledger_id       TEXT NOT NULL,
    stream          TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    cursor_ms       INTEGER,
    covered_from_ms INTEGER,
    covered_to_ms   INTEGER,
    complete        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (ledger_id, stream, symbol)
);

CREATE TABLE IF NOT EXISTS risk_state (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    anchor_id             TEXT NOT NULL,
    date                  TEXT NOT NULL,
    day_start_equity      TEXT,
    peak_equity           TEXT,
    daily_loss_latched    INTEGER NOT NULL DEFAULT 0,
    daily_loss_latched_at TEXT,
    drawdown_latched      INTEGER NOT NULL DEFAULT 0,
    drawdown_latched_at   TEXT,
    consecutive_losses    INTEGER NOT NULL DEFAULT 0,
    cooldown_until        TEXT,
    recovery_required     INTEGER NOT NULL DEFAULT 0,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    rule_id     TEXT,
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL,
    action      TEXT,
    details_json TEXT,
    trace_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_risk_events_at ON risk_events(at DESC);

CREATE TABLE IF NOT EXISTS cycle_runs (
    cycle_id      TEXT PRIMARY KEY,
    slot_id       TEXT,
    state         TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    outcome       TEXT,
    error_code    TEXT,
    decision_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cycle_slot ON cycle_runs(slot_id) WHERE slot_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS control_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    state      TEXT NOT NULL,
    reason     TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    jti           TEXT PRIMARY KEY,
    plan_hash     TEXT NOT NULL,
    cycle_id      TEXT,
    approved_qty  TEXT NOT NULL,
    leverage      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    consumed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, expires_at_ms);
"""


def schema_report(conn: sqlite3.Connection) -> dict[str, object]:
    """§8·C6（G5）：把“声明版本 / 实际迁移版本 / 代码期望版本”三者摊开。

    这三者语义不同，不能互相顶替：
    - meta.schema_version：db.py 的迁移版本（M1.0 起为 1）；
    - meta.ledger_schema_version：本模块的账本表版本（v3）；
    - LEDGER_SCHEMA_VERSION：当前代码期望的账本表版本。
    只要实际建出的列/表落后于代码期望，就必须报 consistent=false 并给出差异，
    不得以旧版本号冒充新结构（否则回滚前的 schema 兼容判断会失效）。
    """

    def _meta(key: str) -> str | None:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return None if row is None else str(row[0])

    declared = _meta("schema_version")
    recorded = _meta("ledger_schema_version")
    applied = dict(
        (name, {row[1] for row in conn.execute(f"PRAGMA table_info({name})")})
        for name in ("capital_flows", "backfill_state", "risk_state", "order_outbox", "positions")
    )
    missing: list[str] = []
    if not applied["capital_flows"]:
        missing.append("capital_flows")
    if not applied["backfill_state"]:
        missing.append("backfill_state")
    if "day_start_equity_source" not in applied["risk_state"]:
        missing.append("risk_state.day_start_equity_source")
    if "not_found_queries" not in applied["order_outbox"]:
        missing.append("order_outbox.not_found_queries")
    if "time_stop_source" not in applied["positions"]:
        missing.append("positions.time_stop_source")
    try:
        recorded_int = int(recorded) if recorded is not None else None
    except (TypeError, ValueError):
        recorded_int = None
    consistent = recorded_int == LEDGER_SCHEMA_VERSION and not missing
    reasons: list[str] = []
    if recorded_int != LEDGER_SCHEMA_VERSION:
        reasons.append(
            f"ledger_schema_version={recorded} 与代码期望 {LEDGER_SCHEMA_VERSION} 不一致"
        )
    if missing:
        reasons.append("缺少实际结构: " + ", ".join(sorted(missing)))
    return {
        "declared_schema_version": declared,
        "recorded_ledger_schema_version": recorded,
        "expected_ledger_schema_version": LEDGER_SCHEMA_VERSION,
        "missing_structures": sorted(missing),
        "consistent": consistent,
        "reason": "；".join(reasons) if reasons else None,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表 + 记录账本 schema 版本（不写 schema_migrations）。"""

    conn.executescript(_SQL)
    if "commission_asset" not in {row[1] for row in conn.execute("PRAGMA table_info(fills)")}:
        conn.execute("ALTER TABLE fills ADD COLUMN commission_asset TEXT")
    # T03 (§8·B6): 系统管理仓位的 time_stop 期限与来源计划版本必须随仓位持久化，
    # 重启后按原期限判断，而不是每次用当前配置重算。
    position_columns = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    for column, ddl in (
        ("deadline_at", "ALTER TABLE positions ADD COLUMN deadline_at TEXT"),
        ("time_stop_source", "ALTER TABLE positions ADD COLUMN time_stop_source TEXT"),
    ):
        if column not in position_columns:
            conn.execute(ddl)
    # T03: 独立记录“交易所明确回答无此单”的查询次数；只用于决定还需要几次
    # 独立事实才收敛，不与提交尝试次数混用（重试次数本身不构成终态依据）。
    outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(order_outbox)")}
    if "not_found_queries" not in outbox_columns:
        conn.execute(
            "ALTER TABLE order_outbox ADD COLUMN not_found_queries INTEGER NOT NULL DEFAULT 0"
        )
    # §8·C4: 水位必须能回答“这个数字是哪来的、哪一天的、按哪个时区算的”。
    # 缺列时增量补齐；旧行保持 NULL 而不是伪造来源。
    risk_columns = {row[1] for row in conn.execute("PRAGMA table_info(risk_state)")}
    for column, ddl in (
        ("day_start_equity_source", "ALTER TABLE risk_state ADD COLUMN day_start_equity_source TEXT"),
        ("day_start_equity_at", "ALTER TABLE risk_state ADD COLUMN day_start_equity_at TEXT"),
        ("day_start_equity_tz", "ALTER TABLE risk_state ADD COLUMN day_start_equity_tz TEXT"),
        ("peak_equity_source", "ALTER TABLE risk_state ADD COLUMN peak_equity_source TEXT"),
        ("peak_equity_at", "ALTER TABLE risk_state ADD COLUMN peak_equity_at TEXT"),
    ):
        if column not in risk_columns:
            conn.execute(ddl)
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES ('ledger_schema_version', ?, "
        "strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (str(LEDGER_SCHEMA_VERSION),),
    )
    conn.commit()
