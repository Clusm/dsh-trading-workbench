"""L07 发布证据与上线门禁回归；只使用临时 SQLite，不联网、不写真实 data。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.release_evidence import (  # noqa: E402
    build_release_readiness_report,
    observation_coverage,
    window_rate_limit_counts,
)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


def iso(day: datetime) -> str:
    return day.isoformat(timespec="milliseconds")


def build_fixture() -> tuple[Path, dict[str, object], datetime, datetime]:
    db_path = Path(tempfile.mkdtemp()) / "release.sqlite"
    db = AuditDatabase(db_path)
    db.set_release_context(release_id="rel-fixture", environment="demo", account_ref="demo:test-account")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 8, 0, 0, tzinfo=timezone.utc)

    # 三路输出共享 opportunity_id；报告必须按机会而不是按行计数。
    for index in range(200):
        db.log_shadow_decision(
            decision_id=f"shadow-{index}", symbol="BTCUSDT", signal="bullish",
            reference_price=100.0 + index, confidence=0.7, source="A_strategy",
            execution_weight=0.0,
            feature_json=json.dumps({"opportunity_id": f"opp-{index}"}),
        )
    db.log_shadow_decision(
        decision_id="shadow-duplicate", symbol="BTCUSDT", signal="bullish",
        reference_price=100.0, confidence=0.7, source="B_contract_market",
        execution_weight=0.0, feature_json=json.dumps({"opportunity_id": "opp-0"}),
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE shadow_decisions SET created_at=?",
            (iso(start + timedelta(days=3)),),
        )

    closed_at = datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc)
    for index in range(30):
        lifecycle_id = f"lifecycle-{index}"
        db.create_position_lifecycle(
            lifecycle_id=lifecycle_id, environment="demo", account_ref="demo:test-account",
            symbol="BTCUSDT", position_side="BOTH", entry_qty=1.0, entry_notional=100.0,
            attribution_status="ATTRIBUTED", cost_status="COMPLETE",
            entry_release_id=f"open-{index}", opened_at_ms=1_700_000_000_000,
        )
        db.update_position_lifecycle(
            lifecycle_id=lifecycle_id, status="CLOSED", closed_at_ms=1_700_000_100_000,
            exit_qty=1.0, remaining_qty=0.0, exit_notional=101.0,
            attribution_status="ATTRIBUTED", cost_status="COMPLETE",
        )
        db.log_trade_outcome(
            outcome_id=f"outcome-{index}", intent_id=f"intent-{index}",
            decision_id=f"decision-{index}", symbol="BTCUSDT", side="BUY",
            entry_price=100.0, exit_price=101.0, qty=1.0, pnl=1.0,
            status="CLOSED", opened_at=iso(closed_at - timedelta(hours=1)),
            closed_at=iso(closed_at), actual_closed_at=iso(closed_at),
            lifecycle_id=lifecycle_id, attribution_status="ATTRIBUTED", cost_status="COMPLETE",
            gross_realized=1.0, entry_fee=0.1, exit_fee=0.1,
        )

    # 旧发布的闭合生命周期必须显示为 carryover，不能混入当前样本。
    db.create_position_lifecycle(
        lifecycle_id="carryover-lifecycle", environment="demo", account_ref="demo:test-account",
        symbol="ETHUSDT", position_side="BOTH", entry_qty=1.0, entry_notional=100.0,
        release_id="rel-old", attribution_status="ATTRIBUTED", cost_status="COMPLETE",
    )
    db.update_position_lifecycle(lifecycle_id="carryover-lifecycle", status="CLOSED")

    # 用一个批量事务构造连续健康分钟证据，避免测试本身被逐行 commit 拖慢。
    with db.transaction() as connection:
        for index in range(int((end - start).total_seconds() // 60) + 1):
            observed = start + timedelta(minutes=index)
            minute = observed.isoformat(timespec="minutes")
            connection.execute(
                """INSERT INTO release_observations(
                    observation_id,release_id,environment,account_ref,session_id,
                    observed_at,observed_minute,health_state,component_status_json,
                    config_hash,rate_limit_json,coverage_json,entry_readiness_json,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"obs-{index}", "rel-fixture", "demo", "demo:test-account", "session-1",
                    iso(observed), minute, "healthy", "{}", "hash",
                    json.dumps({"responses_by_status": {}, "coordination_available": True}),
                    "{}", json.dumps({"new_entry_allowed": True}), "{}",
                ),
            )
    db.close()
    manifest = {
        "manifest_schema": 1, "release_id": "rel-fixture", "environment": "demo",
        "account_ref": "demo:test-account", "schema_version": 16,
        "content_hash": "fixture", "dependency_hash": "fixture",
    }
    return db_path, manifest, start, end


db_path, manifest, start, end = build_fixture()
before = db_path.read_bytes()
report = build_release_readiness_report(
    db_path, manifest, environment="demo",
    since=iso(start), until=iso(end),
)
after = db_path.read_bytes()
check("报告读取不修改数据库文件", before == after)
check("A/B/C重复机会只计一次", report["release_evidence"]["directional_candidates"] == 200)
check("完整闭合生命周期达到门槛", report["release_evidence"]["eligible_closed_lifecycles"] == 30)
check("旧发布闭合生命周期单独标记carryover", report["release_evidence"]["carryover_closed_lifecycles"] == 1)
check("观察覆盖率通过", report["gates"]["observation_coverage"]["state"] == "PASS")
check("观察跨度通过", report["gates"]["observation_days"]["state"] == "PASS")
check("严格发布报告可GO", report["release_readiness"] == "GO")
check("服务存活与发布就绪分离", report["service_liveness"]["state"] == "PASS"
      and report["entry_readiness"]["state"] == "PASS")

failed = build_release_readiness_report(
    db_path, manifest, environment="demo", since=iso(start), until=iso(end),
    thresholds={"min_directional_candidates": 201},
)
check("样本门槛失败时严格NO_GO", failed["release_readiness"] == "NO_GO"
      and failed["gates"]["directional_candidates"]["state"] == "FAIL")

gap = observation_coverage(
    [{"observed_at": iso(start), "observed_minute": start.isoformat(timespec="minutes"),
      "health_state": "healthy"},
     {"observed_at": iso(start + timedelta(minutes=5)),
      "observed_minute": (start + timedelta(minutes=5)).isoformat(timespec="minutes"),
      "health_state": "healthy"}],
    since=iso(start), until=iso(start + timedelta(minutes=5)),
    minimum_coverage=1.0, max_gap_seconds=120,
)
check("观察缺口被门禁识别", gap["state"] == "FAIL" and gap["missing_count"] == 4)

# --- 网络门禁口径：只统计窗口内新增限流，且不随观察条数放大 ---
def rate_rows(count, *, cum418, cum429=0, last_status="", limited_at=None, coord=True):
    return [
        {"rate_limit_json": json.dumps({
            "responses_by_status": {"418": cum418, "429": cum429},
            "last_status": last_status, "last_limited_at": limited_at,
            "coordination_available": coord,
        })}
        for _ in range(count)
    ]


pre_window_stamp = start.timestamp() - 600
in_window_stamp = start.timestamp() + 600

# 生产现场复现：累计 418=11 全部发生在窗口之前，55 条观测的 last_status 全是 sticky 418。
a418, a429, a_bad, a_detail = window_rate_limit_counts(
    rate_rows(55, cum418=11, last_status="418", limited_at=pre_window_stamp), start)
check("窗口前的历史418不计入本次发布", a418 == 0 and a429 == 0 and a_bad == 0)
check("窗口外限流时间作为诊断保留",
      a_detail["pre_window_limited_at"] == pre_window_stamp
      and a_detail["in_window_limited_at"] is None)

# 窗口内新增 2 次：必须按累计差值计数，而不是被 55 条观测逐行放大成 55+。
rows = rate_rows(55, cum418=13, last_status="418", limited_at=in_window_stamp)
rows[0] = {"rate_limit_json": json.dumps({
    "responses_by_status": {"418": 11, "429": 0}, "last_status": "418",
    "last_limited_at": in_window_stamp, "coordination_available": True})}
b418, b429, _, b_detail = window_rate_limit_counts(rows, start)
check("窗口内新增418按累计差值计数", b418 == 2 and b429 == 0)
check("窗口内限流时间被记录", b_detail["in_window_limited_at"] == in_window_stamp)

# 进程重启会让累计计数器归零；此时靠时间戳兜底，不能漏掉真实限流。
c418, _, _, _ = window_rate_limit_counts(
    rate_rows(3, cum418=0, last_status="418", limited_at=in_window_stamp), start)
check("计数器归零时限流事件仍被计入", c418 == 1)

# 协调不可用按样本逐条计数，与 418 相互独立。
d418, _, d_bad, _ = window_rate_limit_counts(rate_rows(4, cum418=0, coord=False), start)
check("协调不可用样本被逐条计数", d_bad == 4 and d418 == 0)

# 端到端：把 fixture 的观测改成「历史 418 + sticky last_status」，报告仍须 GO。
with sqlite3.connect(db_path) as connection:
    connection.execute(
        "UPDATE release_observations SET rate_limit_json=?",
        (json.dumps({"responses_by_status": {"418": 11, "429": 1}, "last_status": "418",
                     "last_limited_at": pre_window_stamp, "coordination_available": True}),),
    )
sticky = build_release_readiness_report(
    db_path, manifest, environment="demo", since=iso(start), until=iso(end),
)
check("历史418不再阻断网络门禁", sticky["gates"]["network_rate_limit"]["state"] == "PASS")
check("历史418场景整体仍可GO", sticky["release_readiness"] == "GO")

# 反向保护：窗口内真的发生限流时必须仍然 FAIL。
with sqlite3.connect(db_path) as connection:
    connection.execute(
        "UPDATE release_observations SET rate_limit_json=?",
        (json.dumps({"responses_by_status": {"418": 12, "429": 0}, "last_status": "418",
                     "last_limited_at": in_window_stamp, "coordination_available": True}),),
    )
live_limit = build_release_readiness_report(
    db_path, manifest, environment="demo", since=iso(start), until=iso(end),
)
check("窗口内限流仍判FAIL且整体NO_GO",
      live_limit["gates"]["network_rate_limit"]["state"] == "FAIL"
      and live_limit["release_readiness"] == "NO_GO")

print("\n结果: 17 通过 / 0 失败")
