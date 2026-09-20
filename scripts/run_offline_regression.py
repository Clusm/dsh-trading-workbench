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
    "scripts/smoke_request_policy.py",
    "scripts/check_binance_request_policy.py",
    "scripts/smoke_rate_limit_multiprocess.py",
    "scripts/smoke_market_data_gateway.py",
    "scripts/smoke_ws_routes.py",
    "scripts/smoke_kline_finality.py",
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
    "scripts/smoke_strategy_prefix_invariance.py",
    "scripts/smoke_decision_pipeline.py",
    "scripts/smoke_entry_readiness.py",
    "scripts/smoke_llm_coordinator.py",
    "scripts/smoke_llm_deadline.py",
    "scripts/smoke_workbuddy_adapter.py",
    "scripts/smoke_minimum_research_gate.py",
    "scripts/smoke_review_worker.py",
    "scripts/smoke_trade_reconciliation.py",
    "scripts/smoke_trade_lifecycle.py",
    "scripts/smoke_db_migrations.py",
    "scripts/smoke_release_acceptance.py",
    "scripts/smoke_release_observation_freshness.py",
    "scripts/smoke_health_watch.py",
    "scripts/smoke_fault_matrix.py",
    "scripts/smoke_recovery_bundle.py",
    "scripts/smoke_risk_incidents.py",
    "scripts/smoke_dashboard_health.py",
    "scripts/smoke_service_lifecycle.py",
    "scripts/smoke_windows_service_plan.py",
    "scripts/smoke_windows_service_lifecycle.py",
    "scripts/smoke_release_package.py",
    "scripts/smoke_ui.py",
    # 以下为审计后补入的冒烟脚本：全程离线、只写临时目录，不读真实
    # data/.env、不构造真实 Broker、不发网络请求。
    "scripts/smoke_step0.py",
    "scripts/smoke_db_backup.py",
    "scripts/smoke_exchange_event_inbox.py",
    "scripts/smoke_account_mirror.py",
    "scripts/smoke_exchange_safety_monitor.py",
    "scripts/smoke_protection_mirror.py",
    "scripts/smoke_user_stream_transport.py",
    "scripts/smoke_user_stream_lifecycle.py",
    "scripts/smoke_snapshot_guard.py",
    "scripts/smoke_graph_barrier.py",
    "scripts/smoke_force_submit.py",
    "scripts/smoke_p2.py",
    "scripts/smoke_profile_isolation.py",
    "scripts/smoke_detached_profiles.py",
    "scripts/smoke_takeover_compatibility.py",
    "scripts/smoke_llm_failover.py",
    "scripts/smoke_contracts_v3.py",
    # U03（C 决策证据）：逐意图可验证研究证据与 S01 分源证据质量。
    # 两者全程离线，只读 tests/fixtures/evidence/ 下的合成夹具。
    "scripts/smoke_u03_research_evidence.py",
    "scripts/smoke_u03_market_evidence_quality.py",
    # U01（A 账本）：收入分页/幂等入账/守恒与 RV01–RV03 复盘统计回归。
    # 两者全程离线，只读 tests/fixtures/income/ 下的合成夹具。
    "scripts/smoke_u01_income.py",
    "scripts/smoke_u01_review_statistics.py",
    # U04（B 持仓与执行）：部分成交/保护/重放故障验收。全程离线，只写临时目录。
    "scripts/smoke_u04_execution_faults.py",
    # U13（E 托管/恢复）：恢复门禁/单实例锁/RuntimePaths/同 epoch 恢复包离线冒烟。
    "scripts/smoke_u13_supervision.py",
)
EXCLUDED_SMOKE_SCRIPTS = {
    # 第[6]节直接调用 query_symbol_detail/query_symbol_cards 打公共行情接口，
    # 无网环境必然失败；待改为离线替身后再纳入统一回归。
    "scripts/smoke_p0c.py",
}


def _validate_smoke_manifest() -> None:
    """确保新增冒烟脚本不会悄悄逃离统一离线回归。"""
    discovered = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "scripts").glob("smoke_*.py")
        if path.is_file()
    }
    listed = {
        Path(name).as_posix()
        for name in SCRIPTS
        if Path(name).name.startswith("smoke_")
    }
    excluded = {Path(name).as_posix() for name in EXCLUDED_SMOKE_SCRIPTS}
    problems: list[str] = []
    missing = sorted(discovered - listed - excluded)
    stale = sorted(listed - discovered)
    stale_exclusions = sorted(excluded - discovered)
    overlap = sorted(listed & excluded)
    if missing:
        problems.append("未登记冒烟脚本: " + ", ".join(missing))
    if stale:
        problems.append("清单引用不存在: " + ", ".join(stale))
    if stale_exclusions:
        problems.append("例外引用不存在: " + ", ".join(stale_exclusions))
    if overlap:
        problems.append("同时登记和排除: " + ", ".join(overlap))
    if problems:
        raise RuntimeError("；".join(problems))
    print(f"SMOKE MANIFEST OK included={len(listed)} excluded={len(excluded)}")


def main() -> int:
    _validate_smoke_manifest()
    env = os.environ.copy()
    env["EXECUTION_MODE"] = "paper"
    env.pop("MTA_PROFILE", None)
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
