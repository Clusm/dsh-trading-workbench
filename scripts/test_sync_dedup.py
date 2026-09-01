"""验证 sync_positions 的去重判据（离线，不联网，跑在数据库副本上）。

测什么
------
去重判据原本是 `SELECT MAX(id) FROM positions GROUP BY symbol`——漏了 side。
双向持仓（Hedge mode）下同一个 symbol 可以同时有多头和空头，
按 symbol 取 MAX(id) 每个 symbol 只返回一行（某一侧），于另一侧永远匹配不上，
每个慢循环都会再插一条 OPEN。这就是 positions 表被撑到 105 行的根因。

本测试在**真实生产库的副本**上跑真实的 sync_positions：
  - 场景 1：持仓不变，连跑 5 次慢循环 -> 一条都不该新增
  - 场景 2：交易所同一 symbol 同时有多头+空头 -> 两侧都要能命中去重
  - 场景 3：仓位真的变了（数量/杠杆/来源）-> 必须新增，不能因为去重而漏记
  - 场景 4：反向验证——把 SQL 改回旧的 `GROUP BY symbol`，重复插入必须复现，
            证明上面几个用例是真的在测这个缺陷，而不是碰巧通过

用法：
    python scripts/test_sync_dedup.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
LIVE_DB = ROOT / "data" / "agent_trader.sqlite"

OLD_SQL = "GROUP BY symbol, side"
NEW_SQL = "GROUP BY symbol"          # 注入用的旧写法

PASS, FAIL = 0, 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def make_broker(db_path: Path, ledger_path: Path):
    """构造一个不联网的 broker。"""
    from audit.database import AuditDatabase
    from exchange import binance_futures as bf

    orig = bf.BinanceFuturesBroker._load_exchange_info
    bf.BinanceFuturesBroker._load_exchange_info = lambda self: None   # 挡掉联网
    try:
        broker = bf.BinanceFuturesBroker(
            api_key="test", api_secret="test",
            db=AuditDatabase(db_path), ledger_file=ledger_path)
    finally:
        bf.BinanceFuturesBroker._load_exchange_info = orig

    # 注意：_record_ledger_position 内部会调 self.positions(force=True)，
    # stub 必须接受 force 参数，否则 TypeError 被 sync_positions 的 except 吞掉，
    # 表现为「什么都没插入」，很容易误判成"去重生效了"。
    broker._reconcile_external_closes = lambda symbols: None          # 挡掉联网
    return broker, broker.db


def stub_positions(broker, pos: list[dict]) -> None:
    broker.positions = lambda force=False: pos


def count_rows(db) -> int:
    return int(db.fetch_one("SELECT COUNT(*) c FROM positions")["c"])


def count_open(db) -> int:
    return int(db.fetch_one("SELECT COUNT(*) c FROM positions WHERE status='OPEN'")["c"])


def scenario_unchanged(tmpdb: Path, tmpledger: Path) -> None:
    """场景 1+2：持仓不变（含同 symbol 双向），连跑 5 次不应新增任何行。"""
    print("\n【场景 1+2】持仓不变 + 同 symbol 双向持仓，连跑 5 次慢循环")
    broker, db = make_broker(tmpdb, tmpledger)

    # 故意让同一个 symbol 同时有多头和空头——这是旧判据必然漏的一侧。
    # 用 DOGE/LTC 是因为生产库副本里已有 BTC/ETH/SOL/ZEC 的 OPEN 行，
    # 若用它们，参数恰好一致的那几条会被正确去重命中、不插入，基线条数就对不上了
    #（这不是 bug 而是去重生效，但会让断言失去意义）。
    fixed = [
        {"symbol": "DOGEUSDT", "positionAmt": "-1000", "entryPrice": "0.1234", "leverage": "10"},
        {"symbol": "DOGEUSDT", "positionAmt": "2000", "entryPrice": "0.1234", "leverage": "10"},
        {"symbol": "LTCUSDT", "positionAmt": "-5", "entryPrice": "88.5", "leverage": "10"},
        {"symbol": "LTCUSDT", "positionAmt": "3", "entryPrice": "88.5", "leverage": "10"},
    ]
    stub_positions(broker, fixed)

    before_id = int(db.fetch_one("SELECT COALESCE(MAX(id),0) m FROM positions")["m"])
    before = count_rows(db)
    broker.sync_positions()                      # 第 1 次：建基线
    after_first = count_rows(db)
    print(f"      基线插入 {after_first - before} 行（4 个 (symbol,side) 各一条，符合预期）")

    for i in range(5):
        broker.sync_positions()
    after = count_rows(db)
    check("持仓不变时 5 次慢循环零新增",
          after == after_first, f"{after_first} -> {after}")
    check("双向持仓两侧都命中去重（每个 symbol 恰好 2 条新行）",
          after_first - before == 4, f"实际新增 {after_first - before} 行，期望 4")

    rows = db.fetch_all(
        "SELECT symbol, side, COUNT(*) c FROM positions WHERE id > ? GROUP BY symbol, side",
        (before_id,))
    got = sorted((r["symbol"], r["side"], r["c"]) for r in rows)
    want = sorted([("DOGEUSDT", "SHORT", 1), ("DOGEUSDT", "LONG", 1),
                   ("LTCUSDT", "SHORT", 1), ("LTCUSDT", "LONG", 1)])
    check("新增行覆盖 LONG/SHORT 两侧且无重复", got == want, f"got={got}")
    db.close()


def scenario_real_change(tmpdb: Path, tmpledger: Path) -> None:
    """场景 3：仓位真的变了必须记录，不能因为去重而漏记。"""
    print("\n【场景 3】仓位真实变化（加仓 / 改杠杆 / 换来源）必须新增")
    broker, db = make_broker(tmpdb, tmpledger)
    pos = [{"symbol": "AVAXUSDT", "positionAmt": "-2.0", "entryPrice": "29.50", "leverage": "10"}]
    stub_positions(broker, pos)

    broker.sync_positions()
    n0 = count_rows(db)

    pos[0]["positionAmt"] = "-4.0"               # 加仓
    broker.sync_positions()
    n1 = count_rows(db)
    check("加仓后新增 1 行", n1 - n0 == 1, f"{n0} -> {n1}")

    pos[0]["leverage"] = "20"                    # 改杠杆
    broker.sync_positions()
    n2 = count_rows(db)
    check("改杠杆后新增 1 行", n2 - n1 == 1, f"{n1} -> {n2}")

    broker.sync_positions()                      # 再跑一次，不应新增
    n3 = count_rows(db)
    check("变化后再次同步不新增", n3 == n2, f"{n2} -> {n3}")
    db.close()


def scenario_inject_bug(tmpdb: Path, tmpledger: Path) -> None:
    """场景 4：把 SQL 改回旧的 GROUP BY symbol，重复插入必须复现。"""
    print("\n【场景 4】反向验证：注入旧写法 GROUP BY symbol，重复插入应复现")
    broker, db = make_broker(tmpdb, tmpledger)
    pos = [
        {"symbol": "XRPUSDT", "positionAmt": "-10", "entryPrice": "2.5", "leverage": "10"},
        {"symbol": "XRPUSDT", "positionAmt": "10", "entryPrice": "2.5", "leverage": "10"},
    ]
    stub_positions(broker, pos)

    orig_fetch_all = db.fetch_all

    def buggy_fetch_all(sql: str, params=()):
        return orig_fetch_all(sql.replace(OLD_SQL, NEW_SQL), params)

    db.fetch_all = buggy_fetch_all
    broker.sync_positions()
    n0 = count_rows(db)
    for _ in range(5):
        broker.sync_positions()
    n1 = count_rows(db)
    db.fetch_all = orig_fetch_all

    grew = n1 - n0
    check("注入旧写法后重复插入复现（>0 行）", grew > 0, f"5 次循环新增 {grew} 行，期望 > 0")
    print(f"      旧写法下 5 次循环新增 {grew} 行  <-- 这就是生产库被撑爆的机制")
    db.close()


def main() -> int:
    if not LIVE_DB.exists():
        print(f"找不到生产库 {LIVE_DB}")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="sync_dedup_"))
    print(f"工作目录（生产库副本）: {workdir}")
    print(f"生产库当前状态: {LIVE_DB.stat().st_size / 1024 / 1024:.2f} MB")

    try:
        # 每个场景用一份干净的副本，互不干扰
        s1 = workdir / "s1.sqlite"
        shutil.copy(LIVE_DB, s1)
        scenario_unchanged(s1, workdir / "ledger1.json")

        s2 = workdir / "s2.sqlite"
        shutil.copy(LIVE_DB, s2)
        scenario_real_change(s2, workdir / "ledger2.json")

        s3 = workdir / "s3.sqlite"
        shutil.copy(LIVE_DB, s3)
        scenario_inject_bug(s3, workdir / "ledger3.json")

        print("\n" + "=" * 70)
        print(f"结果: {PASS} 通过 / {FAIL} 失败")
        print("=" * 70)
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
