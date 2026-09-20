"""P1-2 复现/回归探针：``_open`` 在 qty≈0（chunks 为空）时应优雅跳过。

补丁 02 曾在 ``if not chunks:`` 分支插入 ``self._release_exit(reservation_key, ...)``，
而 ``reservation_key`` 在 ``_open`` 内未定义（全局名）→ 运行时 NameError，
把"有意的优雅跳过"变成一个异常（被逐 intent 的 except Exception 兜住，
只多记一条 severity=error 的 futures_order_error）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402


class _Db:
    def __init__(self) -> None:
        self.events: list[str] = []

    def log_risk_event(self, *, event_type: str, **_: Any) -> None:
        self.events.append(event_type)

    def resolve_risk_incidents_by_type(self, *_: Any, **__: Any) -> None:
        return None


class _Intent:
    symbol = "BTCUSDT"
    side = "LONG"
    qty = 0.0  # 名义数量为 0 -> chunks 为空
    order_type = "MARKET"
    leverage = 5
    margin_mode = "ISOLATED"
    client_order_id = None


def main() -> int:
    broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker.environment = "demo"
    broker.account_ref = "demo:acct-probe"
    broker.db = _Db()
    broker.trading_scope = None
    broker._symbol_filters = {}
    broker.positions = lambda: []  # type: ignore[method-assign]
    broker._signed = lambda *a, **k: {}  # type: ignore[method-assign]
    broker._market_max_qty = lambda s: 5000.0  # type: ignore[method-assign]
    broker.strategy_position_manager = None

    error = ""
    result: Any = "unset"
    try:
        result = broker._open(_Intent())
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    print(f"INFO _open qty=0 -> result={result!r} error={error!r}", flush=True)
    print(f"INFO db events={broker.db.events}", flush=True)
    ok = error == "" and result is None
    print(f"{'PASS' if ok else 'FAIL'} no_nameerror_and_graceful_skip {ok}",
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
