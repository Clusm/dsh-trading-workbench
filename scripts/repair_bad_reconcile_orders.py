"""用交易所真实成交流水，修正/标记被补记 bug 污染的 orders 记录。

背景
----
`_reconcile_one_close` 曾错误假设 `/fapi/v1/userTrades` 按时间倒序返回，
实际是正序且从账户史上最早成交开始，于是 limit=20 拿不到最近的成交，
遍历命中的是历史深处第一笔方向相反的成交。产物就是这几条 orders：

    id=1566 ETHUSDT   oid=16778351602   0.06 @ 2463.25   net_pnl=-0.059118
    id=1567 SOLUSDT   oid=4191385231    1    @ 105.95    net_pnl=-0.04238
    id=1588 UNIUSDT   oid=460044828     3    @ 4.919     net_pnl=-0.0059028

它们的价格、数量、盈亏全部来自错误的成交，会污染「最近订单」和盈亏统计。

本脚本做什么
------------
对每条 `order_source='exchange_sync'` 的记录，拿本地 orderId 去交易所
查这单的真实成交（按 orderId 聚合成交笔数、算加权均价、累加 realizedPnl），
和本地对比后给出修正方案。

**默认 dry-run，只打印不改库。确认无误后加 --apply 才写库。**
写库前会自动备份。

用法
----
    python scripts/repair_bad_reconcile_orders.py            # 预览
    python scripts/repair_bad_reconcile_orders.py --apply    # 实际修正
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from core.net_env import apply_proxy_env  # noqa: E402

apply_proxy_env()

from probe_exchange_positions import signed_get  # noqa: E402

DB_PATH = PROJECT_ROOT / "data" / "agent_trader.sqlite"


def _parse_iso_ms(value: str | None) -> int | None:
    import datetime as dt
    text = str(value or "").strip()
    if not text:
        return None
    try:
        norm = text.replace("Z", "+00:00")
        if "T" not in norm:
            norm = norm.replace(" ", "T", 1)
        d = dt.datetime.fromisoformat(norm)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def group_trades(trades: list, want_side: str, start_ms: int) -> dict:
    """按 orderId 聚合成交：同一单常被拆成多笔（3+8+9），合并后才是真实数量与均价。"""
    groups: dict[str, dict] = {}
    for t in trades:
        if str(t.get("side") or "").upper() != want_side:
            continue
        t_ms = int(t.get("time") or 0)
        if t_ms <= start_ms:
            continue
        oid = str(t.get("orderId") or "")
        if not oid:
            continue
        q = abs(float(t.get("qty") or 0))
        px = float(t.get("price") or 0)
        g = groups.setdefault(oid, {"qty": 0.0, "notional": 0.0, "realized": 0.0,
                                    "commission": 0.0, "time": t_ms, "fills": 0})
        g["qty"] += q
        g["notional"] += q * px
        g["realized"] += float(t.get("realizedPnl") or 0)
        g["commission"] += abs(float(t.get("commission") or 0))
        g["time"] = max(g["time"], t_ms)
        g["fills"] += 1
    return groups


def locate_true_close(symbol: str, position: dict) -> dict | None:
    """复刻修好的 _reconcile_one_close 的选单逻辑，定位真正的平仓成交。

    注意这里**不能**拿本地记录的 orderId 去查——本地 orderId 本身可能就是
    bug 抓错的陈年成交。必须从持仓出发重新定位：方向相反、时间晚于开仓、
    数量与持仓量匹配，取时间最新的那一单。
    """
    open_side = str(position["side"]).upper()
    want_side = "SELL" if open_side == "LONG" else "BUY"
    open_qty = abs(float(position["qty"] or 0))
    opened_ms = _parse_iso_ms(position.get("opened_at"))
    start_ms = (opened_ms if opened_ms else 0) - 10 * 60 * 1000

    trades = signed_get("/fapi/v1/userTrades",
                        {"symbol": symbol, "limit": 200, "startTime": start_ms})
    if not isinstance(trades, list):
        return None
    groups = group_trades(trades, want_side, start_ms)

    for oid, g in sorted(groups.items(), key=lambda kv: kv[1]["time"], reverse=True):
        if g["qty"] <= 0 or g["notional"] <= 0:
            continue
        if g["qty"] > open_qty * 1.05:
            continue
        price = g["notional"] / g["qty"]
        realized = g["realized"]
        if abs(realized) < 1e-12 and float(position.get("entry_price") or 0) > 0:
            entry = float(position["entry_price"])
            diff = (price - entry) if open_side == "LONG" else (entry - price)
            realized = diff * g["qty"]
        return {"oid": oid, "qty": g["qty"], "price": price,
                "net_pnl": realized - g["commission"],
                "commission": g["commission"], "fills": g["fills"],
                "time": g["time"], "margin": round(g["qty"] * price / 10, 8)}
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="修正被补记 bug 污染的 orders 记录")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只预览）")
    args = ap.parse_args()

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, symbol, exchange_order_id, qty, price, raw_json, created_at "
        "FROM orders WHERE order_source='exchange_sync' ORDER BY id").fetchall()

    if not rows:
        print("没有 exchange_sync 补记单，无需处理")
        return 0

    plans: list[dict] = []
    for r in rows:
        local_oid = str(r["exchange_order_id"])
        sym = r["symbol"]
        raw = json.loads(r["raw_json"] or "{}")
        print(f"\n--- order id={r['id']}  {sym}  本地 oid={local_oid} ---")
        print(f"  本地记录: qty={r['qty']:g} @ {r['price']}  net_pnl={raw.get('net_pnl')}")

        # 找到这笔补记对应的持仓：时间上离补记单最近的那条同 symbol 记录
        pos = db.execute(
            "SELECT symbol, side, qty, entry_price, opened_at FROM positions "
            "WHERE symbol=? ORDER BY ABS(julianday(closed_at) - julianday(?)) LIMIT 1",
            (sym, r["created_at"])).fetchone()
        if not pos:
            print("  ⚠ 找不到对应持仓，跳过")
            plans.append({"id": r["id"], "action": "skip", "symbol": sym})
            continue
        pos = dict(pos)
        print(f"  对应持仓: {pos['side']} qty={pos['qty']:g} "
              f"entry={pos['entry_price']} opened_at={pos['opened_at']}")

        try:
            true = locate_true_close(sym, pos)
        except Exception as exc:  # noqa: BLE001
            print(f"  查交易所失败：{type(exc).__name__} {exc}")
            plans.append({"id": r["id"], "action": "skip", "symbol": sym})
            continue

        if true is None:
            print("  ⚠ 按持仓重新定位不到平仓成交，需人工判断")
            plans.append({"id": r["id"], "action": "skip", "symbol": sym})
            continue

        # 置信度判据：定位到的成交数量必须基本覆盖持仓量（允许部分平仓，
        # 但不能是「0.01 vs 持仓 0.12」这种明显对不上的）。对不上说明
        # 持仓关联就错了，此时定位结果同样不可信，必须交给人工。
        pos_qty = abs(float(pos["qty"] or 0))
        ratio = (true["qty"] / pos_qty) if pos_qty > 0 else 0.0
        confident = 0.9 <= ratio <= 1.05

        print(f"  真实平仓: oid={true['oid']} qty={true['qty']:g} @ {true['price']:.6f} "
              f"net_pnl={true['net_pnl']:+.6f}（{true['fills']} 笔成交）")
        print(f"  数量/持仓 = {ratio:.3f} → {'高置信' if confident else '低置信（持仓关联存疑）'}")

        if not confident:
            print("  ⚠ 持仓关联不可靠，不敢自动改，转人工判断")
            plans.append({"id": r["id"], "action": "manual", "symbol": sym,
                          "reason": f"定位成交 {true['qty']:g} 与持仓 {pos_qty:g} 不匹配"})
            continue

        wrong_oid = true["oid"] != local_oid
        qty_diff = abs(float(r["qty"]) - true["qty"]) > 1e-9
        px_diff = abs(float(r["price"]) - true["price"]) > 1e-6
        pnl_diff = abs(float(raw.get("net_pnl") or 0) - true["net_pnl"]) > 1e-6

        if wrong_oid:
            print(f"  ❌ 抓错单：本地 oid={local_oid} 不是这笔平仓，应为 {true['oid']}")
        if qty_diff or px_diff or pnl_diff:
            print(f"  数值偏差: qty {r['qty']:g}→{true['qty']:g}, "
                  f"px {r['price']}→{true['price']:.6f}, "
                  f"pnl {raw.get('net_pnl')}→{true['net_pnl']:+.6f}")

        if not (wrong_oid or qty_diff or px_diff or pnl_diff):
            print("  ✅ 与交易所一致，无需修改")
            plans.append({"id": r["id"], "action": "ok", "symbol": sym})
            continue

        plans.append({"id": r["id"], "action": "fix", "symbol": sym, **true})

    print("\n" + "=" * 70)
    print("汇总")
    for p in plans:
        if p["action"] == "fix":
            print(f"  修正 id={p['id']} {p['symbol']}: → oid={p['oid']} qty={p['qty']:g} "
                  f"@ {p['price']:.6f} net_pnl={p['net_pnl']:+.6f}")
        elif p["action"] == "ok":
            print(f"  跳过 id={p['id']} {p['symbol']}: 与交易所一致")
        elif p["action"] == "manual":
            print(f"  ⚠ id={p['id']} {p['symbol']}: 转人工 —— {p.get('reason')}")
        else:
            print(f"  ⚠ id={p['id']} {p['symbol']}: 无法自动判定，需人工处理")

    if not args.apply:
        print("\n[dry-run] 未改动任何数据。确认无误后加 --apply 执行。")
        return 0

    backup = DB_PATH.with_suffix(
        f".sqlite.bak_repair_{__import__('time').strftime('%Y%m%d_%H%M%S')}")
    shutil.copy(DB_PATH, backup)
    print(f"\n已备份：{backup.name}")

    n = 0
    for p in plans:
        if p["action"] != "fix":
            continue
        db.execute(
            "UPDATE orders SET exchange_order_id=?, client_order_id=?, qty=?, price=?, raw_json=? "
            "WHERE id=?",
            (p["oid"], f"mta-sync-{p['symbol']}-{p['oid']}", p["qty"], round(p["price"], 8),
             json.dumps({
                 "action": "close",
                 "net_pnl": p["net_pnl"],
                 "commission": -p["commission"],
                 "margin": p["margin"],
                 "fills": p["fills"],
                 "reconciled": True,
                 "repaired": True,
                 "note": "由 repair_bad_reconcile_orders.py 按交易所真实成交修正",
             }, ensure_ascii=False), p["id"]))
        n += 1
    db.commit()
    db.close()
    print(f"已修正 {n} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
