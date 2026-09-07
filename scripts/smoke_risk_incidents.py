"""风险事件原始审计与 incident 生命周期回归。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402


def check(name: str, value: bool) -> None:
    if not value:
        raise AssertionError(name)
    print(f"  PASS  {name}")


db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-incidents-")) / "audit.sqlite")
print("\n[1] 重复风险事件仍保留原始审计，但只聚合一个 incident")
db.log_risk_event(event_type="futures_sync_error", severity="warn", details="first")
db.log_risk_event(event_type="futures_sync_error", severity="error", details="second")
check("原始事件保留两条", db.fetch_one("SELECT COUNT(*) n FROM risk_events")["n"] == 2)
rows = db.list_risk_incidents()
check("同类事件去重", len(rows) == 1 and rows[0]["occurrence_count"] == 2)
check("严重度只升级不降级", rows[0]["severity"] == "error" and rows[0]["status"] == "OPEN")

print("\n[2] ACK、RESOLVE 与复发重开")
key = rows[0]["incident_key"]
check("可确认", db.set_risk_incident_status(incident_key=key, status="ACKNOWLEDGED"))
check("状态已确认", db.list_risk_incidents()[0]["status"] == "ACKNOWLEDGED")
check("可解决", db.set_risk_incident_status(incident_key=key, status="RESOLVED"))
db.log_risk_event(event_type="futures_sync_error", severity="warn", details="recurred")
row = db.list_risk_incidents()[0]
check("复发自动重开", row["status"] == "OPEN" and row["occurrence_count"] == 3)
check("摘要正确", db.risk_incident_summary()["open"] == 1)

print("\n[3] 健康恢复可按事件类型自动收口运行类 incident")
db.log_risk_event(event_type="equity_unavailable", severity="warn", details="temporary")
db.log_risk_event(event_type="startup_safety_block", severity="critical", details="temporary")
resolved = db.resolve_risk_incidents_by_type("equity_unavailable", "startup_safety_block")
summary = db.risk_incident_summary()
check("两类运行故障已收口", resolved == 2)
check("自动收口不影响仍在复发的其他事件", summary["resolved"] == 2 and summary["open"] == 1)

print("\n[4] 写入延迟指标")
db.log_portfolio_snapshot(
    equity=100.0,
    margin_balance=100.0,
    available_balance=100.0,
    unrealized_pnl=0.0,
    margin_used=0.0,
)
metrics = db.write_metrics()
check("写入次数可观测", metrics["writes"] >= 1)
check("写入延迟非负", metrics["last_write_ms"] >= 0.0 and metrics["avg_write_ms"] >= 0.0)
db.close()
print("\n结果: 12 通过 / 0 失败")
