"""直接查交易所真实持仓 / 挂单，用于核对本地台账与交易所是否一致。

用法：
    python scripts/probe_exchange_positions.py

只做只读查询（GET /fapi/v3/positionRisk、GET /fapi/v3/openOrders），不下任何单。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# U10b 归档：本项目根深度已 +1（见 docs/archive/README.md 的归档索引）。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 在任何网络调用前固定出口
from core.net_env import apply_proxy_env  # noqa: E402

apply_proxy_env()

BASE = "https://demo-fapi.binance.com"


def _load_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    p = PROJECT_ROOT / ".env"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


_ENV = _load_env_file()
KEY = os.environ.get("BINANCE_DEMO_API_KEY") or _ENV.get("BINANCE_DEMO_API_KEY", "")
SECRET = os.environ.get("BINANCE_DEMO_API_SECRET") or _ENV.get("BINANCE_DEMO_API_SECRET", "")


def signed_get(path: str, params: dict | None = None) -> object:
    if not KEY or not SECRET:
        raise SystemExit("缺少 BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET")
    q = dict(params or {})
    q["timestamp"] = int(time.time() * 1000)
    q["recvWindow"] = 5000
    query = urllib.parse.urlencode(q)
    sig = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE}{path}?{query}&signature={sig}"
    req = urllib.request.Request(url)
    req.add_header("X-MBX-APIKEY", KEY)
    req.add_header("User-Agent", "mta-probe")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    print(f"出口: {urllib.request.getproxies()}")
    print(f"BASE: {BASE}")
    print("=" * 70)

    acct = signed_get("/fapi/v3/account")
    print("== 账户 ==")
    print(f"  totalWalletBalance      = {acct.get('totalWalletBalance')}")
    print(f"  totalUnrealizedProfit   = {acct.get('totalUnrealizedProfit')}")
    print(f"  totalMarginBalance      = {acct.get('totalMarginBalance')}")
    print(f"  availableBalance        = {acct.get('availableBalance')}")
    print(f"  canTrade                = {acct.get('canTrade')}")
    print()

    print("== 交易所真实持仓 (|amt| > 0) ==")
    n = 0
    for p in acct.get("positions", []):
        amt = float(p.get("positionAmt", 0) or 0)
        if abs(amt) < 1e-12:
            continue
        n += 1
        # demo 账户偶有字段为 None（如刚开仓 entryPrice 还没回填），统一兜底
        def f(v, w=12):
            return format(v, f"<{w}") if v is not None else " " * w
        print(
            f"  {p['symbol']:<12} amt={amt:<10.4f} entry={f(p.get('entryPrice'))}"
            f" mark={f(p.get('markPrice'))} upnl={f(p.get('unrealizedProfit'))}"
            f" lever={f(p.get('leverage'), 6)} marginType={p.get('marginType')}"
        )
    if n == 0:
        print("  （交易所侧无任何持仓）")
    print(f"  持仓合约数 = {n}")
    print()

    print("== 未成交挂单 ==")
    try:
        orders = signed_get("/fapi/v1/openOrders")
    except Exception as exc:  # demo 环境该路由偶发 404，不影响持仓结论
        print(f"  （查询失败：{exc}）")
        orders = []
    if not orders:
        print("  （无挂单）")
    for o in orders:
        print(
            f"  {o['symbol']:<12} {o['side']:<6} {o['type']:<22}"
            f" qty={o['origQty']:<8} stop={o.get('stopPrice'):<10} id={o['orderId']}"
        )
    print(f"  挂单数 = {len(orders)}")
    print()

    # 成交流水：用于核对「开 10.0 / 补记平 3.0」这种数量不一致
    since = int((time.time() - 3 * 24 * 3600) * 1000)  # 近 3 天
    print("== 近 3 天成交流水（含手续费与已实现盈亏） ==")
    trades = signed_get("/fapi/v1/userTrades", {"startTime": since, "limit": 200})
    if not trades:
        print("  （无成交）")
    for t in trades:
        print(
            f"  {t.get('time')} {t['symbol']:<12} {t['side']:<6}"
            f" qty={t['qty']:<10} price={t['price']:<12}"
            f" realizedPnl={t.get('realizedPnl')} commission={t.get('commission')}"
            f" {t.get('commissionAsset')}"
        )
    print(f"  成交笔数 = {len(trades)}")


if __name__ == "__main__":
    main()
