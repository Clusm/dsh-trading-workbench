"""只读汇总 Demo 观察门槛；不迁移数据库、不访问网络、不修改交易状态。"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _scalar(connection: sqlite3.Connection, sql: str, default: Any = 0) -> Any:
    try:
        row = connection.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def build_report(db_path: Path) -> dict[str, Any]:
    """以 SQLite 只读 URI 汇总样本与运行风险，不触发任何 schema 初始化。"""
    resolved = Path(db_path).resolve()
    uri = f"file:{resolved.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        first = _scalar(connection, "SELECT MIN(created_at) FROM shadow_decisions", None)
        last = _scalar(connection, "SELECT MAX(created_at) FROM shadow_decisions", None)
        first_dt, last_dt = _parse_time(first), _parse_time(last)
        span_days = (
            max(0.0, (last_dt - first_dt).total_seconds() / 86400.0)
            if first_dt and last_dt else 0.0
        )
        candidates = int(_scalar(
            connection,
            "SELECT COUNT(*) FROM shadow_decisions WHERE signal IN ('bullish','bearish')",
        ))
        closed_trades = int(_scalar(
            connection,
            "SELECT COUNT(*) FROM trade_outcomes WHERE UPPER(status)='CLOSED'",
        ))
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": resolved.name,
            "directional_candidates": candidates,
            "demo_closed_trades": closed_trades,
            # 仅作诊断，不能替代 trade_outcomes 的闭合交易归因门槛。
            "filled_order_rows": int(_scalar(
                connection, "SELECT COUNT(*) FROM orders WHERE status='FILLED'")),
            "closed_position_rows": int(_scalar(
                connection, "SELECT COUNT(*) FROM positions WHERE status='CLOSED'")),
            "observation_days": round(span_days, 3),
            "open_incidents": int(_scalar(
                connection, "SELECT COUNT(*) FROM risk_incidents WHERE status='OPEN'")),
            "critical_open_incidents": int(_scalar(
                connection,
                "SELECT COUNT(*) FROM risk_incidents WHERE status='OPEN' AND severity='critical'",
            )),
            "unknown_orders": int(_scalar(
                connection, "SELECT COUNT(*) FROM order_outbox WHERE status='UNKNOWN'")),
            "submitting_orders": int(_scalar(
                connection, "SELECT COUNT(*) FROM order_outbox WHERE status='SUBMITTING'")),
        }
        report["thresholds"] = {
            "directional_candidates": {"required": 200, "passed": candidates >= 200},
            "demo_closed_trades": {"required": 30, "passed": closed_trades >= 30},
            "observation_days": {"required": 7, "passed": span_days >= 7.0},
        }
        report["sample_gates_passed"] = all(
            item["passed"] for item in report["thresholds"].values()
        )
        return report
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="只读汇总 Demo 验收样本")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "agent_trader.sqlite")
    args = parser.parse_args()
    print(json.dumps(build_report(args.db), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
