"""核心离线行为回归统一入口；不联网、不读取真实交易状态。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "scripts/check_execution_boundaries.py",
    "scripts/test_secret_scanner.py",
    "scripts/test_precheck_fixes.py",
    "scripts/smoke_p0_account_safety.py",
    "scripts/smoke_channels.py",
    "scripts/smoke_baseline_guard.py",
    "scripts/smoke_order_outbox.py",
    "scripts/smoke_order_mirror.py",
    "scripts/smoke_binance_transport.py",
    "scripts/smoke_close_reconcile.py",
    "scripts/smoke_market_query_tools.py",
    "scripts/smoke_public_rate_limit.py",
    "scripts/smoke_rate_limit_multiprocess.py",
    "scripts/smoke_market_data_gateway.py",
    "scripts/smoke_ws_routes.py",
    "scripts/smoke_intel_sidecar.py",
    "scripts/smoke_signal_scanner_integration.py",
    "scripts/smoke_p0b.py",
    "scripts/smoke_trading_scope.py",
    "scripts/smoke_p0b_loop.py",
    "scripts/smoke_p1.py",
    "scripts/smoke_decision_policy.py",
    "scripts/smoke_portfolio_policy.py",
    "scripts/smoke_position_guardian.py",
    "scripts/smoke_usdm_replay.py",
    "scripts/smoke_strategy_evaluation.py",
    "scripts/smoke_decision_pipeline.py",
    "scripts/smoke_entry_readiness.py",
    "scripts/smoke_db_migrations.py",
    "scripts/smoke_recovery_bundle.py",
    "scripts/smoke_risk_incidents.py",
    "scripts/smoke_dashboard_health.py",
    "scripts/smoke_service_lifecycle.py",
    "scripts/smoke_ui.py",
)


def main() -> int:
    env = os.environ.copy()
    env["EXECUTION_MODE"] = "paper"
    env["MTA_NO_PROXY"] = "1"
    # GitHub Windows runner 默认可能是 CP1252；冒烟脚本包含中文测试名，
    # 强制 UTF-8，避免仅因控制台编码而把通过的逻辑误报为失败。
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
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
