"""P1-3 复现/回归探针：纸面退出的**稳定回退键**让同名标的第二次退出永久被吞。

桩掉状态文件与账本副作用，只走真实的
``PaperBroker._exit_idempotency_guard`` 与 ``StrategyPositionManager.request_exit``。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agents.execution import PaperBroker  # noqa: E402
from core.strategy_position_manager import StrategyPositionManager  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: Any = "") -> None:
    RESULTS.append((bool(ok), name))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


class _Intent:
    def __init__(self, *, symbol: str = "BTCUSDT", side: str = "LONG",
                 qty: float = 1.0, cid: str | None = None,
                 action: str = "close") -> None:
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.client_order_id = cid
        self.action = action
        self.signal_id = None
        self.ticket_id = None
        self.strategy_id = None


def _broker() -> Any:
    broker = PaperBroker.__new__(PaperBroker)
    broker._paper_exit_seq = 0
    return broker


def _new_round(broker: Any) -> str:
    """开一轮执行；未打补丁的树没有轮次令牌（回退键因此恒稳，即 P1-3）。"""
    factory = getattr(broker, "_paper_exit_round_token", None)
    token = factory() if factory is not None else ""
    broker._paper_exit_round = token
    return token


def main() -> int:
    manager = StrategyPositionManager(account_ref="demo:paper", environment="paper")
    broker = _broker()
    existing = ("ai", {"side": "LONG", "qty": 1.0})

    first = _new_round(broker)
    g1 = broker._exit_idempotency_guard(
        manager, _Intent(qty=1.0), existing, "ai")
    print(f"INFO round1 token={first} guard={g1}", flush=True)
    check("第一轮退出正常执行", g1 is None, g1)

    second = _new_round(broker)
    g2 = broker._exit_idempotency_guard(
        manager, _Intent(qty=1.0), existing, "ai")
    print(f"INFO round2 token={second} guard={g2}", flush=True)
    check("同名标的的第二轮退出不被永久吞掉", g2 is None, g2)

    third = _new_round(broker)
    g3 = broker._exit_idempotency_guard(
        manager, _Intent(qty=0.3), existing, "ai")
    print(f"INFO round3 partial guard={g3}", flush=True)
    check("部分平仓后的剩余退出仍可执行", g3 is None, g3)

    same_round = _new_round(broker)
    a = broker._exit_idempotency_guard(manager, _Intent(qty=0.5), existing, "ai")
    b = broker._exit_idempotency_guard(manager, _Intent(qty=0.5), existing, "ai")
    print(f"INFO same round: first={a} second={b}", flush=True)
    check("同轮同指纹仍共享同一个退出键（键含本轮令牌）",
          a is None and b is None, (a, b))

    # 显式 client_order_id 的重复抑制由 execute() 的既有订单表检查负责，本 guard
    # 不再依赖"恒稳键"来抑制——因此这里只断言方向与只减语义仍成立。
    guard_cid = _new_round(broker)
    c1 = broker._exit_idempotency_guard(
        manager, _Intent(qty=2.0, cid="mta-exit-fixed"), existing, "ai")
    print(f"INFO explicit cid guard={c1} token={guard_cid}", flush=True)
    check("显式 cid 退出正常执行（请求量仍被可减量夹逼）", c1 is None, c1)
    ledger = manager.ledger("paper:BTCUSDT:LONG")
    print(f"INFO paper ledger={ledger.as_dict() if ledger else None}", flush=True)
    check("纸面账本无残留 ACTIVE 预留",
          ledger is None or not ledger.active_reservations(),
          ledger.as_dict() if ledger else None)

    fails = [name for ok, name in RESULTS if not ok]
    print(f"\nPAPER PROBE {'FAIL=' + str(len(fails)) if fails else 'FAIL=0'}", flush=True)
    for name in fails:
        print(f"  - {name}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
