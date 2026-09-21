"""生产恢复脚本对 live 恢复包的布局：状态 JSON 与门禁落点。"""
from __future__ import annotations
import hashlib, json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))
from audit.database import AuditDatabase
import create_recovery_bundle as crb
import restore_recovery_bundle as rrb

def seed(root: Path, mode: str) -> None:
    d = root / "data"; d.mkdir(parents=True)
    db = AuditDatabase(d / "agent_trader.sqlite"); db.conn.commit(); db.close()
    for n, p in {"capital_state.json": {"mode": mode}, "channel_state.json": {},
                 "control_state.json": {"state": "normal"}, "account_risk_state.json": {}}.items():
        (d / n).write_text(json.dumps(p), encoding="utf-8")

for label, profile in (("epoch.profile=live", "live"), ("epoch.profile=(empty)", "")):
    tmp = Path(tempfile.mkdtemp(prefix="w3-live-restore-"))
    root = tmp / "proj"; seed(root, "demo")
    b = crb.create_recovery_bundle(root, tmp / "bundles")
    m = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    m["epoch"]["profile"] = profile
    raw = (json.dumps(m, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (b / "manifest.json").write_bytes(raw)
    (b / "manifest.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "  manifest.json\n", encoding="ascii", newline="\n")
    tgt = tmp / "restored"
    rrb.restore_to_new_directory(b, tgt, execute=True, expected_environment="demo")
    files = sorted(str(p.relative_to(tgt)).replace("\\", "/")
                   for p in tgt.rglob("*") if p.is_file())
    print(f"\n[{label}] restored layout:")
    for f in files:
        print("   ", f)
