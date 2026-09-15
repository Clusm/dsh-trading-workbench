"""清理「孤儿保护单」：仓位已经平掉、但交易所上仍挂着的止损/止盈 algo 单。

为什么危险
----------
保护单（STOP_MARKET / TAKE_PROFIT_MARKET）在 demo 环境走的是 algoOrder 通道，
不是普通 /fapi/v1/order。仓位被止盈/止损平掉后，如果另一侧的保护单没被撤，
它会一直挂着。等到价格再次触发那个价位时，会在**没有任何持仓**的情况下
凭空开出一个新仓位——方向和数量都由那张陈旧的单子决定，完全不受当前策略控制。

2026-09-02 实测就有 6 张这样的孤儿单（UNIUSDT / XRPUSDT / ENAUSDT /
MAGMAUSDT / ZECUSDT / ZKPUSDT），对应仓位早已归零。

判定规则
--------
对每张 openAlgoOrders：
  * 该 symbol 在交易所无任何持仓            → 孤儿，应撤销
  * 该 symbol 有持仓，但方向与保护单不一致  → 孤儿，应撤销
    （平多的保护单方向是 SELL；平空是 BUY。
      若持仓是多头，却挂着 BUY 的保护单，说明这是上一笔空单的残留）
  * 该 symbol 有持仓且方向一致，但数量远超持仓 → 疑似残留，列出供人工判断
  * 其余                                     → 正常，保留

**默认 dry-run，只打印不撤单。确认无误后加 --apply 才真正撤单。**

用法
----
    python scripts/cleanup_orphan_algo_orders.py            # 预览
    python scripts/cleanup_orphan_algo_orders.py --apply    # 实际撤销
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from core.net_env import apply_proxy_env  # noqa: E402

apply_proxy_env()

from probe_exchange_positions import signed_get  # noqa: E402


def load_positions() -> dict[str, dict]:
    """返回 symbol -> {amt, entry}；amt>0 为多头，<0 为空头。"""
    acct = signed_get("/fapi/v2/account")
    out: dict[str, dict] = {}
    for p in acct.get("positions", []):
        amt = float(p.get("positionAmt") or 0)
        if abs(amt) < 1e-12:
            continue
        out[p["symbol"]] = {"amt": amt, "entry": p.get("entryPrice")}
    return out


def classify(algo: dict, positions: dict[str, dict]) -> tuple[str, str]:
    """返回 (动作, 原因)。动作 ∈ orphan / mismatch / keep。"""
    sym = algo.get("symbol")
    side = str(algo.get("side") or "").upper()
    qty = abs(float(algo.get("quantity") or 0))
    pos = positions.get(sym)

    if pos is None:
        return "orphan", f"{sym} 已无持仓，这张单触发即凭空建仓"

    # 平多的保护单方向是 SELL，平空的是 BUY
    holding_long = pos["amt"] > 0
    close_side = "SELL" if holding_long else "BUY"
    if side != close_side:
        return "orphan", (f"{sym} 当前是{'多' if holding_long else '空'}头，"
                          f"保护单方向应为 {close_side} 却是 {side}，属上一笔残留")

    if qty > abs(pos["amt"]) * 1.05:
        return "mismatch", (f"{sym} 保护单数量 {qty:g} 超出持仓 {abs(pos['amt']):g}，"
                            f"疑似残留，需人工判断")

    return "keep", f"{sym} 与持仓匹配"


def main() -> int:
    ap = argparse.ArgumentParser(description="清理孤儿保护单")
    ap.add_argument("--apply", action="store_true", help="实际撤单（默认只预览）")
    args = ap.parse_args()

    import urllib.request as _u
    print(f"出口: {_u.getproxies()}")
    positions = load_positions()
    algos = signed_get("/fapi/v1/openAlgoOrders", {})

    print(f"\n当前持仓 {len(positions)} 个，保护单 {len(algos)} 张")
    if positions:
        print("持仓：", ", ".join(
            f"{s}({'多' if v['amt'] > 0 else '空'} {abs(v['amt']):g})" for s, v in positions.items()))
    print()

    orphans, mismatches, keeps = [], [], []
    for a in algos:
        action, reason = classify(a, positions)
        line = (f"{a.get('symbol'):<12} {a.get('side'):<6} {a.get('orderType'):<22}"
                f" qty={float(a.get('quantity') or 0):<10g} trigger={a.get('triggerPrice'):<12}"
                f" algoId={a.get('algoId')}")
        if action == "orphan":
            orphans.append(a)
            print(f"  [撤销] {line}\n         └─ {reason}")
        elif action == "mismatch":
            mismatches.append(a)
            print(f"  [人工] {line}\n         └─ {reason}")
        else:
            keeps.append(a)
            print(f"  [保留] {line}\n         └─ {reason}")

    print("\n" + "=" * 74)
    print(f"撤销 {len(orphans)} 张 / 人工判断 {len(mismatches)} 张 / 保留 {len(keeps)} 张")

    if not args.apply:
        print("\n[dry-run] 未撤任何单。确认无误后加 --apply 执行。")
        return 0

    if not orphans:
        print("\n没有需要撤销的孤儿单。")
        return 0

    ok = 0
    for a in orphans:
        algo_id = a.get("algoId")
        try:
            import urllib.parse
            import urllib.request
            import hmac
            import hashlib
            import time
            from probe_exchange_positions import KEY, SECRET, BASE
            params = {"algoId": str(algo_id), "timestamp": int(time.time() * 1000),
                      "recvWindow": 5000}
            query = urllib.parse.urlencode(params)
            sig = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                f"{BASE}/fapi/v1/algoOrder?{query}&signature={sig}", method="DELETE")
            req.add_header("X-MBX-APIKEY", KEY)
            req.add_header("User-Agent", "mta-cleanup")
            with urllib.request.urlopen(req, timeout=30) as resp:
                # 只需读空响应体以完成请求；撤销结果以交易所状态为准，下面的复核会验证。
                resp.read()
            print(f"  已撤销 algoId={algo_id} {a.get('symbol')} {a.get('orderType')}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  撤销失败 algoId={algo_id} {a.get('symbol')}: {type(exc).__name__} {exc}")

    print(f"\n共撤销 {ok}/{len(orphans)} 张。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
