"""审计数据库：阶段 0 先建核心表并记录 agent_runs / llm_calls / artifacts / 决策链路。"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
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

    def close(self) -> None:
        self.conn.close()

    def _insert(self, sql: str, params: tuple[Any, ...]) -> None:
        with self.lock:
            self.conn.execute(sql, params)
            self.conn.commit()

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
        self._insert(
            """INSERT INTO risk_events(event_id, created_at, event_type, severity, details,
               action_taken, raw_json) VALUES(?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), utc_now(), event_type, severity, details, action_taken,
             json.dumps(raw or {}, ensure_ascii=False)),
        )

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
