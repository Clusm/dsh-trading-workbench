"""Dashboard 健康状态的纯判定逻辑。

本模块不读取网络、数据库或交易状态，也不修改控制面。调用方先取得运行快照，再把
快照交给这些函数生成稳定原因码，使健康展示与交易授权保持单向依赖。
"""
from __future__ import annotations

import time
from typing import Any


def freshness_health_reasons(
    runtime: dict[str, Any],
    protection: dict[str, Any],
    stability: dict[str, Any],
    *,
    now: float | None = None,
) -> list[str]:
    """返回快循环和保护对账的时效异常原因，不改变任何风险参数。"""
    current = float(now if now is not None else time.time())
    grace = max(1.0, float(stability.get("startup_grace_seconds", 120)))
    fast_limit = max(1.0, float(stability.get("fast_cycle_stale_seconds", 120)))
    protection_limit = max(
        1.0, float(stability.get("protection_reconcile_stale_seconds", 120))
    )
    started = float(runtime.get("started_at") or current)
    beyond_grace = current - started > grace
    reasons: list[str] = []
    fast_at = runtime.get("last_fast_cycle_at")
    if runtime.get("last_fast_cycle_error"):
        reasons.append("fast_cycle_error")
    if fast_at and current - float(fast_at) > fast_limit:
        reasons.append("fast_cycle_stale")
    elif not fast_at and beyond_grace:
        reasons.append("fast_cycle_never_completed")
    if protection.get("enabled") and protection.get("read_enabled"):
        reconciled_at = protection.get("last_reconcile_at")
        if reconciled_at and current - float(reconciled_at) > protection_limit:
            reasons.append("protection_reconcile_stale")
        elif not reconciled_at and beyond_grace:
            reasons.append("protection_reconcile_never_completed")
        if protection.get("last_error"):
            reasons.append("protection_reconcile_error")
    return reasons
