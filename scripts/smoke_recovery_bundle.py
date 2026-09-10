"""恢复包完整性、状态覆盖和运行锁拒绝回归；全程使用临时目录。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from create_recovery_bundle import create_recovery_bundle  # noqa: E402


tmp = Path(tempfile.mkdtemp(prefix="mta-recovery-"))
project = tmp / "project"
data = project / "data"
(project / "config").mkdir(parents=True)
data.mkdir()
db = sqlite3.connect(data / "agent_trader.sqlite")
db.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT)")
db.execute("INSERT INTO schema_migrations VALUES(14, 'test')")
db.commit()
db.close()
states = {
    "capital_state.json": {"mode": "demo", "anchored_at": "2026-09-10T00:00:00+00:00"},
    "channel_state.json": {"date": "2026-09-10", "ai_halted": False},
    "control_state.json": {"state": "close_only"},
    "account_risk_state.json": {"anchor_id": "demo:test"},
    "futures_source_ledger.json": {"positions": {}},
}
for name, payload in states.items():
    encoding = "utf-8-sig" if name == "channel_state.json" else "utf-8"
    (data / name).write_text(json.dumps(payload), encoding=encoding)
(project / "config" / "risk_profile.lock.json").write_text("{}", encoding="utf-8")
(project / ".env").write_text("SECRET=must-not-copy", encoding="utf-8")

bundle = create_recovery_bundle(project, tmp / "bundles")
manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
assert manifest["environment"] == "demo"
assert manifest["files"]["database"]["schema_version"] == 14
assert all((bundle / "state" / name).is_file() for name in states)
assert not (bundle / ".env").exists()
assert sqlite3.connect(bundle / "agent_trader.sqlite").execute(
    "PRAGMA quick_check").fetchone()[0] == "ok"
print("  PASS  数据库与关键状态进入同一恢复包并通过校验")
print("  PASS  .env及凭据未进入恢复包")

(data / "trading_service.lock").write_text("running", encoding="utf-8")
try:
    create_recovery_bundle(project, tmp / "bundles")
    blocked = False
except RuntimeError:
    blocked = True
assert blocked
print("  PASS  运行实例存在时拒绝创建伪一致恢复包")
print("\n结果: 3 通过 / 0 失败")
