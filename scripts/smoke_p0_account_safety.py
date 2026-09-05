"""P0 账户级风险闸门与单实例锁回归（全离线，不触及真实 data）。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.instance_lock import InstanceLock  # noqa: E402
from core.risk_guard import AccountRiskGuard  # noqa: E402

ok = fail = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name} {extra}")


tmp = Path(tempfile.mkdtemp())
state = tmp / "account_risk_state.json"
guard = AccountRiskGuard(state, daily_loss_stop=0.05, max_drawdown=0.10)

print("\n[1] 初始权益建立日基准和会话高水位")
r0 = guard.sync(500, equity_available=True, anchor_id="demo:anchor-a")
check("初始允许开仓", r0["allow_new"] is True, str(r0))
check("日基准=500", r0["day_start_equity"] == 500, str(r0))
check("状态已原子落盘", state.exists())

print("\n[2] 日亏损 5% 触发且重启后保持")
r1 = guard.sync(474, equity_available=True, anchor_id="demo:anchor-a")
check("日止损禁止开仓", r1["allow_new"] is False and r1["daily_loss_ratio"] >= 0.05, str(r1))
again = AccountRiskGuard(state, daily_loss_stop=0.05, max_drawdown=0.10)
r2 = again.sync(490, equity_available=True, anchor_id="demo:anchor-a")
check("日止损重启后不丢失", r2["allow_new"] is False, str(r2))

print("\n[3] 新资金锚点重建风险计量周期")
r3 = again.sync(532.14, equity_available=True, anchor_id="demo:anchor-b")
check("重锚后允许开仓", r3["allow_new"] is True, str(r3))
check("重锚后日基准刷新", abs(r3["day_start_equity"] - 532.14) < 0.001, str(r3))

print("\n[4] 从会话峰值回撤 10% 触发")
again.sync(600, equity_available=True, anchor_id="demo:anchor-b")
r4 = again.sync(539, equity_available=True, anchor_id="demo:anchor-b")
check("回撤止损禁止开仓", r4["allow_new"] is False and r4["drawdown_ratio"] >= 0.10, str(r4))

print("\n[5] 权益未知时默认禁止开仓且不污染高水位")
before = json.loads(state.read_text(encoding="utf-8"))["peak_equity"]
r5 = again.sync(0, equity_available=False, anchor_id="demo:anchor-b")
after = json.loads(state.read_text(encoding="utf-8"))["peak_equity"]
check("权益未知禁止开仓", r5["allow_new"] is False, str(r5))
check("高水位保持", before == after, f"{before} -> {after}")

print("\n[6] 单实例锁拒绝第二个服务")
lock_path = tmp / "trading_service.lock"
first = InstanceLock(lock_path)
first.acquire()
second = InstanceLock(lock_path)
try:
    second.acquire()
    blocked = False
except RuntimeError:
    blocked = True
finally:
    first.release()
check("第二实例被拒绝", blocked)
third = InstanceLock(lock_path)
third.acquire()
third.release()
check("释放后可重新启动", not lock_path.exists())

print("\n[7] 审计模式只报告原阈值，不改变交易控制")
audit = AccountRiskGuard(tmp / "audit_state.json", daily_loss_stop=0.05,
                         max_drawdown=0.10, mode="audit_only")
audit.sync(500, equity_available=True, anchor_id="demo:audit")
ra = audit.sync(470, equity_available=True, anchor_id="demo:audit")
check("审计模式记录本应拦截", ra["latched"] and not ra["would_allow_new"], str(ra))
check("审计模式不实际拦截", ra["allow_new"] and not ra["enforced"], str(ra))

print(f"\n结果: {ok} 通过 / {fail} 失败")
sys.exit(1 if fail else 0)
