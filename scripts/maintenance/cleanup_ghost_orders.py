# -*- coding: utf-8 -*-
"""清理 orders / positions 表的历史幽灵数据（只改状态、不删行）。

背景：旧代码有两处「只 INSERT 不 UPDATE」的缺陷，导致流水表堆积幽灵数据：
1. 保护单（STOP/TAKE 移动止损）每轮重设都 INSERT 新行，撤单/触发却不记录，
   于是 orders 表里留下大量 status 仍为 NEW、实际早已失效的幽灵保护单。
2. positions 表 log_position 只 INSERT 不 CLOSE，同一持仓的旧快照滞留 OPEN。

本脚本把这些幽灵数据收口：
- orders：每个 (symbol, side) 只保留「最新一条」NEW 的保护单，
  其余历史 NEW 保护单标记为 CANCELED（它们对应已撤/已触发的旧挂单）。
- positions：每个 (symbol, side) 只保留「最新一条」OPEN 行，
  其余 OPEN 行标记为 CLOSED（旧快照）。

用法：
  python scripts/cleanup_ghost_orders.py            # 预演，只打印将要清理的行
  python scripts/cleanup_ghost_orders.py --apply    # 实际执行
  python scripts/cleanup_ghost_orders.py --apply --db <path>   # 指定数据库
"""
import argparse
import sys
from pathlib import Path

# U10b 归档：本项目根深度已 +1（见 docs/archive/README.md 的归档索引）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from audit.database import AuditDatabase, utc_now


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行（默认只预演）")
    ap.add_argument("--db", default=None, help="数据库路径，默认 data/agent_trader.sqlite")
    args = ap.parse_args()

    db_path = args.db or str(Path(__file__).resolve().parent.parent.parent / "data" / "agent_trader.sqlite")
    db = AuditDatabase(Path(db_path))

    # ---- 1. orders：保护单只保留最新 NEW ----
    rows = db.fetch_all(
        "SELECT id, symbol, side, status, price, created_at FROM orders "
        "WHERE side IN ('STOP','TAKE') AND status='NEW' ORDER BY symbol, side, id DESC")
    # 按 (symbol, side) 分组，每组保留最新一条（rows 已按 id DESC 排序，第一条即最新）
    keep = {}
    ghost_ids = []
    for r in rows:
        key = (r["symbol"], r["side"])
        if key not in keep:
            keep[key] = r["id"]
        else:
            ghost_ids.append(r["id"])

    # ---- 2. positions：每个 (symbol, side) 只保留最新 OPEN ----
    pos_rows = db.fetch_all(
        "SELECT id, symbol, side, qty, entry_price, opened_at FROM positions "
        "WHERE status='OPEN' ORDER BY symbol, side, id DESC")
    pos_keep = {}
    pos_ghost_ids = []
    # 当前仍有 OPEN 仓位的 symbol 集合（用于判断保护单是否还有效）
    live_symbols = {r["symbol"] for r in pos_rows}
    for r in pos_rows:
        key = (r["symbol"], r["side"])
        if key not in pos_keep:
            pos_keep[key] = r["id"]
        else:
            pos_ghost_ids.append(r["id"])

    # 重新计算保护单保留组：只保留「symbol 仍有 OPEN 仓位」的最新 STOP/TAKE
    effective_keep = {}
    for (sym, side), oid in keep.items():
        if sym in live_symbols:
            effective_keep[(sym, side)] = oid
    # 被剔除的（symbol 已无仓位）也要标记 CANCELED
    for (sym, side), oid in keep.items():
        if (sym, side) not in effective_keep:
            ghost_ids.append(oid)
    keep = effective_keep

    print("=" * 60)
    print(f"数据库: {db_path}")
    print(f"保护单 (NEW) 将标记 CANCELED: {len(ghost_ids)} 条")
    print(f"  保留有效挂单: {len(keep)} 组")
    for (sym, side), oid in sorted(keep.items()):
        print(f"    - {sym} {side} 保留 id={oid}")
    print(f"positions 旧快照将标记 CLOSED: {len(pos_ghost_ids)} 条")
    print(f"  保留有效 OPEN: {len(pos_keep)} 组")
    for (sym, side), pid in sorted(pos_keep.items()):
        print(f"    - {sym} {side} 保留 id={pid}")
    print("=" * 60)

    if not args.apply:
        print("【预演模式】未做任何修改。加 --apply 实际执行。")
        return 0

    # ---- 执行 ----
    with db.lock:
        for oid in ghost_ids:
            db.conn.execute(
                "UPDATE orders SET status='CANCELED', updated_at=? WHERE id=?",
                (utc_now(), oid))
        for pid in pos_ghost_ids:
            db.conn.execute(
                "UPDATE positions SET status='CLOSED', closed_at=? WHERE id=?",
                (utc_now(), pid))
        db.conn.commit()

    print(f"完成：保护单 CANCELED {len(ghost_ids)} 条，positions CLOSED {len(pos_ghost_ids)} 条。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
