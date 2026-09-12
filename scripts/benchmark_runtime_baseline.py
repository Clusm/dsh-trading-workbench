"""离线运行时性能基线；只使用临时数据库，不访问网络或真实交易状态。

该脚本用于 F11 重构前后对照，不设置交易阈值，也不进入默认 CI。性能数字受机器和
杀毒软件影响，因此只输出样本、P50/P95/最大值，由同机同环境的两次结果进行比较。
"""
from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from dashboard.health import freshness_health_reasons  # noqa: E402


def percentile(values: list[float], quantile: float) -> float:
    """返回最近秩百分位，避免为基准脚本增加统计依赖。"""
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * quantile + 0.999999) - 1))
    return ordered[index]


def summary(values: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "p50_ms": round(statistics.median(values), 4),
        "p95_ms": round(percentile(values, 0.95), 4),
        "max_ms": round(max(values), 4),
    }


def main() -> None:
    health_times: list[float] = []
    runtime = {"started_at": 1.0, "last_fast_cycle_at": 900.0}
    protection = {"enabled": True, "read_enabled": True, "last_reconcile_at": 900.0}
    config = {"startup_grace_seconds": 120, "fast_cycle_stale_seconds": 120,
              "protection_reconcile_stale_seconds": 120}
    for _ in range(10_000):
        started = time.perf_counter()
        freshness_health_reasons(runtime, protection, config, now=1000.0)
        health_times.append((time.perf_counter() - started) * 1000.0)

    with tempfile.TemporaryDirectory(prefix="mta-performance-") as temp_dir:
        db = AuditDatabase(Path(temp_dir) / "audit.sqlite")
        write_times: list[float] = []
        read_times: list[float] = []
        for index in range(200):
            started = time.perf_counter()
            db.log_risk_event(
                event_type="benchmark", severity="info", details=f"sample-{index}",
                action_taken="none",
            )
            write_times.append((time.perf_counter() - started) * 1000.0)
            started = time.perf_counter()
            db.fetch_all("SELECT id, event_type FROM risk_events ORDER BY id DESC LIMIT 50")
            read_times.append((time.perf_counter() - started) * 1000.0)
        db.close()

    result = {
        "health_pure_function": summary(health_times),
        "sqlite_risk_event_write": summary(write_times),
        "sqlite_recent_event_read": summary(read_times),
        "environment": {"python": sys.version.split()[0], "platform": sys.platform},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
