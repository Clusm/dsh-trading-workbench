"""P0 复现/回归探针：退出预留泄漏 -> 该 (env,account,symbol,side) 域永久不可退出。

真实代码路径：``BinanceFuturesBroker._close_or_reduce`` / ``_reserve_exit`` /
``_settle_exit`` / ``_release_exit`` 与 ``StrategyPositionManager.request_exit``；
只把网络与账本副作用桩掉（不联网、不下单撤单、不读 .env/data）。

用例：占量成功后 ``_submit_standard_order`` 抛"确定性拒绝"（HTTP 400
reduceOnly rejected，正是 F03/R09 的并发 TP 场景）。随后：
  [A] 换新 key 重试退出 -> 是否还能退出？
  [B] 用同一稳定 key（guardian `mta-g-<sha24>`）重试 -> 是否还能退出？
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.strategy_position_manager import StrategyPositionManager  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: Any = "") -> None:
    RESULTS.append((bool(ok), name))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


class _Db:
    """最小 db 替身：只记录事件类型。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def log_risk_event(self, *, event_type: str, **_: Any) -> None:
        self.events.append(event_type)

    def fetch_one(self, *_: Any, **__: Any) -> None:
        return None


class _Intent:
    def __init__(self, *, qty: float, cid: str) -> None:
        self.symbol = "BTCUSDT"
        self.side = "LONG"
        self.qty = qty
        self.order_type = "MARKET"
        self.client_order_id = cid
        self.trade_level = "short"
        self.stop_loss = 1.0
        self.take_profit = 2.0
        self.order_source = "ai"


def _make_broker(*, manager: StrategyPositionManager, submit_raises: Any) -> Any:
    broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker.environment = "demo"
    broker.mode = "demo"
    broker.account_ref = "demo:acct-probe"
    broker.account_ref_source = "declared"
    broker.db = _Db()
    broker.trade_reconciliation = None
    broker.strategy_position_manager = manager
    pos = {"symbol": "BTCUSDT", "positionAmt": "1.0", "entryPrice": "100.0"}
    broker._single_symbol_position = (  # type: ignore[method-assign]
        lambda symbol, force=False: dict(pos))
    broker._position_side = lambda p: "LONG"  # type: ignore[method-assign]
    broker._ledger_source = lambda s: ("ai", {})  # type: ignore[method-assign]
    broker._market_max_qty = lambda s: 5000.0  # type: ignore[method-assign]
    broker._format_qty = lambda s, q: f"{float(q):.3f}"  # type: ignore[method-assign]
    broker._standard_order_mode_params = lambda side, reduce=False: {}  # type: ignore[method-assign]
    broker._wait_fill = lambda s, oid: None  # type: ignore[method-assign]
    broker._fill_confirmed = lambda d: True  # type: ignore[method-assign]
    broker._order_trade_fee = lambda s, oid: (None, None)  # type: ignore[method-assign]
    broker._position_margin = lambda s: 0.0  # type: ignore[method-assign]
    broker._invalidate_cache = lambda *a, **k: None  # type: ignore[method-assign]
    broker._log_order = lambda **k: None  # type: ignore[method-assign]
    broker._record_trade_outcome = lambda **k: None  # type: ignore[method-assign]
    broker._remove_ledger_position = lambda s: None  # type: ignore[method-assign]
    broker._record_ledger_position = lambda *a, **k: None  # type: ignore[method-assign]
    broker._resync_protection = lambda s, intent=None: None  # type: ignore[method-assign]
    broker._ensure_lifecycle_from_snapshot = lambda *a, **k: None  # type: ignore[method-assign]
    broker._reconcile_stored_lifecycle = lambda *a, **k: None  # type: ignore[method-assign]
    broker._reconcile_job_retry = lambda *a, **k: None  # type: ignore[method-assign]
    broker._reservation_domain = "demo:demo:acct-probe:BTCUSDT:LONG"

    submitted: list[dict[str, Any]] = []

    def _submit(params: dict[str, Any], *, purpose: str = "") -> dict[str, Any]:
        submitted.append(dict(params))
        # submit_raises 可以是 bool 或 {"raise": bool} 开关（模拟交易所恢复）。
        should_raise = (bool(submit_raises.get("raise"))
                        if isinstance(submit_raises, dict) else bool(submit_raises))
        if should_raise:
            # 与 `_submit_standard_order` 对"确定性拒绝"的处理一致：重新 raise。
            raise RuntimeError("Binance HTTP 400 reduceOnly rejected")
        return {"orderId": 1, "status": "FILLED", "avgPrice": "100.0"}

    broker._submit_standard_order = _submit  # type: ignore[method-assign]
    broker.submitted = submitted  # type: ignore[attr-defined]
    return broker


def main() -> int:
    manager = StrategyPositionManager(
        account_ref="demo:acct-probe", environment="demo")
    stable_key = "mta-g-" + "a" * 24  # position_guardian 的稳定退出 key
    domain = "demo:demo:acct-probe:BTCUSDT:LONG"

    broker = _make_broker(manager=manager, submit_raises=True)
    intent = _Intent(qty=1.0, cid=stable_key)

    # ---- 1. 占量成功后提交异常 -------------------------------------------
    raised = ""
    try:
        broker._close_or_reduce(intent)
    except Exception as exc:  # noqa: BLE001
        raised = f"{type(exc).__name__}: {exc}"
    check("submit_error_propagates", raised.startswith("RuntimeError"), raised)

    ledger = manager.ledger(domain)
    active = [r for r in ledger.active_reservations()]
    print(f"INFO ledger={ledger.as_dict()}", flush=True)
    check("exception_path_releases_reservation", not active,
          [(r.reservation_id, str(r.remaining_qty), r.status) for r in active])
    check("released_reservation_restores_available_qty",
          ledger.available_qty == Decimal("1.0"), ledger.available_qty)

    # ---- 2. 稳定 key 直接重试（guardian 每轮复用同一个 key） --------------
    same = manager.request_exit(
        exit_key=stable_key, lifecycle_id=domain, symbol="BTCUSDT",
        position_side="LONG", qty=1.0, current_qty=1.0, now_ms=2_000)
    check("stable_key_can_resubmit_after_release",
          same.accepted and same.submit_qty == Decimal("1.0")
          and same.action == "SUBMIT_REDUCE",
          {"action": same.action, "duplicate": same.duplicate,
           "reason_codes": list(same.reason_codes)})

    # ---- 3. 成交确认后同 key 保持幂等（不得二次减仓） ---------------------
    check("confirm_exit_consumes_reservation", manager.confirm_exit(
        stable_key, engaged_qty=Decimal("1.0")) is True)
    consumed = manager.request_exit(
        exit_key=stable_key, lifecycle_id=domain, symbol="BTCUSDT",
        position_side="LONG", qty=1.0, current_qty=0.0, now_ms=3_000)
    check("consumed_key_stays_duplicate_skipped",
          bool(consumed.duplicate) and consumed.submit_qty == Decimal(0),
          {"action": "duplicate_skipped" if consumed.duplicate else consumed.action,
           "reason_codes": list(consumed.reason_codes)})

    # ---- 4. 交易所已恢复：该标的还能不能退出？（活性判定） -----------------
    # 全新管理器 + 同样的稳定 key：模拟"占量->异常->下一轮"（进程内真实形态）。
    manager2 = StrategyPositionManager(
        account_ref="demo:acct-probe", environment="demo")
    exchange = {"raise": True}
    broker2 = _make_broker(manager=manager2, submit_raises=exchange)
    round_results: list[str] = []
    submitted = 0
    for round_no in (1, 2, 3):
        before = len(broker2.submitted)
        try:
            broker2._close_or_reduce(intent)
        except Exception as exc:  # noqa: BLE001
            round_results.append(f"{type(exc).__name__}")
        else:
            round_results.append("submitted" if len(broker2.submitted) > before
                                 else "none")
        # 交易所恢复：下一轮同 key 重试（guardian 每轮复用同一个稳定 key）。
        exchange["raise"] = False
        before2 = len(broker2.submitted)
        try:
            broker2._close_or_reduce(intent)
        except Exception as exc:  # noqa: BLE001
            round_results.append(f"{type(exc).__name__}")
        else:
            round_results.append("submitted" if len(broker2.submitted) > before2
                                 else "none")
        submitted = len(broker2.submitted)
    print(f"INFO rounds={round_results} submitted={submitted}", flush=True)
    check("symbol_can_still_exit_after_submit_recovers",
          submitted >= 1 and "submitted" in round_results,
          f"{round_results} submitted={submitted}")

    fails = [name for ok, name in RESULTS if not ok]
    print(f"\nP0 PROBE {'FAIL=' + str(len(fails)) if fails else 'FAIL=0'}", flush=True)
    for name in fails:
        print(f"  - {name}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
