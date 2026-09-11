"""纸面执行 Agent：双虚拟账本（策略 60% / AI 40%）+ 限价单队列。

权益口径（逐仓）：
- book equity = 可用现金 + 持仓保证金 + 未实现盈亏
- LONG upnl = qty*(price-entry)；SHORT upnl = qty*(entry-price)
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase, utc_now
from data.market_query import get_live_prices
from agents.models import RiskVerdict, TradeDecision


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class PaperBroker:
    """本地纸面成交 Broker，用于无交易所密钥的功能验证。

    状态持久化到项目根派生的文件，并模拟 strategy/ai 双账本；它不应与 demo/live 共用
    资金状态文件，也不能作为交易所真实成交、手续费或滑点的准确替代。
    """
    def __init__(self, state_file: Path, initial_equity: float = 200.0,
                 strategy_weight: float = 0.6, ai_weight: float = 0.4):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.strategy_weight = float(strategy_weight)
        self.ai_weight = float(ai_weight)
        if self.state_file.exists():
            try:
                self._state = json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                self._state = {}
            if "books" not in self._state:
                # 旧状态迁移：旧 orchestrator 的仓位全部归 AI 通道，然后重平衡
                old_positions = self._state.get("positions", []) or []
                for pos in old_positions:
                    pos.setdefault("source", "ai")
                    pos.setdefault("margin_mode", "ISOLATED")
                old_cash = float(self._state.get("cash", initial_equity))
                self._state = {
                    "initial_equity": float(self._state.get("initial_equity", initial_equity)),
                    "books": {
                        "strategy": self._empty_book(0.0),
                        "ai": {"cash": old_cash, "initial_equity": old_cash,
                               "day_start_equity": old_cash,
                               "positions": old_positions, "pending_orders": []},
                    },
                }
                self._save()
        else:
            self._state = {"initial_equity": float(initial_equity), "books": {}}
            self.reset_capital(float(initial_equity))

    @staticmethod
    def _empty_book(cash: float) -> dict[str, Any]:
        return {"cash": float(cash), "initial_equity": float(cash),
                "day_start_equity": float(cash), "positions": [], "pending_orders": []}

    def reset_capital(self, initial_equity: float | None = None) -> None:
        total = float(initial_equity or self._state.get("initial_equity", 200.0))
        s = total * self.strategy_weight
        a = total * self.ai_weight
        self._state = {
            "initial_equity": total,
            "books": {"strategy": self._empty_book(s), "ai": self._empty_book(a)},
        }
        self._save()

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    @property
    def mode(self) -> str:
        return "paper"

    def _book(self, source: str) -> dict[str, Any]:
        source = "strategy" if source == "strategy" else "ai"
        return self._state["books"][source]

    def _book_equity(self, book: dict[str, Any], prices: dict[str, float]) -> float:
        eq = float(book.get("cash", 0.0))
        for pos in book.get("positions", []):
            price = float(prices.get(pos["symbol"], pos.get("entry_price", 0.0)))
            qty = float(pos["qty"])
            entry = float(pos["entry_price"])
            margin = float(pos.get("margin", 0.0))
            if pos["side"] == "LONG":
                upnl = qty * (price - entry)
            else:
                upnl = qty * (entry - price)
            eq += margin + upnl
        return eq

    def equity(self, prices: dict[str, float] | None = None, source: str | None = None) -> float:
        prices = prices or {}
        if source is not None:
            return self._book_equity(self._book(source), prices)
        return self._book_equity(self._book("strategy"), prices) + self._book_equity(self._book("ai"), prices)

    @property
    def state(self) -> dict[str, Any]:
        books = self._state.get("books", {})
        positions: list[dict[str, Any]] = []
        for book in books.values():
            positions.extend(book.get("positions", []))
        return {
            "cash": sum(float(b.get("cash", 0.0)) for b in books.values()),
            "initial_equity": float(self._state.get("initial_equity", 0.0)),
            "positions": positions,
            "books": books,
        }

    def _find_position(self, symbol: str) -> tuple[str, dict[str, Any]] | None:
        for source, book in self._state.get("books", {}).items():
            for pos in book.get("positions", []):
                if pos["symbol"] == symbol:
                    return source, pos
        return None

    def rebalance_books(self, prices: dict[str, float] | None = None,
                        db: AuditDatabase | None = None) -> dict[str, float]:
        """按总权益 6:4 重平衡两个账本；只动现金，不强制平仓。"""
        prices = prices or {}
        books = self._state["books"]
        eq_s = self._book_equity(books["strategy"], prices)
        eq_a = self._book_equity(books["ai"], prices)
        total = eq_s + eq_a
        target_s = total * self.strategy_weight
        target_a = total * self.ai_weight
        transfer_a = target_a - eq_a
        books["ai"]["cash"] = float(books["ai"]["cash"]) + transfer_a
        books["strategy"]["cash"] = float(books["strategy"]["cash"]) - transfer_a
        books["ai"]["day_start_equity"] = target_a
        books["strategy"]["day_start_equity"] = target_s
        books["ai"]["initial_equity"] = target_a
        books["strategy"]["initial_equity"] = target_s
        self._save()
        if db:
            db.log_risk_event(event_type="channel_daily_rebalance", severity="info",
                              details=f"total={total:.2f} strategy={eq_s:.2f}->{target_s:.2f} "
                                      f"ai={eq_a:.2f}->{target_a:.2f} transfer_ai={transfer_a:+.2f}",
                              action_taken="rebalance_cash_only")
        return {"total": total, "strategy": target_s, "ai": target_a,
                "transfer_ai": transfer_a}


    def _realized_pnl(self, pos: dict[str, Any], close_qty: float, price: float) -> float:
        if pos["side"] == "LONG":
            return close_qty * (price - float(pos["entry_price"]))
        return close_qty * (float(pos["entry_price"]) - price)

    def _record_outcome(self, db: AuditDatabase, source: str, pos: dict[str, Any],
                        close_price: float, close_qty: float, pnl: float,
                        exit_reason: str, status: str = "CLOSED") -> None:
        """平仓归因：把最终盈亏连回当初的 open intent 与 thesis（invalidation）。

        归因键：symbol + side + source 的最近一条 approved open intent。
        """
        try:
            row = db.fetch_latest_open_intent(str(pos.get("symbol")), str(pos.get("side")), source)
            entry = float(pos.get("entry_price") or 0.0)
            holding = None
            opened_at = pos.get("opened_at")
            if opened_at:
                opened_dt = _parse_time(str(opened_at))
                if opened_dt is not None:
                    holding = max(0, int((_now() - opened_dt).total_seconds() // 60))
            notional = entry * float(close_qty)
            intent_raw: dict[str, Any] = {}
            if row and row["raw_json"]:
                try:
                    intent_raw = json.loads(row["raw_json"] or "{}")
                except Exception:
                    intent_raw = {}
            db.log_trade_outcome(
                outcome_id=f"oc-{uuid.uuid4()}",
                intent_id=row["intent_id"] if row else None,
                decision_id=row["decision_id"] if row else None,
                symbol=str(pos.get("symbol")), side=str(pos.get("side")),
                source=source,
                strategy_id=intent_raw.get("strategy_id") or pos.get("strategy_id"),
                ticket_id=intent_raw.get("ticket_id") or pos.get("ticket_id"),
                confidence=intent_raw.get("confidence"),
                entry_price=entry,
                exit_price=float(close_price),
                qty=float(close_qty),
                pnl=float(pnl),
                pnl_pct=(float(pnl) / notional * 100) if notional else None,
                holding_minutes=holding,
                expected_holding_minutes=pos.get("expected_holding_minutes"),
                invalidation=row["invalidation"] if row else None,
                exit_reason=exit_reason,
                status=status,
                opened_at=opened_at,
                closed_at=utc_now(),
            )
        except Exception as e:
            db.log_risk_event(event_type="outcome_log_error", severity="warn",
                              details=f"{type(e).__name__}: {e}", action_taken="skip outcome")

    def _log_order(self, db: AuditDatabase, intent: Any, price: float, qty: float,
                   margin: float = 0.0, action: str = "open", pnl: float | None = None,
                   status: str = "FILLED", order_id: str | None = None) -> str:
        oid = order_id or getattr(intent, "client_order_id", None) or f"paper-{uuid.uuid4()}"
        order_source = str(getattr(intent, "order_source", "ai") or "ai")
        raw = {"mode": "paper", "margin": margin, "action": action,
               "trade_level": getattr(intent, "trade_level", "short"),
               "order_source": order_source,
               "strategy_id": getattr(intent, "strategy_id", None),
               "ticket_id": getattr(intent, "ticket_id", None),
               "margin_mode": getattr(intent, "margin_mode", "ISOLATED"),
               "limit_price": getattr(intent, "limit_price", None)}
        if pnl is not None:
            raw["pnl"] = pnl
        db.log_order(client_order_id=oid, exchange_order_id=oid, symbol=intent.symbol,
                     side=intent.side, order_type=getattr(intent, "order_type", "MARKET"),
                     qty=qty, price=price, status=status, raw=raw,
                     order_source=order_source,
                     strategy_id=getattr(intent, "strategy_id", None),
                     ticket_id=getattr(intent, "ticket_id", None),
                     margin_mode=str(getattr(intent, "margin_mode", "ISOLATED")))
        return oid

    def execute(self, decision: TradeDecision, verdict: RiskVerdict, prices: dict[str, float],
                risk_config: dict[str, Any], db: AuditDatabase) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        if getattr(self, "require_pipeline_authorization", False):
            from core.decision_pipeline import filter_unauthorized_opens
            decision, verdict = filter_unauthorized_opens(
                decision, verdict, db, getattr(self, "_pipeline_secret", None))
        # 每轮先处理待成交限价单（成交/过期），即使本轮 no_trade 也要处理；
        # 市场快照里可能没有挂单 symbol，这里实时补齐。
        pending_symbols = []
        for book in self._state.get("books", {}).values():
            for order in book.get("pending_orders", []):
                pending_symbols.append(str(order["symbol"]))
        missing_pending = [s for s in pending_symbols if s not in prices]
        if missing_pending:
            try:
                prices = {**prices, **get_live_prices(missing_pending)}
            except Exception:
                pass
        receipts.extend(self._process_pending_orders(prices, risk_config, db))
        if decision.decision == "no_trade":
            self._save()
            if receipts:
                self._snapshot(db, prices)
            return receipts
        if verdict.verdict not in ("approved", "reduced"):
            db.log_risk_event(event_type="paper_skip", severity="warn",
                              details=f"风控未批准，不执行: {verdict.verdict}")
            self._save()
            return receipts
        intents = verdict.adjusted_intents if verdict.adjusted_intents else decision.intents
        for intent in intents:
            price = prices.get(intent.symbol)
            if not price:
                db.log_risk_event(event_type="paper_reject_no_price", severity="warn",
                                  details=f"{intent.symbol} 无最新价格，跳过")
                continue
            source = str(getattr(intent, "order_source", "ai") or "ai")
            if getattr(intent, "client_order_id", None):
                old_order = db.fetch_one(
                    "SELECT status FROM orders WHERE client_order_id=?",
                    (str(intent.client_order_id),),
                )
                if old_order:
                    receipts.append({"order_id": intent.client_order_id, "action": "duplicate_skipped",
                                     "symbol": intent.symbol, "source": source,
                                     "existing_status": old_order["status"]})
                    continue
            existing = self._find_position(intent.symbol)
            action = getattr(intent, "action", None) or decision.decision
            if action == "open":
                receipt = self._open(intent, float(price), source, existing, risk_config, db)
            elif action in ("close", "reduce"):
                receipt = self._close_or_reduce(intent, float(price), existing, db)
            elif action == "adjust":
                receipt = self._adjust(intent, existing, db)
            else:
                receipt = None
            if receipt:
                receipts.append(receipt)
        self._save()
        if receipts:
            self._snapshot(db, prices)
        return receipts

    def _margin_available(self, book: dict[str, Any], risk_config: dict[str, Any]) -> float:
        usage = float(risk_config.get("channel_max_margin_usage",
                                      risk_config.get("max_margin_usage", 0.8)))
        return float(book.get("cash", 0.0)) * usage

    def _open(self, intent: Any, price: float, source: str,
              existing: tuple[str, dict[str, Any]] | None,
              risk_config: dict[str, Any], db: AuditDatabase) -> dict[str, Any] | None:
        if intent.stop_loss is None or float(intent.stop_loss) <= 0                 or intent.take_profit is None or float(intent.take_profit) <= 0:
            db.log_risk_event(event_type="paper_reject_bare", severity="warn",
                              details=f"{intent.symbol} 新开仓必须同时设置初始止损和止盈，跳过")
            return None
        if intent.order_type == "LIMIT":
            return self._queue_limit(intent, price, source, existing, risk_config, db)
        if existing is not None:
            old_source, old_pos = existing
            if old_pos["side"] != intent.side:
                db.log_risk_event(event_type="paper_reject_conflict", severity="warn",
                                  details=f"{intent.symbol} 已有反向仓位，拒绝")
                return None
            if old_source != source:
                db.log_risk_event(event_type="paper_reject_conflict", severity="warn",
                                  details=f"{intent.symbol} 已被 {old_source} 通道持有，{source} 通道拒绝")
                return None
        return self._fill_open(intent, price, source, existing, db, risk_config)

    def _fill_open(self, intent: Any, price: float, source: str,
                   existing: tuple[str, dict[str, Any]] | None,
                   db: AuditDatabase,
                   risk_config: dict[str, Any] | None = None) -> dict[str, Any] | None:
        book = self._book(source)
        notional = float(intent.qty) * float(price)
        margin = notional / int(intent.leverage)
        if margin > self._margin_available(book, risk_config or {}):
            db.log_risk_event(event_type="paper_reject_margin", severity="warn",
                              details=f"{intent.symbol} {source} 保证金{margin:.2f}超过账本可用上限")
            return None
        if existing is not None:
            old_source, old_pos = existing
            if old_source == source and old_pos["side"] == intent.side:
                old_qty = float(old_pos["qty"])
                new_qty = old_qty + float(intent.qty)
                old_pos["entry_price"] = (old_qty * float(old_pos["entry_price"])
                                          + float(intent.qty) * price) / new_qty
                old_pos["qty"] = new_qty
                old_pos["margin"] = float(old_pos.get("margin", 0.0)) + margin
                old_pos["stop_loss"] = intent.stop_loss
                old_pos["take_profit"] = intent.take_profit
                old_pos["open_client_order_id"] = getattr(intent, "client_order_id", None)
                old_pos["margin_mode"] = str(getattr(intent, "margin_mode", "ISOLATED"))
            else:
                db.log_risk_event(event_type="paper_reject_conflict", severity="warn",
                                  details=f"{intent.symbol} 通道冲突，拒绝开仓")
                return None
        else:
            book.setdefault("positions", []).append({
                "symbol": intent.symbol, "side": intent.side, "qty": float(intent.qty),
                "entry_price": float(price), "leverage": int(intent.leverage), "margin": margin,
                "stop_loss": intent.stop_loss, "take_profit": intent.take_profit,
                "source": source,
                "strategy_id": getattr(intent, "strategy_id", None),
                "ticket_id": getattr(intent, "ticket_id", None),
                "open_client_order_id": getattr(intent, "client_order_id", None),
                "margin_mode": str(getattr(intent, "margin_mode", "ISOLATED")),
                "opened_at": utc_now(),
                "expected_holding_minutes": int(getattr(intent, "expected_holding_minutes", 60) or 60),
            })
        book["cash"] = float(book["cash"]) - margin
        oid = self._log_order(db, intent, price, float(intent.qty), margin)
        if getattr(intent, "ticket_id", None):
            db.update_signal_ticket(ticket_id=str(intent.ticket_id), status="filled")
        db.log_position(symbol=intent.symbol, side=intent.side, qty=float(intent.qty),
                        entry_price=float(price), leverage=int(intent.leverage),
                        stop_loss=intent.stop_loss, take_profit=intent.take_profit,
                        status="OPEN", source=source,
                        strategy_id=getattr(intent, "strategy_id", None),
                        ticket_id=getattr(intent, "ticket_id", None),
                        margin_mode=str(getattr(intent, "margin_mode", "ISOLATED")))
        return {"order_id": oid, "action": "open", "symbol": intent.symbol, "side": intent.side,
                "qty": intent.qty, "price": price, "source": source}

    def _queue_limit(self, intent: Any, price: float, source: str,
                     existing: tuple[str, dict[str, Any]] | None,
                     risk_config: dict[str, Any], db: AuditDatabase) -> dict[str, Any] | None:
        limit_price = float(getattr(intent, "limit_price", 0.0) or 0.0)
        if limit_price <= 0:
            db.log_risk_event(event_type="paper_reject_limit_price", severity="warn",
                              details=f"{intent.symbol} 限价单缺少 limit_price")
            return None
        crossed = (intent.side == "LONG" and price <= limit_price) or                   (intent.side == "SHORT" and price >= limit_price)
        if crossed:
            return self._fill_open(intent, limit_price, source, existing, db, risk_config)
        if existing is not None:
            db.log_risk_event(event_type="paper_reject_conflict", severity="warn",
                              details=f"{intent.symbol} 已有仓位，新限价单挂起前拒绝")
            return None
        book = self._book(source)
        if any(o["symbol"] == intent.symbol for o in book.get("pending_orders", [])):
            db.log_risk_event(event_type="paper_reject_duplicate_limit", severity="warn",
                              details=f"{intent.symbol} 已有同 symbol 挂单，拒绝重复挂单")
            return None
        margin_est = float(intent.qty) * limit_price / int(intent.leverage)
        if margin_est > self._margin_available(book, risk_config):
            db.log_risk_event(event_type="paper_reject_margin", severity="warn",
                              details=f"{intent.symbol} 挂单保证金预计超过账本上限")
            return None
        order = {
            "order_id": getattr(intent, "client_order_id", None) or f"paper-{uuid.uuid4()}",
            "symbol": intent.symbol, "side": intent.side, "qty": float(intent.qty),
            "limit_price": limit_price, "leverage": int(intent.leverage),
            "stop_loss": intent.stop_loss, "take_profit": intent.take_profit,
            "source": source, "strategy_id": getattr(intent, "strategy_id", None),
            "ticket_id": getattr(intent, "ticket_id", None),
            "margin_mode": str(getattr(intent, "margin_mode", "ISOLATED")),
            "trade_level": getattr(intent, "trade_level", "short"),
            "expected_holding_minutes": getattr(intent, "expected_holding_minutes", 60),
            "created_at": utc_now(), "expires_at": getattr(intent, "limit_expires_at", None),
        }
        book.setdefault("pending_orders", []).append(order)
        oid = self._log_order(db, intent, limit_price, float(intent.qty),
                              margin_est, action="open_limit_queued", status="NEW",
                              order_id=order["order_id"])
        return {"order_id": oid, "action": "open_queued", "symbol": intent.symbol,
                "side": intent.side, "qty": intent.qty, "limit_price": limit_price,
                "source": source}

    def _process_pending_orders(self, prices: dict[str, float],
                                risk_config: dict[str, Any], db: AuditDatabase) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        now = _now()
        for source, book in self._state.get("books", {}).items():
            remaining: list[dict[str, Any]] = []
            for order in book.get("pending_orders", []):
                price = float(prices.get(order["symbol"], 0.0) or 0.0)
                if price <= 0:
                    remaining.append(order)
                    continue
                expired = _parse_time(order.get("expires_at"))
                crossed = (order["side"] == "LONG" and price <= float(order["limit_price"])) or                           (order["side"] == "SHORT" and price >= float(order["limit_price"]))
                if expired is not None and expired <= now:
                    db.update_order(client_order_id=order["order_id"], status="CANCELED")
                    receipts.append({"order_id": order["order_id"], "action": "cancel_expired",
                                     "symbol": order["symbol"], "source": source})
                    continue
                if crossed:
                    receipt = self._fill_pending_order(order, float(order["limit_price"]), source, db, risk_config)
                    if receipt:
                        receipts.append(receipt)
                else:
                    remaining.append(order)
            book["pending_orders"] = remaining
        return receipts

    def _fill_pending_order(self, order: dict[str, Any], fill_price: float,
                            source: str, db: AuditDatabase,
                            risk_config: dict[str, Any] | None = None) -> dict[str, Any] | None:
        book = self._book(source)
        existing = self._find_position(order["symbol"])
        if existing is not None:
            db.update_order(client_order_id=order["order_id"], status="CANCELED")
            return {"order_id": order["order_id"], "action": "cancel_conflict",
                    "symbol": order["symbol"], "source": source}
        qty = float(order["qty"])
        margin = qty * fill_price / int(order["leverage"])
        if margin > self._margin_available(book, risk_config or {}):
            db.update_order(client_order_id=order["order_id"], status="CANCELED")
            return {"order_id": order["order_id"], "action": "cancel_margin",
                    "symbol": order["symbol"], "source": source}
        book["cash"] = float(book["cash"]) - margin
        book.setdefault("positions", []).append({
            "symbol": order["symbol"], "side": order["side"], "qty": qty,
            "entry_price": fill_price, "leverage": int(order["leverage"]), "margin": margin,
            "stop_loss": order.get("stop_loss"), "take_profit": order.get("take_profit"),
            "source": source, "strategy_id": order.get("strategy_id"),
            "ticket_id": order.get("ticket_id"),
            "open_client_order_id": order.get("order_id"),
            "margin_mode": order.get("margin_mode", "ISOLATED"),
            "opened_at": utc_now(),
        })
        db.update_order(client_order_id=order["order_id"], status="FILLED", price=fill_price)
        if order.get("ticket_id"):
            db.update_signal_ticket(ticket_id=str(order["ticket_id"]), status="filled")
        db.log_position(symbol=order["symbol"], side=order["side"], qty=qty,
                        entry_price=fill_price, leverage=int(order["leverage"]),
                        stop_loss=order.get("stop_loss"), take_profit=order.get("take_profit"),
                        status="OPEN", source=source,
                        strategy_id=order.get("strategy_id"),
                        ticket_id=order.get("ticket_id"),
                        margin_mode=order.get("margin_mode", "ISOLATED"))
        return {"order_id": order["order_id"], "action": "open_queued_filled",
                "symbol": order["symbol"], "side": order["side"], "qty": qty,
                "price": fill_price, "source": source}

    def _close_or_reduce(self, intent: Any, price: float,
                         existing: tuple[str, dict[str, Any]] | None,
                         db: AuditDatabase) -> dict[str, Any] | None:
        if existing is None:
            db.log_risk_event(event_type="paper_close_nopos", severity="warn",
                              details=f"{intent.symbol} 无仓位可平")
            return None
        source, pos = existing
        book = self._book(source)
        close_qty = min(float(intent.qty), float(pos["qty"]))
        pnl = self._realized_pnl(pos, close_qty, float(price))
        released = float(pos.get("margin", 0.0)) * (close_qty / float(pos["qty"]))
        pos["qty"] = float(pos["qty"]) - close_qty
        pos["margin"] = float(pos.get("margin", 0.0)) - released
        book["cash"] = float(book["cash"]) + released + pnl
        if float(pos["qty"]) <= 1e-12:
            book["positions"].remove(pos)
            status = "CLOSED"
        else:
            status = "REDUCED"
        oid = self._log_order(db, intent, price, close_qty, action="close", pnl=pnl)
        self._record_outcome(db, source, pos, float(price), close_qty, pnl,
                             exit_reason="ai_decision", status=status)
        db.log_position(symbol=intent.symbol, side=pos["side"], qty=-close_qty,
                        entry_price=float(price), leverage=int(pos.get("leverage", 1)),
                        stop_loss=None, take_profit=None, status=status,
                        source=source, strategy_id=pos.get("strategy_id"),
                        ticket_id=pos.get("ticket_id"),
                        margin_mode=pos.get("margin_mode", "ISOLATED"))
        return {"order_id": oid, "action": "close", "symbol": intent.symbol,
                "side": pos["side"], "qty": close_qty, "price": price, "pnl": pnl,
                "source": source}

    def _adjust(self, intent: Any, existing: tuple[str, dict[str, Any]] | None,
                db: AuditDatabase) -> dict[str, Any] | None:
        if existing is None:
            return None
        source, pos = existing
        if intent.stop_loss is not None:
            pos["stop_loss"] = intent.stop_loss
        if intent.take_profit is not None:
            pos["take_profit"] = intent.take_profit
        db.log_risk_event(event_type="paper_adjust", severity="info",
                          details=f"{intent.symbol} {source} 调整止损/止盈")
        return {"action": "adjust", "symbol": intent.symbol,
                "stop_loss": intent.stop_loss, "take_profit": intent.take_profit,
                "source": source}

    def close_all(self, prices: dict[str, float], db: AuditDatabase) -> list[dict[str, Any]]:
        receipts = []
        for source, book in self._state.get("books", {}).items():
            for pos in list(book.get("positions", [])):
                price = float(prices.get(pos["symbol"], pos["entry_price"]))
                pnl = self._realized_pnl(pos, float(pos["qty"]), price)
                margin = float(pos.get("margin", 0.0))
                book["cash"] = float(book["cash"]) + margin + pnl
                oid = f"paper-{uuid.uuid4()}"
                db.log_order(client_order_id=oid, exchange_order_id=oid, symbol=pos["symbol"],
                             side=pos["side"], order_type="MARKET", qty=float(pos["qty"]),
                             price=price, status="FILLED",
                             raw={"mode": "paper", "action": "close_all",
                                  "trade_level": pos.get("trade_level", "short"), "pnl": pnl},
                             order_source=source, strategy_id=pos.get("strategy_id"),
                             ticket_id=pos.get("ticket_id"),
                             margin_mode=pos.get("margin_mode", "ISOLATED"))
                db.log_position(symbol=pos["symbol"], side=pos["side"], qty=-float(pos["qty"]),
                                entry_price=price, leverage=int(pos.get("leverage", 1)),
                                stop_loss=None, take_profit=None, status="CLOSED",
                                source=source, strategy_id=pos.get("strategy_id"),
                                ticket_id=pos.get("ticket_id"),
                                margin_mode=pos.get("margin_mode", "ISOLATED"))
                self._record_outcome(db, source, pos, float(price), float(pos["qty"]), pnl,
                                     exit_reason="close_all")
                receipts.append({"order_id": oid, "action": "close_all", "symbol": pos["symbol"],
                                 "qty": pos["qty"], "price": price, "pnl": pnl, "source": source})
            book["positions"] = []
            book["pending_orders"] = []
        self._save()
        if receipts:
            self._snapshot(db, prices)
        return receipts

    def _snapshot(self, db: AuditDatabase, prices: dict[str, float]) -> None:
        eq = self.equity(prices)
        books = self._state.get("books", {})
        upnl = 0.0
        for book in books.values():
            for pos in book.get("positions", []):
                price = float(prices.get(pos["symbol"], pos.get("entry_price", 0.0)))
                qty = float(pos["qty"])
                entry = float(pos["entry_price"])
                if pos["side"] == "LONG":
                    upnl += qty * (price - entry)
                else:
                    upnl += qty * (entry - price)
        cash = sum(float(b.get("cash", 0.0)) for b in books.values())
        margin = self.margin_used()
        db.log_portfolio_snapshot(equity=eq, margin_balance=cash + margin,
                                  available_balance=cash,
                                  unrealized_pnl=upnl,
                                  margin_used=margin,
                                  raw={"mode": "paper",
                                       "strategy_equity": self.equity(prices, source="strategy"),
                                       "ai_equity": self.equity(prices, source="ai")})

    def margin_used(self, source: str | None = None) -> float:
        total = 0.0
        books = [source] if source else list(self._state.get("books", {}).keys())
        for s in books:
            if s in self._state.get("books", {}):
                for pos in self._state["books"][s].get("positions", []):
                    total += float(pos.get("margin", 0.0))
        return total

    def close_source(self, source: str, prices: dict[str, float],
                     db: AuditDatabase) -> list[dict[str, Any]]:
        receipts = []
        book = self._book(source)
        for pos in list(book.get("positions", [])):
            price = float(prices.get(pos["symbol"], pos["entry_price"]))
            pnl = self._realized_pnl(pos, float(pos["qty"]), price)
            margin = float(pos.get("margin", 0.0))
            book["cash"] = float(book["cash"]) + margin + pnl
            oid = f"paper-{uuid.uuid4()}"
            db.log_order(client_order_id=oid, exchange_order_id=oid, symbol=pos["symbol"],
                         side=pos["side"], order_type="MARKET", qty=float(pos["qty"]),
                         price=price, status="FILLED",
                         raw={"mode": "paper", "action": "close_source", "pnl": pnl},
                         order_source=source, strategy_id=pos.get("strategy_id"),
                         ticket_id=pos.get("ticket_id"),
                         margin_mode=pos.get("margin_mode", "ISOLATED"))
            db.log_position(symbol=pos["symbol"], side=pos["side"], qty=-float(pos["qty"]),
                            entry_price=price, leverage=int(pos.get("leverage", 1)),
                            stop_loss=None, take_profit=None, status="CLOSED",
                            source=source, strategy_id=pos.get("strategy_id"),
                            ticket_id=pos.get("ticket_id"),
                            margin_mode=pos.get("margin_mode", "ISOLATED"))
            self._record_outcome(db, source, pos, float(price), float(pos["qty"]), pnl,
                                 exit_reason="close_source")
            receipts.append({"order_id": oid, "action": "close_source", "symbol": pos["symbol"],
                             "qty": pos["qty"], "price": price, "pnl": pnl, "source": source})
        book["positions"] = []
        book["pending_orders"] = []
        self._save()
        return receipts
