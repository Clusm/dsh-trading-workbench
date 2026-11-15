"""风控状态与控制态持久化（docs/03 §7.5/§7.6）。

- 日亏/会话回撤 **latch**：一旦置位，只有 UTC 日切（日亏）或人工重新锚定（回撤）才解除；
- 控制态：``normal / pause / close_only / emergency``，落库、重启沿用、损坏 fail-closed；
- 权益不可用时不更新水位、不写基线（避免把错误值固化）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ..ledger.numbers import to_decimal
from ..ledger.store import Ledger, utc_day, utc_now
from .config import RiskLimits

__all__ = [
    "CONTROL_STATES",
    "ControlSnapshot",
    "RiskStateStore",
    "RiskStateError",
]

CONTROL_STATES = ("normal", "pause", "close_only", "emergency")
_TERMINAL = ("emergency",)


class RiskStateError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class ControlSnapshot:
    state: str
    reason: str
    updated_at: str
    updated_by: str

    def as_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "reason": self.reason,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


class RiskStateStore:
    def __init__(self, ledger: Ledger, limits: RiskLimits) -> None:
        self.ledger = ledger
        self.limits = limits

    # ------------------------------------------------------------------ state
    @property
    def anchor_id(self) -> str:
        return f"demo:{self.ledger.ledger_id}"

    def _fresh(self, equity: Decimal | None) -> dict[str, Any]:
        return {
            "anchor_id": self.anchor_id,
            "date": utc_day(),
            "day_start_equity": equity,
            "peak_equity": equity,
            # §8·C4：水位必须带来源与时间，否则无法回答“这个基线是哪来的”。
            "day_start_equity_source": "exchange" if equity is not None else None,
            "day_start_equity_at": utc_now() if equity is not None else None,
            "day_start_equity_tz": "UTC",
            "peak_equity_source": "exchange" if equity is not None else None,
            "peak_equity_at": utc_now() if equity is not None else None,
            "daily_loss_latched": 0,
            "daily_loss_latched_at": None,
            "drawdown_latched": 0,
            "drawdown_latched_at": None,
            "consecutive_losses": 0,
            "cooldown_until": None,
            "recovery_required": 0,
        }

    def _load(self) -> dict[str, Any] | None:
        try:
            return self.ledger.load_risk_state()
        except Exception:  # noqa: BLE001 - 损坏/缺列按需重建
            return None

    def sync(self, equity: Decimal | None, *, equity_available: bool) -> dict[str, Any]:
        """更新水位并返回快照；权益不可用时只读、不改峰值/基线。"""

        row = self._load()
        changed = False
        if row is None or row.get("anchor_id") != self.anchor_id:
            row = self._fresh(equity if equity_available else None)
            changed = True
        else:
            row = dict(row)
            if row.get("date") != utc_day():
                row["date"] = utc_day()
                row["day_start_equity"] = to_decimal(equity) if equity_available else None
                # 新的一天：基线的来源必须跟着刷新；取不到权益就记 None，
                # 不能用昨天或 0 顶替（§8·C4：权益未知不能假装通过）。
                row["day_start_equity_source"] = "exchange" if equity_available else None
                row["day_start_equity_at"] = utc_now() if equity_available else None
                row["day_start_equity_tz"] = "UTC"
                row["daily_loss_latched"] = 0
                row["daily_loss_latched_at"] = None
                changed = True

        if not equity_available or equity is None or equity <= 0:
            if changed:
                self._save(row)
            return self._snapshot(row, equity=None, available=False)

        equity = to_decimal(equity) or Decimal("0")
        day_start = to_decimal(row.get("day_start_equity"))
        peak = to_decimal(row.get("peak_equity"))
        if day_start is None or day_start <= 0:
            day_start = equity
            row["day_start_equity"] = equity
            row["day_start_equity_source"] = "exchange"
            row["day_start_equity_at"] = utc_now()
            row["day_start_equity_tz"] = "UTC"
            changed = True
        if peak is None or peak <= 0 or equity > peak:
            peak = equity
            row["peak_equity"] = equity
            row["peak_equity_source"] = "exchange"
            row["peak_equity_at"] = utc_now()
            changed = True

        daily_ratio = max(Decimal("0"), (day_start - equity) / day_start) if day_start > 0 else Decimal("0")
        drawdown_ratio = max(Decimal("0"), (peak - equity) / peak) if peak > 0 else Decimal("0")

        if daily_ratio >= Decimal(str(self.limits.daily_loss_stop)) and not row.get(
            "daily_loss_latched"
        ):
            row["daily_loss_latched"] = 1
            row["daily_loss_latched_at"] = utc_now()
            changed = True
        if drawdown_ratio >= Decimal(str(self.limits.max_drawdown_from_peak)) and not row.get(
            "drawdown_latched"
        ):
            row["drawdown_latched"] = 1
            row["drawdown_latched_at"] = utc_now()
            changed = True

        if changed:
            self._save(row)

        snapshot = self._snapshot(row, equity=equity, available=True)
        snapshot["daily_loss_ratio"] = daily_ratio
        snapshot["drawdown_ratio"] = drawdown_ratio
        self._apply_auto_control(snapshot)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        row = self._load()
        if row is None or row.get("anchor_id") != self.anchor_id:
            return self._snapshot(self._fresh(None), equity=None, available=False)
        return self._snapshot(row, equity=None, available=False)

    def _snapshot(
        self, row: dict[str, Any], *, equity: Decimal | None, available: bool
    ) -> dict[str, Any]:
        day_start = to_decimal(row.get("day_start_equity"))
        peak = to_decimal(row.get("peak_equity"))
        equity_value = equity if equity is not None else None
        daily_ratio = None
        drawdown_ratio = None
        if available and equity_value is not None and day_start and day_start > 0:
            daily_ratio = max(Decimal("0"), (day_start - equity_value) / day_start)
        if available and equity_value is not None and peak and peak > 0:
            drawdown_ratio = max(Decimal("0"), (peak - equity_value) / peak)
        return {
            "anchor_id": row.get("anchor_id"),
            "date": row.get("date"),
            "available": available,
            "equity_session": None if equity_value is None else str(equity_value),
            "day_start_equity": None if day_start is None else str(day_start),
            "day_start_equity_source": row.get("day_start_equity_source"),
            "day_start_equity_at": row.get("day_start_equity_at"),
            "day_start_equity_tz": row.get("day_start_equity_tz"),
            "peak_equity": None if peak is None else str(peak),
            "peak_equity_source": row.get("peak_equity_source"),
            "peak_equity_at": row.get("peak_equity_at"),
            # 风控只采用交易所权益口径（equity_exchange），不用本地策略盈亏替代。
            "equity_caliber": "exchange_wallet_plus_upnl",
            "daily_loss_ratio": None if daily_ratio is None else str(daily_ratio),
            "drawdown_ratio": None if drawdown_ratio is None else str(drawdown_ratio),
            "daily_loss_latched": bool(row.get("daily_loss_latched")),
            "daily_loss_latched_at": row.get("daily_loss_latched_at"),
            "drawdown_latched": bool(row.get("drawdown_latched")),
            "drawdown_latched_at": row.get("drawdown_latched_at"),
            "consecutive_losses": int(row.get("consecutive_losses") or 0),
            "cooldown_until": row.get("cooldown_until"),
            "recovery_required": bool(row.get("recovery_required")),
            "updated_at": row.get("updated_at"),
        }

    def _save(self, row: dict[str, Any]) -> None:
        payload = dict(row)
        payload["updated_at"] = utc_now()
        self.ledger.save_risk_state(payload)

    def _apply_auto_control(self, snapshot: dict[str, Any]) -> None:
        control = self.control()
        if snapshot.get("drawdown_latched") and control.state != "emergency":
            self.set_control(
                "emergency",
                "auto: drawdown latch ≥ "
                f"{self.limits.max_drawdown_from_peak:.1%}",
                updated_by="risk_engine",
            )
            self.ledger.log_risk_event(
                event_type="risk_drawdown_latched",
                severity="critical",
                rule_id="R19",
                action="control=emergency",
                details={"drawdown_ratio": snapshot.get("drawdown_ratio")},
            )
        elif snapshot.get("daily_loss_latched") and control.state == "normal":
            self.set_control(
                "close_only",
                f"auto: daily loss ≥ {self.limits.daily_loss_stop:.1%}",
                updated_by="risk_engine",
            )
            self.ledger.log_risk_event(
                event_type="risk_daily_loss_latched",
                severity="critical",
                rule_id="R18",
                action="control=close_only",
                details={"daily_loss_ratio": snapshot.get("daily_loss_ratio")},
            )

    def set_cooldown(self, until_iso: str | None, *, consecutive_losses: int) -> None:
        row = self._load() or self._fresh(None)
        row = dict(row)
        row["consecutive_losses"] = int(consecutive_losses)
        row["cooldown_until"] = until_iso
        self._save(row)

    def cooldown_active(self) -> bool:
        row = self._load()
        if not row or not row.get("cooldown_until"):
            return False
        try:
            until = datetime.fromisoformat(str(row["cooldown_until"]).replace("Z", "+00:00"))
        except ValueError:
            return False
        return until > datetime.now(timezone.utc)

    # ------------------------------------------------------------------ control
    def control(self) -> ControlSnapshot:
        row = self.ledger.load_control()
        if row is None:
            return ControlSnapshot("normal", "default", "", "system")
        state = str(row.get("state") or "normal")
        if state not in CONTROL_STATES:
            state = "close_only"
            reason = "control state corrupted; fail-closed"
        else:
            reason = str(row.get("reason") or "")
        return ControlSnapshot(state, reason, str(row.get("updated_at") or ""), str(row.get("updated_by") or ""))

    def set_control(
        self, state: str, reason: str, *, updated_by: str = "operator"
    ) -> ControlSnapshot:
        target = str(state or "").strip().lower()
        if target not in CONTROL_STATES:
            raise RiskStateError(
                "E_VALIDATION",
                f"unknown control state {state!r}",
                details={"allowed": list(CONTROL_STATES)},
            )
        if not str(reason or "").strip():
            raise RiskStateError("E_VALIDATION", "control state change requires a reason")
        if target == "normal":
            risk_state = self.snapshot()
            blocked = []
            if risk_state.get("daily_loss_latched"):
                blocked.append("R18_DAILY_LOSS_LATCHED")
            if risk_state.get("drawdown_latched"):
                blocked.append("R19_DRAWDOWN_LATCHED")
            if blocked:
                raise RiskStateError(
                    "E_CONTROL_LOCKED",
                    "cannot return to normal while risk latches are active",
                    details={"blocked_by": blocked},
                )
        self.ledger.save_control(state=target, reason=str(reason).strip(), updated_by=updated_by)
        self.ledger.log_risk_event(
            event_type="control_state_changed",
            severity="warn" if target != "normal" else "info",
            rule_id="R22",
            action=f"control={target}",
            details={"reason": str(reason).strip(), "updated_by": updated_by},
        )
        return self.control()

    def emergency(self, reason: str) -> ControlSnapshot:
        return self.set_control("emergency", reason, updated_by="risk_engine")

    def tomorrow_utc(self) -> str:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.isoformat(timespec="seconds").replace("+00:00", "Z")
