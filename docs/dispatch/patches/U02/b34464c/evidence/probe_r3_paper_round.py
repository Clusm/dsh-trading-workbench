"""U02/r3 前后对照探针 ③：纸面退出回退键的"同轮抑制"是否与声明一致。

跑法：``python probe_r3_paper_round.py <tree>``（tree = 含 ``src/`` 的目录）
只驱动 ``PaperBroker._exit_idempotency_guard``（不联网、不下单、不读 .env/data）。
"""
from __future__ import annotations

import sys
from pathlib import Path

TREE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent / "sb_int"
sys.path.insert(0, str(TREE / "src"))

from agents.execution import PaperBroker  # noqa: E402
from core.strategy_position_manager import StrategyPositionManager  # noqa: E402

FAIL: list[str] = []


class Intent:
    def __init__(self, *, qty: float, cid: str | None = None, symbol: str = "BTCUSDT",
                 side: str = "LONG", action: str = "close") -> None:
        self.qty = qty
        self.client_order_id = cid
        self.symbol = symbol
        self.side = side
        self.action = action
        self.signal_id = None
        self.ticket_id = None
        self.strategy_id = None


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {'' if ok else detail}")
    if not ok:
        FAIL.append(name)


def info(text: str) -> None:
    print(f"INFO {text}")


def new_broker() -> tuple[PaperBroker, StrategyPositionManager]:
    mgr = StrategyPositionManager(account_ref="paper", environment="paper")
    b = PaperBroker.__new__(PaperBroker)
    b.environment = "paper"
    b.strategy_position_manager = mgr
    b._paper_exit_seq = 0
    return b, mgr


def guard(b: PaperBroker, mgr: StrategyPositionManager, *, symbol: str = "BTCUSDT",
          qty: float = 1.0, side: str = "LONG", action: str = "close",
          pos_qty: float = 1.0) -> str:
    it = Intent(qty=qty, symbol=symbol, side=side, action=action)
    res = b._exit_idempotency_guard(mgr, it, ("", {"side": side, "qty": str(pos_qty)}), "ai")
    return "EXECUTED" if res is None else str(res.get("action"))


def bump(b: PaperBroker) -> None:
    token = getattr(b, "_paper_exit_round_token", None)
    b._paper_exit_round = token() if callable(token) else "r0"


# ---- 1. 同轮：同名同量同动作，第二次必须被抑制 ------------------------------
b, mgr = new_broker()
bump(b)
first = guard(b, mgr)
second = guard(b, mgr)
info(f"1_same_round first={first} second={second}")
check("1_same_round_duplicate_suppressed",
      first == "EXECUTED" and second == "duplicate_skipped", (first, second))

# ---- 1b. 同轮 + 仓位已被那一笔减掉（0.4），仍不得再提交 ---------------------
b, mgr = new_broker()
bump(b)
e1 = guard(b, mgr, qty=1.0, pos_qty=1.0)
e2 = guard(b, mgr, qty=1.0, pos_qty=0.4)
info(f"1b_after_fill first={e1} second={e2}")
check("1b_same_round_after_fill_still_suppressed",
      e1 == "EXECUTED" and e2 == "duplicate_skipped", (e1, e2))


# ---- 2. 跨轮：必须真的执行 ---------------------------------------------------
b, mgr = new_broker()
bump(b)
r1 = guard(b, mgr)
bump(b)
r2 = guard(b, mgr)
info(f"2_next_round round1={r1} round2={r2}")
check("2_second_round_exit_executes", r1 == "EXECUTED" and r2 == "EXECUTED", (r1, r2))


# ---- 3. 连续 5 轮：每轮都执行 ------------------------------------------------
b, mgr = new_broker()
acts = []
for _ in range(5):
    bump(b)
    acts.append(guard(b, mgr))
info(f"3_five_rounds={acts}")
check("3_five_rounds_all_execute", acts == ["EXECUTED"] * 5, acts)


# ---- 4. 同轮"先部分平 0.3、再平剩余 0.7"：指纹不同，都要执行 ---------------
b, mgr = new_broker()
bump(b)
p1 = guard(b, mgr, qty=0.3)
p2 = guard(b, mgr, qty=0.7)
info(f"4_same_round_partial_then_rest={p1},{p2}")
check("4_same_round_different_qty_both_execute",
      p1 == "EXECUTED" and p2 == "EXECUTED", (p1, p2))


# ---- 5. 同轮重复同一笔部分平 0.3：必须被抑制 --------------------------------
b, mgr = new_broker()
bump(b)
q1 = guard(b, mgr, qty=0.3)
q2 = guard(b, mgr, qty=0.3)
info(f"5_same_round_same_partial={q1},{q2}")
check("5_same_round_same_partial_suppressed",
      q1 == "EXECUTED" and q2 == "duplicate_skipped", (q1, q2))


# ---- 6. 跨标的 + 跨轮：彼此独立 ---------------------------------------------
b, mgr = new_broker()
bump(b)
a1 = guard(b, mgr, symbol="BTCUSDT")
c1 = guard(b, mgr, symbol="ETHUSDT")
bump(b)
a2 = guard(b, mgr, symbol="BTCUSDT")
d1 = guard(b, mgr, symbol="ETHUSDT")
info(f"6_cross_symbol_round={a1},{c1},{a2},{d1}")
check("6_cross_symbol_and_round_independent",
      all(x == "EXECUTED" for x in (a1, c1, a2, d1)), (a1, c1, a2, d1))


# ---- 7. 显式 client_order_id（观察，两个版本都不因此判错） ------------------
b, mgr = new_broker()
bump(b)
cid = "mta-g-" + "a" * 24
it = Intent(qty=1.0, cid=cid)
g1 = b._exit_idempotency_guard(mgr, it, ("", {"side": "LONG", "qty": "1.0"}), "ai")
bump(b)
g2 = b._exit_idempotency_guard(mgr, it, ("", {"side": "LONG", "qty": "1.0"}), "ai")
info(f"7_explicit_cid first={'EXECUTED' if g1 is None else g1} "
     f"second={'EXECUTED' if g2 is None else g2}")


print(f"\nR3_PAPER_ROUND_PROBE tree={TREE.name} FAIL={len(FAIL)} {FAIL}")
raise SystemExit(1 if FAIL else 0)
