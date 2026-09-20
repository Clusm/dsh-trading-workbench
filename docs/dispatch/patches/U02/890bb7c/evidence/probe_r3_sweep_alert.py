"""U02/r3 探针 ⑤：退出预留巡检失败是否有运维可见的告警事件。

跑法：``python probe_r3_sweep_alert.py <tree>``
只桩掉 db（记录 event_type）与管理器的 reclaim_stale_exits（抛错），不联网。
另外验证"告警自身的 db 也坏掉"时不得把异常抛回退出路径。
"""
from __future__ import annotations

import sys
from pathlib import Path

TREE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent / "sb_int"
sys.path.insert(0, str(TREE / "src"))

from core.strategy_position_manager import StrategyPositionManager  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402

FAIL: list[str] = []


class FakeDB:
    def __init__(self, explode: bool = False) -> None:
        self.events: list[str] = []
        self.explode = explode

    def log_risk_event(self, *, event_type: str, severity: str = "warn",
                       details: str = "", action_taken: str = "") -> None:
        if self.explode:
            raise RuntimeError("db down")
        self.events.append(event_type)


class Intent:
    symbol = "BTCUSDT"
    client_order_id = None
    qty = 1.0


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {'' if ok else detail}")
    if not ok:
        FAIL.append(name)


def build(db: FakeDB):
    broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker.db = db
    broker.environment = "demo"
    broker.account_ref = "demo:probe"
    manager = StrategyPositionManager(account_ref="demo:probe", environment="demo")

    def boom(**kwargs):
        raise RuntimeError("sweep exploded")

    manager.reclaim_stale_exits = boom
    broker.strategy_position_manager = manager
    return broker, manager


db = FakeDB()
broker, manager = build(db)
res = broker._reserve_exit("BTCUSDT", "LONG", Intent(), 1.0)
print(f"INFO events={db.events} res_skip={res.get('skip')} submit={res.get('submit_qty')}")
check("5_sweep_failure_is_alerted",
      "futures_exit_reservation_sweep_failed" in db.events, db.events)
check("5_sweep_failure_does_not_block_exit",
      res.get("skip") is False and res.get("submit_qty") is not None,
      {k: str(v) for k, v in res.items()})

# 告警自身的 db 也坏掉：仍不得把异常抛回退出路径（审计失败不得影响交易）。
db2 = FakeDB(explode=True)
broker2, _ = build(db2)
res2 = broker2._reserve_exit("BTCUSDT", "LONG", Intent(), 1.0)
print(f"INFO db_down_res_skip={res2.get('skip')}")
check("5_alert_failure_does_not_break_exit", res2.get("skip") is False, res2)

print(f"\nR3_SWEEP_ALERT_PROBE tree={TREE.name} FAIL={len(FAIL)} {FAIL}")
raise SystemExit(1 if FAIL else 0)
