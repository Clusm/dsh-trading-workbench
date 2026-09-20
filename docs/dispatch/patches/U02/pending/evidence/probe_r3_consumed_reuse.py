"""U02/r3 前后对照探针：已 CONSUMED 的退出键是否还能被回收并**重发**。

跑法：``python probe_r3_consumed_reuse.py <tree>``（tree = 含 ``src/`` 的目录）
用例：
  A 账本层：``release()`` 对 CONSUMED 是否幂等 + 释放后能否重新 reserve（NEW-2）
  B 管理器层：账本条目被 ``MAX_TERMINAL_RESERVATIONS`` 淘汰（真实 >2000 条）
    后，TTL 到期的巡检是否把该键当"陈旧 ACTIVE"回收并重发（NEW-1）
  C 管理器层：``_exit_keys`` 幂等记忆也被淘汰后，同键是否仍不重发
  D 反向对照：**被 release 的键必须仍能重发**（防止修 ①② 时把 P0-1 修回去）
  E 守恒式：以上操作后三分量守恒仍成立

全程离线：只 import 两个 U02 独占模块，不联网、不读 .env/data、不构造 Broker。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

TREE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent / "after"
sys.path.insert(0, str(TREE / "src"))

from core.management_authorization import (  # noqa: E402
    ManagementReservationLedger,
    reservation_conserved,
)
from core.strategy_position_manager import (  # noqa: E402
    StrategyPositionManager,
)

DOMAIN = "demo:acct-probe:BTCUSDT:LONG"
STABLE = "mta-g-" + "a" * 24
TTL = 900_000
LATER = 10 ** 15          # 远超任何 TTL
FAIL: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {'' if ok else detail}")
    if not ok:
        FAIL.append(name)


def info(text: str) -> None:
    print(f"INFO {text}")


def new_manager() -> StrategyPositionManager:
    return StrategyPositionManager(account_ref="demo:acct-probe", environment="demo")


# ---- A：账本层 release() 对 CONSUMED 的幂等性（NEW-2） ----------------------
led = ManagementReservationLedger(position_qty="1.0", lifecycle_id="lc-a")
led.reserve(reservation_id="r1", qty="1.0", now_ms=1)
led.consume("r1", engaged_qty=Decimal("1.0"))
before_status = led.reservation("r1").status
rel = led.release("r1", reason="probe")
after_status = led.reservation("r1").status
re_reserve = led.reserve(reservation_id="r1", qty="1.0", now_ms=2)
info(f"A_before={before_status} release_accepted={rel.accepted} "
     f"after={after_status} re_reserve_accepted={re_reserve.accepted}")
check("A_release_is_idempotent_on_consumed",
      before_status == "CONSUMED" and after_status == "CONSUMED"
      and not rel.accepted and not re_reserve.accepted,
      {"release": rel.accepted, "after": after_status,
       "re_reserve": re_reserve.accepted})


# ---- B：账本条目被淘汰（真实 >2000 条）后，TTL 巡检不得回收该键（NEW-1） ----
m = new_manager()
first = m.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                       position_side="LONG", qty="1.0", current_qty="1.0", now_ms=1)
m.confirm_exit(STABLE, engaged_qty=Decimal("1.0"))
led_b = m.ledger(DOMAIN)
limit = ManagementReservationLedger.MAX_TERMINAL_RESERVATIONS
for index in range(limit + 5):                      # 用真实上限，制造淘汰
    key = f"filler-{index}"
    m.request_exit(exit_key=key, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                   position_side="LONG", qty="1.0", current_qty="1.0",
                   now_ms=100 + index)
    m.confirm_exit(key, engaged_qty=Decimal("1.0"))
pruned = led_b.reservation(STABLE) is None
info(f"B_limit={limit} consumed_record_pruned={pruned} "
     f"exit_key_count={m.as_dict()['exit_key_count']}")
check("B_consumed_record_pruned", pruned, led_b.as_dict()["reservations"][:1])
re_b = m.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                      position_side="LONG", qty="1.0", current_qty="1.0",
                      now_ms=LATER, in_flight_ttl_ms=TTL)
info(f"B_after_prune_same_key={{'action': {re_b.action!r}, 'duplicate': {re_b.duplicate},"
     f" 'qty': {str(re_b.submit_qty)}, 'reasons': {list(re_b.reason_codes)}}}")
check("B_consumed_not_reclaimed_when_record_pruned",
      bool(re_b.duplicate) and re_b.submit_qty == Decimal(0),
      {"action": re_b.action, "reasons": list(re_b.reason_codes)})
check("B_no_reclaim_audit_for_consumed",
      all(item["exit_key"] != STABLE for item in m.as_dict()["released_exits"]),
      m.as_dict()["released_exits"])


# ---- B2：账本条目被淘汰、幂等记忆仍在 -> 正是 TTL 巡检的 else 分支 ----------
m4 = new_manager()
m4.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                position_side="LONG", qty="1.0", current_qty="1.0", now_ms=1)
m4.confirm_exit(STABLE, engaged_qty=Decimal("1.0"))
saved_limit = ManagementReservationLedger.MAX_TERMINAL_RESERVATIONS
ManagementReservationLedger.MAX_TERMINAL_RESERVATIONS = 3
try:
    for index in range(8):                          # 淘汰账本里的 CONSUMED 记录
        key = f"small-{index}"
        m4.request_exit(exit_key=key, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                        position_side="LONG", qty="0.1", current_qty="1.0",
                        now_ms=300 + index)
        m4.confirm_exit(key, engaged_qty=Decimal("0.1"))
finally:
    ManagementReservationLedger.MAX_TERMINAL_RESERVATIONS = saved_limit
led_b2 = m4.ledger(DOMAIN)
info(f"B2_record_pruned={led_b2.reservation(STABLE) is None} "
     f"exit_key_count={m4.as_dict()['exit_key_count']}")
re_b2 = m4.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                        position_side="LONG", qty="1.0", current_qty="1.0",
                        now_ms=LATER, in_flight_ttl_ms=TTL)
info(f"B2_after_prune_same_key={{'action': {re_b2.action!r}, 'duplicate': {re_b2.duplicate},"
     f" 'qty': {str(re_b2.submit_qty)}, 'reasons': {list(re_b2.reason_codes)}}}")
check("B2_consumed_not_reclaimed_via_ttl_sweep",
      bool(re_b2.duplicate) and re_b2.submit_qty == Decimal(0),
      {"action": re_b2.action, "reasons": list(re_b2.reason_codes)})
check("B2_no_reclaim_audit_for_consumed",
      all(item["exit_key"] != STABLE for item in m4.as_dict()["released_exits"]),
      m4.as_dict()["released_exits"])


# ---- C：幂等记忆（_exit_keys）也被淘汰后，同键仍不得重发 --------------------
m2 = new_manager()
m2.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                position_side="LONG", qty="1.0", current_qty="1.0", now_ms=1)
m2.confirm_exit(STABLE, engaged_qty=Decimal("1.0"))
saved_keys = m2.MAX_EXIT_KEYS
m2.MAX_EXIT_KEYS = 0                                # 淘汰已终结的幂等键
for index in range(3):
    key = f"tiny-{index}"
    m2.request_exit(exit_key=key, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                    position_side="LONG", qty="0.1", current_qty="1.0",
                    now_ms=200 + index)
    m2.confirm_exit(key, engaged_qty=Decimal("0.1"))
m2.MAX_EXIT_KEYS = saved_keys
re_c = m2.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                       position_side="LONG", qty="1.0", current_qty="1.0",
                       now_ms=LATER, in_flight_ttl_ms=TTL)
info(f"C_after_key_eviction={{'action': {re_c.action!r}, 'duplicate': {re_c.duplicate},"
     f" 'accepted': {re_c.accepted}, 'qty': {str(re_c.submit_qty)}}}")
check("C_consumed_not_resubmitted_when_key_evicted",
      bool(re_c.duplicate) and re_c.submit_qty == Decimal(0),
      {"action": re_c.action, "accepted": re_c.accepted})


# ---- D：反向对照——被 release 的键必须仍可重发（P0-1 不得被修回去） ---------
m3 = new_manager()
m3.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                position_side="LONG", qty="1.0", current_qty="1.0", now_ms=1)
released = m3.release_exit(STABLE, reason="submit_failed")
retried = m3.request_exit(exit_key=STABLE, lifecycle_id=DOMAIN, symbol="BTCUSDT",
                          position_side="LONG", qty="1.0", current_qty="1.0",
                          now_ms=2, in_flight_ttl_ms=TTL)
info(f"D_released={released} retry={{'action': {retried.action!r},"
     f" 'qty': {str(retried.submit_qty)}}}")
check("D_released_key_can_still_resubmit",
      released is True and retried.accepted and retried.action == "SUBMIT_REDUCE",
      retried.as_dict())
check("D_released_key_never_opens_position", retried.opens_position is False)


# ---- E：守恒式 --------------------------------------------------------------
for label, ledger in (("B", led_b), ("C", m2.ledger(DOMAIN)), ("D", m3.ledger(DOMAIN))):
    ok = reservation_conserved(ledger)
    check(f"E_conserved_{label}", ok, "" if ok else ledger.as_dict())


print(f"\nR3_CONSUMED_PROBE tree={TREE.name} FAIL={len(FAIL)} {FAIL}")
raise SystemExit(1 if FAIL else 0)
