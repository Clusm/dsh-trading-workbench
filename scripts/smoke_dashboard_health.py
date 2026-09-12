"""Dashboard 健康纯函数回归；不构造服务、不访问网络或真实运行状态。"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dashboard.health import freshness_health_reasons  # noqa: E402


def main() -> None:
    config = {
        "startup_grace_seconds": 120,
        "fast_cycle_stale_seconds": 120,
        "protection_reconcile_stale_seconds": 120,
    }
    healthy = freshness_health_reasons(
        {"started_at": 900, "last_fast_cycle_at": 990, "last_fast_cycle_error": None},
        {"enabled": True, "read_enabled": True, "last_reconcile_at": 995},
        config,
        now=1000,
    )
    assert healthy == [], healthy

    stale = freshness_health_reasons(
        {"started_at": 1, "last_fast_cycle_at": 700, "last_fast_cycle_error": None},
        {"enabled": True, "read_enabled": True, "last_reconcile_at": 700},
        config,
        now=1000,
    )
    assert stale == ["fast_cycle_stale", "protection_reconcile_stale"], stale

    never = freshness_health_reasons(
        {"started_at": 1, "last_fast_cycle_at": None, "last_fast_cycle_error": "TimeoutError"},
        {"enabled": True, "read_enabled": True, "last_reconcile_at": None,
         "last_error": "TransportError"},
        config,
        now=1000,
    )
    assert never == [
        "fast_cycle_error",
        "fast_cycle_never_completed",
        "protection_reconcile_never_completed",
        "protection_reconcile_error",
    ], never

    grace = freshness_health_reasons(
        {"started_at": 950, "last_fast_cycle_at": None},
        {"enabled": True, "read_enabled": True, "last_reconcile_at": None},
        config,
        now=1000,
    )
    assert grace == [], grace
    print("DASHBOARD_HEALTH_OK cases=4")


if __name__ == "__main__":
    main()
