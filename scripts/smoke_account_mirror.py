"""账户影子镜像的增量、乱序、REST差异与恢复回归。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.account_mirror import AccountMirror  # noqa: E402
from exchange.event_inbox import ExchangeEventInbox  # noqa: E402
from exchange.event_models import normalize_user_event  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-account-mirror-")) / "mirror.sqlite")
mirror = AccountMirror(db, environment="demo", allowed_symbols=["BTCUSDT"])

print("\n[1] 私有流账户增量只写USDT和允许的永续标的")
frame = {
    "e": "ACCOUNT_UPDATE", "E": 1000, "T": 1001,
    "a": {
        "B": [
            {"a": "USDT", "wb": "500.5", "cw": "490.5", "bc": "1.2"},
            {"a": "BNB", "wb": "2", "cw": "2", "bc": "0"},
        ],
        "P": [
            {"s": "BTCUSDT", "pa": "0.1", "ep": "60000", "bep": "60001",
             "cr": "0", "up": "2.5", "mt": "isolated", "iw": "300", "ps": "LONG"},
            {"s": "ETHUSDT", "pa": "1", "ep": "3000", "up": "1", "ps": "LONG"},
        ],
    },
}
check("ACCOUNT_UPDATE已应用", mirror.apply_event(normalize_user_event(frame)))
balance = db.fetch_one("SELECT * FROM account_balances WHERE asset='USDT'")
position = db.fetch_one("SELECT * FROM account_positions WHERE symbol='BTCUSDT'")
check("USDT余额字段正确", balance["wallet_balance"] == 500.5 and
      balance["balance_change"] == 1.2)
check("BTCUSDT仓位字段正确", position["position_amt"] == 0.1 and
      position["position_side"] == "LONG" and position["entry_price"] == 60000)
check("非白名单标的不进入镜像", not db.fetch_one(
    "SELECT symbol FROM account_positions WHERE symbol='ETHUSDT'"))

print("\n[2] 迟到事件不能覆盖更新状态")
older = {
    "e": "ACCOUNT_UPDATE", "E": 900, "T": 900,
    "a": {"B": [{"a": "USDT", "wb": "100", "cw": "100", "bc": "-400"}],
          "P": [{"s": "BTCUSDT", "pa": "9", "ep": "1", "ps": "LONG"}]},
}
mirror.apply_event(normalize_user_event(older))
balance = db.fetch_one("SELECT wallet_balance FROM account_balances WHERE asset='USDT'")
position = db.fetch_one("SELECT position_amt FROM account_positions WHERE symbol='BTCUSDT'")
check("余额未倒退", balance["wallet_balance"] == 500.5)
check("仓位未倒退", position["position_amt"] == 0.1)

account = {
    "totalWalletBalance": "500.5", "availableBalance": "200.5",
    "totalUnrealizedProfit": "2.5",
    "assets": [{"asset": "USDT", "walletBalance": "500.5",
                "crossWalletBalance": "490.5", "availableBalance": "200.5",
                "unrealizedProfit": "2.5"}],
}
rest_position = {
    "symbol": "BTCUSDT", "positionSide": "LONG", "positionAmt": "0.1",
    "entryPrice": "60000", "breakEvenPrice": "60001", "unRealizedProfit": "2.5",
    "isolatedWallet": "300", "marginType": "isolated", "leverage": "20",
}
print("\n[3] REST一致性报告先比较、再用真值纠偏")
matched = mirror.reconcile_snapshot(account, [rest_position])
check("一致快照通过", matched["matched"] is True)
mismatch_position = dict(rest_position, positionAmt="0.2")
mismatch = mirror.reconcile_snapshot(account, [mismatch_position])
check("数量差异被记录", mismatch["matched"] is False and
      mismatch["qty_mismatch"] == ["BTCUSDT:LONG"])
repaired = db.fetch_one(
    "SELECT position_amt,source FROM account_positions WHERE symbol='BTCUSDT'")
check("差异后按REST真值纠偏", repaired["position_amt"] == 0.2 and
      repaired["source"] == "rest_reconcile")

print("\n[4] 重启可重放未处理账户事件")
inbox = ExchangeEventInbox(db)
pending, _ = inbox.receive({
    "e": "ACCOUNT_UPDATE", "E": 5000, "T": 5000,
    "a": {"B": [{"a": "USDT", "wb": "501", "cw": "491", "bc": "0.5"}], "P": []},
})
restarted = AccountMirror(db, environment="demo", allowed_symbols=["BTCUSDT"])
check("未处理事件被重放", restarted.replay_pending() == 1)
pending_row = db.fetch_one(
    "SELECT status FROM exchange_events WHERE event_key=?", (pending.event_key,))
check("重放后事件进入APPLIED", pending_row["status"] == "APPLIED")
check("镜像状态保持shadow_only", restarted.status()["shadow_only"] is True)

versions = [r["version"] for r in db.fetch_all(
    "SELECT version FROM schema_migrations ORDER BY version")]
check("数据库迁移版本1到13完整", versions == list(range(1, 14)))
db.close()
print("\n结果: 14 通过 / 0 失败")
