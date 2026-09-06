"""账户配置事件与MARGIN_CALL安全监视器回归；不访问网络、不改变控制状态。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.event_inbox import ExchangeEventInbox  # noqa: E402
from exchange.event_models import normalize_user_event  # noqa: E402
from exchange.safety_monitor import ExchangeSafetyMonitor  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


class FakeAccountMirror:
    def __init__(self):
        self.calls = 0

    def reconcile_broker(self, broker):
        assert broker is fake_broker
        self.calls += 1
        return {"matched": True}


db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-safety-monitor-")) / "safety.sqlite")
fake_broker = object()
fake_mirror = FakeAccountMirror()
monitor = ExchangeSafetyMonitor(
    db, environment="demo", account_mirror=fake_mirror, broker=fake_broker)

print("\n[1] ACCOUNT_CONFIG_UPDATE镜像且不允许迟到覆盖")
leverage = normalize_user_event({
    "e": "ACCOUNT_CONFIG_UPDATE", "E": 1000, "T": 1000,
    "ac": {"s": "BTCUSDT", "l": 20},
})
check("杠杆配置事件已应用", monitor.apply_event(leverage))
multi_asset = normalize_user_event({
    "e": "ACCOUNT_CONFIG_UPDATE", "E": 1100, "T": 1100,
    "ai": {"j": "false"},
})
monitor.apply_event(multi_asset)
symbol_row = db.fetch_one(
    "SELECT leverage FROM account_config_mirror WHERE environment='demo' AND scope='SYMBOL:BTCUSDT'")
account_row = db.fetch_one(
    "SELECT multi_assets_mode FROM account_config_mirror WHERE environment='demo' AND scope='ACCOUNT'")
check("symbol杠杆已镜像", symbol_row["leverage"] == 20)
check("字符串false不会误判为true", account_row["multi_assets_mode"] == 0)
monitor.apply_event(normalize_user_event({
    "e": "ACCOUNT_CONFIG_UPDATE", "E": 900, "T": 900,
    "ac": {"s": "BTCUSDT", "l": 50},
}))
symbol_row = db.fetch_one(
    "SELECT leverage FROM account_config_mirror WHERE environment='demo' AND scope='SYMBOL:BTCUSDT'")
check("迟到配置未覆盖新值", symbol_row["leverage"] == 20)

print("\n[2] MARGIN_CALL只告警并立即执行只读REST刷新")
margin = normalize_user_event({
    "e": "MARGIN_CALL", "E": 2000, "cw": "100",
    "p": [{"s": "BTCUSDT", "ps": "LONG", "pa": "0.1"}],
})
check("保证金告警已消费", monitor.apply_event(margin))
check("触发一次账户REST对账", fake_mirror.calls == 1)
monitor.apply_event(margin)
check("重复告警不重复对账", fake_mirror.calls == 1)
safety_rows = db.fetch_all("SELECT * FROM exchange_safety_events")
risk_rows = db.fetch_all(
    "SELECT * FROM risk_events WHERE event_type='exchange_margin_call'")
check("安全事件幂等且已对账", len(safety_rows) == 1 and safety_rows[0]["reconciled"] == 1)
check("critical风险事件只记录一次", len(risk_rows) == 1 and risk_rows[0]["severity"] == "critical")

print("\n[3] 重启可重放未处理配置事件")
inbox = ExchangeEventInbox(db)
pending, _ = inbox.receive({
    "e": "ACCOUNT_CONFIG_UPDATE", "E": 3000, "T": 3000,
    "ac": {"s": "ETHUSDT", "l": 10},
})
restarted = ExchangeSafetyMonitor(db, environment="demo")
check("未处理配置事件已重放", restarted.replay_pending() == 1)
pending_row = db.fetch_one(
    "SELECT status FROM exchange_events WHERE event_key=?", (pending.event_key,))
check("重放事件进入APPLIED", pending_row["status"] == "APPLIED")
check("状态接口保持只观察", restarted.status()["observe_only"] is True)

versions = [r["version"] for r in db.fetch_all(
    "SELECT version FROM schema_migrations ORDER BY version")]
check("数据库迁移版本1到13完整", versions == list(range(1, 14)))
db.close()
print("\n结果: 13 通过 / 0 失败")
