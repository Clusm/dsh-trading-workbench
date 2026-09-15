"""端到端验证外部平仓补记：主库只读副本 + 真实交易所成交流水。

为什么这么绕
------------
补记是在「台账有 OPEN 仓、交易所已平」时触发的，而现在两者都是空的，
真实环境跑不到这段逻辑。为了验证修复确实有效，构造当时的现场：

  1. 用 SQLite 在线备份 API 把主库快照成临时库（主库全程只读，零风险）
  2. 在临时库上把 UNIUSDT 那条改回 OPEN、删掉那条错误的补记单
  3. 用真实 broker 连交易所跑一遍 _reconcile_one_close
  4. 检查补记结果是否等于交易所真实成交：10 张 @ 5.8830、+3.575

用法：
    python scripts/verify_close_reconcile_live.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from core.net_env import apply_proxy_env  # noqa: E402

apply_proxy_env()

from audit.database import AuditDatabase  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402


def load_dotenv_into_os() -> None:
    """把 .env 的密钥写进 os.environ（create_broker 靠环境变量取 API key）。"""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

MAIN_DB = PROJECT_ROOT / "data" / "agent_trader.sqlite"
TMP_DB = PROJECT_ROOT / "data" / "_verify_reconcile.sqlite"
SYMBOL = "UNIUSDT"

# 交易所真实成交（2026-09-02 11:18:22，止盈单触发）
EXPECT_QTY = 10.0
EXPECT_PX = 5.8830
EXPECT_REALIZED = 3.575


def snapshot_database(source: Path, target: Path) -> None:
    """用 SQLite 在线备份 API 生成一致快照。

    不能直接 ``shutil.copy`` 主库文件：本库以 WAL 模式运行（``audit/database.py``
    设置 ``PRAGMA journal_mode=WAL``），已提交但尚未回写主文件的事务只存在于
    ``-wal`` 中。只复制主文件会得到一个缺失近期写入、甚至内部不一致的快照，
    而验证结论正是建立在"副本等于主库当时状态"这个前提上。

    项目在 ``b93e745`` 已就同一问题立过规则（L00 第 4 条："服务有 WAL 时不得
    仅复制主数据库"）。``mode=ro`` 不加 ``immutable``，是为了让 SQLite 正常读取
    ``-wal``/``-shm``，从而拿到包含已提交事务的真实快照。
    """
    target.unlink(missing_ok=True)
    uri = f"file:{Path(source).as_posix()}?mode=ro"
    source_conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        target_conn = sqlite3.connect(str(target))
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
    finally:
        source_conn.close()


def prepare() -> bool:
    """在副本上还原事故现场；返回 False 表示现场搭不起来。"""
    snapshot_database(MAIN_DB, TMP_DB)
    db = sqlite3.connect(TMP_DB)

    row = db.execute(
        "SELECT id, symbol, side, qty, entry_price, leverage FROM positions "
        "WHERE symbol=? ORDER BY id DESC LIMIT 1", (SYMBOL,)).fetchone()
    if not row:
        print(f"副本里没有 {SYMBOL} 的持仓记录，无法验证")
        return False
    pos_id, symbol, side, qty, entry, lev = row
    print(f"现场持仓：{symbol} {side} qty={qty} entry={entry} lev={lev}")

    db.execute("UPDATE positions SET status='OPEN', closed_at=NULL WHERE id=?", (pos_id,))
    deleted = db.execute(
        "DELETE FROM orders WHERE symbol=? AND order_source='exchange_sync'", (SYMBOL,)).rowcount
    print(f"清掉历史补记单 {deleted} 条，把 id={pos_id} 改回 OPEN")
    db.commit()
    db.close()
    return True


def main() -> int:
    if not prepare():
        return 1

    load_dotenv_into_os()
    api_key = os.environ.get("BINANCE_DEMO_API_KEY", "").strip()
    api_secret = os.environ.get("BINANCE_DEMO_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print("缺少 BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET，无法连真实交易所")
        return 1

    db = AuditDatabase(TMP_DB)
    broker = BinanceFuturesBroker(
        api_key=api_key, api_secret=api_secret,
        base_url="https://demo-fapi.binance.com",
        db=db, environment="demo")

    print("\n-- 调用修复后的 _reconcile_one_close --")
    broker._reconcile_one_close(SYMBOL)

    check = sqlite3.connect(TMP_DB)
    check.row_factory = sqlite3.Row
    rows = check.execute(
        "SELECT exchange_order_id, side, qty, price, raw_json FROM orders "
        "WHERE symbol=? AND order_source='exchange_sync' ORDER BY id DESC LIMIT 1",
        (SYMBOL,)).fetchone()

    ok = True
    if not rows:
        print("FAIL：没有补记任何记录")
        return 1

    import json
    raw = json.loads(rows["raw_json"] or "{}")
    print(f"\n补记结果：oid={rows['exchange_order_id']} side={rows['side']} "
          f"qty={rows['qty']} price={rows['price']}")
    print(f"          net_pnl={raw.get('net_pnl')} commission={raw.get('commission')} "
          f"partial={raw.get('partial')}")

    def cmp(name, got, want, tol=1e-6):
        nonlocal ok
        good = abs(float(got) - float(want)) <= tol
        ok = ok and good
        print(f"  {'OK  ' if good else 'FAIL'} {name}: {got} （期望 {want}）")

    print("\n对照交易所真实成交：")
    cmp("qty", rows["qty"], EXPECT_QTY)
    cmp("price", rows["price"], EXPECT_PX)
    # net_pnl = realized - commission = 3.575 - 0.023532
    cmp("net_pnl", raw.get("net_pnl", 0), EXPECT_REALIZED - 0.023532)
    if rows["exchange_order_id"] != "462253131":
        ok = False
        print(f"  FAIL orderId: {rows['exchange_order_id']} （期望 462253131，"
              f"即真实止盈成交；460044828 是 08-30 的陈年成交）")
    else:
        print(f"  OK   orderId: {rows['exchange_order_id']}")

    print("\n-- 补记产生的风控事件 --")
    for e in check.execute(
            "SELECT event_type, details FROM risk_events "
            "WHERE event_type LIKE '%reconcil%' ORDER BY id DESC LIMIT 3"):
        print(f"  [{e['event_type']}] {e['details']}")

    check.close()
    print(f"\n结论：{'通过 ✅ 修复有效' if ok else '未通过 ❌'}")
    print(f"（主库未改动，验证全部发生在副本 {TMP_DB.name}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
