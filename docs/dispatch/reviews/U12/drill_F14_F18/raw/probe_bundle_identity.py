"""恢复包是否携带运行代（runtime_generation.json）？恢复后身份是否延续？"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
from audit.database import AuditDatabase
from core.ops.recovery import create_epoch_bundle, verify_epoch_bundle, drill_epoch_bundle
from core.ops.supervision import load_or_mint_run_record
from core.runtime_paths import RuntimePaths

tmp = Path(tempfile.mkdtemp(prefix="w3-bundle-id-"))
base = tmp / "root" / "data"
base.mkdir(parents=True)
db = AuditDatabase(base / "agent_trader.sqlite"); db.conn.commit(); db.close()
for n, p in {"capital_state.json": {"mode": "demo"}, "channel_state.json": {},
             "control_state.json": {"state": "normal"},
             "account_risk_state.json": {}}.items():
    (base / n).write_text(json.dumps(p), encoding="utf-8")
paths = RuntimePaths.resolve(tmp / "root", profile="demo", state_root=base)
rec_before, _ = load_or_mint_run_record(paths, environment="demo", account_ref="demo:acct-A")
print("registry file exists:", paths.run_registry_file.is_file(),
      "->", paths.run_registry_file.name)
bundle = create_epoch_bundle(paths, tmp / "bundles", environment="demo",
                             account_ref="demo:acct-A")
manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
print("bundle members:", sorted(manifest["files"].keys()))
print("runtime_generation.json in bundle:", "runtime_generation.json" in manifest["files"])
target = tmp / "restored"
drill_epoch_bundle(bundle, target, execute=True)
rp = RuntimePaths.resolve(target, profile="demo", state_root=target / "data")
print("restored registry file exists:", rp.run_registry_file.is_file())
rec_after, new = load_or_mint_run_record(rp, environment="demo", account_ref="demo:acct-A")
print("before iid:", rec_before.runtime_instance_id[:12])
print("after  iid:", rec_after.runtime_instance_id[:12], "new_minted:", new)
print("identity_continuous:", rec_before.runtime_instance_id == rec_after.runtime_instance_id)
