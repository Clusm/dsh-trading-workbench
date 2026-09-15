"""实盘接管前只读核对：强制 GET，输出仓位与挂单摘要，不输出凭据。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.config_loader import load_settings  # noqa: E402
from core.net_env import apply_proxy_env  # noqa: E402
from data.rate_limit import configure_rate_limit_state  # noqa: E402
from exchange.binance_futures import (  # noqa: E402
    BinanceFuturesBroker,
    assess_takeover_compatibility,
)


class ReadOnlyBroker(BinanceFuturesBroker):
    def _request(self, method, url, **kwargs):
        if method != "GET":
            raise RuntimeError("接管预检只允许 GET")
        return super()._request(method, url, **kwargs)


def main() -> int:
    apply_proxy_env()
    settings = load_settings(ROOT, profile="live")
    configure_rate_limit_state(settings.rate_limit_state)
    key = os.getenv("BINANCE_LIVE_API_KEY", "")
    secret = os.getenv("BINANCE_LIVE_API_SECRET", "")
    if not key or not secret:
        print(json.dumps({"ok": False, "reason": "live_credentials_missing"}))
        return 1
    with tempfile.TemporaryDirectory(prefix="mta-live-preflight-") as tmp:
        broker = None
        try:
            broker = ReadOnlyBroker(key, secret, base_url="https://fapi.binance.com",
                                    environment="live", ledger_file=Path(tmp) / "ledger.json")
            account = broker.account(force=True)
            positions = broker.positions(force=True)
            orders = broker.open_orders()
            algos = broker.open_algo_orders()
            position_mode = broker._signed("GET", "/fapi/v1/positionSide/dual", {})
            takeover = assess_takeover_compatibility(
                position_mode if isinstance(position_mode, dict) else None,
                positions, orders, algos,
            )
            equity = float(account.get("totalMarginBalance") or 0.0)
            max_margin_usage = float(settings.raw_risk.get(
                "strong_max_margin_usage",
                settings.raw_risk.get("max_margin_usage", 0.80),
            ))
            total_margin = sum(broker._margin_from_position(row) for row in positions)
            margin_limit = equity * max_margin_usage
            margin_breached = equity <= 0 or total_margin > margin_limit
            margin_reason = (
                "账户权益不可用，无法核验总保证金上限" if equity <= 0 else
                f"存量总保证金 {total_margin:.2f}U 超过风险档上限 {margin_limit:.2f}U"
                if margin_breached else ""
            )
            takeover["margin_cap"] = {
                "evaluated": equity > 0,
                "breached": margin_breached,
                "equity": equity,
                "total_margin": total_margin,
                "limit": margin_limit,
                "max_margin_usage": max_margin_usage,
                "reason": margin_reason,
            }
            if margin_breached:
                takeover["ok"] = False
                takeover["reason"] = "；".join(
                    value for value in (str(takeover.get("reason") or ""), margin_reason)
                    if value
                )
            result = {
                "ok": True, "environment": "live", "can_trade": account.get("canTrade"),
                "wallet_balance": account.get("totalWalletBalance"),
                "margin_balance": account.get("totalMarginBalance"),
                "available_balance": account.get("availableBalance"),
                "positions": [{k: p.get(k) for k in (
                    "symbol", "positionSide", "positionAmt", "entryPrice", "markPrice",
                    "unRealizedProfit", "leverage", "marginType") } for p in positions],
                "orders": [{k: p.get(k) for k in (
                    "symbol", "side", "positionSide", "type", "origQty", "executedQty",
                    "price", "stopPrice", "reduceOnly", "closePosition", "status") } for p in orders],
                "algo_orders": [{k: p.get(k) for k in (
                    "symbol", "side", "positionSide", "orderType", "quantity",
                    "triggerPrice", "reduceOnly", "closePosition", "algoStatus") } for p in algos],
                "takeover": takeover,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            # 网络异常可能包含签名 URL；仅输出类型及有限的错误码。
            import re
            code = re.search(r"Binance (-?\d+):", str(exc))
            print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                              "exchange_code": code.group(1) if code else None}))
            return 1
        finally:
            if broker is not None:
                broker.close()


if __name__ == "__main__":
    raise SystemExit(main())
