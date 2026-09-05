"""全局账户风险闸门。

单笔风控只能限制一笔订单，不能限制“今天已经亏了多少”或“从会话高点回撤了多少”。
本模块把这两类账户级约束持久化，重启后仍然有效；状态以 capital 的锚点时间分段，
人工重锚资金后不会把上一轮会话的高水位错误带入新一轮。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


RISK_GUARD_MODES = {"audit_only", "enforce"}


class AccountRiskGuard:
    """日损失/会话回撤的最后一道开仓闸门。"""

    def __init__(self, state_file: Path, daily_loss_stop: float, max_drawdown: float,
                 mode: str = "enforce"):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.daily_loss_stop = max(0.0, float(daily_loss_stop))
        self.max_drawdown = max(0.0, float(max_drawdown))
        normalized_mode = str(mode or "audit_only").strip().lower()
        if normalized_mode not in RISK_GUARD_MODES:
            raise ValueError(f"未知账户风险守卫模式: {mode!r}")
        self.mode = normalized_mode
        self._lock = threading.RLock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        tmp = self.state_file.with_name(
            f"{self.state_file.stem}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def sync(self, equity: float, *, equity_available: bool, anchor_id: str) -> dict[str, Any]:
        """更新水位并返回是否允许开仓。

        账户权益未知时绝不更新水位；调用方应只允许保护/平仓，不允许开仓。
        日损失会在 UTC 日切重新计量；从会话峰值的回撤仅在资本锚点变更时重置。
        """
        with self._lock:
            day = _utc_day()
            anchor = str(anchor_id or "unknown")
            eq = max(0.0, float(equity or 0.0))
            changed = False

            if self.state.get("anchor_id") != anchor:
                self.state = {
                    "anchor_id": anchor,
                    "date": day,
                    "day_start_equity": eq if equity_available else None,
                    "peak_equity": eq if equity_available else None,
                    "daily_loss_latched": False,
                    "drawdown_latched": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                changed = True
            elif self.state.get("date") != day:
                self.state.update({
                    "date": day,
                    "day_start_equity": eq if equity_available else None,
                    "daily_loss_latched": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                })
                changed = True

            if not equity_available or eq <= 0:
                if changed:
                    self._save()
                return {
                    "allow_new": False,
                    "would_allow_new": False,
                    "reason": "账户权益不可用，禁止新开仓",
                    "daily_loss_ratio": None,
                    "drawdown_ratio": None,
                    "latched": False,
                    "mode": self.mode,
                    "enforced": True,
                }

            day_start = float(self.state.get("day_start_equity") or 0.0)
            peak = float(self.state.get("peak_equity") or 0.0)
            if day_start <= 0:
                day_start = eq
                self.state["day_start_equity"] = day_start
                changed = True
            if peak <= 0 or eq > peak:
                peak = eq
                self.state["peak_equity"] = peak
                changed = True

            daily_loss_ratio = max(0.0, (day_start - eq) / day_start) if day_start else 0.0
            drawdown_ratio = max(0.0, (peak - eq) / peak) if peak else 0.0
            if self.daily_loss_stop and daily_loss_ratio >= self.daily_loss_stop:
                if not self.state.get("daily_loss_latched"):
                    self.state["daily_loss_latched"] = True
                    self.state["daily_loss_latched_at"] = datetime.now(timezone.utc).isoformat()
                    changed = True
            if self.max_drawdown and drawdown_ratio >= self.max_drawdown:
                if not self.state.get("drawdown_latched"):
                    self.state["drawdown_latched"] = True
                    self.state["drawdown_latched_at"] = datetime.now(timezone.utc).isoformat()
                    changed = True

            self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
            if changed:
                self._save()

            daily_latched = bool(self.state.get("daily_loss_latched"))
            drawdown_latched = bool(self.state.get("drawdown_latched"))
            reasons: list[str] = []
            if daily_latched:
                reasons.append(f"日亏损 {daily_loss_ratio:.2%} 达到 {self.daily_loss_stop:.2%}")
            if drawdown_latched:
                reasons.append(f"会话回撤 {drawdown_ratio:.2%} 达到 {self.max_drawdown:.2%}")
            would_allow_new = not reasons
            enforced = self.mode == "enforce"
            return {
                "allow_new": would_allow_new if enforced else True,
                "would_allow_new": would_allow_new,
                "reason": "；".join(reasons),
                "daily_loss_ratio": daily_loss_ratio,
                "drawdown_ratio": drawdown_ratio,
                "latched": bool(reasons),
                "mode": self.mode,
                "enforced": enforced,
                "day_start_equity": day_start,
                "peak_equity": peak,
            }
