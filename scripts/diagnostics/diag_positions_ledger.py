"""诊断 positions 台账重复问题（只读，不修改任何数据）。

输出：
1. 台账里 OPEN 条目的分组统计（按 symbol+side+entry_price 聚合，看重复度）
2. 重复组的 opened_at 分布，判断是"每轮插入一条"还是"某个时刻爆发"
3. 与交易所真实持仓对比（需要 10808 代理）
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# U10b 归档：本项目根深度已 +1（见 docs/archive/README.md 的归档索引）。
ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "agent_trader.sqlite"

CHECK_EXCHANGE = "--no-exchange" not in sys.argv


def ledger_stats() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=" * 96)
    print("【1】台账 OPEN 条目分组统计（按 symbol / side / entry_price 聚合）")
    print("=" * 96)
    rows = list(con.execute(
        "SELECT id, symbol, side, qty, entry_price, leverage, stop_loss, take_profit,"
        "       opened_at, source, ticket_id, strategy_id "
        "FROM positions WHERE status='OPEN' ORDER BY symbol, side, id"))

    groups: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[(r["symbol"], r["side"], round(float(r["entry_price"] or 0), 8))].append(r)

    print(f"OPEN 条目总数 {len(rows)}，去重后 {len(groups)} 组")
    print()
    print(f"{'symbol':<10}{'side':<7}{'entry':>11}{'条数':>6}{'总qty':>10}   opened_at 首次 → 末次")
    print("-" * 96)
    for (sym, side, ep), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        tot_q = sum(float(i["qty"] or 0) for i in items)
        first, last = items[0]["opened_at"][:19], items[-1]["opened_at"][:19]
        flag = "  <<< 重复" if len(items) > 1 else ""
        print(f"{sym:<10}{side:<7}{ep:>11.2f}{len(items):>6}{tot_q:>10.4f}   {first} → {last}{flag}")

    print()
    print("=" * 96)
    print("【2】重复组的 opened_at 时间分布（判断插入模式）")
    print("=" * 96)
    for (sym, side, ep), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(items) < 2:
            continue
        stamps = [i["opened_at"][:19] for i in items]
        deltas = []
        for a, b in zip(stamps, stamps[1:]):
            from datetime import datetime
            da, db = datetime.fromisoformat(a), datetime.fromisoformat(b)
            deltas.append((db - da).total_seconds())
        if deltas:
            print(f"  {sym} {side} @{ep}: {len(items)} 条")
            print(f"    间隔(s): 中位数 {sorted(deltas)[len(deltas)//2]:.0f}  "
                  f"最小 {min(deltas):.0f}  最大 {max(deltas):.0f}")
            print(f"    前 5 个 opened_at: {stamps[:5]}")

    print()
    print("=" * 96)
    print("【3】id 连续性：重复条目是连续 id 还是分散的？")
    print("=" * 96)
    for (sym, side, ep), items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(items) < 2:
            continue
        ids = [i["id"] for i in items]
        print(f"  {sym} {side} @{ep}: id {ids[:12]}{' ...' if len(ids) > 12 else ''}")

    con.close()


def exchange_positions() -> list[dict]:
    """查交易所真实持仓（demo 永续）。需要 10808 代理。"""
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    import httpx

    sys.path.insert(0, str(ROOT / "src"))
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

    base = env.get("BINANCE_DEMO_BASE_URL") or "https://demo-fapi.binance.com"
    key = env.get("BINANCE_API_KEY") or env.get("BINANCE_DEMO_API_KEY") or ""
    sec = env.get("BINANCE_API_SECRET") or env.get("BINANCE_DEMO_API_SECRET") or ""
    if not (key and sec):
        print("  找不到 API key/secret（检查 .env 里的 BINANCE_* 变量）")
        return []

    qs = urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 5000})
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    headers = {"X-MBX-APIKEY": key}
    proxy = "http://127.0.0.1:10808"
    try:
        r = httpx.get(f"{base}/fapi/v2/positionRisk", params=f"{qs}&signature={sig}",
                      headers=headers, proxy=proxy, timeout=25.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  查询失败: {type(e).__name__}: {str(e)[:120]}")
        return []
    return [d for d in data if abs(float(d.get("positionAmt") or 0)) > 1e-9]


def main() -> None:
    ledger_stats()

    if not CHECK_EXCHANGE:
        return
    print()
    print("=" * 96)
    print("【4】交易所真实持仓（demo-fapi /fapi/v2/positionRisk，走 10808 代理）")
    print("=" * 96)
    live = exchange_positions()
    if not live:
        print("  交易所当前无持仓，或查询失败")
        return
    print(f"{'symbol':<12}{'side':<8}{'amt':>12}{'entry':>12}{'leverage':>10}{'unPnL':>12}")
    print("-" * 96)
    for d in live:
        amt = float(d.get("positionAmt") or 0)
        side = "LONG" if amt > 0 else "SHORT"
        print(f"{d.get('symbol',''):<12}{side:<8}{abs(amt):>12.4f}"
              f"{float(d.get('entryPrice') or 0):>12.2f}"
              f"{str(d.get('leverage')):>10}{float(d.get('unRealizedProfit') or 0):>12.4f}")

    print()
    print("=" * 96)
    print("【5】对账：台账 vs 交易所")
    print("=" * 96)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    led: dict[tuple, float] = defaultdict(float)
    for r in con.execute(
            "SELECT symbol, side, qty FROM positions WHERE status='OPEN'"):
        led[(r["symbol"], r["side"])] += float(r["qty"] or 0)
    exc: dict[tuple, float] = {}
    for d in live:
        amt = float(d.get("positionAmt") or 0)
        exc[(d.get("symbol", ""), "LONG" if amt > 0 else "SHORT")] = abs(amt)
    keys = sorted(set(led) | set(exc))
    print(f"{'symbol':<12}{'side':<8}{'台账合计':>14}{'交易所':>14}{'差额':>14}   判定")
    print("-" * 96)
    for k in keys:
        a, b = led.get(k, 0.0), exc.get(k, 0.0)
        diff = a - b
        if b == 0 and a > 0:
            verdict = "台账有、交易所无（幽灵持仓）"
        elif a == 0 and b > 0:
            verdict = "交易所有、台账无（漏记）"
        elif abs(diff) < 1e-9:
            verdict = "一致"
        else:
            verdict = f"数量不符（放大 {a/b:.1f}x）" if b else "数量不符"
        print(f"{k[0]:<12}{k[1]:<8}{a:>14.4f}{b:>14.4f}{diff:>14.4f}   {verdict}")
    con.close()


if __name__ == "__main__":
    main()
