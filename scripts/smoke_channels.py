"""通道资金日切与空仓对账回归测试。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.channels import ChannelManager, utc_today  # noqa: E402


class FlatBroker:
    """最小交易所替身：明确返回空仓，不提供纸面分账能力。"""

    @staticmethod
    def positions() -> list[dict]:
        return []


ok = fail = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}: {detail}")


tmp = Path(tempfile.mkdtemp())
db = AuditDatabase(tmp / "audit.sqlite")
state_file = tmp / "channel_state.json"

print("\n[1] 空仓小幅残差自动修复")
state_file.write_text(json.dumps({
    "date": utc_today(),
    "strategy_day_start_equity": 300.0,
    "ai_day_start_equity": 200.0,
    "ai_halted": False,
    "pnl_reset_at": None,
}), encoding="utf-8")
manager = ChannelManager(state_file, FlatBroker(), db)
result = manager.sync(session_equity=520.0)
total = result["strategy"]["equity"] + result["ai"]["equity"]
check("通道合计回到会话权益", abs(total - 520.0) < 1e-8, str(total))
check("仍按原 6:4 权重", abs(result["strategy"]["equity"] - 312.0) < 1e-8,
      str(result["strategy"]))
check("本轮标记已对账", result["reconciled"] is True, str(result))
result_again = manager.sync(session_equity=520.0)
events = db.fetch_all(
    "SELECT * FROM risk_events WHERE event_type='channel_equity_reconciled'"
)
check("重复同步不重复调整或记事件",
      result_again["reconciled"] is False and len(events) == 1, str(len(events)))

print("\n[2] 跨日以交易所真实权益为总锚点")
state_file_2 = tmp / "channel_state_rollover.json"
state_file_2.write_text(json.dumps({
    "date": "2000-01-01",
    "strategy_day_start_equity": 300.0,
    "ai_day_start_equity": 200.0,
    "ai_halted": False,
    "pnl_reset_at": None,
}), encoding="utf-8")
manager_2 = ChannelManager(state_file_2, FlatBroker(), db)
rolled = manager_2.sync(session_equity=550.0)
rolled_total = rolled["strategy"]["equity"] + rolled["ai"]["equity"]
check("发生日切", rolled["rebalanced"] is False and manager_2.state["date"] == utc_today(),
      str(rolled))
check("日切不再丢失账外资金变化", abs(rolled_total - 550.0) < 1e-8,
      str(rolled_total))
check("日切后按 6:4 分配", abs(rolled["strategy"]["equity"] - 330.0) < 1e-8,
      str(rolled["strategy"]))

print("\n[3] 大额异常不自动吸收")
state_file_3 = tmp / "channel_state_large_drift.json"
state_file_3.write_text(json.dumps({
    "date": utc_today(),
    "strategy_day_start_equity": 300.0,
    "ai_day_start_equity": 200.0,
    "ai_halted": False,
    "pnl_reset_at": None,
}), encoding="utf-8")
manager_3 = ChannelManager(state_file_3, FlatBroker(), db)
large = manager_3.sync(session_equity=700.0)
large_total = large["strategy"]["equity"] + large["ai"]["equity"]
check("超过 10% 的偏差保持原账本", abs(large_total - 500.0) < 1e-8, str(large_total))
check("大额偏差产生待核对事件", len(db.fetch_all(
    "SELECT * FROM risk_events WHERE event_type='channel_equity_drift'"
)) == 1)

print(f"\n结果: {ok} 通过 / {fail} 失败")
raise SystemExit(1 if fail else 0)
