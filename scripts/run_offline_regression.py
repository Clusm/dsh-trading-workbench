"""核心离线行为回归统一入口；不联网、不读取真实交易状态。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "scripts/test_precheck_fixes.py",
    "scripts/smoke_p0_account_safety.py",
    "scripts/smoke_channels.py",
    "scripts/smoke_baseline_guard.py",
    "scripts/smoke_order_outbox.py",
    "scripts/smoke_binance_transport.py",
    "scripts/smoke_db_migrations.py",
    "scripts/smoke_recovery_bundle.py",
    "scripts/smoke_risk_incidents.py",
    "scripts/smoke_service_lifecycle.py",
    "scripts/smoke_ui.py",
)


def main() -> int:
    env = os.environ.copy()
    env["EXECUTION_MODE"] = "paper"
    env["MTA_NO_PROXY"] = "1"
    # 默认回归不得依赖本机凭据；测试服务全部指向临时 project_root。
    for name in tuple(env):
        upper = name.upper()
        if ("BINANCE" in upper or "SENSENOVA" in upper) and (
                "KEY" in upper or "SECRET" in upper or "TOKEN" in upper):
            env.pop(name, None)
    for script in SCRIPTS:
        print(f"\n=== OFFLINE {script} ===", flush=True)
        subprocess.run([sys.executable, script], cwd=PROJECT_ROOT,
                       env=env, check=True)
    print(f"\nOFFLINE REGRESSION OK scripts={len(SCRIPTS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
