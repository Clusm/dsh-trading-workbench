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


class ActiveBroker:
    """带一笔 AI 浮盈仓位，用于验证浮盈不会被固化进通道基准。"""

    @staticmethod
    def positions() -> list[dict]:
        return [{"symbol": "BTCUSDT", "unRealizedProfit": 5.0}]

    @staticmethod
    def _load_ledger() -> dict:
        return {"positions": {"BTCUSDT": {"source": "ai"}}}


class AtomicAccountBroker(ActiveBroker):
    """账户快照浮盈与逐仓浮盈不同，验证对账优先采用原子账户字段。"""

    @staticmethod
    def account() -> dict:
        return {"totalUnrealizedProfit": 4.0}


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

print("\n[1] 小幅残差自动修复")
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

print("\n[1b] 持仓浮盈不写入日初基准")
active_state = tmp / "channel_state_active.json"
active_state.write_text(json.dumps({
    "date": utc_today(),
    "strategy_day_start_equity": 300.0,
    "ai_day_start_equity": 200.0,
    "ai_halted": False,
    "pnl_reset_at": None,
}), encoding="utf-8")
active_manager = ChannelManager(active_state, ActiveBroker(), db)
active = active_manager.sync(session_equity=525.0)
active_total = active["strategy"]["equity"] + active["ai"]["equity"]
check("持仓时通道合计仍对齐", abs(active_total - 525.0) < 1e-8, str(active_total))
check("5U 浮盈只进入 AI 实时权益", abs(active["ai"]["day_start_equity"] - 208.0) < 1e-8
      and abs(active["ai"]["equity"] - 213.0) < 1e-8, str(active["ai"]))

print("\n[1c] 账户原子快照优先于逐仓异步快照")
atomic_state = tmp / "channel_state_atomic.json"
atomic_state.write_text(json.dumps({
    "date": utc_today(),
    "strategy_day_start_equity": 300.0,
    "ai_day_start_equity": 200.0,
    "ai_halted": False,
    "pnl_reset_at": None,
}), encoding="utf-8")
atomic_manager = ChannelManager(atomic_state, AtomicAccountBroker(), db)
atomic_manager.sync(session_equity=524.0)
check("对账使用账户快照的 4U 浮盈",
      abs(atomic_manager.state["strategy_day_start_equity"] - 312.0) < 1e-8,
      str(atomic_manager.state))

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

print("\n[4] 通道状态损坏时禁止推断并覆盖水位")
corrupt_path = tmp / "channel_state_corrupt.json"
corrupt_path.write_text('{"date":', encoding="utf-8")
corrupt_before = corrupt_path.read_text(encoding="utf-8")
corrupt_manager = ChannelManager(corrupt_path, FlatBroker(), db)
corrupt_status = corrupt_manager.sync(session_equity=500.0)
check("两个通道均 fail-closed",
      not corrupt_status["strategy"]["allow_new"] and not corrupt_status["ai"]["allow_new"],
      str(corrupt_status))
check("普通同步不覆盖损坏文件", corrupt_path.read_text(encoding="utf-8") == corrupt_before)
check("状态明确要求恢复", corrupt_status.get("recovery_required") is True)
corrupt_manager.reset_day_start(500.0)
restored = corrupt_manager.sync(session_equity=500.0)
check("显式重置后恢复", not restored.get("recovery_required")
      and restored["strategy"]["allow_new"] and restored["ai"]["allow_new"], str(restored))

print("\n[5] 实盘全部资金分配给 AI，保证金使用上限保持原值")
ai_only = ChannelManager(tmp / "ai_only.json", FlatBroker(), db,
                         strategy_weight=0.0, ai_weight=1.0)
ai_status = ai_only.sync(session_equity=500.0)
check("AI取得500，策略分配为0", ai_status["ai"]["equity"] == 500.0
      and ai_status["strategy"]["equity"] == 0.0)
check("策略不允许新增仓位", ai_status["strategy"]["allow_new"] is False)
check("AI保证金上限仍是80%", ai_status["ai"]["margin_cap"] == 400.0)
print(f"\n结果: {ok} 通过 / {fail} 失败")
raise SystemExit(1 if fail else 0)
