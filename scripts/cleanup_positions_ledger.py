"""清理 positions 表里的幽灵 OPEN 台账（把重复/已平的旧行收口成 CLOSED）。

背景
----
positions 是**流水表**：sync_positions 每次发现"仓位变了"就插一条 OPEN。
但它原来的去重判据 `SELECT MAX(id) ... GROUP BY symbol` 漏了 side，
双向持仓（Hedge mode）下每个 symbol 只能取到一侧的行，
于是 LONG/SHORT 交替时永远有一侧匹配不上，每个慢循环都再插一条。
结果：同一个仓位在表里躺了几十上百条 OPEN 行（实测 105 行里 104 条是幽灵）。

本脚本做什么
-----------
1. 拉交易所真实持仓（demo 永续 /fapi/v2/positionRisk，走 10808 代理）
2. 台账里每个 (symbol, side) 只保留 id 最大的那行，且仅当交易所确实还有这个方向的仓
3. 其余 OPEN 行一律置为 CLOSED（**改状态、不删行**，审计链完整）
4. 写一条 risk_events 记录，说明动了什么

用法
----
    python scripts/cleanup_positions_ledger.py            # 预演，只打印不改
    python scripts/cleanup_positions_ledger.py --apply    # 真正落库
    python scripts/cleanup_positions_ledger.py --no-exchange --apply
                                                          # 不查交易所，直接按台账收口
                                                          # （交易所查不通时用；此时
                                                          #   所有 (symbol,side) 只留最新行）

注意：--apply 前脚本会自己再备一次份（data/agent_trader.sqlite.bak_<时间戳>）。
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "agent_trader.sqlite"
sys.path.insert(0, str(ROOT / "src"))

APPLY = "--apply" in sys.argv
CHECK_EXCHANGE = "--no-exchange" not in sys.argv
NOTE = "positions 去重判据缺陷（GROUP BY 漏 side）导致的重复 OPEN 行，由 cleanup_positions_ledger.py 收口"


def backup() -> Path:
    """用 sqlite3 的 backup API 复制，保证 WAL 里未 checkpoint 的页也带上。"""
    dst = DB.with_name(f"{DB.name}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    src = sqlite3.connect(str(DB))
    out = sqlite3.connect(str(dst))
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dst


def exchange_positions() -> dict[tuple[str, str], float]:
    """交易所真实持仓 -> {(symbol, side): abs(amt)}。查不通返回 None。"""
    import hashlib
    import hmac
    import time
    from urllib.parse import urlencode

    import httpx

    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

    base = env.get("BINANCE_DEMO_BASE_URL") or "https://demo-fapi.binance.com"
    key = env.get("BINANCE_API_KEY") or env.get("BINANCE_DEMO_API_KEY") or ""
    sec = env.get("BINANCE_API_SECRET") or env.get("BINANCE_DEMO_API_SECRET") or ""
    if not (key and sec):
        print("  ! .env 里找不到 API key/secret，跳过交易所对账")
        return {}

    qs = urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 5000})
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    try:
        r = httpx.get(f"{base}/fapi/v2/positionRisk",
                      params=f"{qs}&signature={sig}",
                      headers={"X-MBX-APIKEY": key},
                      proxy="http://127.0.0.1:10808", timeout=25.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ! 交易所查询失败: {type(e).__name__}: {str(e)[:120]}")
        return {}

    out: dict[tuple[str, str], float] = {}
    for d in data:
        amt = float(d.get("positionAmt") or 0)
        if abs(amt) < 1e-9:
            continue
        out[(str(d.get("symbol", "")), "LONG" if amt > 0 else "SHORT")] = abs(amt)
    return out


def main() -> int:
    print("=" * 100)
    print("positions 幽灵台账清理" + ("（--apply 真改）" if APPLY else "（预演，不加 --apply 不会改任何数据）"))
    print("=" * 100)

    live: dict[tuple[str, str], float] = {}
    if CHECK_EXCHANGE:
        print("\n[1/4] 拉交易所真实持仓（10808 代理）...")
        live = exchange_positions()
        if live:
            for (s, sd), q in sorted(live.items()):
                print(f"      {s:<12}{sd:<7}{q:>12.4f}")
        elif live == {}:
            print("      交易所当前无持仓（或查询失败，见上方提示）")
    else:
        print("\n[1/4] 跳过交易所对账（--no-exchange）")

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        "SELECT id, symbol, side, qty, entry_price, leverage, source, opened_at "
        "FROM positions WHERE status='OPEN' ORDER BY symbol, side, id"))
    con.close()

    print(f"\n[2/4] 台账里 OPEN 行 {len(rows)} 条，按 (symbol, side) 分组决定保留哪些")
    by_key: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_key[(str(r["symbol"]), str(r["side"]))].append(r)

    keep: list[int] = []
    close: list[sqlite3.Row] = []
    for key, items in sorted(by_key.items()):
        newest = items[-1]                      # ORDER BY id 保证最后一个是 MAX(id)
        alive = (not CHECK_EXCHANGE) or (key in live)
        keep.append(int(newest["id"]))
        for r in items[:-1]:
            close.append(r)
        if not alive:
            # 交易所已经没有这个方向了，最新那行也得关掉
            close.append(newest)
            keep.pop()
        print(f"      {key[0]:<12}{key[1]:<7} 共 {len(items):>3} 条  "
              f"保留 id={newest['id']:<6}" + ("" if alive else "  <<< 交易所已无，整组关闭"))

    print(f"\n[3/4] 判定结果：保留 OPEN {len(keep)} 条，收口为 CLOSED {len(close)} 条")
    if close:
        agg: dict[tuple[str, str], int] = defaultdict(int)
        for r in close:
            agg[(str(r["symbol"]), str(r["side"]))] += 1
        print(f"\n      {'symbol':<12}{'side':<8}{'收口条数':>8}")
        print("      " + "-" * 34)
        for k, n in sorted(agg.items(), key=lambda kv: -kv[1]):
            print(f"      {k[0]:<12}{k[1]:<8}{n:>8}")

    if not close:
        print("\n      没有需要清理的行，结束。")
        return 0

    if not APPLY:
        print("\n[4/4] 预演结束。确认无误后加 --apply 执行（执行前会自动再备一次份）。")
        return 0

    bak = backup()
    print(f"\n[4/4] 已备份 -> {bak.name}  ({bak.stat().st_size / 1024 / 1024:.2f} MB)")

    from audit.database import AuditDatabase
    db = AuditDatabase(DB)
    n = db.close_open_positions(exclude_ids=keep, note=NOTE)
    db.log_risk_event(
        event_type="positions_ledger_cleanup", severity="warn",
        details=(f"清理幽灵 OPEN 台账：{n} 行置为 CLOSED，保留 {len(keep)} 行 OPEN。"
                 f"根因=sync_positions 去重判据 GROUP BY 漏 side（双向持仓下每轮重复插入）。"
                 f"备份={bak.name}"),
        action_taken="close_open_positions",
        raw={"closed_ids": [int(r["id"]) for r in close],
             "kept_ids": keep,
             "note": NOTE},
    )
    db.close()
    print(f"      已收口 {n} 行，写了一条 risk_events(positions_ledger_cleanup)。")

    # 复核
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    left = con.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    con.close()
    print(f"      复核：positions 总行数 {total}（不变，未删行），仍为 OPEN 的 {left} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
