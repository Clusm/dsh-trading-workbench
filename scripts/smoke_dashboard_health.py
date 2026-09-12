"""Dashboard 健康纯函数回归；不构造服务、不访问网络或真实运行状态。"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dashboard.health import freshness_health_reasons  # noqa: E402
from dashboard.formatting import (  # noqa: E402
    format_age,
    format_number,
    format_percent,
    format_price,
    format_signed,
    pnl_class,
)


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
    assert format_number(1234.5) == "1,234.50"
    assert format_signed(-2.5) == "-2.50"
    assert format_percent(0.1234) == "+12.34%"
    assert format_price(744.53325581) == "744.53"
    assert format_price(0.000012345678) == "0.000012346"
    assert format_age(940.0, now=1000.0) == "1分钟前"
    assert pnl_class(-0.1) == "neg" and pnl_class("bad") == ""
    print("DASHBOARD_HEALTH_OK health_cases=4 formatting_cases=7")


if __name__ == "__main__":
    main()
