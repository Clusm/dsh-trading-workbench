"""交易所安全事件监视器：告警与只读刷新，不改变仓位或控制状态。"""
from __future__ import annotations

import json
import time
from typing import Any

from audit.database import AuditDatabase, utc_now
from exchange.event_models import ExchangeEvent, normalize_user_event


class ExchangeSafetyMonitor:
    """消费账户配置与保证金告警事件的只读安全监视器。

    监视器不改变控制状态、不平仓；MARGIN_CALL 只写 critical 审计并立即触发一次 REST
    账户对账，以便控制层获得新鲜真值。事件键保证重复投递不会重复制造安全事件。
    """
    def __init__(self, db: AuditDatabase, *, environment: str,
                 account_mirror: Any = None, broker: Any = None) -> None:
        self.db = db
        self.environment = str(environment)
        self.account_mirror = account_mirror
        self.broker = broker
        self.last_event_at: float | None = None
        self.last_error: str | None = None
        self.config_events = 0
        self.margin_calls = 0
        self.reconcile_failures = 0

    def apply_event(self, event: ExchangeEvent) -> bool:
        """消费支持的安全事件；成功识别并处理时返回 True。"""
        if event.event_type == "ACCOUNT_CONFIG_UPDATE":
            self._apply_config(event)
            self.config_events += 1
            self.last_event_at = time.time()
            self.last_error = None
            return True
        if event.event_type == "MARGIN_CALL":
            self._apply_margin_call(event)
            self.last_event_at = time.time()
            return True
        return False

    def _apply_config(self, event: ExchangeEvent) -> None:
        payload = event.payload
        account_config = payload.get("ac") if isinstance(payload.get("ac"), dict) else {}
        account_info = payload.get("ai") if isinstance(payload.get("ai"), dict) else {}
        event_ms = int(event.transaction_time_ms or event.event_time_ms or 0)
        now = utc_now()
        rows: list[tuple[str, str | None, int | None, int | None]] = []
        symbol = str(account_config.get("s") or "").upper() or None
        if symbol:
            try:
                leverage = int(float(account_config.get("l")))
            except (TypeError, ValueError):
                leverage = None
            rows.append((f"SYMBOL:{symbol}", symbol, leverage, None))
        if "j" in account_info:
            raw_mode = account_info.get("j")
            multi_assets = (str(raw_mode).strip().lower() in {"1", "true", "yes", "on"}
                            if isinstance(raw_mode, str) else bool(raw_mode))
            rows.append(("ACCOUNT", None, None, int(multi_assets)))
        # 未识别的新字段也完成事件消费；原始payload仍保存在exchange_events供审计。
        with self.db.lock:
            for scope, sym, leverage, multi_assets in rows:
                self.db.conn.execute(
                    """INSERT INTO account_config_mirror(
                        environment,scope,symbol,leverage,multi_assets_mode,
                        event_time_ms,updated_at,source
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(environment,scope) DO UPDATE SET
                        symbol=excluded.symbol,
                        leverage=COALESCE(excluded.leverage,account_config_mirror.leverage),
                        multi_assets_mode=COALESCE(
                            excluded.multi_assets_mode,account_config_mirror.multi_assets_mode),
                        event_time_ms=excluded.event_time_ms,updated_at=excluded.updated_at,
                        source=excluded.source
                    WHERE excluded.event_time_ms >= account_config_mirror.event_time_ms""",
                    (self.environment, scope, sym, leverage, multi_assets,
                     event_ms, now, "user_stream"),
                )
            self.db.conn.commit()

    def _apply_margin_call(self, event: ExchangeEvent) -> None:
        positions = event.payload.get("p") if isinstance(event.payload.get("p"), list) else []
        symbols = sorted({str(p.get("s") or "").upper() for p in positions
                          if isinstance(p, dict) and p.get("s")})
        details = (f"Binance USD-M MARGIN_CALL，涉及 {len(symbols)} 个仓位"
                   + (f"：{', '.join(symbols[:8])}" if symbols else ""))
        now = utc_now()
        with self.db.lock:
            cursor = self.db.conn.execute(
                """INSERT OR IGNORE INTO exchange_safety_events(
                    event_key,event_type,severity,observed_at,details,reconciled
                ) VALUES(?,?,?,?,?,0)""",
                (event.event_key, event.event_type, "critical", now, details),
            )
            self.db.conn.commit()
            inserted = cursor.rowcount == 1
        if not inserted:
            return
        self.margin_calls += 1
        self.db.log_risk_event(
            event_type="exchange_margin_call", severity="critical",
            details=details, action_taken="alert + immediate REST account reconcile",
        )
        reconciled = False
        reconcile_error = None
        if self.account_mirror is not None and self.broker is not None:
            try:
                self.account_mirror.reconcile_broker(self.broker)
                reconciled = True
                self.last_error = None
            except Exception as exc:
                reconcile_error = type(exc).__name__
                self.last_error = reconcile_error
                self.reconcile_failures += 1
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE exchange_safety_events SET reconciled=?,reconcile_error=? "
                "WHERE event_key=?",
                (int(reconciled), reconcile_error, event.event_key),
            )
            self.db.conn.commit()

    def replay_pending(self) -> int:
        """重放收件箱中尚未应用的安全事件，返回本轮应用数量。"""
        rows = self.db.fetch_all(
            "SELECT event_key,payload_json,source FROM exchange_events "
            "WHERE status='RECEIVED' AND event_type IN ('ACCOUNT_CONFIG_UPDATE','MARGIN_CALL') "
            "ORDER BY id")
        if not rows:
            return 0
        from exchange.event_inbox import ExchangeEventInbox
        inbox = ExchangeEventInbox(self.db)
        applied = 0
        for row in rows:
            try:
                event = normalize_user_event(
                    json.loads(row["payload_json"]), source=str(row["source"]))
                if self.apply_event(event):
                    inbox.mark(str(row["event_key"]), "APPLIED")
                    applied += 1
            except Exception as exc:
                self.last_error = type(exc).__name__
        return applied

    def status(self) -> dict[str, Any]:
        """返回观察计数、错误和最近一条安全事件。"""
        latest = self.db.fetch_one(
            "SELECT * FROM exchange_safety_events ORDER BY observed_at DESC LIMIT 1")
        return {
            "enabled": True,
            "observe_only": True,
            "environment": self.environment,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "config_events": self.config_events,
            "margin_calls": self.margin_calls,
            "reconcile_failures": self.reconcile_failures,
            "last_safety_event": dict(latest) if latest else None,
        }
