"""会话资金管理：固定 200U 起始；会话权益亏到阈值则平仓并从 200U 重启。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase


class CapitalManager:
    def __init__(self, state_file: Path, broker: Any, db: AuditDatabase,
                 initial_equity: float = 200.0, bankruptcy_threshold: float = 1.0,
                 reset_on_bankruptcy: bool = True, max_margin_usage: float = 0.80):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.broker = broker
        self.db = db
        self.initial_equity = float(initial_equity)
        self.threshold = float(bankruptcy_threshold)
        self.reset_on_bankruptcy = bool(reset_on_bankruptcy)
        self.max_margin_usage = float(max_margin_usage)
        # baseline 读写锁必须先于 _load_or_init_baseline()：构造期间的兕底
        # 路径会调用 _save() 落盘初始 baseline。用 RLock：重置路径在持锁状态下
        # 更新 self.baseline 后再调 _save()（内部也加锁），同线程重入不能死锁。
        self._save_lock = threading.RLock()
        # 权益兜底属性必须先于 _load_or_init_baseline() 初始化：
        # 非 paper 模式构造期间会调 _exchange_equity()，接口不可用走
        # 兜底路径时读取 _last_exchange_equity，若此时还未定义会直接崩。
        self.last_reset: dict[str, Any] | None = None
        self._last_exchange_equity: float | None = None
        self._equity_stale = False
        self.baseline = self._load_or_init_baseline()

    def _load_or_init_baseline(self) -> float:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if data.get("initial_equity") == self.initial_equity:
                    mode = getattr(self.broker, "mode", "paper")
                    if mode == "paper":
                        return 0.0
                    # baseline >= 0 都是合法基线（0 = 会话起点交易所权益
                    # 恰好等于起始资金，比如测试网重置后钱包就是 500、
                    # 起始资金也是 500）。只有负值或键缺失才需要重算。
                    if "baseline" in data:
                        baseline = float(data["baseline"])
                        if baseline >= 0:
                            return baseline
            except Exception:
                pass
        mode = getattr(self.broker, "mode", "paper")
        exchange_eq = self._exchange_equity() if mode != "paper" else 0.0
        if mode == "paper":
            baseline = 0.0
        elif exchange_eq <= 0:
            # 接口不可用且无缓存：不落盘猜测值（错误基线一旦固化，
            # 接口恢复后 session_equity 会永久偏移），内存里临时用 0，
            # 下次启动接口恢复时再正确固化。
            return 0.0
        else:
            baseline = exchange_eq - self.initial_equity
            if baseline < 0:
                baseline = max(exchange_eq, self.initial_equity) - self.initial_equity
        self._save(baseline)
        return baseline

    def _exchange_equity(self) -> float:
        for _ in range(3):
            try:
                v = float(self.broker.equity())
                if v and v > 0:
                    self._last_exchange_equity = v
                    self._equity_stale = False
                    return v
            except Exception:
                pass
            try:
                state = getattr(self.broker, "state", {})
                if isinstance(state, dict) and "error" not in state:
                    v = float(state.get("initial_equity") or 0.0)
                    if v and v > 0:
                        self._last_exchange_equity = v
                        self._equity_stale = False
                        return v
            except Exception:
                pass
            time.sleep(1.0)
        # 接口暂时不可用时，返回最近一次有效权益，绝不能当成 0/爆仓
        if self._last_exchange_equity and self._last_exchange_equity > 0:
            self._equity_stale = True
            return self._last_exchange_equity
        self._equity_stale = True
        return 0.0

    def _save(self, baseline: float) -> None:
        with self._save_lock:
            tmp = self.state_file.with_name(
                f"{self.state_file.stem}.tmp-{os.getpid()}-{threading.get_ident()}")
            tmp.write_text(json.dumps({
                "initial_equity": self.initial_equity,
                "baseline": baseline,
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_file)

    def session_equity(self) -> float:
        eq = self._exchange_equity()
        if eq <= 0:
            # 接口完全不可用且无缓存时 _exchange_equity() 返回 0。
            # 之前直接算 0 - baseline，会把 baseline 本身当负权益返回
            # （如 -4493.59），喂给 LLM portfolio 和通道资金分配后
            # 引发完全错误的连锁判断。未知时退回起始资金这个中性值。
            return round(self.initial_equity, 2)
        return round(eq - self.baseline, 2)

    def _total_margin(self) -> float:
        try:
            state = self.broker.state
            if isinstance(state, dict):
                total = 0.0
                for p in state.get("positions", []):
                    m = float(p.get("margin") or 0.0)
                    if m <= 0:
                        notional = abs(float(p.get("entry_price") or 0.0) * float(p.get("qty") or 0.0))
                        m = notional / max(1, int(p.get("leverage") or 1))
                    total += m
                return total
        except Exception:
            pass
        return 0.0

    def sync(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        exchange_eq = self._exchange_equity()
        if getattr(self, "_equity_stale", False):
            session_eq = round(exchange_eq - self.baseline, 2) if exchange_eq > 0 else self.session_equity()
            self.db.log_risk_event(event_type="equity_unavailable", severity="warn",
                                   details=f"币安账户权益接口暂时不可用，本轮跳过破产检查和保证金全平检查。最近有效权益={exchange_eq:.2f}",
                                   action_taken="skip capital checks; block new opens")
            return {"session_equity": session_eq, "reset": False,
                    "equity_available": False, "stale": True}
        session_eq = round(exchange_eq - self.baseline, 2)
        # 硬规则：保证金不得超过会话权益上限
        if session_eq > 0:
            total_margin = self._total_margin()
            limit = session_eq * self.max_margin_usage
            if total_margin > limit:
                try:
                    self.broker.close_all(prices or {}, self.db)
                except Exception as e:
                    self.db.log_risk_event(event_type="margin_cap_close_error", severity="error",
                                           details=f"{type(e).__name__}: {e}", action_taken="manual check")
                self.db.log_risk_event(event_type="margin_cap_breach", severity="critical",
                                       details=f"总保证金 {total_margin:.2f} > {limit:.2f}，已全平恢复资金约束",
                                       action_taken="close_all")
                session_eq = self.session_equity()
        result = {"session_equity": session_eq, "reset": False, "equity_available": True}
        if session_eq <= self.threshold and self.reset_on_bankruptcy:
            try:
                self.broker.close_all(prices or {}, self.db)
            except Exception as e:
                self.db.log_risk_event(event_type="capital_reset_close_error", severity="error",
                                       details=f"{type(e).__name__}: {e}", action_taken="continue")
            if getattr(self.broker, "mode", "paper") == "paper" and hasattr(self.broker, "reset_capital"):
                self.broker.reset_capital(self.initial_equity)
            new_baseline = 0.0 if getattr(self.broker, "mode", "paper") == "paper" else (self._exchange_equity() - self.initial_equity)
            self.last_reset = {
                "old_baseline": self.baseline,
                "new_baseline": new_baseline,
                "session_equity_after_reset": self.initial_equity,
            }
            with self._save_lock:
                self.baseline = new_baseline
                self._save(new_baseline)
            self.db.log_risk_event(event_type="capital_reset", severity="critical",
                                   details=f"会话权益 {session_eq:.2f} <= {self.threshold}，"
                                           f"平仓并从 {self.initial_equity:.0f}U 重启",
                                   action_taken="close_all + reset baseline")
            result = {"session_equity": self.initial_equity, "reset": True}
        return result

    def manual_reset(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        """手动重置：平掉全部仓位，资金回到 initial_equity。仅测试模式可用。

        实盘模式下资金是真实的，不存在"重置"——dashboard 端也不显示该按钮。
        """
        mode = getattr(self.broker, "mode", "paper")
        if mode == "live":
            self.db.log_risk_event(event_type="manual_reset_rejected", severity="warn",
                                   details="实盘模式不允许手动重置资金",
                                   action_taken="rejected")
            return {"ok": False, "error": "实盘模式不允许重置资金"}
        try:
            self.broker.close_all(prices or {}, self.db)
        except Exception as e:
            self.db.log_risk_event(event_type="manual_reset_close_error", severity="error",
                                   details=f"{type(e).__name__}: {e}", action_taken="continue")
        if hasattr(self.broker, "reset_capital"):
            self.broker.reset_capital(self.initial_equity)
        # paper：baseline=0，session_equity 直接等于账本现金；
        # demo/testnet：baseline=交易所余额-初始权益，使 session_equity 回到 initial_equity。
        new_baseline = 0.0 if mode == "paper" else (self._exchange_equity() - self.initial_equity)
        self.last_reset = {
            "old_baseline": self.baseline,
            "new_baseline": new_baseline,
            "session_equity_after_reset": self.initial_equity,
            "manual": True,
        }
        with self._save_lock:
            self.baseline = new_baseline
            self._save(new_baseline)
        self.db.log_risk_event(event_type="manual_capital_reset", severity="warn",
                               details=f"手动重置：平掉全部仓位，资金回到 {self.initial_equity:.0f}U",
                               action_taken="close_all + reset")
        return {"ok": True, "session_equity": self.initial_equity}

    def portfolio(self) -> dict[str, Any]:
        state = {}
        try:
            state = dict(self.broker.state)
        except Exception:
            pass
        return {
            "cash": max(0.0, self.session_equity()),
            "session_initial_equity": self.initial_equity,
            "session_equity": self.session_equity(),
            "positions": state.get("positions", []),
            "exchange_mode": getattr(self.broker, "mode", "unknown"),
        }
