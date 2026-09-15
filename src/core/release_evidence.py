"""发布验收证据：只读读取按版本隔离的样本并计算上线门禁。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


PASS = "PASS"
FAIL = "FAIL"
NO_DATA = "NO_DATA"


DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_directional_candidates": 200,
    "min_eligible_closed_trades": 30,
    "min_observation_days": 7.0,
    "min_observation_coverage": 0.999,
    "max_observation_gap_seconds": 120.0,
}


def parse_iso(value: Any) -> datetime | None:
    """解析 ISO 时间并统一为 UTC；坏值返回 None，交由门禁报告显示。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def minute_bucket(value: Any) -> str | None:
    parsed = parse_iso(value)
    if parsed is None:
        return None
    return parsed.replace(second=0, microsecond=0).isoformat(timespec="minutes")


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict"):
        candidate = value.to_dict()
        if isinstance(candidate, Mapping):
            return candidate
    return {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _gate(state: str, *, actual: Any = None, required: Any = None,
          reason: str = "", **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "state": state,
        "passed": state == PASS,
        "actual": actual,
        "required": required,
        "reason": reason,
    }
    result.update(extra)
    return result


def observation_coverage(
    observations: Sequence[Mapping[str, Any]], *, since: Any = None, until: Any = None,
    minimum_coverage: float = 0.999, max_gap_seconds: float = 120.0,
) -> dict[str, Any]:
    """计算健康分钟样本覆盖率与最大缺口，缺证据时明确返回 ``NO_DATA``。"""
    rows = [dict(item) for item in observations]
    parsed_rows: list[tuple[datetime, str, str]] = []
    for row in rows:
        observed = parse_iso(row.get("observed_at"))
        bucket = minute_bucket(row.get("observed_minute") or row.get("observed_at"))
        state = str(row.get("health_state") or "").lower()
        if observed is not None and bucket is not None:
            parsed_rows.append((observed, bucket, state))
    healthy = sorted({(observed, bucket) for observed, bucket, state in parsed_rows
                      if state == "healthy"})
    start = parse_iso(since)
    end = parse_iso(until)
    if start is None and healthy:
        start = healthy[0][0]
    if end is None and healthy:
        end = healthy[-1][0]
    if start is not None and end is not None and end < start:
        raise ValueError("observation window until must not precede since")
    if start is None or end is None or not healthy:
        return {
            "state": NO_DATA,
            "coverage": 0.0,
            "observed_count": 0,
            "expected_count": 0,
            "healthy_count": len(healthy),
            "observation_days": 0.0,
            "max_gap_seconds": None,
            "missing_count": 0,
            "missing_minutes": [],
            "since": start.isoformat() if start else None,
            "until": end.isoformat() if end else None,
        }

    start_minute = start.replace(second=0, microsecond=0)
    end_minute = end.replace(second=0, microsecond=0)
    expected_count = int((end_minute - start_minute).total_seconds() // 60) + 1
    expected_count = max(1, expected_count)
    valid_buckets = {
        bucket for observed, bucket, state in parsed_rows
        if state == "healthy" and start_minute <= observed.replace(second=0, microsecond=0) <= end_minute
    }
    observed_count = len(valid_buckets)
    coverage = observed_count / expected_count
    valid_times = sorted(
        start_minute + timedelta(minutes=index)
        for index in range(expected_count)
        if (start_minute + timedelta(minutes=index)).isoformat(timespec="minutes") in valid_buckets
    )
    max_gap = 0.0
    if valid_times:
        max_gap = max(
            (valid_times[0] - start_minute).total_seconds(),
            (end_minute - valid_times[-1]).total_seconds(),
            *(b.total_seconds() for a, b in zip(valid_times, valid_times[1:])
              for b in [b - a]),
        )
    missing = [
        (start_minute + timedelta(minutes=index)).isoformat(timespec="minutes")
        for index in range(expected_count)
        if (start_minute + timedelta(minutes=index)).isoformat(timespec="minutes")
        not in valid_buckets
    ]
    state = PASS if coverage >= float(minimum_coverage) and max_gap <= float(max_gap_seconds) else FAIL
    observation_days = max(0.0, (valid_times[-1] - valid_times[0]).total_seconds() / 86400.0) if valid_times else 0.0
    return {
        "state": state,
        "coverage": round(coverage, 6),
        "observed_count": observed_count,
        "expected_count": expected_count,
        "healthy_count": len(healthy),
        "observation_days": round(observation_days, 6),
        "max_gap_seconds": round(max_gap, 3),
        "missing_count": len(missing),
        "missing_minutes": missing[:20],
        "since": start_minute.isoformat(timespec="minutes"),
        "until": end_minute.isoformat(timespec="minutes"),
    }


def _require_schema(connection: sqlite3.Connection) -> None:
    required_tables = {
        "schema_migrations", "shadow_decisions", "trade_outcomes", "position_lifecycles",
        "risk_incidents", "order_outbox", "release_observations",
    }
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    available = {str(row[0]) for row in rows}
    missing_tables = sorted(required_tables - available)
    if missing_tables:
        raise RuntimeError("release evidence schema incomplete; missing tables: "
                           + ", ".join(missing_tables))
    required_columns = {
        "shadow_decisions": {"environment", "release_id", "created_at", "signal", "feature_json"},
        "trade_outcomes": {"environment", "release_id", "lifecycle_id", "actual_closed_at",
                            "attribution_status", "cost_status", "status"},
        "position_lifecycles": {"environment", "release_id", "account_ref", "status"},
        "release_observations": {"release_id", "environment", "account_ref", "observed_minute",
                                  "health_state", "entry_readiness_json", "rate_limit_json"},
    }
    for table, expected in required_columns.items():
        actual = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = sorted(expected - actual)
        if missing:
            raise RuntimeError(f"release evidence schema incomplete; {table} missing: {', '.join(missing)}")


def _opportunity_key(row: Mapping[str, Any]) -> str:
    feature = _json_object(row.get("feature_json"))
    explicit = feature.get("opportunity_id") or feature.get("opportunity_key")
    if explicit:
        return str(explicit)
    return "|".join((str(row.get("symbol") or ""), str(row.get("signal") or ""),
                     str(row.get("created_at") or "")[:16]))


def _window_bounds(
    observations: Sequence[Mapping[str, Any]], since: Any, until: Any,
) -> tuple[datetime | None, datetime | None]:
    start = parse_iso(since)
    end = parse_iso(until)
    parsed = sorted(filter(None, (parse_iso(row.get("observed_at")) for row in observations)))
    if start is None and parsed:
        start = parsed[0]
    if end is None and parsed:
        end = parsed[-1]
    if start is not None and end is not None and end < start:
        raise ValueError("release evidence until must not precede since")
    return start, end


def _time_clause(column: str, start: datetime | None, end: datetime | None) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if start is not None:
        clauses.append(f"{column}>=?")
        params.append(start.isoformat())
    if end is not None:
        clauses.append(f"{column}<=?")
        params.append(end.isoformat())
    return (" AND ".join(clauses) if clauses else "1=1"), params


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def window_rate_limit_counts(
    observations: Sequence[Mapping[str, Any]], window_start: datetime | None,
) -> tuple[int, int, int, dict[str, Any]]:
    """统计发布观察窗口内**新增**的限流事件。

    为什么不能直接取 `responses_by_status` 的 max：该字段是限流器自进程启动以来的
    **累计**计数器（rate_limit.py 的会话级 `responses_by_status`），不是窗口内计数。
    直接取 max 会把窗口开始前就已发生的 418 计入本次发布候选。

    为什么不能逐行累加 sticky 的 `last_status`：`last_status` 一旦变成 418 就不会
    自动复原，逐行 +1 会随观察条数线性放大（55 条观测 → 虚报 65 次）。

    因此这里以「累计计数器首尾差值」为窗口内新增数，并用绝对时间戳
    `last_limited_at` 交叉校验：仅当它落在窗口起点之后才计入本次发布。进程重启会让
    累计计数器归零，此时用时间戳兜底补计一次，避免真实事件被漏掉。

    返回 (新增418, 新增429, 协调不可用样本数, 诊断明细)。诊断明细包含窗口外的最近
    一次限流时间，只用于排查展示，不参与门禁判定。
    """
    first_counts: dict[str, int] = {}
    last_counts: dict[str, int] = {}
    coordination_bad = 0
    newest_limited_at: float | None = None
    latest_status = ""
    for row in observations:
        rate = _json_object(row.get("rate_limit_json"))
        statuses = rate.get("responses_by_status")
        if isinstance(statuses, Mapping):
            for code in ("418", "429"):
                value = _as_int(statuses.get(code))
                first_counts.setdefault(code, value)
                last_counts[code] = value
        if rate.get("coordination_available") is False:
            coordination_bad += 1
        stamp = rate.get("last_limited_at")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            newest_limited_at = max(newest_limited_at or 0.0, float(stamp))
        status = str(rate.get("last_status") or "")
        if status in {"418", "429"}:
            latest_status = status

    in_window = {code: max(0, last_counts.get(code, 0) - first_counts.get(code, 0))
                 for code in ("418", "429")}
    window_limited_at: float | None = None
    pre_window_limited_at: float | None = None
    if newest_limited_at is not None and window_start is not None:
        if newest_limited_at >= window_start.timestamp():
            window_limited_at = newest_limited_at
            if latest_status in in_window and in_window[latest_status] == 0:
                in_window[latest_status] = 1
        else:
            pre_window_limited_at = newest_limited_at

    detail = {
        "latest_status": latest_status or None,
        "in_window_limited_at": window_limited_at,
        "pre_window_limited_at": pre_window_limited_at,
        "cumulative_418": last_counts.get("418", 0),
        "cumulative_429": last_counts.get("429", 0),
    }
    return in_window["418"], in_window["429"], coordination_bad, detail


def _open_read_only(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"database not found: {resolved}")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def build_release_readiness_report(
    db_path: Path, manifest: Mapping[str, Any] | Any, *, environment: str,
    since: Any = None, until: Any = None, thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构建严格只读报告；任何缺表/坏 SQL 都抛错而不是伪造通过。"""
    manifest_data = dict(_as_mapping(manifest))
    release_id = str(manifest_data.get("release_id") or "")
    expected_environment = str(environment or "unknown").lower()
    manifest_environment = str(manifest_data.get("environment") or "").lower()
    if not release_id:
        raise ValueError("manifest.release_id is required")
    if manifest_environment and manifest_environment != expected_environment:
        raise ValueError("manifest environment does not match requested environment")
    account_ref = str(manifest_data.get("account_ref") or "unknown")
    threshold = dict(DEFAULT_THRESHOLDS)
    threshold.update(dict(thresholds or {}))
    connection = _open_read_only(db_path)
    try:
        _require_schema(connection)
        schema_row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        schema_version = int(schema_row["version"] or 0) if schema_row else 0
        manifest_schema = int(manifest_data.get("schema_version") or 0)
        observations_raw = connection.execute(
            """SELECT * FROM release_observations
               WHERE release_id=? AND environment=? AND account_ref=?
               ORDER BY observed_at,observed_minute""",
            (release_id, expected_environment, account_ref),
        ).fetchall()
        observations = [dict(row) for row in observations_raw]
        window_start, window_end = _window_bounds(observations, since, until)
        window_start_iso = window_start.isoformat() if window_start else None
        window_end_iso = window_end.isoformat() if window_end else None

        candidate_time, candidate_params = _time_clause("created_at", window_start, window_end)
        candidate_rows = connection.execute(
            f"""SELECT decision_id,created_at,symbol,signal,feature_json,source
                FROM shadow_decisions
                WHERE release_id=? AND environment=? AND signal IN ('bullish','bearish')
                  AND {candidate_time}
                ORDER BY created_at,decision_id""",
            (release_id, expected_environment, *candidate_params),
        ).fetchall()
        candidate_keys = {_opportunity_key(dict(row)) for row in candidate_rows}
        source_counts: dict[str, int] = {}
        for row in candidate_rows:
            key = _opportunity_key(dict(row))
            if key in candidate_keys:
                source_counts[str(row["source"])] = source_counts.get(str(row["source"]), 0) + 1
        # source_counts 是诊断值；门禁使用去重后的机会键，避免 A/B/C 三路重复放大样本。
        closed_time, closed_params = _time_clause("COALESCE(o.actual_closed_at,o.closed_at)", window_start, window_end)
        lifecycle_rows = connection.execute(
            f"""SELECT l.lifecycle_id,l.status AS lifecycle_status,l.release_id AS lifecycle_release_id,
                       l.account_ref,o.outcome_id,o.status,o.actual_closed_at,o.entry_price,o.exit_price,o.qty,
                       o.attribution_status,o.cost_status,o.pnl,o.gross_realized,o.entry_fee,o.exit_fee
                FROM position_lifecycles l
                LEFT JOIN trade_outcomes o ON o.lifecycle_id=l.lifecycle_id
                WHERE l.release_id=? AND l.environment=? AND l.account_ref=?
                  AND l.status='CLOSED' AND {closed_time}
                ORDER BY COALESCE(o.actual_closed_at,o.closed_at),l.lifecycle_id""",
            (release_id, expected_environment, account_ref, *closed_params),
        ).fetchall()
        eligible_lifecycles: set[str] = set()
        eligible_rows: list[dict[str, Any]] = []
        for row in lifecycle_rows:
            item = dict(row)
            if (str(item.get("status") or "").upper() == "CLOSED"
                    and item.get("outcome_id")
                    and item.get("actual_closed_at")
                    and str(item.get("attribution_status") or "").upper() == "ATTRIBUTED"
                    and str(item.get("cost_status") or "").upper() == "COMPLETE"
                    and float(item.get("entry_price") or 0) > 0
                    and float(item.get("exit_price") or 0) > 0
                    and float(item.get("qty") or 0) > 0):
                lifecycle_id = str(item["lifecycle_id"])
                if lifecycle_id not in eligible_lifecycles:
                    eligible_lifecycles.add(lifecycle_id)
                    eligible_rows.append(item)
        current_closed_count = int(connection.execute(
            """SELECT COUNT(*) FROM position_lifecycles
               WHERE release_id=? AND environment=? AND account_ref=? AND status='CLOSED'""",
            (release_id, expected_environment, account_ref),
        ).fetchone()[0])
        missing_outcome_count = max(0, current_closed_count - len({str(row["lifecycle_id"]) for row in lifecycle_rows}))
        incomplete_closed_count = len({str(row["lifecycle_id"]) for row in lifecycle_rows}) - len(eligible_lifecycles)
        carryover_count = int(connection.execute(
            """SELECT COUNT(*) FROM position_lifecycles
               WHERE environment=? AND account_ref=? AND status='CLOSED' AND release_id<>?""",
            (expected_environment, account_ref, release_id),
        ).fetchone()[0])

        critical_open = int(connection.execute(
            "SELECT COUNT(*) FROM risk_incidents WHERE status='OPEN' AND severity='critical'"
        ).fetchone()[0])
        unknown_orders = int(connection.execute(
            "SELECT COUNT(*) FROM order_outbox WHERE environment=? AND status='UNKNOWN'",
            (expected_environment,),
        ).fetchone()[0])
        submitting_orders = int(connection.execute(
            "SELECT COUNT(*) FROM order_outbox WHERE environment=? AND status='SUBMITTING'",
            (expected_environment,),
        ).fetchone()[0])

        coverage = observation_coverage(
            observations, since=window_start_iso, until=window_end_iso,
            minimum_coverage=float(threshold["min_observation_coverage"]),
            max_gap_seconds=float(threshold["max_observation_gap_seconds"]),
        )
        healthy_days = float(coverage.get("observation_days") or 0.0)
        latest_observation = observations[-1] if observations else None
        latest_entry = _json_object(latest_observation.get("entry_readiness_json")) if latest_observation else {}
        if not latest_entry:
            entry_gate = _gate(NO_DATA, actual=None, required=True, reason="没有当前发布的就绪观察")
        elif "new_entry_allowed" not in latest_entry:
            entry_gate = _gate(NO_DATA, actual=latest_entry, required=True,
                               reason="就绪观察缺少 new_entry_allowed")
        elif bool(latest_entry.get("new_entry_allowed")):
            entry_gate = _gate(PASS, actual=True, required=True, reason="最新观察允许新开仓")
        else:
            entry_gate = _gate(FAIL, actual=False, required=True,
                               reason="最新观察仍阻断新开仓", blocking_reasons=latest_entry.get("blocking_reasons", []))

        limited_418, limited_429, coordination_bad, rate_limit_detail = window_rate_limit_counts(
            observations, window_start,
        )
        if not observations:
            network_gate = _gate(NO_DATA, actual=None, required="zero 418/429 and coordination available",
                                 reason="没有当前发布的网络观察")
        elif limited_418 or limited_429 or coordination_bad:
            network_gate = _gate(
                FAIL, actual={"http_418": limited_418, "http_429": limited_429,
                              "coordination_unavailable_samples": coordination_bad},
                required={"http_418": 0, "http_429": 0, "coordination_unavailable_samples": 0},
                reason="当前发布观察含限流/封禁或跨进程协调不可用",
                window_limited_at=rate_limit_detail,
            )
        else:
            network_gate = _gate(PASS, actual={"http_418": 0, "http_429": 0,
                                                "coordination_unavailable_samples": 0},
                                 required={"http_418": 0, "http_429": 0,
                                           "coordination_unavailable_samples": 0},
                                 reason="观察窗口内无 418/429 且协调可用",
                                 window_limited_at=rate_limit_detail)

        gates = {
            "schema_version": _gate(
                PASS if manifest_schema and schema_version == manifest_schema else FAIL,
                actual=schema_version, required=manifest_schema,
                reason="数据库迁移版本与发布清单一致" if manifest_schema and schema_version == manifest_schema
                else "数据库迁移版本与发布清单不一致",
            ),
            "directional_candidates": _gate(
                PASS if len(candidate_keys) >= int(threshold["min_directional_candidates"]) else FAIL,
                actual=len(candidate_keys), required=int(threshold["min_directional_candidates"]),
                reason="按机会键去重后的方向性影子样本",
            ),
            "eligible_closed_trades": _gate(
                PASS if len(eligible_lifecycles) >= int(threshold["min_eligible_closed_trades"]) else FAIL,
                actual=len(eligible_lifecycles), required=int(threshold["min_eligible_closed_trades"]),
                reason="仅计入完整生命周期、归因和成本证据的闭合交易",
                current_closed=current_closed_count, incomplete_closed=incomplete_closed_count,
                missing_outcome=missing_outcome_count, carryover_closed=carryover_count,
            ),
            "observation_coverage": _gate(
                str(coverage.get("state") or NO_DATA), actual=coverage.get("coverage"),
                required=float(threshold["min_observation_coverage"]),
                reason="健康分钟观察覆盖率与缺口门禁", details=coverage,
            ),
            "observation_days": _gate(
                PASS if healthy_days >= float(threshold["min_observation_days"]) else FAIL,
                actual=round(healthy_days, 6), required=float(threshold["min_observation_days"]),
                reason="健康观察跨度门禁",
            ),
            "network_rate_limit": network_gate,
            "critical_open_incidents": _gate(
                PASS if critical_open == 0 else FAIL, actual=critical_open, required=0,
                reason="未收口的 critical incident 必须为零",
            ),
            "unknown_orders": _gate(
                PASS if unknown_orders == 0 else FAIL, actual=unknown_orders, required=0,
                reason="未知订单必须为零",
            ),
            "submitting_orders": _gate(
                PASS if submitting_orders == 0 else FAIL, actual=submitting_orders, required=0,
                reason="在途提交订单必须为零",
            ),
            "entry_readiness": entry_gate,
        }
        release_passed = all(item["passed"] for item in gates.values())
        liveness_state = PASS if observations else NO_DATA
        entry_state = entry_gate["state"]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "read_only": True,
            "database": Path(db_path).resolve().name,
            "environment": expected_environment,
            "account_ref": account_ref,
            "release_id": release_id,
            "manifest": manifest_data,
            "window": {"since": window_start_iso, "until": window_end_iso},
            "service_liveness": {
                "state": liveness_state,
                "passed": liveness_state == PASS,
                "latest_observed_at": latest_observation.get("observed_at") if latest_observation else None,
                "observation_count": len(observations),
            },
            "entry_readiness": {
                "state": entry_state,
                "passed": entry_state == PASS,
                "latest": latest_entry or None,
            },
            "release_evidence": {
                "directional_candidates": len(candidate_keys),
                "shadow_rows": len(candidate_rows),
                "shadow_source_rows": source_counts,
                "eligible_closed_lifecycles": len(eligible_lifecycles),
                "eligible_closed_rows": eligible_rows,
                "current_closed_lifecycles": current_closed_count,
                "incomplete_closed_lifecycles": incomplete_closed_count,
                "missing_outcome_lifecycles": missing_outcome_count,
                "carryover_closed_lifecycles": carryover_count,
                "observations": len(observations),
                "observation_coverage": coverage,
            },
            "runtime": {
                "critical_open_incidents": critical_open,
                "unknown_orders": unknown_orders,
                "submitting_orders": submitting_orders,
            },
            "schema_version": schema_version,
            "gates": gates,
            "release_readiness": "GO" if release_passed else "NO_GO",
            "release_gates_passed": release_passed,
        }
    finally:
        connection.close()
