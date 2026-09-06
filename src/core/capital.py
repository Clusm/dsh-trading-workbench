"""会话资金管理：固定起始资金；会话权益亏到阈值则平仓并从起始资金重启。

基线口径：会话权益只反映「交易成果」，与交易所账户里的绝对余额无关——
    session_equity = 交易所权益 - baseline
    baseline      = 锚点时刻交易所权益 - 起始资金
因此 demo 账户自带的钱（本例 5000U）不会计入会话资金，后续充值/提现也不该
被算作交易收益。

历史故障（2026-09-05 修复）：某个以 EXECUTION_MODE=paper 启动的进程触发了
手动重置，paper 分支把 baseline 写成 0 并落盘；而基线状态文件当时不记录
执行模式，后续 demo 进程把 0 当成合法基线继承，会话权益从此等于交易所全部
余额（5028U）。加固要点：
  1. 状态文件记录 mode，跨执行模式绝不复用基线；
  2. 非 paper 模式下 baseline=0 只在「交易所权益恰好等于起始资金」时合法；
  3. 权益接口不可用时拒绝落盘基线（宁可内存临时值，不固化错误值）；
  4. 不再把 paper 账本的配置值当成交易所权益。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase


class CapitalManager:
    """维护交易所绝对权益与本项目会话权益之间的稳定映射。

    状态文件持久化 ``baseline``、执行模式和锚定时间。所有写入采用临时文件替换，
    并由可重入锁保护；交易所权益未知时宁可返回保守口径，也不会固化猜测基线。
    本类还承担破产线与总保证金硬上限检查，触发动作会写入风险审计。
    """
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

    def _mode(self) -> str:
        return str(getattr(self.broker, "mode", "paper") or "paper")

    @property
    def anchor_id(self) -> str:
        """当前资金锚点的稳定标识，供账户级风控切分会话使用。"""
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            anchored_at = str(data.get("anchored_at") or "").strip()
            if anchored_at:
                return f"{self._mode()}:{anchored_at}"
        except Exception:
            pass
        return f"{self._mode()}:{self.initial_equity:.8f}:{self.baseline:.8f}"

    def _baseline_plausible(self, baseline: float, exchange_eq: float) -> bool:
        """基线合理性校验——堵住「基线被清零/写坏后永久继承」这类故障。

        非 paper 模式下 baseline=0 只在一种情况成立：锚点时刻交易所权益恰好
        等于起始资金（例如测试网重置后钱包正好 500）。若交易所权益与起始资金
        存在显著差距而 baseline 却为 0，说明基线已被污染（历史上正是 paper
        分支写入的 0），必须重新锚定而不是照单全收。
        """
        if exchange_eq <= 0:
            # 交易所权益拿不到，无从判断，保守沿用现有基线
            return True
        if baseline < 0:
            return False
        # 会话权益不可能亏穿起始资金的负数区间（真爆仓会先触发重置）
        if exchange_eq - baseline < -self.initial_equity:
            return False
        gap = exchange_eq - self.initial_equity
        if baseline <= 0 and abs(gap) > max(1.0, abs(self.initial_equity) * 0.01):
            return False
        return True

    def _load_or_init_baseline(self) -> float:
        mode = self._mode()
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                # 起始资金被改过（如 200 → 500）或执行模式变了，基线必须重算。
                # mode 不同即视为不同账本，paper 写入的基线绝不能给 demo 用。
                if data.get("initial_equity") == self.initial_equity and data.get("mode") == mode:
                    if mode == "paper":
                        return 0.0
                    if "baseline" in data:
                        baseline = float(data["baseline"])
                        exchange_eq = self._exchange_equity()
                        if self._baseline_plausible(baseline, exchange_eq):
                            return baseline
                        self.db.log_risk_event(
                            event_type="baseline_implausible", severity="critical",
                            details=f"基线失效：baseline={baseline:.2f} 与交易所权益 {exchange_eq:.2f}、"
                                    f"起始资金 {self.initial_equity:.2f} 不匹配（多为跨执行模式复用或基线被清零），已重新锚定",
                            action_taken="re-anchor baseline")
            except Exception:
                pass
        if mode == "paper":
            baseline = 0.0
        else:
            exchange_eq = self._exchange_equity()
            if exchange_eq <= 0:
                # 接口不可用且无缓存：不落盘猜测值（错误基线一旦固化，
                # 接口恢复后 session_equity 会永久偏移），内存里临时用 0，
                # 下次启动接口恢复时再正确固化。
                return 0.0
            baseline = exchange_eq - self.initial_equity
            if baseline < 0:
                baseline = max(exchange_eq, self.initial_equity) - self.initial_equity
            if self._equity_stale:
                # 用的是缓存权益，能正确算出会话权益但不固化，
                # 等接口恢复后再落盘。
                return baseline
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
            # 备用口径：demo/testnet/live broker 的 state["initial_equity"] 取自
            # 交易所真实权益，可用；但 paper broker 的该字段是本地账本的配置值
            # （等于起始资金），拿它当交易所权益会算出 baseline = 配置值 - 起始
            # 资金 = 0，正是历史上基线被清零的路径。paper 模式禁用此兜底。
            if self._mode() != "paper":
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
            payload: dict[str, Any] = {}
            try:
                if self.state_file.exists():
                    payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            # mode 与 baseline 同写同读：baseline 只在同执行模式下有意义
            payload.update({
                "initial_equity": self.initial_equity,
                "baseline": baseline,
                "mode": self._mode(),
                "anchored_at": datetime.now(timezone.utc).isoformat(),
            })
            tmp = self.state_file.with_name(
                f"{self.state_file.stem}.tmp-{os.getpid()}-{threading.get_ident()}")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.state_file)

    def session_equity(self) -> float:
        """返回以配置起始资金为锚的会话权益，单位为 USDT。

        交易所暂时不可用且没有可信缓存时返回起始资金作为中性展示值；调用方必须结合
        ``sync()`` 返回的 ``equity_available`` 判断能否开新仓。
        """
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
        """同步权益并执行资金级硬保护。

        可能的副作用包括全平、重锚和写风险事件。权益为旧缓存时跳过破产及保证金处置，
        返回 ``equity_available=False``，由上层阻断新开仓。
        """
        mode = self._mode()
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
            # 重置前必须再确认一次权益是新鲜的：用失效/缓存权益算出的基线一旦
            # 落盘，接口恢复后会话权益会永久偏移（历史故障根因之一）。
            recheck_eq = self._exchange_equity()
            if recheck_eq <= 0 or self._equity_stale:
                self.db.log_risk_event(
                    event_type="capital_reset_skipped", severity="warn",
                    details=f"会话权益 {session_eq:.2f} 已触及破产线，但交易所权益不可用"
                            f"（{recheck_eq:.2f}），本轮跳过重置以免写入错误基线",
                    action_taken="skip reset; keep positions")
                return result
            try:
                self.broker.close_all(prices or {}, self.db)
            except Exception as e:
                self.db.log_risk_event(event_type="capital_reset_close_error", severity="error",
                                       details=f"{type(e).__name__}: {e}", action_taken="continue")
            if getattr(self.broker, "mode", "paper") == "paper" and hasattr(self.broker, "reset_capital"):
                self.broker.reset_capital(self.initial_equity)
            new_baseline = 0.0 if mode == "paper" else (recheck_eq - self.initial_equity)
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
        mode = self._mode()
        if mode == "live":
            self.db.log_risk_event(event_type="manual_reset_rejected", severity="warn",
                                   details="实盘模式不允许手动重置资金",
                                   action_taken="rejected")
            return {"ok": False, "error": "实盘模式不允许重置资金"}
        # 非 paper 模式下基线要按交易所权益重算：权益拿不到就别动仓位也别落盘，
        # 否则 0 - 起始资金 会被固化成负基线（历史故障：基线被写成 0）。
        exchange_eq = 0.0
        if mode != "paper":
            exchange_eq = self._exchange_equity()
            if exchange_eq <= 0 or self._equity_stale:
                self.db.log_risk_event(
                    event_type="manual_reset_rejected", severity="warn",
                    details=f"交易所权益不可用（{exchange_eq:.2f}），拒绝重置以免写入错误基线",
                    action_taken="rejected")
                return {"ok": False, "error": "交易所权益接口不可用，拒绝重置基线（未平仓）"}
        try:
            self.broker.close_all(prices or {}, self.db)
        except Exception as e:
            self.db.log_risk_event(event_type="manual_reset_close_error", severity="error",
                                   details=f"{type(e).__name__}: {e}", action_taken="continue")
        if hasattr(self.broker, "reset_capital"):
            self.broker.reset_capital(self.initial_equity)
        # paper：baseline=0，session_equity 直接等于账本现金；
        # demo/testnet：baseline=交易所余额-初始权益，使 session_equity 回到 initial_equity。
        new_baseline = 0.0 if mode == "paper" else (exchange_eq - self.initial_equity)
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
        """生成提供给决策层的会话资金快照，不暴露交易所容器绝对余额。"""
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
