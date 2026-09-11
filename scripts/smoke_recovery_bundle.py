"""恢复包完整性、状态覆盖和运行锁拒绝回归；全程使用临时目录。"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from create_recovery_bundle import create_recovery_bundle  # noqa: E402
from restore_recovery_bundle import (  # noqa: E402
    restore_to_new_directory,
    verify_recovery_bundle,
)


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
assert manifest["bundle_schema"] == 2
assert (bundle / "manifest.sha256").is_file()
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

print("\n[2] 独立介质复制、dry-run与全新目录恢复")
(data / "trading_service.lock").unlink()
media = tmp / "independent_media" / bundle.name
media.parent.mkdir()
shutil.copytree(bundle, media)
verified = verify_recovery_bundle(media, expected_environment="demo")
assert verified["database_schema"] == 14
print("  PASS  独立介质副本通过清单/哈希/SQLite校验")
target = tmp / "restore_target"
preview = restore_to_new_directory(media, target, expected_environment="demo")
assert preview["status"] == "dry_run" and not target.exists()
assert preview["independent_volume"] is False
print("  PASS  默认dry-run不创建任何恢复文件")
restored = restore_to_new_directory(
    media, target, execute=True, expected_environment="demo")
assert restored["status"] == "restored"
assert (target / "data" / "agent_trader.sqlite").is_file()
assert (target / "data" / "capital_state.json").is_file()
assert (target / "config" / "risk_profile.lock.json").is_file()
assert (target / "restore_receipt.json").is_file()
assert sqlite3.connect(target / "data" / "agent_trader.sqlite").execute(
    "PRAGMA quick_check").fetchone()[0] == "ok"
print("  PASS  只恢复到全新目录并生成恢复回执")
try:
    restore_to_new_directory(media, target, execute=True)
    overwrite_blocked = False
except FileExistsError:
    overwrite_blocked = True
assert overwrite_blocked
print("  PASS  已存在目标拒绝覆盖")
try:
    restore_to_new_directory(
        media, tmp / "same_volume", require_independent_volume=True)
    volume_blocked = False
except ValueError:
    volume_blocked = True
assert volume_blocked
print("  PASS  要求独立卷时同盘目标被拒绝")

print("\n[3] 损坏、夹带文件与环境不匹配均fail-closed")
corrupt = tmp / "corrupt_media"
shutil.copytree(bundle, corrupt)
with (corrupt / "state" / "capital_state.json").open("ab") as output:
    output.write(b" ")
try:
    verify_recovery_bundle(corrupt)
    corrupt_blocked = False
except ValueError:
    corrupt_blocked = True
assert corrupt_blocked
print("  PASS  任一成员被改写即拒绝")
extra = tmp / "extra_media"
shutil.copytree(bundle, extra)
(extra / ".env").write_text("SECRET=forbidden", encoding="utf-8")
try:
    verify_recovery_bundle(extra)
    extra_blocked = False
except ValueError:
    extra_blocked = True
assert extra_blocked
print("  PASS  未列入清单的凭据文件被拒绝")
try:
    verify_recovery_bundle(media, expected_environment="live")
    mode_blocked = False
except ValueError:
    mode_blocked = True
assert mode_blocked
print("  PASS  执行环境不匹配被拒绝")
print("\n结果: 11 通过 / 0 失败")
