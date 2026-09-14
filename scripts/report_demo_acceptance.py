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
    """执行必需查询；SQL/schema 错误必须让验收失败，不能伪装成 0。"""
    try:
        row = connection.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error as exc:
        raise RuntimeError(f"acceptance query failed: {sql}: {exc}") from exc


def _require_tables(connection: sqlite3.Connection, names: set[str]) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    available = {str(row[0]) for row in rows}
    missing = sorted(names - available)
    if missing:
        raise RuntimeError(f"acceptance schema incomplete; missing tables: {', '.join(missing)}")


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
        _require_tables(connection, {
            "schema_migrations", "shadow_decisions", "shadow_outcomes", "trade_outcomes",
            "orders", "positions", "risk_incidents", "order_outbox",
        })
        schema_version = int(_scalar(connection, "SELECT MAX(version) FROM schema_migrations", 0))
        first = _scalar(
            connection,
            "SELECT MIN(created_at) FROM shadow_decisions "
            "WHERE signal IN ('bullish','bearish')", None,
        )
        last = _scalar(
            connection,
            "SELECT MAX(created_at) FROM shadow_decisions "
            "WHERE signal IN ('bullish','bearish')", None,
        )
        first_dt, last_dt = _parse_time(first), _parse_time(last)
        span_days = (
            max(0.0, (last_dt - first_dt).total_seconds() / 86400.0)
            if first_dt and last_dt else 0.0
        )
        candidates = int(_scalar(
            connection,
            "SELECT COUNT(DISTINCT decision_id) FROM shadow_decisions "
            "WHERE signal IN ('bullish','bearish')",
        ))
        closed_trade_rows = int(_scalar(
            connection,
            "SELECT COUNT(*) FROM trade_outcomes WHERE UPPER(status)='CLOSED'",
        ))
        closed_trades = int(_scalar(
            connection,
            "SELECT COUNT(*) FROM trade_outcomes "
            "WHERE UPPER(status)='CLOSED' AND entry_price>0 AND exit_price>0 "
            "AND qty>0 AND opened_at IS NOT NULL AND closed_at IS NOT NULL",
        ))
        source_rows = connection.execute(
            "SELECT source,COUNT(DISTINCT decision_id) AS count "
            "FROM shadow_decisions WHERE signal IN ('bullish','bearish') "
            "GROUP BY source ORDER BY source"
        ).fetchall()
        source_counts = {str(row[0]): int(row[1]) for row in source_rows}
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": resolved.name,
            "schema_version": schema_version,
            "read_only": True,
            "cohort_scope": "shadow_decisions directional signals; trade_outcomes attributable CLOSED rows",
            "directional_candidates": candidates,
            "demo_closed_trades": closed_trades,
            "closed_trade_rows": closed_trade_rows,
            "shadow_source_counts": source_counts,
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
        report["runtime_gates"] = {
            "critical_open_incidents": {
                "required": 0,
                "passed": report["critical_open_incidents"] == 0,
            },
            "unknown_orders": {
                "required": 0,
                "passed": report["unknown_orders"] == 0,
            },
            "submitting_orders": {
                "required": 0,
                "passed": report["submitting_orders"] == 0,
            },
        }
        report["runtime_gates_passed"] = all(
            item["passed"] for item in report["runtime_gates"].values()
        )
        report["release_gates_passed"] = bool(
            report["sample_gates_passed"] and report["runtime_gates_passed"]
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
