"""账本存储层：单进程内线程安全的 SQLite 封装。

原则（docs/03 §4.5 / §6.4）：
- 交易所是真值，本库是镜像 + 审计；
- 所有金额/数量以十进制文本落库；
- 写操作幂等（订单主键 = client_order_id，成交主键 = 交易所 tradeId）；
- 连接使用 WAL + busy_timeout，``db.py`` 的只读探测与之共存。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .numbers import dec_text, to_decimal
from .schema import LEDGER_SCHEMA_VERSION, ensure_schema

__all__ = [
    "Ledger",
    "DuplicateOrderError",
    "PendingOrderConflict",
    "utc_now",
    "utc_now_ms",
    "utc_day",
    "bucket_iso",
]

_DB_FILENAME = "workbench-v1.sqlite"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def bucket_iso(seconds: int, *, now: datetime | None = None) -> str:
    """把当前时刻向下对齐到 ``seconds`` 桶，返回 ISO 串（用于权益快照去重）。"""

    seconds = max(1, int(seconds))
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    aligned = epoch - (epoch % seconds)
    return datetime.fromtimestamp(aligned, tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _ms_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return (
        datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _coverage_block(state: dict[str, Any]) -> dict[str, Any]:
    """把一条 backfill_state 行翻译成“覆盖了什么、什么时候更新的”。"""

    if not state:
        return {
            "covered_from": None,
            "covered_to": None,
            "complete": False,
            "updated_at": None,
            "last_error": None,
            "note": "尚未回填",
        }
    complete = bool(state.get("complete"))
    return {
        "covered_from": _ms_iso(state.get("covered_from_ms")),
        "covered_to": _ms_iso(state.get("covered_to_ms")),
        "complete": complete,
        "updated_at": state.get("updated_at"),
        "last_error": state.get("last_error"),
        "note": None if complete else "回填未完成",
    }


def _iso_ago(seconds: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=max(0.0, seconds))
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


class DuplicateOrderError(RuntimeError):
    """同一 client_order_id 已存在（幂等拒绝）。"""


class PendingOrderConflict(RuntimeError):
    """同一业务键已有在途开仓单。"""


class Ledger:
    """执行/风控账本；构造即打开连接，首次使用时建表。"""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / _DB_FILENAME
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ 基础
    def connection(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self.path), timeout=10.0, check_same_thread=False
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                ensure_schema(conn)
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection().execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection().execute(sql, tuple(params)).fetchone()
            return None if row is None else dict(row)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            conn = self.connection()
            cursor = conn.execute(sql, tuple(params))
            conn.commit()
            return int(cursor.rowcount)

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str) -> str | None:
        try:
            row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        except sqlite3.Error:
            return None
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, utc_now()),
        )

    def insert_meta_if_absent(self, key: str, value: str) -> bool:
        """Cross-process primitive: succeed only if the key did not exist."""
        return self.execute(
            "INSERT OR IGNORE INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, utc_now()),
        ) == 1

    def compare_and_set_meta(self, key: str, expected: str, value: str) -> bool:
        """Cross-process primitive: replace only when the exact old value matches."""
        return self.execute(
            "UPDATE meta SET value=?, updated_at=? WHERE key=? AND value=?",
            (value, utc_now(), key, expected),
        ) == 1

    def bump_meta_int(self, key: str, default: int = 0) -> int:
        """Atomically increment an integer meta key and return the new value."""
        with self._lock:
            conn = self.connection()
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            current = 0
            if row is not None:
                try:
                    current = int(row["value"])
                except (TypeError, ValueError):
                    current = default
            value = current + 1
            conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), utc_now()),
            )
            conn.commit()
            return value

    @property
    def ledger_id(self) -> str:
        """稳定账本 ID：优先复用 ``db.init_db`` 写入的值，否则创建。"""

        existing = self.get_meta("ledger_id")
        if existing:
            return existing
        created = uuid.uuid4().hex
        self.set_meta("ledger_id", created)
        self.set_meta("created_at", utc_now())
        return created

    def bind_venue(self, venue: str) -> dict[str, Any]:
        """§4.2：把账本与一个环境身份绑定；身份不符则拒绝（不自动改标签）。

        每库只有一个环境。第一次绑定写入 meta；之后每次打开都要核对——
        错目录、错身份必须停下，而不是把 live 数据写进 demo 账本（或反之）。
        """

        venue = str(venue or "").strip().lower()
        if not venue:
            raise ValueError("venue is required")
        existing = self.get_meta("venue")
        if existing is None:
            self.set_meta("venue", venue)
            return {"venue": venue, "bound": True, "changed": True}
        if str(existing) != venue:
            raise ValueError(
                f"ledger venue mismatch: this database is bound to {existing!r}, "
                f"but this process is {venue!r}; refusing to write"
            )
        return {"venue": venue, "bound": True, "changed": False}

    def bind_account(self, venue: str, fingerprint: str, *, source: str) -> dict[str, Any]:
        """§4.2：把账本绑定到一组凭据（脱敏指纹），防止换账户复用同一账本。

        诚实边界：Key 指纹只能证明“用的是同一把 key”，**不能证明是同一个交易账户**
        （不同 key 可以是同一账户，同一 key 也可能被重新签发）。所以这里记
        `account_identity_verified=false`，不去编造“已验证”。
        """

        venue = str(venue or "").strip().lower()
        fingerprint = str(fingerprint or "").strip()
        if not venue or not fingerprint:
            raise ValueError("venue and fingerprint are required")
        existing = self.get_meta("account_fingerprint")
        record = {
            "venue": venue,
            "fingerprint": fingerprint,
            "source": source,
            "verified": False,
            "verified_note": "key fingerprint identifies the credential, not the account",
        }
        if existing is None:
            self.set_meta("account_fingerprint", fingerprint)
            self.set_meta("account_fingerprint_source", source)
            self.set_meta("account_identity_verified", "false")
            return {**record, "changed": True}
        if str(existing) != fingerprint:
            raise ValueError(
                "account credential mismatch: this ledger is bound to a different "
                f"key fingerprint ({str(existing)[:8]}…), refusing to write with {fingerprint[:8]}…"
            )
        return {**record, "changed": False}

    def schema_version(self) -> int:
        raw = self.get_meta("ledger_schema_version")
        try:
            return int(raw) if raw is not None else LEDGER_SCHEMA_VERSION
        except ValueError:
            return 0

    # ------------------------------------------------------------------ orders
    _ORDER_COLUMNS: tuple[str, ...] = (
        "client_order_id",
        "exchange_order_id",
        "kind",
        "purpose",
        "symbol",
        "side",
        "position_side",
        "order_type",
        "qty_requested",
        "qty_filled",
        "avg_price",
        "trigger_price",
        "reduce_only",
        "close_position",
        "status",
        "cycle_id",
        "binance_code",
        "error_msg",
        "raw_json",
        "created_at",
        "updated_at",
        "resolved_at",
    )

    def insert_order(self, record: dict[str, Any]) -> None:
        """插入订单审计镜像；主键重复抛 :class:`DuplicateOrderError`。"""

        now = utc_now()
        values: dict[str, Any] = {
            "exchange_order_id": None,
            "kind": "STANDARD",
            "purpose": "ENTRY",
            "position_side": None,
            "qty_filled": "0",
            "avg_price": None,
            "trigger_price": None,
            "reduce_only": 0,
            "close_position": 0,
            "cycle_id": None,
            "binance_code": None,
            "error_msg": None,
            "raw_json": None,
            "resolved_at": None,
        }
        values.update(record)
        values.setdefault("created_at", now)
        values.setdefault("updated_at", now)
        values["qty_requested"] = dec_text(values["qty_requested"])
        values["qty_filled"] = dec_text(values.get("qty_filled", "0"))
        if values.get("raw_json") is not None and not isinstance(values["raw_json"], str):
            values["raw_json"] = json.dumps(values["raw_json"], ensure_ascii=False, default=str)
        columns = ", ".join(self._ORDER_COLUMNS)
        placeholders = ", ".join("?" for _ in self._ORDER_COLUMNS)
        try:
            self.execute(
                f"INSERT INTO orders ({columns}) VALUES ({placeholders})",
                tuple(values.get(column) for column in self._ORDER_COLUMNS),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateOrderError(str(exc)) from exc

    def update_order(self, client_order_id: str, **fields: Any) -> None:
        allowed = {
            "exchange_order_id",
            "status",
            "qty_filled",
            "avg_price",
            "trigger_price",
            "binance_code",
            "error_msg",
            "raw_json",
            "resolved_at",
            "position_side",
            "cycle_id",
            "trigger_price",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        if "raw_json" in updates and updates["raw_json"] is not None and not isinstance(
            updates["raw_json"], str
        ):
            updates["raw_json"] = json.dumps(updates["raw_json"], ensure_ascii=False, default=str)
        if "qty_filled" in updates:
            updates["qty_filled"] = dec_text(updates["qty_filled"])
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(
            f"UPDATE orders SET {assignments}, updated_at=? WHERE client_order_id=?",
            (*updates.values(), utc_now(), client_order_id),
        )

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        )

    def list_orders(
        self, *, status: str | None = None, symbol: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status.upper())
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        return self.query(
            f"SELECT * FROM orders {where} ORDER BY created_at DESC LIMIT ?", tuple(params)
        )

    def count_entries_since(self, since_iso: str, *, symbol: str | None = None) -> int:
        sql = (
            "SELECT COUNT(*) AS n FROM orders WHERE purpose='ENTRY' AND created_at>=? "
            "AND status IN ('SUBMITTING','ACKNOWLEDGED','NEW','PARTIALLY_FILLED','FILLED')"
        )
        params: list[Any] = [since_iso]
        if symbol:
            sql += " AND symbol=?"
            params.append(symbol.upper())
        row = self.query_one(sql, tuple(params))
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ outbox
    def outbox_prepare(self, record: dict[str, Any]) -> None:
        """POST 前落库；重复 clientOrderId 或在途开仓冲突都抛结构化异常。"""

        now = utc_now()
        with self._lock:
            existing = self.query_one(
                "SELECT status FROM order_outbox WHERE client_order_id=?",
                (record["client_order_id"],),
            )
            if existing:
                raise DuplicateOrderError(
                    f"clientOrderId {record['client_order_id']} already {existing['status']}"
                )
            business_key = str(record["business_key"])
            if business_key.upper().startswith("ENTRY:"):
                pending = self.query_one(
                    "SELECT client_order_id FROM order_outbox WHERE environment=? AND "
                    "business_key=? AND status IN "
                    "('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED') LIMIT 1",
                    (record.get("environment", "demo"), business_key),
                )
                if pending:
                    raise PendingOrderConflict(
                        f"unresolved entry {pending['client_order_id']} blocks {business_key}"
                    )
            request = record.get("request")
            self.execute(
                "INSERT INTO order_outbox(client_order_id, environment, business_key, endpoint, "
                "request_json, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    record["client_order_id"],
                    record.get("environment", "demo"),
                    business_key,
                    record["endpoint"],
                    json.dumps(request or {}, ensure_ascii=False, separators=(",", ":"), default=str),
                    record.get("status", "SUBMITTING"),
                    now,
                    now,
                ),
            )

    def outbox_get(self, client_order_id: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM order_outbox WHERE client_order_id=?", (client_order_id,)
        )

    def outbox_update(self, client_order_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "attempts",
            "unknown_since",
            "next_retry_at",
            "last_error",
            "resolved_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key}=?" for key in updates)
        self.execute(
            f"UPDATE order_outbox SET {assignments}, updated_at=? WHERE client_order_id=?",
            (*updates.values(), utc_now(), client_order_id),
        )

    def outbox_counts(self) -> dict[str, int]:
        rows = self.query(
            "SELECT status, COUNT(*) AS n FROM order_outbox GROUP BY status"
        )
        counts = {str(row["status"]): int(row["n"]) for row in rows}
        counts["unknown"] = counts.get("UNKNOWN", 0)
        counts["submitting"] = counts.get("SUBMITTING", 0)
        return counts

    def outbox_unknown(self, limit: int = 5) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM order_outbox WHERE status='UNKNOWN' ORDER BY updated_at LIMIT ?",
            (max(1, int(limit)),),
        )

    def outbox_active_entry(self, business_key: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM order_outbox WHERE business_key=? AND status IN "
            "('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED') LIMIT 1",
            (business_key,),
        )

    def outbox_active(self, limit: int = 50) -> list[dict[str, Any]]:
        """所有工作态（未收敛）outbox 行；用于展示“待确认”与启动收敛。"""

        return self.query(
            "SELECT * FROM order_outbox WHERE status IN "
            "('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED') "
            "ORDER BY created_at LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )

    def outbox_note_query(
        self, client_order_id: str, *, found: bool, error: str | None = None
    ) -> int:
        """记录一次“按原标识查询”的交易所事实，返回累计“明确查不到”的次数。

        该计数只用于决定“还需要几次独立事实才收敛”，**不用于**把未决状态
        改写成失败（T03 验收：不得因重试次数用尽改成确定失败）。
        """

        self.execute(
            "UPDATE order_outbox SET attempts=attempts+1, "
            "not_found_queries=not_found_queries + ?, last_error=?, updated_at=? "
            "WHERE client_order_id=?",
            (0 if found else 1, None if found else error, utc_now(), client_order_id),
        )
        row = self.outbox_get(client_order_id)
        return int(row.get("not_found_queries") or 0) if row else 0

    def outbox_force_terminal(
        self, client_order_id: str, *, status: str, reason: str
    ) -> bool:
        """把仍在工作态的 outbox 行收敛到明确终态（只由交易所事实驱动）。"""

        return self.execute(
            "UPDATE order_outbox SET status=?, last_error=?, resolved_at=?, updated_at=? "
            "WHERE client_order_id=? AND status IN "
            "('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED')",
            (status.upper(), reason, utc_now(), utc_now(), client_order_id),
        ) > 0

    def list_active_orders(
        self, *, purposes: Sequence[str] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """orders 表中仍处工作态的行（NEW/ACKNOWLEDGED/PARTIALLY_FILLED…）。"""

        sql = (
            "SELECT * FROM orders WHERE status IN "
            "('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED')"
        )
        params: list[Any] = []
        if purposes:
            marks = ", ".join("?" for _ in purposes)
            sql += f" AND purpose IN ({marks})"
            params.extend(str(purpose).upper() for purpose in purposes)
        sql += " ORDER BY created_at LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return self.query(sql, tuple(params))

    def list_orders_with_outbox(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """订单镜像 + outbox 视角（business_key/attempts/last_error），只读 join。"""

        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("o.status=?")
            params.append(status.upper())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        return self.query(
            "SELECT o.*, b.business_key AS outbox_business_key, "
            "b.status AS outbox_status, b.attempts AS outbox_attempts, "
            "b.last_error AS outbox_last_error, b.unknown_since AS outbox_unknown_since "
            "FROM orders o LEFT JOIN order_outbox b ON b.client_order_id = o.client_order_id "
            f"{where} ORDER BY o.created_at DESC, o.updated_at DESC LIMIT ?",
            tuple(params),
        )

    # ------------------------------------------------------------------ fills
    def insert_fill(self, record: dict[str, Any]) -> bool:
        cursor = self.execute(
            "INSERT OR IGNORE INTO fills(fill_id, client_order_id, symbol, side, price, qty, "
            "realized_pnl, commission, trade_time_ms, source, commission_asset) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(record["fill_id"]),
                record["client_order_id"],
                record["symbol"],
                record["side"],
                dec_text(record["price"]),
                dec_text(record["qty"]),
                None if record.get("realized_pnl") is None else dec_text(record["realized_pnl"]),
                None if record.get("commission") is None else dec_text(record["commission"]),
                int(record.get("trade_time_ms") or utc_now_ms()),
                record.get("source", "userTrades"),
                record.get("commission_asset"),
            ),
        )
        if record.get("commission_asset"):
            self.execute("UPDATE fills SET commission_asset=? WHERE fill_id=? AND commission_asset IS NULL",
                         (record["commission_asset"], str(record["fill_id"])))
        return cursor > 0

    def list_fills(self, limit: int = 100, *, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            return self.query(
                "SELECT * FROM fills WHERE symbol=? ORDER BY trade_time_ms DESC LIMIT ?",
                (symbol.upper(), max(1, min(int(limit), 500))),
            )
        return self.query(
            "SELECT * FROM fills ORDER BY trade_time_ms DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )

    def operation_start(self, request_id: str, action: str, target: str, reason: str) -> bool:
        return self.execute(
            "INSERT OR IGNORE INTO operation_history(request_id,action,target,reason,status,started_at) "
            "VALUES(?,?,?,?, 'pending',?)", (request_id, action, target, reason, utc_now())
        ) > 0

    def operation_finish(self, request_id: str, status: str, *, code: str | None = None, summary: dict | None = None) -> None:
        self.execute(
            "UPDATE operation_history SET status=?,finished_at=?,error_code=?,summary_json=? WHERE request_id=?",
            (status, utc_now(), code, json.dumps(summary or {}, ensure_ascii=False), request_id),
        )

    def operations(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM operation_history ORDER BY started_at DESC, rowid DESC LIMIT ?", (max(1, min(limit, 500)),))
        for row in rows:
            row["summary"] = json.loads(row.pop("summary_json") or "{}")
        return rows

    def local_day_performance(self, *, now: datetime | None = None) -> dict[str, Any]:
        # Read the complete day's ledger, never just the last 100 fills in the UI.
        from decimal import Decimal
        from .numbers import to_decimal
        current = now or datetime.now(timezone.utc)
        local = current.astimezone(timezone(timedelta(hours=8)))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        rows = self.query("SELECT realized_pnl,commission,commission_asset FROM fills WHERE trade_time_ms>=? AND trade_time_ms<?",
                          (int(start.timestamp()*1000), int(end.timestamp()*1000)))
        baseline = self.query_one("SELECT ts_bucket,equity_session FROM equity_snapshots WHERE ts_bucket>=? AND ts_bucket<? ORDER BY ts_bucket LIMIT 1",
                                  (start.isoformat(timespec="seconds").replace('+00:00','Z'), end.isoformat(timespec="seconds").replace('+00:00','Z')))
        known = [to_decimal(row['realized_pnl']) for row in rows]
        fees: dict[str, Decimal] = {}
        unknown_fees = 0
        for row in rows:
            asset, fee = row.get('commission_asset'), to_decimal(row.get('commission'))
            if not asset or fee is None:
                unknown_fees += 1
            else:
                fees[asset] = fees.get(asset, Decimal('0')) + fee
        realized = None if any(x is None for x in known) else sum(known, Decimal('0'))
        # §8·C2/C5：今日视图也必须走同一套口径，不再另算一份。
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        strategy = self.strategy_pnl(start_ms=start_ms, end_ms=end_ms)
        return {
            "date": local.date().isoformat(), "timezone": "Asia/Shanghai", "source": "local_ledger",
            "fill_count": len(rows), "realized_pnl": None if realized is None else str(realized),
            "commissions": {asset: str(value) for asset, value in fees.items()},
            "unknown_commission_count": unknown_fees,
            "net_realized_pnl": str(realized - fees.get('USDT', Decimal('0'))) if realized is not None and not unknown_fees and set(fees).issubset({'USDT'}) else None,
            "net_pnl": strategy["net_strategy_pnl"],
            "funding_pnl": strategy["funding_pnl"],
            "transfers": strategy["transfers"],
            "strategy": strategy,
            "baseline_at": baseline['ts_bucket'] if baseline else None,
            "baseline_equity": baseline['equity_session'] if baseline else None,
            "coverage": strategy["coverage"],
        }

    def recent_close_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        """平仓成交流水（按时间倒序）；开仓 fill 的 realized=0 不参与连亏计算。"""

        return self.query(
            "SELECT f.realized_pnl, f.commission, f.client_order_id, f.trade_time_ms "
            "FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id "
            "WHERE o.purpose = 'EXIT' ORDER BY f.trade_time_ms DESC LIMIT ?",
            (max(1, int(limit)),),
        )

    def realized_pnl_since(self, since_iso: str) -> float:
        row = self.query_one(
            "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)), 0) AS pnl, COUNT(*) AS n "
            "FROM fills WHERE trade_time_ms >= ?",
            (int(datetime.fromisoformat(since_iso.replace("Z", "+00:00")).timestamp() * 1000),),
        )
        return float(row["pnl"]) if row else 0.0

    # ------------------------------------------------------------------ positions
    def upsert_position(self, record: dict[str, Any]) -> None:
        now = utc_now()
        self.execute(
            "INSERT INTO positions(ledger_id, symbol, position_side, qty, entry_price, mark_price, "
            "upnl, leverage, margin, margin_mode, liq_price, sl_price, tp_price, "
            "protection_status, origin, opened_at, deadline_at, time_stop_source, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ledger_id, symbol, position_side) DO UPDATE SET "
            "qty=excluded.qty, entry_price=excluded.entry_price, mark_price=excluded.mark_price, "
            "upnl=excluded.upnl, leverage=excluded.leverage, margin=excluded.margin, "
            "margin_mode=excluded.margin_mode, liq_price=excluded.liq_price, "
            "sl_price=COALESCE(excluded.sl_price, positions.sl_price), "
            "tp_price=COALESCE(excluded.tp_price, positions.tp_price), "
            "protection_status=excluded.protection_status, "
            # T03: origin / opened_at / time_stop 期限是建仓时的身份事实，
            # 后续对账刷新不得改写（否则外部仓位会被永久改标成系统仓位）。
            "origin=COALESCE(positions.origin, excluded.origin), "
            "opened_at=COALESCE(positions.opened_at, excluded.opened_at), "
            "deadline_at=COALESCE(excluded.deadline_at, positions.deadline_at), "
            "time_stop_source=COALESCE(excluded.time_stop_source, positions.time_stop_source), "
            "updated_at=excluded.updated_at",
            (
                record.get("ledger_id") or self.ledger_id,
                record["symbol"],
                record["position_side"],
                dec_text(record["qty"]),
                dec_text(record["entry_price"]),
                None if record.get("mark_price") is None else dec_text(record["mark_price"]),
                None if record.get("upnl") is None else dec_text(record["upnl"]),
                int(record["leverage"]) if record.get("leverage") is not None else None,
                None if record.get("margin") is None else dec_text(record["margin"]),
                record.get("margin_mode"),
                None if record.get("liq_price") is None else dec_text(record["liq_price"]),
                None if record.get("sl_price") is None else dec_text(record["sl_price"]),
                None if record.get("tp_price") is None else dec_text(record["tp_price"]),
                record.get("protection_status", "UNKNOWN"),
                record.get("origin") or "OPENED_BY_WORKBENCH",
                record.get("opened_at") or now,
                record.get("deadline_at"),
                record.get("time_stop_source"),
                now,
            ),
        )

    def list_positions(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM positions ORDER BY symbol, position_side")

    def get_position(self, symbol: str, position_side: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM positions WHERE symbol=? AND position_side=?",
            (symbol.upper(), position_side.upper()),
        )

    def set_position_time_stop(
        self,
        symbol: str,
        position_side: str,
        *,
        opened_at: str | None = None,
        deadline_at: str | None,
        source: str | None,
    ) -> int:
        """T03: 记录系统管理仓位的 time_stop 期限与来源计划版本（重启后按原期限）。"""

        return self.execute(
            "UPDATE positions SET deadline_at=?, time_stop_source=?, "
            "opened_at=COALESCE(?, opened_at), updated_at=? "
            "WHERE symbol=? AND position_side=?",
            (
                deadline_at,
                source,
                opened_at,
                utc_now(),
                symbol.upper(),
                position_side.upper(),
            ),
        )

    def delete_position(self, symbol: str, position_side: str) -> None:
        self.execute(
            "DELETE FROM positions WHERE symbol=? AND position_side=?",
            (symbol.upper(), position_side.upper()),
        )

    def insert_position_event(
        self,
        *,
        symbol: str,
        position_side: str,
        kind: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        note: str = "",
        at: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO position_events(at, symbol, position_side, kind, before_json, "
            "after_json, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                at or utc_now(),
                symbol,
                position_side,
                kind,
                None if before is None else json.dumps(before, ensure_ascii=False, default=str),
                None if after is None else json.dumps(after, ensure_ascii=False, default=str),
                note,
            ),
        )

    def list_position_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM position_events ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        )

    # ------------------------------------------------------------------ equity
    def insert_equity_snapshot(self, record: dict[str, Any]) -> None:
        bucket = record.get("ts_bucket") or bucket_iso(30)
        self.execute(
            "INSERT OR REPLACE INTO equity_snapshots(ledger_id, ts_bucket, equity_exchange, "
            "equity_session, wallet_balance, available, upnl, margin_used, day_start_equity, "
            "peak_equity, daily_loss_ratio, drawdown_ratio, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.get("ledger_id") or self.ledger_id,
                bucket,
                dec_text(record["equity_exchange"]),
                dec_text(record["equity_session"]),
                None if record.get("wallet_balance") is None else dec_text(record["wallet_balance"]),
                None if record.get("available") is None else dec_text(record["available"]),
                None if record.get("upnl") is None else dec_text(record["upnl"]),
                None if record.get("margin_used") is None else dec_text(record["margin_used"]),
                None if record.get("day_start_equity") is None else dec_text(record["day_start_equity"]),
                None if record.get("peak_equity") is None else dec_text(record["peak_equity"]),
                None if record.get("daily_loss_ratio") is None else dec_text(record["daily_loss_ratio"]),
                None if record.get("drawdown_ratio") is None else dec_text(record["drawdown_ratio"]),
                record.get("source", "loop"),
            ),
        )

    def list_equity_curve(self, *, since: str, limit: int = 2000) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM equity_snapshots WHERE ledger_id=? AND ts_bucket>=? "
            "ORDER BY ts_bucket ASC LIMIT ?",
            (self.ledger_id, since, max(1, min(int(limit), 5000))),
        )

    def latest_equity(self) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM equity_snapshots WHERE ledger_id=? ORDER BY ts_bucket DESC LIMIT 1",
            (self.ledger_id,),
        )

    def latest_equity_since(self, seconds: float) -> dict[str, Any] | None:
        row = self.latest_equity()
        if row is None:
            return None
        try:
            stamp = datetime.fromisoformat(str(row["ts_bucket"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        return row if age <= seconds else None

    # ------------------------------------------------------------------ §8·C 资金口径
    def upsert_capital_flows(self, rows: Iterable[dict[str, Any]]) -> int:
        """幂等写入交易所 income 流水（资金费、划转、返佣等）。返回真正新增的行数。

        幂等键是交易所的 income id；重复回填不产生重复行，也不会改写既有金额。
        划转与资金费必须分开存放：划转是资金流，不是策略盈利。
        """

        now = utc_now()
        inserted = 0
        for record in rows:
            ref_id = str(record.get("ref_id") or "").strip()
            if not ref_id:
                continue
            payload = record.get("raw")
            inserted += self.execute(
                "INSERT OR IGNORE INTO capital_flows(ref_id, at_ms, kind, asset, amount, "
                "income_type, symbol, source, raw_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ref_id,
                    int(record.get("at_ms") or 0),
                    str(record.get("kind") or "OTHER"),
                    str(record.get("asset") or "USDT").upper(),
                    dec_text(record.get("amount")),
                    record.get("income_type"),
                    record.get("symbol"),
                    record.get("source", "income"),
                    None if payload is None else json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                ),
            )
        return inserted

    def list_capital_flows(self, *, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM capital_flows WHERE at_ms>=? AND at_ms<? ORDER BY at_ms ASC",
            (int(start_ms), int(end_ms)),
        )

    def backfill_state(self, stream: str, symbol: str = "ALL") -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM backfill_state WHERE ledger_id=? AND stream=? AND symbol=?",
            (self.ledger_id, stream, symbol.upper()),
        )

    def backfill_report(self, stream: str, symbol: str = "ALL") -> dict[str, Any]:
        """把一条 backfill_state 行翻译成可读的覆盖/失败报告（§8·C5 · T18）。

        与 ``_coverage_block`` 的区别：这里额外给出 cursor 与毫秒原值，供调度器
        决定“从哪继续拉”，也让诊断不必再猜“资金费为什么还是未知”——失败原因
        直接来自 ``last_error``，而不是重新翻日志。
        """

        state = self.backfill_state(stream, symbol) or {}
        block = _coverage_block(state)
        block.update(
            {
                "stream": stream,
                "symbol": symbol.upper(),
                "cursor": _ms_iso(state.get("cursor_ms")),
                "cursor_ms": state.get("cursor_ms"),
                "covered_from_ms": state.get("covered_from_ms"),
                "covered_to_ms": state.get("covered_to_ms"),
                "complete": bool(state.get("complete")),
            }
        )
        return block

    def set_backfill_state(
        self,
        stream: str,
        *,
        symbol: str = "ALL",
        cursor_ms: int | None = None,
        covered_from_ms: int | None = None,
        covered_to_ms: int | None = None,
        complete: bool = False,
        last_error: str | None = None,
    ) -> None:
        """按标的保存回填游标与覆盖区间（§8·C5）。游标只进不退，避免重复翻页。

        完成标记同样是单调的：一次局部翻页（complete=False）不能把已经
        “回填完成”的流改回未完成，否则覆盖范围会随调用顺序反复变化。
        需要重新回填时调用方必须显式重建（清空该行），而不是靠默认参数覆盖。
        """

        symbol = symbol.upper()
        previous = self.backfill_state(stream, symbol) or {}
        candidates = [value for value in (cursor_ms, previous.get("cursor_ms")) if value is not None]
        merged_cursor = max(candidates) if candidates else None
        previous_from = previous.get("covered_from_ms")
        if previous_from is None:
            merged_from = covered_from_ms
        elif covered_from_ms is None:
            merged_from = previous_from
        else:
            merged_from = min(int(previous_from), int(covered_from_ms))
        self.execute(
            "INSERT INTO backfill_state(ledger_id, stream, symbol, cursor_ms, covered_from_ms, "
            "covered_to_ms, complete, last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ledger_id, stream, symbol) DO UPDATE SET cursor_ms=excluded.cursor_ms, "
            "covered_from_ms=excluded.covered_from_ms, covered_to_ms=excluded.covered_to_ms, "
            "complete=excluded.complete, last_error=excluded.last_error, updated_at=excluded.updated_at",
            (
                self.ledger_id,
                stream,
                symbol,
                merged_cursor,
                merged_from,
                covered_to_ms if covered_to_ms is not None else previous.get("covered_to_ms"),
                1 if (complete or previous.get("complete")) else 0,
                last_error,
                utc_now(),
            ),
        )
    def strategy_pnl(
        self,
        *,
        start_ms: int,
        end_ms: int,
        quote_assets: Sequence[str] = ("USDT", "USDC", "BUSD"),
    ) -> dict[str, Any]:
        """§8·C2：把“策略净盈亏”拆成 已实现盈亏 / 手续费 / 资金费 / 划转 四本账。

        口径约定（与 §8·C1 一致，不合并成一个 equity 字段）：
        - 已实现盈亏只来自成交（fills.realized_pnl），未知即 None，绝不当 0；
        - 手续费永远是成本；非计价币手续费没有可靠换算时记 unknown，不当作零成本；
        - 资金费单独归集（可正可负），来自交易所 income 流水；
        - 转入转出是资金流，单列，不计入策略盈利；
        - net_strategy_pnl 只有在上述各项都完整时才计算，否则 None + unknown 原因。
        """

        quote = {asset.upper() for asset in quote_assets}
        start_ms, end_ms = int(start_ms), int(end_ms)
        fills = self.query(
            "SELECT realized_pnl, commission, commission_asset, symbol FROM fills "
            "WHERE trade_time_ms>=? AND trade_time_ms<?",
            (start_ms, end_ms),
        )
        flows = self.list_capital_flows(start_ms=start_ms, end_ms=end_ms)
        unknown: list[str] = []

        realized_values = [to_decimal(row.get("realized_pnl")) for row in fills]
        if any(value is None for value in realized_values):
            realized = None
            unknown.append("部分成交缺少已实现盈亏（realized_pnl 为空），不能按 0 计入")
        else:
            realized = sum((value for value in realized_values if value is not None), Decimal("0"))

        commissions: dict[str, Decimal] = {}
        unknown_commission_count = 0
        for row in fills:
            asset = str(row.get("commission_asset") or "").upper()
            fee = to_decimal(row.get("commission"))
            if fee is None or fee == 0:
                continue
            if not asset:
                unknown_commission_count += 1
                continue
            commissions[asset] = commissions.get(asset, Decimal("0")) + fee
        commission_quote = sum(
            (value for asset, value in commissions.items() if asset in quote), Decimal("0")
        )
        unconverted_assets = sorted(asset for asset in commissions if asset not in quote)
        if unknown_commission_count:
            unknown.append(f"{unknown_commission_count} 笔手续费缺少币种，无法归集")
        if unconverted_assets:
            unknown.append(
                "非计价币手续费无可靠换算: " + ", ".join(unconverted_assets)
            )

        # V12：划转只能来自显式 TRANSFER_* 类型。旧实现用 amount>=0 猜正负，
        # 会把 income 同步写进来的 REALIZED_PNL（正）当成转入、COMMISSION（负）
        # 当成转出——而这两项已由 fills 计入本金与费用，重复计入会让 expected
        # 凭空多出（已实现−手续费），使对账恒为 partial 并掩盖真实差额。
        funding_by_asset: dict[str, Decimal] = {}
        transfers_in: dict[str, Decimal] = {}
        transfers_out: dict[str, Decimal] = {}
        unclassified: dict[str, int] = {}
        for flow in flows:
            asset = str(flow.get("asset") or "").upper()
            amount = to_decimal(flow.get("amount"), Decimal("0")) or Decimal("0")
            kind = str(flow.get("kind") or "").upper()
            if kind == "FUNDING_FEE":
                funding_by_asset[asset] = funding_by_asset.get(asset, Decimal("0")) + amount
            elif kind == "TRANSFER_IN":
                transfers_in[asset] = transfers_in.get(asset, Decimal("0")) + amount
            elif kind == "TRANSFER_OUT":
                transfers_out[asset] = transfers_out.get(asset, Decimal("0")) + amount
            elif kind in {"REALIZED_PNL", "COMMISSION"}:
                # 已在 fills 归集，这里只留档，不参与任何一侧。
                continue
            else:
                # 未识别类型不静默归类：记数量并让调用方可见。
                unclassified[kind or "UNKNOWN"] = unclassified.get(kind or "UNKNOWN", 0) + 1

        funding_quote = sum(
            (value for asset, value in funding_by_asset.items() if asset in quote), Decimal("0")
        )
        if any(asset not in quote for asset in funding_by_asset):
            unknown.append("非计价币资金费无可靠换算")
        if unclassified:
            unknown.append(
                "未识别的资金流水类型未归类: "
                + ", ".join(f"{name}×{count}" for name, count in sorted(unclassified.items()))
            )
        transfer_in_quote = sum(
            (value for asset, value in transfers_in.items() if asset in quote), Decimal("0")
        )
        transfer_out_quote = sum(
            (value for asset, value in transfers_out.items() if asset in quote), Decimal("0")
        )

        fills_state = self.backfill_state("fills") or {}
        income_state = self.backfill_state("income") or {}
        # 资金费未回填时，"没有资金费流水"只说明没查到，不说明等于 0。
        # 把它当 0 是典型的“用零冒充未知”，会让策略盈亏看起来比实际更好。
        funding_known = bool(income_state.get("complete"))
        if not funding_known:
            unknown.append("资金费未回填（income 流未标记完成），不能按 0 计入")

        net: Decimal | None = None
        if realized is not None and not unknown:
            net = realized - commission_quote + funding_quote

        covered_from = [
            state.get("covered_from_ms")
            for state in (fills_state, income_state)
            if state.get("covered_from_ms") is not None
        ]
        coverage = {
            "fills": _coverage_block(fills_state),
            "income": _coverage_block(income_state),
            "covered_from_ms": min(covered_from) if covered_from else None,
            "complete": bool(fills_state.get("complete")) and bool(income_state.get("complete")),
            "note": None
            if covered_from and fills_state.get("complete") and income_state.get("complete")
            else "统计覆盖不完整：资金费/划转可能未回填，历史不代表完整账户历史",
        }

        return {
            "window": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start": _ms_iso(start_ms),
                "end": _ms_iso(end_ms),
            },
            "fill_count": len(fills),
            "flow_count": len(flows),
            "realized_pnl": None if realized is None else dec_text(realized),
            "commissions": {asset: dec_text(value) for asset, value in sorted(commissions.items())},
            "commission_quote": dec_text(commission_quote),
            "funding_pnl": dec_text(funding_quote),
            "funding_by_asset": {
                asset: dec_text(value) for asset, value in sorted(funding_by_asset.items())
            },
            "transfers": {
                "in": {asset: dec_text(value) for asset, value in sorted(transfers_in.items())},
                "out": {asset: dec_text(value) for asset, value in sorted(transfers_out.items())},
                "net_quote": dec_text(transfer_in_quote + transfer_out_quote),
                "count": len(transfers_in) + len(transfers_out),
            },
            "funding_known": funding_known,
            "net_strategy_pnl": None if net is None else dec_text(net),
            "net_strategy_pnl_includes": ["realized_pnl", "-commission", "+funding"],
            "excludes": ["transfers"],
            "unknown": unknown,
            "coverage": coverage,
        }

    def reconcile_funds(
        self,
        *,
        start_ms: int,
        end_ms: int,
        exchange_delta: object = None,
        tolerance: object = "0.01",
        quote_assets: Sequence[str] = ("USDT", "USDC", "BUSD"),
    ) -> dict[str, Any]:
        """§8·C3：账本口径与交易所权益变动对账，输出 matched/partial/unknown。

        恒等式：交易所权益变动 ≈ 策略净盈亏 + 净划转。
        容差按调用方给的精度设定；差额不得用大容差掩盖，超出即 partial 并给出原因。
        """

        report = self.strategy_pnl(
            start_ms=start_ms, end_ms=end_ms, quote_assets=quote_assets
        )
        tolerance_dec = to_decimal(tolerance, Decimal("0.01")) or Decimal("0.01")
        net = to_decimal(report.get("net_strategy_pnl"))
        transfers_net = to_decimal(report["transfers"].get("net_quote"), Decimal("0"))
        actual = to_decimal(exchange_delta)
        checked_at = utc_now()
        reasons: list[str] = []

        if actual is None:
            reasons.append("交易所权益变动不可用（未取到同一时点的权益或查询失败）")
        if net is None:
            reasons.append("账本净盈亏不完整：" + "；".join(report["unknown"] or ["未知原因"]))

        expected = None
        diff = None
        if net is not None and transfers_net is not None:
            expected = net + transfers_net
        if expected is not None and actual is not None:
            diff = actual - expected

        if diff is None:
            status = "unknown"
        elif abs(diff) <= tolerance_dec and not report["unknown"]:
            status = "matched"
        else:
            status = "partial"
            if abs(diff) > tolerance_dec:
                reasons.append(
                    f"差额 {dec_text(diff)} 超出容差 {dec_text(tolerance_dec)}"
                )
            reasons.extend(report["unknown"])

        last_success_at = self.get_meta("reconcile_funds_last_success_at")
        if status == "matched":
            last_success_at = checked_at
            self.set_meta("reconcile_funds_last_success_at", checked_at)

        return {
            "status": status,
            "checked_at": checked_at,
            "last_success_at": last_success_at,
            "tolerance": dec_text(tolerance_dec),
            "exchange_delta": None if actual is None else dec_text(actual),
            "expected_delta": None if expected is None else dec_text(expected),
            "diff": None if diff is None else dec_text(diff),
            "reasons": reasons,
            "window": report["window"],
            "coverage": report["coverage"],
            "ledger": report,
        }

    def schema_report(self) -> dict[str, Any]:
        """§8·C6（G5）：账本 schema 版本一致性，供 /health 与诊断页消费。"""

        from .schema import schema_report as _report

        return dict(_report(self.connection()))

    # ------------------------------------------------------------------ risk state
    def load_risk_state(self) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM risk_state WHERE id=1")

    def save_risk_state(self, state: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO risk_state(id, anchor_id, date, day_start_equity, peak_equity, "
            "day_start_equity_source, day_start_equity_at, day_start_equity_tz, "
            "peak_equity_source, peak_equity_at, "
            "daily_loss_latched, daily_loss_latched_at, drawdown_latched, drawdown_latched_at, "
            "consecutive_losses, cooldown_until, recovery_required, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET anchor_id=excluded.anchor_id, date=excluded.date, "
            "day_start_equity=excluded.day_start_equity, peak_equity=excluded.peak_equity, "
            "day_start_equity_source=excluded.day_start_equity_source, "
            "day_start_equity_at=excluded.day_start_equity_at, "
            "day_start_equity_tz=excluded.day_start_equity_tz, "
            "peak_equity_source=excluded.peak_equity_source, "
            "peak_equity_at=excluded.peak_equity_at, "
            "daily_loss_latched=excluded.daily_loss_latched, "
            "daily_loss_latched_at=excluded.daily_loss_latched_at, "
            "drawdown_latched=excluded.drawdown_latched, "
            "drawdown_latched_at=excluded.drawdown_latched_at, "
            "consecutive_losses=excluded.consecutive_losses, cooldown_until=excluded.cooldown_until, "
            "recovery_required=excluded.recovery_required, updated_at=excluded.updated_at",
            (
                state["anchor_id"],
                state["date"],
                None if state.get("day_start_equity") is None else dec_text(state["day_start_equity"]),
                None if state.get("peak_equity") is None else dec_text(state["peak_equity"]),
                state.get("day_start_equity_source"),
                state.get("day_start_equity_at"),
                state.get("day_start_equity_tz"),
                state.get("peak_equity_source"),
                state.get("peak_equity_at"),
                int(state.get("daily_loss_latched", 0)),
                state.get("daily_loss_latched_at"),
                int(state.get("drawdown_latched", 0)),
                state.get("drawdown_latched_at"),
                int(state.get("consecutive_losses", 0)),
                state.get("cooldown_until"),
                int(state.get("recovery_required", 0)),
                utc_now(),
            ),
        )

    def load_control(self) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM control_state WHERE id=1")

    def save_control(self, *, state: str, reason: str, updated_by: str) -> None:
        self.execute(
            "INSERT INTO control_state(id, state, reason, updated_at, updated_by) "
            "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
            "reason=excluded.reason, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
            (state, reason, utc_now(), updated_by),
        )

    # ------------------------------------------------------------------ events
    def log_risk_event(
        self,
        *,
        event_type: str,
        severity: str = "info",
        rule_id: str | None = None,
        action: str | None = None,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO risk_events(at, rule_id, event_type, severity, action, details_json, "
            "trace_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                utc_now(),
                rule_id,
                event_type,
                severity,
                action,
                None if details is None else json.dumps(details, ensure_ascii=False, default=str),
                trace_id,
            ),
        )

    def list_risk_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (max(1, int(limit)),))

    # ------------------------------------------------------------------ approval
    def insert_approval(self, record: dict[str, Any]) -> None:
        self.execute(
            "INSERT INTO approvals(jti, plan_hash, cycle_id, approved_qty, leverage, status, "
            "created_at, expires_at_ms) VALUES (?, ?, ?, ?, ?, 'ISSUED', ?, ?)",
            (
                record["jti"],
                record["plan_hash"],
                record.get("cycle_id"),
                dec_text(record["approved_qty"]),
                int(record["leverage"]),
                record.get("created_at") or utc_now(),
                int(record["expires_at_ms"]),
            ),
        )

    def get_approval(self, jti: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM approvals WHERE jti=?", (jti,))

    def consume_approval(self, jti: str) -> bool:
        """原子单次消费；返回 False 表示已被消费（重放）。"""

        rowcount = self.execute(
            "UPDATE approvals SET status='CONSUMED', consumed_at=? WHERE jti=? AND status='ISSUED'",
            (utc_now(), jti),
        )
        return rowcount == 1

    def purge_expired_approvals(self, now_ms: int | None = None) -> int:
        return self.execute(
            "DELETE FROM approvals WHERE expires_at_ms < ? AND status='ISSUED'",
            (int(now_ms if now_ms is not None else utc_now_ms()),),
        )

    # ------------------------------------------------------------------ cycles
    def start_cycle(self, cycle_id: str, slot_id: str | None, decision_json: str | None = None) -> None:
        self.execute(
            "INSERT INTO cycle_runs(cycle_id, slot_id, state, started_at, decision_json) "
            "VALUES (?, ?, 'RUNNING', ?, ?) ON CONFLICT(cycle_id) DO NOTHING",
            (cycle_id, slot_id, utc_now(), decision_json),
        )

    def finish_cycle(
        self, cycle_id: str, *, state: str = "DONE", outcome: str | None = None, error_code: str | None = None
    ) -> None:
        self.execute(
            "UPDATE cycle_runs SET state=?, finished_at=?, outcome=?, error_code=? WHERE cycle_id=?",
            (state, utc_now(), outcome, error_code, cycle_id),
        )

    def recent_cycles(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM cycle_runs ORDER BY started_at DESC LIMIT ?", (max(1, int(limit)),)
        )

    def iter_table_names(self) -> Iterable[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [str(row["name"]) for row in rows]
