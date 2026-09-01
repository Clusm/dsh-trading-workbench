"""通道资金管理器：虚拟账本 6:4、UTC 每日重算、AI 当日 -30% 熔断、分通道保证金上限。"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class ChannelManager:
    def __init__(self, state_file: Path, broker: Any, db: AuditDatabase,
                 strategy_weight: float = 0.6, ai_weight: float = 0.4,
                 channel_max_margin_usage: float = 0.8,
                 ai_daily_loss_stop: float = 0.30):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.broker = broker
        self.db = db
        self.strategy_weight = float(strategy_weight)
        self.ai_weight = float(ai_weight)
        self.channel_max_margin_usage = float(channel_max_margin_usage)
        self.ai_daily_loss_stop = float(ai_daily_loss_stop)
        self.lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"date": None, "strategy_day_start_equity": None,
                "ai_day_start_equity": None, "ai_halted": False,
                "pnl_reset_at": None}

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def reset_day_start(self, session_equity: float) -> None:
        """手动重置资金后，按新的会话权益重算两通道的当日基准。

        否则 channel_state.json 里残留旧 day_start_equity，导致重置后
        通道权益/保证金上限仍显示旧值。
        """
        with self.lock:
            eq_s = float(session_equity) * self.strategy_weight
            eq_a = float(session_equity) * self.ai_weight
            self.state.update({
                "date": utc_today(),
                "strategy_day_start_equity": eq_s,
                "ai_day_start_equity": eq_a,
                "ai_halted": False,
                # 重置资金后，通道盈亏从此刻重新统计，不再叠加历史平仓盈亏
                "pnl_reset_at": datetime.now(timezone.utc).isoformat(),
            })
            self._save()

    def _channel_day_start(self, channel: str,
                           session_equity: float | None = None) -> float:
        # 会话权益可用时实时计算通道基准（demo/实盘通道本金 = 会话权益 × 权重），
        # 避免同日陈旧的 day_start 残留导致通道资金不随会话权益变化。
        if session_equity is not None and float(session_equity) > 0:
            weight = self.strategy_weight if channel == "strategy" else self.ai_weight
            return float(session_equity) * weight
        stored = self.state.get(f"{channel}_day_start_equity")
        if stored:
            return float(stored)
        if session_equity is None:
            try:
                session_equity = float(self.broker.equity({}) if hasattr(self.broker, "equity") else 0.0)
            except Exception:
                session_equity = 0.0
        weight = self.strategy_weight if channel == "strategy" else self.ai_weight
        return float(session_equity) * weight

    def _channel_unrealized(self, channel: str) -> float:
        """Demo/实盘：按来源台账计算该通道持仓的未实现盈亏。"""
        if not hasattr(self.broker, "positions"):
            return 0.0
        try:
            positions = self.broker.positions()
            ledger = getattr(self.broker, "_load_ledger", lambda: {})().get("positions", {})
            total = 0.0
            for pos in positions or []:
                symbol = str(pos.get("symbol") or "")
                source = str(ledger.get(symbol, {}).get("source", "ai") or "ai")
                if source != channel:
                    continue
                total += float(pos.get("unRealizedProfit") or pos.get("upnl") or 0.0)
            return total
        except Exception:
            return 0.0

    def _channel_pnl(self, channel: str) -> float:
        """该通道今日已实现盈亏（净额，含手续费）。"""
        return float(self.daily_trade_stats(channel).get("realized_pnl", 0.0))

    def _channel_equity(self, prices: dict[str, float], channel: str,
                        session_equity: float | None = None) -> float:
        if hasattr(self.broker, "equity"):
            try:
                value = self.broker.equity(prices, source=channel)
                if value and value > 0:
                    return float(value)
            except TypeError:
                pass
        # Demo/实盘：通道权益 = 当日初始权益 + 本通道已实现净盈亏 + 本通道未实现盈亏
        return (self._channel_day_start(channel, session_equity)
                + self._channel_pnl(channel)
                + self._channel_unrealized(channel))

    def daily_trade_stats(self, channel: str = "ai", date: str | None = None) -> dict[str, Any]:
        """按 UTC 日统计某通道已平仓订单：胜率、平仓笔数、已实现盈亏。

        若手动重置过资金，只统计重置时刻之后的订单（避免历史盈亏重复计入）。
        """
        date = date or utc_today()
        reset_at = self.state.get("pnl_reset_at")
        if reset_at:
            rows = self.db.fetch_all(
                "SELECT created_at, raw_json FROM orders WHERE order_source=? AND status='FILLED' "
                "AND created_at >= ? AND created_at LIKE ? ORDER BY id",
                (channel, reset_at, date + "%"),
            )
        else:
            rows = self.db.fetch_all(
                "SELECT created_at, raw_json FROM orders WHERE order_source=? AND status='FILLED' "
                "AND created_at LIKE ? ORDER BY id",
                (channel, date + "%"),
            )
        pnl = 0.0
        wins = 0
        losses = 0
        fees = 0.0
        for r in rows:
            try:
                raw = json.loads(r["raw_json"] or "{}")
            except Exception:
                raw = {}
            action = str(raw.get("action") or "")
            if action not in ("close", "close_all", "close_source"):
                continue
            trade_pnl = float(raw.get("net_pnl", raw.get("pnl", 0.0)) or 0.0)
            fee = float(raw.get("commission", raw.get("fee", 0.0)) or 0.0)
            pnl += trade_pnl
            fees += fee
            if trade_pnl > 0:
                wins += 1
            elif trade_pnl < 0:
                losses += 1
        closed = wins + losses
        return {
            "date": date,
            "channel": channel,
            "closed_trades": closed,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / closed) if closed else 0.0,
            "realized_pnl": round(pnl, 4),
            "fees": round(fees, 4),
        }

    def _margin_used(self, channel: str) -> float:
        if hasattr(self.broker, "margin_used"):
            try:
                return float(self.broker.margin_used(channel))
            except Exception:
                pass
        return 0.0

    def _rebalance_if_needed(self, prices: dict[str, float],
                             session_equity: float | None = None) -> bool:
        today = utc_today()
        if self.state.get("date") == today:
            return False
        eq_s = self._channel_equity(prices, "strategy", session_equity)
        eq_a = self._channel_equity(prices, "ai", session_equity)
        # 先把上一日统计落库（初始状态不落库）
        if self.state.get("date"):
            for channel, end_eq, start_eq in (
                ("strategy", eq_s, float(self.state.get("strategy_day_start_equity") or eq_s)),
                ("ai", eq_a, float(self.state.get("ai_day_start_equity") or eq_a)),
            ):
                self.db.log_channel_daily_stats(
                    stat_date=str(self.state["date"]), channel=channel,
                    start_equity=start_eq, end_equity=end_eq,
                    realized_pnl=end_eq - start_eq,
                    rebalanced_from=end_eq,
                    rebalanced_to=None,
                    status="closed" if channel == "ai" and self.state.get("ai_halted") else "active",
                )
        rebalanced = False
        if hasattr(self.broker, "rebalance_books"):
            result = self.broker.rebalance_books(prices, self.db)
            eq_s = float(result.get("strategy", eq_s))
            eq_a = float(result.get("ai", eq_a))
            rebalanced = True
        else:
            total = eq_s + eq_a
            if total <= 0 and session_equity and session_equity > 0:
                total = float(session_equity)
            eq_s = total * self.strategy_weight
            eq_a = total * self.ai_weight
        self.state.update({
            "date": today,
            "strategy_day_start_equity": eq_s,
            "ai_day_start_equity": eq_a,
            "ai_halted": False,
        })
        self._save()
        if rebalanced and hasattr(self.broker, "state"):
            self.db.log_daily_rebalance(
                rebalance_date=today, channel="strategy",
                from_equity=float(self.state.get("strategy_day_start_equity") or eq_s),
                to_equity=eq_s, transfer_amount=0.0, reason="daily_60_40")
        return rebalanced

    def sync(self, prices: dict[str, float] | None = None,
             session_equity: float | None = None,
             read_only: bool = False) -> dict[str, Any]:
        with self.lock:
            return self._sync_locked(prices, session_equity, read_only)

    def _sync_locked(self, prices: dict[str, float] | None,
                     session_equity: float | None,
                     read_only: bool) -> dict[str, Any]:
        prices = dict(prices or {})
        rebalanced = False if read_only else self._rebalance_if_needed(prices, session_equity)
        status: dict[str, Any] = {"date": utc_today(), "rebalanced": rebalanced}
        for channel in ("strategy", "ai"):
            eq = self._channel_equity(prices, channel, session_equity)
            day_start = self._channel_day_start(channel, session_equity)
            if day_start <= 0:
                day_start = eq
            # 同步写回基准，保证通道资金随会话权益动态变化
            self.state[f"{channel}_day_start_equity"] = day_start
            margin = self._margin_used(channel)
            cap = eq * self.channel_max_margin_usage
            allow_new = margin <= cap
            status[channel] = {
                "equity": eq,
                "day_start_equity": day_start,
                "day_pnl": eq - day_start,
                "day_return": (eq - day_start) / day_start if day_start else 0.0,
                "margin_used": margin,
                "margin_cap": cap,
                "allow_new": allow_new,
                "halted": False,
            }
        # 通道保证金超限处理：超过上限 5% 时，纸面账本强制平掉该通道全部仓位。
        for channel in ("strategy", "ai"):
            ch = status[channel]
            if not read_only and ch.get("margin_used", 0.0) > ch.get("margin_cap", 0.0) * 1.05                     and hasattr(self.broker, "close_source"):
                try:
                    receipts = self.broker.close_source(channel, prices, self.db)
                    self.db.log_risk_event(
                        event_type="channel_margin_breach", severity="critical",
                        details=f"{channel} 通道保证金 {ch['margin_used']:.2f} 超过上限 {ch['margin_cap']:.2f} 的 105%，"
                                f"已平仓 {len(receipts)} 笔",
                        action_taken="close_source_all")
                    ch = {
                        "equity": self._channel_equity(prices, channel, session_equity),
                        "day_start_equity": ch["day_start_equity"],
                        "day_pnl": 0.0, "day_return": 0.0,
                        "margin_used": self._margin_used(channel),
                        "margin_cap": ch["margin_cap"],
                        "allow_new": False,
                        "halted": ch.get("halted", False),
                    }
                    status[channel] = ch
                except Exception as e:
                    self.db.log_risk_event(event_type="channel_margin_breach", severity="critical",
                                           details=f"{channel} 通道保证金超限但强平失败: {type(e).__name__} {e}",
                                           action_taken="manual attention required")

        # AI 当日 -30% 熔断（UTC 日）
        ai = status["ai"]
        ai_loss_ratio = -ai["day_return"] if ai["day_start_equity"] else 0.0
        if not read_only and ai_loss_ratio >= self.ai_daily_loss_stop and not self.state.get("ai_halted"):
            self.state["ai_halted"] = True
            self._save()
            self.db.log_risk_event(event_type="ai_daily_loss_stop", severity="critical",
                                   details=f"AI账户当日亏损 {ai_loss_ratio:.1%} >= {self.ai_daily_loss_stop:.1%}，"
                                           f"权益 {ai['equity']:.2f}/{ai['day_start_equity']:.2f}",
                                   action_taken="AI通道当日 close_only")
        ai["halted"] = bool(self.state.get("ai_halted"))
        ai["allow_new"] = ai["allow_new"] and not ai["halted"]
        ai["daily_stats"] = self.daily_trade_stats("ai")
        status["strategy"]["daily_stats"] = self.daily_trade_stats("strategy")
        if not read_only:
            self._save()
        return status
