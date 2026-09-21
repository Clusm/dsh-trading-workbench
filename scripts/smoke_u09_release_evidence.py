"""U09 离线冒烟：发布证据口径与事件恢复门禁（F10/F12/F16/F23/F24）。

    PYTHONUTF8=1 <venv>/python.exe scripts/smoke_u09_release_evidence.py

全程离线：只在系统临时目录里造合成 SQLite，不读真实 ``data``/``.env``/密钥、
不联网、不启动服务、不下单、不发告警。逐项打印 PASS/FAIL，任一失败退出码 1。

**两态可跑**（本项目血的教训）：本脚本不假定 U09 补丁是否已应用。

* 补丁**不在**：断言基线缺陷确实存在（S08 影子泄漏使方向候选从 200 变 300），
  用同一份夹具把"未修时的数字"如实记下来；
* 补丁**在**：断言修复后的口径（来源过滤 / 窗口首尾 / NO_EDGE / 迟到修订 /
  ACK≠恢复 / 重启≠换版）。两种状态的期望都是真的，不存在 SKIP 掩盖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core import health_watch  # noqa: E402
from core import release_evidence as evidence  # noqa: E402
from intel import shadow_status  # noqa: E402


FAILURES: list[str] = []
PASSED = 0


def check(name: str, condition: bool, detail: Any = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL  {name}  {detail}")


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def minute(moment: datetime) -> str:
    return moment.replace(second=0, microsecond=0).isoformat(timespec="minutes")


PATCHED = bool(getattr(evidence, "U09_EVIDENCE_VERSION", ""))
SHARED = bool(getattr(health_watch, "U09_INTEGRATION", ""))
print(f"U09 证据版本标记: {PATCHED}  health_watch 共享接线: {SHARED}")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class Fixture:
    """一个临时库 + 发布上下文；``release_id`` 可切换以构造"不同 RC 混样"。"""

    def __init__(self, root: Path, *, release_id: str = "rel-u09-e2e",
                 environment: str = "demo", account_ref: str = "demo:u09") -> None:
        self.path = root / "release.sqlite"
        self.db = AuditDatabase(self.path)
        self.release_id = release_id
        self.environment = environment
        self.account_ref = account_ref
        self.db.set_release_context(release_id=release_id, environment=environment,
                                    account_ref=account_ref)

    def close(self) -> None:
        self.db.close()


def add_shadow_rows(db: AuditDatabase, *, prefix: str, source: str, count: int,
                    signal: str = "bullish", feature: dict[str, Any] | None = None,
                    start_index: int = 0) -> None:
    for index in range(count):
        payload = {"opportunity_id": f"{prefix}-opp-{start_index + index}"}
        payload.update(feature or {})
        db.log_shadow_decision(
            decision_id=f"{prefix}-{start_index + index}", symbol="BTCUSDT", signal=signal,
            reference_price=100.0, confidence=0.7, source=source, execution_weight=0.0,
            feature_json=json.dumps(payload, ensure_ascii=False),
        )


def add_observations(db: AuditDatabase, *, release_id: str, environment: str,
                     account_ref: str, start: datetime, count: int, session_id: str,
                     config_hash: str = "cfg-a", health_state: str = "healthy",
                     step_minutes: int = 1, rate_limit: dict[str, Any] | None = None,
                     entry_readiness: dict[str, Any] | None = None) -> None:
    """按分钟批量写入观察证据（同分钟 UNIQUE，重复写入被忽略）。"""
    with db.transaction() as connection:
        for index in range(count):
            observed = start + timedelta(minutes=index * step_minutes)
            connection.execute(
                """INSERT OR IGNORE INTO release_observations(
                    observation_id,release_id,environment,account_ref,session_id,
                    observed_at,observed_minute,health_state,component_status_json,
                    config_hash,rate_limit_json,coverage_json,entry_readiness_json,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"obs-{release_id}-{session_id}-{index}", release_id, environment,
                    account_ref, session_id, iso(observed), minute(observed), health_state,
                    "{}", config_hash,
                    json.dumps(rate_limit or {"responses_by_status": {},
                                              "coordination_available": True}),
                    "{}", json.dumps(entry_readiness or {"new_entry_allowed": True}), "{}",
                ),
            )


def add_lifecycle(db: AuditDatabase, index: int, *, closed_at: datetime,
                  lifecycle_id: str | None = None, symbol: str = "BTCUSDT",
                  entry_price: float = 100.0, exit_price: float = 101.0,
                  gross: float = 1.0, entry_fee: float = 0.1, exit_fee: float = 0.1,
                  funding: float = 0.0, side: str = "BOTH",
                  cost_status: str = "COMPLETE",
                  outcome_id: str | None = None, recorded_at: str | None = None,
                  release_id: str | None = None, environment: str = "demo",
                  account_ref: str = "demo:u09") -> None:
    lifecycle = lifecycle_id or f"lc-{index}"
    db.create_position_lifecycle(
        lifecycle_id=lifecycle, environment=environment, account_ref=account_ref,
        symbol=symbol, position_side=side, entry_qty=1.0, entry_notional=entry_price,
        attribution_status="ATTRIBUTED", cost_status=cost_status,
        opened_at_ms=1_700_000_000_000, release_id=release_id,
    )
    db.update_position_lifecycle(
        lifecycle_id=lifecycle, status="CLOSED",
        closed_at_ms=int(closed_at.timestamp() * 1000), exit_qty=1.0, remaining_qty=0.0,
        exit_notional=exit_price, attribution_status="ATTRIBUTED", cost_status=cost_status,
    )
    db.log_trade_outcome(
        outcome_id=outcome_id or f"outcome-{lifecycle}", intent_id=f"intent-{lifecycle}",
        decision_id=f"decision-{lifecycle}", symbol=symbol, side="BUY",
        entry_price=entry_price, exit_price=exit_price, qty=1.0, pnl=gross,
        status="CLOSED", opened_at=iso(closed_at - timedelta(hours=1)),
        closed_at=iso(closed_at), actual_closed_at=iso(closed_at),
        lifecycle_id=lifecycle, attribution_status="ATTRIBUTED", cost_status=cost_status,
        gross_realized=gross, entry_fee=entry_fee, exit_fee=exit_fee,
        funding_cashflow=funding, recorded_at=recorded_at or iso(closed_at),
        environment=environment, release_id=release_id,
    )


def add_alert_delivery(db: AuditDatabase, *, incident_key: str, state: str,
                       attempt: int = 1, state_rank: int = 1,
                       provider_receipt: str | None = None,
                       device_receipt: str | None = None,
                       delivered_at_ms: int | None = None,
                       ack_at_ms: int | None = None,
                       environment: str = "demo") -> None:
    with db.transaction() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO alert_deliveries(
                environment,incident_key,attempt,state,state_rank,severity,
                first_occurred_at,first_occurred_at_ms,created_at,submitted_at_ms,
                delivered_at_ms,ack_at_ms,provider_receipt,device_receipt,
                receiver_ref,record_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (environment, incident_key, attempt, state, state_rank, "critical",
             "2026-01-01T00:00:00+00:00", 1_700_000_000_000,
             "2026-01-01T00:00:00+00:00", 1_700_000_000_000,
             delivered_at_ms, ack_at_ms, provider_receipt, device_receipt,
             "ops-channel", "{}"),
        )


def add_incident(db: AuditDatabase, *, incident_key: str, severity: str = "critical",
                 status: str = "OPEN", acknowledged_at: str | None = None,
                 resolved_at: str | None = None) -> None:
    with db.transaction() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO risk_incidents(
                incident_key,event_type,scope,severity,status,first_seen_at,last_seen_at,
                occurrence_count,acknowledged_at,resolved_at,raw_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (incident_key, "risk_breach", "account", severity, status,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", 1,
             acknowledged_at, resolved_at, "{}"),
        )


def build_report(fixture: Fixture, *, since: Any, until: Any,
                 thresholds: dict[str, Any] | None = None,
                 declaration: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = {
        "manifest_schema": 1, "release_id": fixture.release_id,
        "environment": fixture.environment, "account_ref": fixture.account_ref,
        "schema_version": fixture.db.current_schema_version(),
        "artifact_hash": "sha256:u09-artifact", "content_hash": "sha256:u09-artifact",
        "runtime_instance_id": "run-u09-1",
    }
    return evidence.build_release_readiness_report(
        fixture.path, manifest, environment=fixture.environment,
        since=since, until=until, thresholds=thresholds, declaration=declaration,
    )


# --------------------------------------------------------------------------- #
# [1] S08 影子泄漏：B/C 研究样本不得冒充执行候选（F16）
# --------------------------------------------------------------------------- #
def scenario_shadow_source_leak(root: Path) -> None:
    print("\n[1] F16/S08：B/C 研究影子样本不得进 200 方向候选分母")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=120)
    thresholds = {"min_observation_days": 0.0, "min_eligible_closed_trades": 0}
    # 两份夹具：只有 A（200 条）与 A+B+C（300 条）。分母若不随 B/C 变化，
    # 说明口径可撤销、可重算（不靠删数据掩盖）。
    reports = {}
    for label, with_research in (("A", False), ("ABC", True)):
        fixture = Fixture(root / f"leak-{label}")
        add_shadow_rows(fixture.db, prefix="a", source=shadow_status.SOURCE_A, count=200)
        if with_research:
            add_shadow_rows(fixture.db, prefix="b", source=shadow_status.SOURCE_B, count=50,
                            start_index=1000)
            add_shadow_rows(fixture.db, prefix="c", source=shadow_status.SOURCE_C, count=50,
                            start_index=2000)
        add_observations(fixture.db, release_id=fixture.release_id,
                         environment=fixture.environment, account_ref=fixture.account_ref,
                         start=start, count=121, session_id="session-1")
        with fixture.db.transaction() as connection:
            connection.execute("UPDATE shadow_decisions SET created_at=?",
                               (iso(start + timedelta(minutes=5)),))
        reports[label] = build_report(fixture, since=iso(start), until=iso(end),
                                     thresholds=thresholds)
        fixture.close()
    only_a = reports["A"]["release_evidence"]
    evidence_block = reports["ABC"]["release_evidence"]
    print(f"    A-only: candidates={only_a['directional_candidates']} "
          f"rows={only_a['shadow_rows']}")
    print(f"    A+B+C:  candidates={evidence_block['directional_candidates']} "
          f"rows={evidence_block['shadow_rows']} "
          f"source_rows={evidence_block['shadow_source_rows']}")
    if not PATCHED:
        # 基线态：记录缺陷证据（B/C 使分母从 200 抬到 300）。
        check("基线态（未打补丁）：只有 A 时分母=200", only_a["directional_candidates"] == 200,
              only_a["directional_candidates"])
        check("基线态：B/C 影子确实污染分母（200 → 300）",
              evidence_block["directional_candidates"] == 300,
              evidence_block["directional_candidates"])
        check("基线态：B/C 混在同一分母里、无法按来源撤销",
              evidence_block["shadow_source_rows"].get(shadow_status.SOURCE_B) == 50
              and evidence_block["shadow_source_rows"].get(shadow_status.SOURCE_C) == 50,
              evidence_block["shadow_source_rows"])
        return

    check("补丁态：分母只计 A 组（200），B/C 不再抬升（300 → 200）",
          evidence_block["directional_candidates"] == 200,
          evidence_block["directional_candidates"])
    check("补丁态：口径可撤销、可重算（去掉 B/C 前后分母同为 200）",
          only_a["directional_candidates"] == evidence_block["directional_candidates"] == 200,
          (only_a["directional_candidates"], evidence_block["directional_candidates"]))
    check("补丁态：B/C 只在诊断字段里出现（如实披露但不计分）",
          evidence_block["shadow_source_rows"].get(shadow_status.SOURCE_B) == 50
          and evidence_block["shadow_source_rows"].get(shadow_status.SOURCE_C) == 50,
          evidence_block["shadow_source_rows"])
    check("补丁态：来源口径来自 intel.shadow_status（不是第二份 source 名单）",
          evidence_block["candidate_source_filter"]["authority"] == "intel.shadow_status"
          and shadow_status.SOURCE_A in evidence_block["candidate_source_filter"]["sources"]
          and shadow_status.SOURCE_B not in evidence_block["candidate_source_filter"]["sources"],
          evidence_block["candidate_source_filter"])
    check("补丁态：研究影子被排除且原因可核（research_only_source=100）",
          evidence_block["shadow_excluded_by_reason"].get("research_only_source") == 100,
          evidence_block["shadow_excluded_by_reason"])

    fixture = Fixture(root / "leak-noedge")
    add_shadow_rows(fixture.db, prefix="a", source=shadow_status.SOURCE_A, count=200)
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start, count=121, session_id="session-1")

    # --- 纯 NO_EDGE / 无方向信号不得凑 200 ---
    print("\n[1b] F24：纯 NO_EDGE / 无方向信号不得凑 200")
    add_shadow_rows(fixture.db, prefix="noedge", source=shadow_status.SOURCE_A, count=30,
                    start_index=3000, feature={"no_edge": True})
    add_shadow_rows(fixture.db, prefix="reason", source=shadow_status.SOURCE_A, count=30,
                    start_index=4000, feature={"reason_code": "NO_EDGE"})
    add_shadow_rows(fixture.db, prefix="neutral", source=shadow_status.SOURCE_A, count=40,
                    signal="neutral", start_index=5000)
    add_shadow_rows(fixture.db, prefix="unknown", source=shadow_status.SOURCE_A, count=10,
                    signal="unknown", start_index=6000)
    with fixture.db.transaction() as connection:
        connection.execute("UPDATE shadow_decisions SET created_at=?", (iso(start + timedelta(minutes=5)),))
    after = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    block = after["release_evidence"]
    check("补丁态：NO_EDGE/无方向样本不改变分母（仍为 200）",
          block["directional_candidates"] == 200, block["directional_candidates"])
    check("补丁态：被排除原因逐项可核（no_edge_declared=60, non_directional_signal=50）",
          block["shadow_excluded_by_reason"].get("no_edge_declared") == 60
          and block["shadow_excluded_by_reason"].get("non_directional_signal") == 50,
          block["shadow_excluded_by_reason"])
    check("补丁态：候选来源计数只含执行决策来源",
          set(block["shadow_counted_sources"]) == {shadow_status.SOURCE_A},
          block["shadow_counted_sources"])
    fixture.close()


# --------------------------------------------------------------------------- #
# [2] 观察窗口：首尾缺失计入分母，绝不自动截断（F24）
# --------------------------------------------------------------------------- #
def scenario_observation_window(root: Path) -> None:
    print("\n[2] F24：窗口首尾断档计入分母，禁止自动截到首末健康记录")
    fixture = Fixture(root / "window")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    # 观察从 +3 分钟才开始、到 -2 分钟就停：首尾各缺一段。
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start + timedelta(minutes=3), count=26, session_id="session-1")
    report = build_report(fixture, since=iso(start), until=iso(end),
                          thresholds={"min_observation_days": 0.0})
    coverage = report["release_evidence"]["observation_coverage"]
    print(f"    expected={coverage['expected_count']} observed={coverage['observed_count']} "
          f"head_missing={coverage['head_missing_count']} tail_missing={coverage['tail_missing_count']} "
          f"days={coverage['observation_days']}")
    check("窗口首尾缺失计入分母（expected=31，不是观察到的 26）",
          coverage["expected_count"] == 31 and coverage["observed_count"] == 26,
          coverage)
    check("首尾缺失被单独披露（head=3, tail=2）",
          coverage["head_missing_count"] == 3 and coverage["tail_missing_count"] == 2,
          coverage)
    check("覆盖率因此不通过（不得用 26/26=100% 冒充）",
          report["gates"]["observation_coverage"]["state"] == "FAIL", coverage)
    check("窗口不来自观察数据（derived_from_observations=False）",
          coverage["derived_from_observations"] is False
          and report["window"]["derived_from_observations"] is False,
          report["window"])
    continuity = report["run_continuity"]
    check("观察者断档分钟与窗口比对披露（head=3, tail=2）",
          continuity.get("head_gap_minutes") == 3
          and continuity.get("tail_gap_minutes") == 2,
          {"head": continuity.get("head_gap_minutes"),
           "tail": continuity.get("tail_gap_minutes")})

    # 未声明窗口：必须 NO_DATA + WINDOW_MISSING，绝不自动截到首末健康记录。
    undeclared = evidence.build_release_readiness_report(
        fixture.path,
        {"release_id": fixture.release_id, "environment": fixture.environment,
         "account_ref": fixture.account_ref, "schema_version": 1},
        environment=fixture.environment,
        thresholds={"min_observation_days": 0.0})
    undeclared_coverage = undeclared["gates"]["observation_coverage"]
    check("补丁态：未声明窗口 → NO_DATA + WINDOW_MISSING（不再自动截断）",
          undeclared_coverage["state"] == "NO_DATA"
          and "WINDOW_MISSING" in undeclared["window"]["reason_codes"],
          undeclared["window"])
    check("补丁态：窗口不成立时方向候选/闭合交易不为 0 冒充，而是 NO_DATA",
          undeclared["gates"]["directional_candidates"]["state"] == "NO_DATA"
          and undeclared["gates"]["directional_candidates"]["actual"] is None,
          undeclared["gates"]["directional_candidates"])
    fixture.close()


def scenario_window_invalid(root: Path) -> None:
    print("\n[3] K06：畸形窗口 FAIL + WINDOW_INVALID，报告不崩溃")
    fixture = Fixture(root / "window_invalid")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start, count=31, session_id="session-1")
    bad_values = ["not-a-date", float("inf"), "1e400", float("nan"), 2 ** 64, 2 ** 64 + 1,
                  "99999999999999999999"]
    for value in bad_values:
        try:
            report = build_report(fixture, since=value, until=iso(start + timedelta(minutes=30)),
                                  thresholds={"min_observation_days": 0.0})
        except Exception as exc:  # noqa: BLE001 - 崩溃即失败
            check(f"畸形窗口 {value!r} 不崩溃", False, f"{type(exc).__name__}: {exc}")
            continue
        coverage = report["gates"]["observation_coverage"]
        check(f"畸形窗口 {value!r} → FAIL + WINDOW_INVALID",
              coverage["state"] == "FAIL" and "WINDOW_INVALID" in report["window"]["reason_codes"],
              report["window"]["reason_codes"])
    reversed_window = build_report(
        fixture, since=iso(start + timedelta(minutes=30)), until=iso(start),
        thresholds={"min_observation_days": 0.0})
    check("反向窗口 → FAIL + WINDOW_INVALID（不抛异常）",
          reversed_window["gates"]["observation_coverage"]["state"] == "FAIL"
          and "WINDOW_INVALID" in reversed_window["window"]["reason_codes"],
          reversed_window["window"])
    fixture.close()


# --------------------------------------------------------------------------- #
# [4] F23/F10：去重计数、迟到修订、冲突隔离、净收益重算
# --------------------------------------------------------------------------- #
def scenario_lifecycle_revision(root: Path) -> None:
    print("\n[4] F23/F10：lifecycle 去重、迟到修订重算、冲突隔离")
    fixture = Fixture(root / "revisions")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    closed_at = start + timedelta(minutes=10)
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start, count=31, session_id="session-1")
    thresholds = {"min_observation_days": 0.0, "min_eligible_closed_trades": 30}
    # 30 笔完整闭合；lc-0/lc-1/lc-2 落在同一毫秒（满页/同毫秒），lc-3 是双向仓的另一边。
    for index in range(30):
        add_lifecycle(fixture.db, index, closed_at=closed_at,
                      lifecycle_id=f"lc-{index}",
                      symbol="ETHUSDT" if index == 29 else "BTCUSDT",
                      side="SHORT" if index == 3 else "BOTH",
                      funding=0.0 if index % 2 else 0.05)
    baseline = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    block = baseline["release_evidence"]
    check("30 笔完整闭合按 lifecycle_id 计数", block["eligible_closed_lifecycles"] == 30,
          block["eligible_closed_lifecycles"])
    check("同一毫秒多笔互不吞并（满页不虚报也不漏报）",
          block["current_closed_lifecycles"] == 30, block["current_closed_lifecycles"])
    expected_net = sum(
        (1.0 - 0.1 - 0.1 + (0.0 if index % 2 else 0.05)) for index in range(30))
    before_net = block["eligible_net_realized_total"]
    check("净收益按 gross - 费用 + 资金费重算（零资金费不等于缺证据）",
          abs(before_net - round(expected_net, 10)) < 1e-9, (before_net, expected_net))

    # 迟到到达：同一 lifecycle 再落一条**新 outcome_id** 且费用被修订。
    base_lc0 = next(item for item in block["eligible_closed_rows"]
                    if item["lifecycle_id"] == "lc-0")
    add_lifecycle(fixture.db, 0, closed_at=closed_at, lifecycle_id="lc-0",
                  gross=1.0, entry_fee=0.1, exit_fee=0.4, funding=0.05,
                  outcome_id="outcome-lc-0-revised",
                  recorded_at=iso(closed_at + timedelta(hours=2)))
    revised = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    revised_block = revised["release_evidence"]
    check("迟到修订不重复计数（lifecycle 仍为 30，不是 31）",
          revised_block["eligible_closed_lifecycles"] == 30,
          revised_block["eligible_closed_lifecycles"])
    check("迟到修订被登记（revised_lifecycles 含 lc-0 且记录被取代的 outcome）",
          [item["lifecycle_id"] for item in revised_block["revised_lifecycles"]] == ["lc-0"]
          and revised_block["revised_lifecycles"][0]["superseded_outcome_ids"]
          == ["outcome-lc-0"],
          revised_block["revised_lifecycles"])
    revised_lc0 = next(item for item in revised_block["eligible_closed_rows"]
                       if item["lifecycle_id"] == "lc-0")
    expected_after = expected_net - base_lc0["net_realized"] + revised_lc0["net_realized"]
    check("净收益随修订重算（不是把两条相加，也不是保留旧值）",
          abs(revised_block["eligible_net_realized_total"]
              - round(expected_after, 10)) < 1e-9
          and revised_lc0["net_realized"] < base_lc0["net_realized"],
          (revised_block["eligible_net_realized_total"], expected_after,
           base_lc0["net_realized"], revised_lc0["net_realized"]))
    check("最新修订的成本被真正采信（exit_fee 0.1 → 0.4）",
          abs(revised_lc0["net_realized"] - 0.55) < 1e-9 and revised_lc0["cost_recomputed"] is True,
          revised_lc0)
    new_hash = next(item["evidence_revision_hash"]
                    for item in revised_block["eligible_closed_rows"]
                    if item["lifecycle_id"] == "lc-0")
    check("成本修订改变证据 hash（发布证据可修订、可复核）",
          base_lc0["evidence_revision_hash"] != new_hash,
          (base_lc0["evidence_revision_hash"], new_hash))

    # 同一次补录里两条互相矛盾（同一 lifecycle、同一 recorded_at、内容不同）：
    # 无法定序 → 冲突隔离，不得进 30。
    add_lifecycle(fixture.db, 20, closed_at=closed_at, lifecycle_id="lc-20",
                  gross=9.99, outcome_id="outcome-lc-20-b", recorded_at=iso(closed_at))
    conflicted = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    conflicted_block = conflicted["release_evidence"]
    check("同一 recorded_at 上内容冲突 → 冲突隔离（lc-20 被排除）",
          [item["lifecycle_id"] for item in conflicted_block["conflicted_lifecycles"]] == ["lc-20"],
          conflicted_block["conflicted_lifecycles"])
    check("冲突生命周期不计入 30（29 笔，拒绝统计伪通过）",
          conflicted_block["eligible_closed_lifecycles"] == 29
          and conflicted["gates"]["eligible_closed_trades"]["state"] == "FAIL",
          conflicted_block["eligible_closed_lifecycles"])
    check("冲突被登记为 REVISION_CONFLICT 且不虚报覆盖",
          "REVISION_CONFLICT" in str(conflicted_block["conflicted_lifecycles"][0]["conflict_reason"])
          and conflicted_block["current_closed_lifecycles"] == 30,
          conflicted_block["conflicted_lifecycles"][0])

    # 纯函数级：重复 outcome_id 不同内容也必须隔离（DB 层 INSERT OR IGNORE 挡不住的
    # 历史/回滚库仍可能带这种行，判定不得靠"库不会这样"）。
    duplicates = evidence.select_lifecycle_revisions([
        {"lifecycle_id": "lc-dup", "outcome_id": "dup-1", "recorded_at": iso(closed_at),
         "status": "CLOSED", "attribution_status": "ATTRIBUTED", "cost_status": "COMPLETE",
         "actual_closed_at": iso(closed_at), "entry_price": 100.0, "exit_price": 101.0,
         "qty": 1.0, "pnl": 1.0, "gross_realized": 1.0, "entry_fee": 0.1, "exit_fee": 0.1,
         "funding_cashflow": 0.0},
        {"lifecycle_id": "lc-dup", "outcome_id": "dup-1", "recorded_at": iso(closed_at),
         "status": "CLOSED", "attribution_status": "ATTRIBUTED", "cost_status": "COMPLETE",
         "actual_closed_at": iso(closed_at), "entry_price": 100.0, "exit_price": 101.0,
         "qty": 1.0, "pnl": 9.99, "gross_realized": 9.99, "entry_fee": 0.1, "exit_fee": 0.1,
         "funding_cashflow": 0.0},
    ])
    check("重复 outcome_id 不同内容 → REVISION_CONFLICT（不静默挑一条）",
          duplicates["lc-dup"]["conflicted"] is True
          and "outcome_id=dup-1" in str(duplicates["lc-dup"]["conflict_reason"]),
          duplicates["lc-dup"])
    clean = evidence.select_lifecycle_revisions([
        {"lifecycle_id": "lc-ok", "outcome_id": "ok-1", "recorded_at": "2026-01-01T00:00:00.000+00:00",
         "status": "CLOSED", "attribution_status": "ATTRIBUTED", "cost_status": "COMPLETE",
         "actual_closed_at": "2026-01-01T00:00:00.000+00:00", "entry_price": 100.0,
         "exit_price": 101.0, "qty": 1.0, "pnl": 1.0, "gross_realized": 1.0,
         "entry_fee": 0.1, "exit_fee": 0.1, "funding_cashflow": 0.0},
        {"lifecycle_id": "lc-ok", "outcome_id": "ok-2", "recorded_at": "2026-01-02T00:00:00.000+00:00",
         "status": "CLOSED", "attribution_status": "ATTRIBUTED", "cost_status": "COMPLETE",
         "actual_closed_at": "2026-01-01T00:00:00.000+00:00", "entry_price": 100.0,
         "exit_price": 101.0, "qty": 1.0, "pnl": 1.0, "gross_realized": 1.0,
         "entry_fee": 0.1, "exit_fee": 0.1, "funding_cashflow": 0.0},
    ])
    check("正常迟到修订不定为冲突（时点可定序）",
          clean["lc-ok"]["conflicted"] is False and clean["lc-ok"]["revised"] is True
          and clean["lc-ok"]["revisions"] == 2, clean["lc-ok"])

    # 未知成本仍未知：cost_status 非 COMPLETE 不得进 30，也不把净收益当 0。
    add_lifecycle(fixture.db, 30, closed_at=closed_at, lifecycle_id="lc-30",
                  cost_status="UNKNOWN")
    unknown_cost = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    unknown_block = unknown_cost["release_evidence"]
    check("成本未完成 → 不计入 30（未知仍未知）",
          unknown_block["eligible_closed_lifecycles"] == 29
          and unknown_cost["gates"]["eligible_closed_trades"]["incomplete_closed"] >= 1,
          unknown_block["eligible_closed_lifecycles"])
    check("闭合总数如实披露（含不完整/冲突，不虚报覆盖）",
          unknown_block["current_closed_lifecycles"] == 31,
          unknown_block["current_closed_lifecycles"])
    fixture.close()


# --------------------------------------------------------------------------- #
# [5] F12：ACK ≠ 送达、ACK ≠ 恢复
# --------------------------------------------------------------------------- #
def scenario_ack_not_recovery(root: Path) -> None:
    print("\n[5] F12/K05：已 ACK 不得读成已送达/已恢复")
    fixture = Fixture(root / "ack")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start, count=31, session_id="session-1")
    thresholds = {"min_observation_days": 0.0, "min_eligible_closed_trades": 0}
    add_alert_delivery(fixture.db, incident_key="delivered-1", state="ACKNOWLEDGED",
                       state_rank=4, delivered_at_ms=1_700_000_100_000,
                       device_receipt="device-ok", ack_at_ms=1_700_000_100_500)
    add_alert_delivery(fixture.db, incident_key="ack-only-1", state="ACKNOWLEDGED",
                       state_rank=4, ack_at_ms=1_700_000_200_000,
                       provider_receipt="provider-202")
    add_alert_delivery(fixture.db, incident_key="accepted-only-1", state="ACCEPTED",
                       state_rank=2, provider_receipt="provider-303")
    add_incident(fixture.db, incident_key="incident-acked-open",
                 acknowledged_at="2026-01-01T00:10:00+00:00", resolved_at=None)
    report = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    recovery = report["event_recovery"]
    delivery = recovery["delivery"]
    print(f"    delivered={delivery['delivered_confirmed']} "
          f"ack_without_delivery={len(delivery['ack_without_delivery'])} "
          f"provider_accepted_only={delivery['provider_accepted_only']} "
          f"reason={list(recovery['reason_codes'])}")
    check("真实回执才算送达（只有 delivered_at_ms + device_receipt 的那条）",
          delivery["delivered_confirmed"] == 1, delivery)
    check("已 ACK 无设备回执 → 列入 ack_without_delivery，不冒充送达",
          [item["incident_key"] for item in delivery["ack_without_delivery"]] == ["ack-only-1"],
          delivery["ack_without_delivery"])
    check("供应商接受但不送达 → 计为未送达（不是 delivered）",
          delivery["provider_accepted_only"] == 1, delivery)
    check("ACK 口径显式声明（ack_is_not_delivery / ack_is_not_recovery）",
          recovery["ack_is_not_delivery"] is True and recovery["ack_is_not_recovery"] is True,
          recovery)
    check("告警证据判 FAIL 且原因码来自 U14（MANUAL_ACK_ONLY/NO_CONFIRMED_DELIVERY）",
          delivery["state"] == "FAIL" and recovery["state"] == "FAIL"
          and {"MANUAL_ACK_ONLY", "NO_CONFIRMED_DELIVERY"}.issubset(set(recovery["reason_codes"])),
          recovery["reason_codes"])
    check("已 ACK 未 resolved 的 incident 不计为恢复",
          report["event_recovery"]["incidents"]["acked_not_recovered_count"] == 1
          and "ACK_WITHOUT_RECOVERY" in report["event_recovery"]["reason_codes"],
          report["event_recovery"]["incidents"])
    check("critical_open_incidents 门禁同时披露 acknowledged_but_unresolved",
          report["gates"]["critical_open_incidents"]["acknowledged_but_unresolved"] == 1,
          report["gates"]["critical_open_incidents"])

    # 只有 ack_at_ms 被换成真实回执后，才允许读成送达。
    add_alert_delivery(fixture.db, incident_key="ack-only-1", state="ACKNOWLEDGED",
                       state_rank=4, ack_at_ms=1_700_000_200_000,
                       delivered_at_ms=1_700_000_200_500, device_receipt="device-ok-2",
                       provider_receipt="provider-202")
    fixed = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    check("补上真实回执后才算送达（2 条 confirmed，ack_only 清空）",
          fixed["event_recovery"]["delivery"]["delivered_confirmed"] == 2
          and not fixed["event_recovery"]["delivery"]["ack_without_delivery"],
          fixed["event_recovery"]["delivery"])
    fixture.close()


# --------------------------------------------------------------------------- #
# [6] F24：同工件重启 vs 不同 RC 换版
# --------------------------------------------------------------------------- #
def scenario_restart_versus_release_change(root: Path) -> None:
    print("\n[6] F24：同工件重启不得读成换版，不同配置混样必须暴露")
    fixture = Fixture(root / "continuity")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    thresholds = {"min_observation_days": 0.0, "min_eligible_closed_trades": 0}
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start, count=15, session_id="session-1")
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start + timedelta(minutes=15), count=16, session_id="session-2")
    restarted = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    continuity = restarted["run_continuity"]
    print(f"    verdict={continuity['verdict']} sessions={continuity.get('sessions')} "
          f"reason={list(continuity['reason_codes'])}")
    check("同工件的两次 session → RESTART_SAME_ARTIFACT（不是换版）",
          continuity["verdict"] == "RESTART_SAME_ARTIFACT"
          and continuity["restart_is_version_change"] is False
          and continuity["state"] == "PASS",
          continuity)
    check("重启次数如实披露", continuity["restart_count"] == 1, continuity)

    # 不同配置混样：同一发布里出现第二个 config_hash（不覆盖既有分钟）。
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start + timedelta(minutes=40), count=1, session_id="session-3",
                     config_hash="cfg-b")
    mixed = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    check("同窗口两种 config_hash → CONFIG_CHANGED_WITHIN_WINDOW + MIXED_CONFIG_SAMPLES",
          mixed["run_continuity"]["verdict"] == "CONFIG_CHANGED_WITHIN_WINDOW"
          and "MIXED_CONFIG_SAMPLES" in mixed["run_continuity"]["reason_codes"]
          and mixed["run_continuity"]["state"] == "FAIL",
          mixed["run_continuity"])
    check("换版使 runtime_observation 组不得 PASS",
          _group_status(mixed, "runtime_observation") != "PASS",
          _group_status(mixed, "runtime_observation"))

    # 不同 release_id 的观察混进同一窗口 → 外部样本被计数披露。
    add_observations(fixture.db, release_id="rel-u09-other",
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start + timedelta(minutes=5), count=1, session_id="session-9")
    foreign = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    check("窗口内其它发布的观察被披露为 MIXED_RELEASE_SAMPLES",
          foreign["run_continuity"]["foreign_release_rows"] >= 1
          and "MIXED_RELEASE_SAMPLES" in foreign["run_continuity"]["reason_codes"],
          foreign["run_continuity"])
    fixture.close()


def _group_status(report: dict[str, Any], group: str) -> str | None:
    for item in (report.get("k06") or {}).get("groups") or []:
        if item.get("group") == group:
            return item.get("status")
    return None


# --------------------------------------------------------------------------- #
# [7] K06 五组：NO_DATA != PASS、声明 PASS 必须有证据
# --------------------------------------------------------------------------- #
def scenario_k06_groups(root: Path) -> None:
    print("\n[7] K06：五组独立、NO_DATA != PASS、声明 PASS 必须自证")
    fixture = Fixture(root / "k06")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start, count=31, session_id="session-1")
    thresholds = {"min_observation_days": 0.0, "min_eligible_closed_trades": 0}
    report = build_report(fixture, since=iso(start), until=iso(end), thresholds=thresholds)
    groups = [item["group"] for item in (report["k06"] or {}).get("groups") or []]
    check("K06 固定五组齐全",
          groups == ["engineering", "accounting", "runtime_observation", "operations",
                     "strategy_evaluation"], groups)
    check("报告不新增整体 GO 字段（release_readiness 仍在且为既有语义）",
          report["release_readiness"] in {"GO", "NO_GO"}
          and set(report["gates"]) == {
              "schema_version", "directional_candidates", "eligible_closed_trades",
              "observation_coverage", "observation_days", "network_rate_limit",
              "critical_open_incidents", "unknown_orders", "submitting_orders",
              "entry_readiness"},
          sorted(report["gates"]))
    check("K06 报告 hash 可复算且稳定",
          bool(report["k06"]["report_hash"])
          and report["k06"]["report_hash"]
          == build_report(fixture, since=iso(start), until=iso(end),
                          thresholds=thresholds)["k06"]["report_hash"],
          report["k06"]["report_hash"])
    accounting = _group_status(report, "accounting")
    check("无任何闭合交易时 accounting=NO_DATA（不是 PASS）", accounting == "NO_DATA", accounting)
    check("NO_DATA 组必须带阻断项",
          any(item["group"] == "accounting" for item in report["blocking_items"]),
          report["blocking_items"])

    # 声明 PASS 但窗口缺失 → FAIL + PASS_WITHOUT_EVIDENCE/WINDOW_MISSING。
    claimed = evidence.build_release_readiness_report(
        fixture.path,
        {"release_id": fixture.release_id, "environment": fixture.environment,
         "account_ref": fixture.account_ref,
         "schema_version": fixture.db.current_schema_version()},
        environment=fixture.environment,
        declaration={"strategy_evaluation": "PASS", "engineering": "PASS"},
        thresholds={"min_observation_days": 0.0, "min_eligible_closed_trades": 0})
    strategy = _group_status(claimed, "strategy_evaluation")
    codes = [item["code"] for item in claimed["blocking_items"]
             if item["group"] == "strategy_evaluation"]
    check("声明 PASS 而窗口缺失 → FAIL + PASS_WITHOUT_EVIDENCE/WINDOW_MISSING",
          strategy == "FAIL" and "PASS_WITHOUT_EVIDENCE" in codes
          and "WINDOW_MISSING" in codes, codes)
    engineering = [item["code"] for item in claimed["blocking_items"]
                   if item["group"] == "engineering"]
    check("身份引用可解析时 engineering 声明 PASS 但窗口缺失仍 FAIL",
          _group_status(claimed, "engineering") == "FAIL"
          and "WINDOW_MISSING" in engineering, engineering)
    missing_identity = evidence.build_release_readiness_report(
        fixture.path,
        {"release_id": fixture.release_id, "environment": fixture.environment,
         "account_ref": fixture.account_ref,
         "schema_version": fixture.db.current_schema_version()},
        environment=fixture.environment, since=iso(start), until=iso(end),
        declaration={"accounting": "PASS"},
        thresholds={"min_observation_days": 0.0, "min_eligible_closed_trades": 0})
    check("缺运行工件身份 → IDENTITY_REF_MISSING 阻断（不得凭声明通过）",
          "IDENTITY_REF_MISSING" in [
              item["code"] for item in missing_identity["blocking_items"]
              if item["group"] == "accounting"]
          or _group_status(missing_identity, "accounting") == "FAIL",
          missing_identity["blocking_items"])
    fixture.close()


# --------------------------------------------------------------------------- #
# [8] 只读性
# --------------------------------------------------------------------------- #
def scenario_read_only(root: Path) -> None:
    print("\n[8] 只读性：报告不修改数据库文件")
    fixture = Fixture(root / "readonly")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    add_observations(fixture.db, release_id=fixture.release_id,
                     environment=fixture.environment, account_ref=fixture.account_ref,
                     start=start, count=31, session_id="session-1")
    before = fixture.path.read_bytes()
    build_report(fixture, since=iso(start), until=iso(end),
                 thresholds={"min_observation_days": 0.0, "min_eligible_closed_trades": 0})
    after = fixture.path.read_bytes()
    check("报告前后数据库文件字节一致", before == after)
    fixture.close()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="u09-smoke-"))
    print(f"U09 冒烟工作目录: {root}")
    try:
        scenario_shadow_source_leak(root)
        if PATCHED:
            scenario_observation_window(root)
            scenario_window_invalid(root)
            scenario_lifecycle_revision(root)
            scenario_ack_not_recovery(root)
            scenario_restart_versus_release_change(root)
            scenario_k06_groups(root)
        else:
            print("\n[2-8] U09 补丁未应用：新口径断言按设计跳过，"
                  "仅保留基线缺陷证据（上面的 200→300）。")
        scenario_read_only(root)
    finally:
        if not FAILURES:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"\n（保留失败现场以便排查：{root}）")
    print(f"\nU09 冒烟: {PASSED} 通过 / {len(FAILURES)} 失败  (PATCHED={PATCHED}, SHARED={SHARED})")
    if FAILURES:
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
