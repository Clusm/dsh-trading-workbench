"""USD-M账户影子镜像；仅接收私有流和Signed REST事实，不参与交易执行。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

from audit.database import AuditDatabase, utc_now
from exchange.event_models import ExchangeEvent, normalize_user_event


_USDT_PERPETUAL = re.compile(r"^[0-9A-Z]+USDT$")


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class AccountMirror:
    def __init__(self, db: AuditDatabase, *, environment: str,
                 allowed_symbols: Iterable[str] = ()) -> None:
        self.db = db
        self.environment = str(environment)
        self.allowed_symbols = {str(s).upper() for s in allowed_symbols if s}
        self.last_event_at: float | None = None
        self.last_reconcile_at: float | None = None
        self.last_error: str | None = None
        self.events_applied = 0
        self.out_of_scope = 0

    def _allowed(self, symbol: str) -> bool:
        normalized = str(symbol or "").upper()
        return bool(_USDT_PERPETUAL.fullmatch(normalized)) and (
            not self.allowed_symbols or normalized in self.allowed_symbols)

    @staticmethod
    def _position_side(row: dict[str, Any]) -> str:
        side = str(row.get("ps") or row.get("positionSide") or "BOTH").upper()
        if side in {"LONG", "SHORT"}:
            return side
        return "LONG" if _float(row.get("pa", row.get("positionAmt"))) >= 0 else "SHORT"

    def apply_event(self, event: ExchangeEvent) -> bool:
        if event.event_type != "ACCOUNT_UPDATE":
            return False
        account = event.payload.get("a") if isinstance(event.payload.get("a"), dict) else {}
        balances = account.get("B") if isinstance(account.get("B"), list) else []
        positions = account.get("P") if isinstance(account.get("P"), list) else []
        event_ms = int(event.transaction_time_ms or event.event_time_ms or 0)
        now = utc_now()
        with self.db.lock:
            for row in balances:
                if not isinstance(row, dict) or str(row.get("a") or "").upper() != "USDT":
                    if isinstance(row, dict) and row.get("a"):
                        self.out_of_scope += 1
                    continue
                self._upsert_balance(row, event_ms, now, "user_stream")
            for row in positions:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("s") or "").upper()
                if not self._allowed(symbol):
                    self.out_of_scope += 1
                    continue
                self._upsert_position(row, event_ms, now, "user_stream")
            self.db.conn.commit()
        self.last_event_at = time.time()
        self.last_error = None
        self.events_applied += 1
        return True

    def _upsert_balance(self, row: dict[str, Any], event_ms: int,
                        updated_at: str, source: str) -> None:
        self.db.conn.execute(
            """INSERT INTO account_balances(
                asset,wallet_balance,cross_wallet_balance,available_balance,
                unrealized_pnl,balance_change,event_time_ms,updated_at,source
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset) DO UPDATE SET
                wallet_balance=excluded.wallet_balance,
                cross_wallet_balance=excluded.cross_wallet_balance,
                available_balance=COALESCE(excluded.available_balance,account_balances.available_balance),
                unrealized_pnl=excluded.unrealized_pnl,
                balance_change=excluded.balance_change,event_time_ms=excluded.event_time_ms,
                updated_at=excluded.updated_at,source=excluded.source
            WHERE excluded.event_time_ms >= account_balances.event_time_ms""",
            (
                "USDT", _float(row.get("wb", row.get("walletBalance"))),
                _float(row.get("cw", row.get("crossWalletBalance"))),
                (_float(row.get("availableBalance")) if row.get("availableBalance") is not None else None),
                _float(row.get("unrealizedProfit")),
                (_float(row.get("bc")) if row.get("bc") is not None else None),
                event_ms, updated_at, source,
            ),
        )

    def _upsert_position(self, row: dict[str, Any], event_ms: int,
                         updated_at: str, source: str) -> None:
        symbol = str(row.get("s", row.get("symbol")) or "").upper()
        side = self._position_side(row)
        self.db.conn.execute(
            """INSERT INTO account_positions(
                symbol,position_side,position_amt,entry_price,break_even_price,
                unrealized_pnl,isolated_wallet,margin_type,leverage,
                event_time_ms,updated_at,source
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(symbol,position_side) DO UPDATE SET
                position_amt=excluded.position_amt,entry_price=excluded.entry_price,
                break_even_price=excluded.break_even_price,
                unrealized_pnl=excluded.unrealized_pnl,
                isolated_wallet=excluded.isolated_wallet,margin_type=excluded.margin_type,
                leverage=COALESCE(excluded.leverage,account_positions.leverage),
                event_time_ms=excluded.event_time_ms,updated_at=excluded.updated_at,
                source=excluded.source
            WHERE excluded.event_time_ms >= account_positions.event_time_ms""",
            (
                symbol, side, _float(row.get("pa", row.get("positionAmt"))),
                _float(row.get("ep", row.get("entryPrice"))),
                (_float(row.get("bep", row.get("breakEvenPrice")))
                 if row.get("bep", row.get("breakEvenPrice")) is not None else None),
                _float(row.get("up", row.get("unRealizedProfit"))),
                _float(row.get("iw", row.get("isolatedWallet"))),
                str(row.get("mt", row.get("marginType")) or ""),
                _int(row.get("l", row.get("leverage"))), event_ms, updated_at, source,
            ),
        )

    def replay_pending(self) -> int:
        rows = self.db.fetch_all(
            "SELECT event_key,payload_json,source FROM exchange_events "
            "WHERE status='RECEIVED' AND event_type='ACCOUNT_UPDATE' ORDER BY id")
        applied = 0
        for row in rows:
            try:
                event = normalize_user_event(
                    json.loads(row["payload_json"]), source=str(row["source"]))
                if self.apply_event(event):
                    from exchange.event_inbox import ExchangeEventInbox
                    ExchangeEventInbox(self.db).mark(str(row["event_key"]), "APPLIED")
                    applied += 1
            except Exception as exc:
                self.last_error = type(exc).__name__
        return applied

    def reconcile_broker(self, broker: Any) -> dict[str, Any]:
        """比较REST真值与事件镜像，记录差异后用REST快照纠偏。"""
        try:
            account = broker.account(force=True)
            positions = broker.positions(force=True)
            result = self.reconcile_snapshot(account, positions)
            self.last_error = None
            return result
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise

    def reconcile_snapshot(self, account: dict[str, Any],
                           positions: list[dict[str, Any]]) -> dict[str, Any]:
        event_ms = int(time.time() * 1000)
        mirror_rows = self.db.fetch_all(
            "SELECT symbol,position_side,position_amt FROM account_positions "
            "WHERE ABS(position_amt)>1e-12")
        mirror = {(str(r["symbol"]), str(r["position_side"])): _float(r["position_amt"])
                  for r in mirror_rows}
        rest: dict[tuple[str, str], float] = {}
        valid_positions: list[dict[str, Any]] = []
        for row in positions or []:
            symbol = str(row.get("symbol") or "").upper()
            if not self._allowed(symbol):
                self.out_of_scope += 1
                continue
            amount = _float(row.get("positionAmt"))
            if abs(amount) <= 1e-12:
                continue
            side = self._position_side(row)
            rest[(symbol, side)] = amount
            valid_positions.append(row)

        initialized = not bool(self.db.fetch_one("SELECT asset FROM account_balances LIMIT 1"))
        missing_mirror = sorted(f"{s}:{p}" for (s, p) in rest.keys() - mirror.keys())
        missing_rest = sorted(f"{s}:{p}" for (s, p) in mirror.keys() - rest.keys())
        qty_mismatch = sorted(
            f"{s}:{p}" for (s, p) in rest.keys() & mirror.keys()
            if abs(rest[(s, p)] - mirror[(s, p)]) > 1e-9)
        balance = self.db.fetch_one(
            "SELECT wallet_balance FROM account_balances WHERE asset='USDT'")
        rest_wallet = _float(account.get("totalWalletBalance"))
        mirror_wallet = _float(balance["wallet_balance"]) if balance else None
        wallet_delta = (rest_wallet - mirror_wallet) if mirror_wallet is not None else None
        matched = not (missing_mirror or missing_rest or qty_mismatch) and (
            wallet_delta is None or abs(wallet_delta) <= 1e-8)
        details = {
            "missing_mirror": missing_mirror, "missing_rest": missing_rest,
            "qty_mismatch": qty_mismatch,
        }
        now = utc_now()
        with self.db.lock:
            self.db.conn.execute(
                """INSERT INTO mirror_reconciliations(
                    checked_at,environment,initialized,matched,missing_mirror,
                    missing_rest,qty_mismatch,wallet_delta,details_json
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (now, self.environment, int(initialized), int(matched or initialized),
                 len(missing_mirror), len(missing_rest), len(qty_mismatch), wallet_delta,
                 json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
            )
            # REST是最终真值：先把镜像中已消失的仓位归零，再写入当前非零仓位。
            self.db.conn.execute(
                "UPDATE account_positions SET position_amt=0,updated_at=?,source='rest_reconcile',"
                "event_time_ms=? WHERE ABS(position_amt)>1e-12", (now, event_ms))
            assets = account.get("assets") if isinstance(account.get("assets"), list) else []
            usdt = next((a for a in assets if str(a.get("asset") or "").upper() == "USDT"), None)
            balance_row = dict(usdt or {
                "walletBalance": account.get("totalWalletBalance"),
                "crossWalletBalance": account.get("totalCrossWalletBalance"),
                "availableBalance": account.get("availableBalance"),
                "unrealizedProfit": account.get("totalUnrealizedProfit"),
            })
            self._upsert_balance(balance_row, event_ms, now, "rest_reconcile")
            for row in valid_positions:
                self._upsert_position(row, event_ms, now, "rest_reconcile")
            self.db.conn.commit()
        self.last_reconcile_at = time.time()
        return {"initialized": initialized, "matched": bool(matched or initialized), **details,
                "wallet_delta": wallet_delta, "checked_at": now}

    def status(self) -> dict[str, Any]:
        latest = self.db.fetch_one(
            "SELECT * FROM mirror_reconciliations ORDER BY id DESC LIMIT 1")
        positions = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM account_positions WHERE ABS(position_amt)>1e-12")
        return {
            "enabled": True,
            "shadow_only": True,
            "environment": self.environment,
            "last_event_at": self.last_event_at,
            "last_reconcile_at": self.last_reconcile_at,
            "last_error": self.last_error,
            "events_applied": self.events_applied,
            "out_of_scope": self.out_of_scope,
            "open_positions": int(positions["count"] if positions else 0),
            "last_reconciliation": dict(latest) if latest else None,
        }
