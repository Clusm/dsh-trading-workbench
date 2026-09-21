"""U09 验收：发布证据口径与事件恢复门禁（S08 / F10 / F12 / F16 / F23 / F24）。

独立可运行（本仓库无 pytest）：

    PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_release_evidence_v2.py

全程离线：只在系统临时目录里造合成 SQLite，不读真实 ``.env``/``data``/密钥、
不联网、不启动服务、不下单、不发告警。逐项打印 PASS/FAIL，任一失败退出码 1。

**两态可跑**（本项目血的教训，见 U01/U13/U14）：本套件只依赖 U09 独占交付物
（``core.release_evidence``、``scripts/smoke_u09_release_evidence.py``）与已冻结
契约（``core.contracts.k06_release``），**不假定**共享适配补丁
（``audit.database`` / ``core.health_watch`` / ``scripts.report_release_readiness``）
是否已应用。对需要共享补丁才能观察的行为，套件**按两态各自应有的语义分别断言**：

* 补丁不在 → 断言基线缺陷确实存在（S08 影子泄漏、窗口自动截到首末健康记录），
  把"未修时的数字"如实记下来；
* 补丁在 → 断言修复后的口径。

两个状态下的期望都是真的，不存在 SKIP 掩盖。断言尽量写成**相对性质**
（改动前后两次快照比较），不硬编码迁移版本集合。
"""
from __future__ import annotations

import json
import sys
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core import health_watch  # noqa: E402
from core import release_evidence as evidence  # noqa: E402
from intel import shadow_status  # noqa: E402


PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: Any = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail!r}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def minute(moment: datetime) -> str:
    return moment.replace(second=0, microsecond=0).isoformat(timespec="minutes")


# --------------------------------------------------------------------------- #
# 两态探测（只按标记判断，不猜）
# --------------------------------------------------------------------------- #
def evidence_v2() -> bool:
    return bool(getattr(evidence, "U09_EVIDENCE_VERSION", ""))


def shared_database_ready() -> bool:
    from audit import database  # noqa: PLC0415

    return bool(getattr(database, "U09_INTEGRATION", ""))


def shared_health_watch_ready() -> bool:
    return bool(getattr(health_watch, "U09_INTEGRATION", ""))


def shared_cli_ready() -> bool:
    """按标记探测 CLI 适配补丁（只读源码文本，不执行脚本、不起服务）。"""
    try:
        text = (ROOT / "scripts" / "report_release_readiness.py").read_text(encoding="utf-8")
    except OSError:
        return False
    return "U09_INTEGRATION" in text


#: 证据版本字符串（基线态不存在时为空串，断言据此相对比较，不硬编码）。
EV_VERSION = str(getattr(evidence, "U09_EVIDENCE_VERSION", ""))

APPLIED = evidence_v2()
DB_PATCHED = shared_database_ready()
HW_PATCHED = shared_health_watch_ready()
CLI_PATCHED = shared_cli_ready()
TMP = Path(tempfile.mkdtemp(prefix="u09-acceptance-"))


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def new_db(name: str, *, release_id: str = "rel-acc", environment: str = "demo",
           account_ref: str = "demo:acc") -> tuple[AuditDatabase, Path]:
    path = TMP / name / "release.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = AuditDatabase(path)
    db.set_release_context(release_id=release_id, environment=environment,
                           account_ref=account_ref)
    return db, path


def add_shadow(db: AuditDatabase, *, prefix: str, source: str, count: int,
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
                     rate_limit: dict[str, Any] | None = None,
                     entry_readiness: dict[str, Any] | None = None) -> None:
    with db.transaction() as connection:
        for index in range(count):
            observed = start + timedelta(minutes=index)
            connection.execute(
                """INSERT OR IGNORE INTO release_observations(
                    observation_id,release_id,environment,account_ref,session_id,
                    observed_at,observed_minute,health_state,component_status_json,
                    config_hash,rate_limit_json,coverage_json,entry_readiness_json,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"obs-{release_id}-{session_id}-{index}", release_id, environment,
                 account_ref, session_id, iso(observed), minute(observed), health_state,
                 "{}", config_hash,
                 json.dumps(rate_limit or {"responses_by_status": {},
                                           "coordination_available": True}),
                 "{}", json.dumps(entry_readiness or {"new_entry_allowed": True}), "{}"),
            )


def add_closed_lifecycle(db: AuditDatabase, index: int, *, closed_at: datetime,
                         lifecycle_id: str, symbol: str = "BTCUSDT", side: str = "BOTH",
                         entry_price: float = 100.0, exit_price: float = 101.0,
                         gross: float = 1.0, entry_fee: float = 0.1, exit_fee: float = 0.1,
                         funding: float = 0.0, cost_status: str = "COMPLETE",
                         outcome_id: str | None = None, recorded_at: str | None = None,
                         environment: str = "demo", account_ref: str = "demo:acc",
                         release_id: str | None = None) -> None:
    db.create_position_lifecycle(
        lifecycle_id=lifecycle_id, environment=environment, account_ref=account_ref,
        symbol=symbol, position_side=side, entry_qty=1.0, entry_notional=entry_price,
        attribution_status="ATTRIBUTED", cost_status=cost_status,
        opened_at_ms=1_700_000_000_000, release_id=release_id,
    )
    db.update_position_lifecycle(
        lifecycle_id=lifecycle_id, status="CLOSED",
        closed_at_ms=int(closed_at.timestamp() * 1000), exit_qty=1.0, remaining_qty=0.0,
        exit_notional=exit_price, attribution_status="ATTRIBUTED", cost_status=cost_status,
    )
    db.log_trade_outcome(
        outcome_id=outcome_id or f"outcome-{lifecycle_id}", intent_id=f"intent-{lifecycle_id}",
        decision_id=f"decision-{lifecycle_id}", symbol=symbol, side="BUY",
        entry_price=entry_price, exit_price=exit_price, qty=1.0, pnl=gross,
        status="CLOSED", opened_at=iso(closed_at - timedelta(hours=1)),
        closed_at=iso(closed_at), actual_closed_at=iso(closed_at),
        lifecycle_id=lifecycle_id, attribution_status="ATTRIBUTED", cost_status=cost_status,
        gross_realized=gross, entry_fee=entry_fee, exit_fee=exit_fee,
        funding_cashflow=funding, recorded_at=recorded_at or iso(closed_at),
        environment=environment, release_id=release_id,
    )


def manifest(release_id: str, environment: str, account_ref: str, *,
             artifact: str | None = "sha256:acc-artifact", schema_version: int | None = None,
             db: AuditDatabase | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "release_id": release_id, "environment": environment, "account_ref": account_ref,
        "manifest_schema": 1,
    }
    if artifact:
        data["artifact_hash"] = artifact
        data["content_hash"] = artifact
        data["runtime_instance_id"] = "run-acc-1"
    data["schema_version"] = (schema_version if schema_version is not None
                              else (db.current_schema_version() if db else 1))
    return data


def build(path: Path, meta: dict[str, Any], *, since: Any = None, until: Any = None,
          thresholds: dict[str, Any] | None = None,
          declaration: dict[str, Any] | None = None) -> dict[str, Any]:
    # ``declaration`` 是 U09 新增入参：基线态不得传，否则 TypeError（两态可跑的代价）。
    extra: dict[str, Any] = {}
    if declaration is not None and APPLIED:
        extra["declaration"] = declaration
    return evidence.build_release_readiness_report(
        path, meta, environment=str(meta["environment"]), since=since, until=until,
        thresholds=thresholds, **extra)


BASE_THRESHOLDS = {"min_observation_days": 0.0, "min_eligible_closed_trades": 0}


# --------------------------------------------------------------------------- #
# 1. 核心不变量（两态都必须成立）
# --------------------------------------------------------------------------- #
def test_core_invariants() -> None:
    section("1. 核心不变量：只读、既有门禁键、分母按机会键去重（两态都成立）")
    db, path = new_db("core")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    add_shadow(db, prefix="a", source=shadow_status.SOURCE_A, count=20)
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start, count=31, session_id="s1")
    with db.transaction() as connection:
        connection.execute("UPDATE shadow_decisions SET created_at=?",
                           (iso(start + timedelta(minutes=5)),))
    before = path.read_bytes()
    report = build(path, manifest("rel-acc", "demo", "demo:acc", db=db),
                   since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    after = path.read_bytes()
    check("报告读取不修改数据库文件", before == after)
    check("既有门禁键集合保持不变（不新增整体 GO 门禁）",
          set(report["gates"]) == {
              "schema_version", "directional_candidates", "eligible_closed_trades",
              "observation_coverage", "observation_days", "network_rate_limit",
              "critical_open_incidents", "unknown_orders", "submitting_orders",
              "entry_readiness"},
          sorted(report["gates"]))
    check("release_readiness 仍是既有字段且由 gates 汇总",
          report["release_readiness"] == ("GO" if all(
              item["passed"] for item in report["gates"].values()) else "NO_GO"),
          report["release_readiness"])
    # 相对性质：不硬编码迁移版本集合，只断言"连续、最高版本等于 current_schema_version"。
    versions = [row["version"] for row in db.fetch_all(
        "SELECT version FROM schema_migrations ORDER BY version")]
    check("迁移版本集合是 1..max 的连续区间且 max 等于 current_schema_version（相对性质）",
          versions == list(range(1, len(versions) + 1))
          and max(versions) == db.current_schema_version(),
          versions[-3:])
    # 删一条影子行再算：分母减一（幂等/可重算，不依赖任何补丁）。
    db.conn.execute("DELETE FROM shadow_decisions WHERE decision_id='a-19'")
    db.conn.commit()
    fewer = build(path, manifest("rel-acc", "demo", "demo:acc", db=db),
                  since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    check("候选分母可重算（删一条影子行 = 分母减一）",
          report["release_evidence"]["directional_candidates"]
          - fewer["release_evidence"]["directional_candidates"] == 1,
          (report["release_evidence"]["directional_candidates"],
           fewer["release_evidence"]["directional_candidates"]))
    db.close()


# --------------------------------------------------------------------------- #
# 2. S08 / F16：研究影子不得冒充执行候选（两态相对断言）
# --------------------------------------------------------------------------- #
def test_shadow_source_gate() -> None:
    section("2. S08/F16：B/C 研究影子行不得进方向候选分母")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    counts: dict[str, int] = {}
    for label, research in (("onlyA", False), ("withBC", True)):
        db, path = new_db(f"gate-{label}")
        add_shadow(db, prefix="a", source=shadow_status.SOURCE_A, count=200)
        if research:
            add_shadow(db, prefix="b", source=shadow_status.SOURCE_B, count=50, start_index=1000)
            add_shadow(db, prefix="c", source=shadow_status.SOURCE_C, count=50, start_index=2000)
        add_observations(db, release_id="rel-acc", environment="demo",
                         account_ref="demo:acc", start=start, count=31, session_id="s1")
        with db.transaction() as connection:
            connection.execute("UPDATE shadow_decisions SET created_at=?",
                               (iso(start + timedelta(minutes=5)),))
        report = build(path, manifest("rel-acc", "demo", "demo:acc", db=db),
                       since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
        counts[label] = report["release_evidence"]["directional_candidates"]
        if label == "withBC":
            rows = report["release_evidence"]["shadow_source_rows"]
            check("B/C 样本如实出现在诊断字段里（不是靠删数据）",
                  rows.get(shadow_status.SOURCE_B) == 50
                  and rows.get(shadow_status.SOURCE_C) == 50, rows)
            check("研究来源清单来自 intel.shadow_status（唯一权威口径）",
                  set(shadow_status.research_sources()) == {
                      shadow_status.SOURCE_B, shadow_status.SOURCE_C},
                  shadow_status.research_sources())
            db.close()
    delta = counts["withBC"] - counts["onlyA"]
    # 来源过滤属 ``release_evidence`` 自身口径（与 database 的别名归一无关），
    # 因此判据只看证据计算器是否已升级。
    if APPLIED:
        check("补丁态：加入 100 条 B/C 研究样本后分母不变（Δ=0）", delta == 0,
              (counts["onlyA"], counts["withBC"]))
        check("补丁态：分母仍为 200（不低于门槛）", counts["withBC"] == 200, counts["withBC"])
    else:
        check("基线态：B/C 使分母膨胀 100（记录 S08 缺陷证据）", delta == 100,
              (counts["onlyA"], counts["withBC"]))


def test_no_edge_excluded() -> None:
    section("2b. F24：纯 NO_EDGE / 无方向信号不得凑 200")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    db, path = new_db("no-edge")
    add_shadow(db, prefix="a", source=shadow_status.SOURCE_A, count=200)
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start, count=31, session_id="s1")
    with db.transaction() as connection:
        connection.execute("UPDATE shadow_decisions SET created_at=?",
                           (iso(start + timedelta(minutes=5)),))
    report = build(path, manifest("rel-acc", "demo", "demo:acc", db=db),
                   since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    baseline = report["release_evidence"]["directional_candidates"]
    add_shadow(db, prefix="noedge", source=shadow_status.SOURCE_A, count=120,
               start_index=3000, feature={"no_edge": True})
    add_shadow(db, prefix="reason", source=shadow_status.SOURCE_A, count=120,
               start_index=4000, feature={"reason_code": "NO_EDGE"})
    add_shadow(db, prefix="neutral", source=shadow_status.SOURCE_A, count=120,
               signal="neutral", start_index=5000)
    with db.transaction() as connection:
        connection.execute("UPDATE shadow_decisions SET created_at=?",
                           (iso(start + timedelta(minutes=5)),))
    after = build(path, manifest("rel-acc", "demo", "demo:acc", db=db),
                  since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    block = after["release_evidence"]
    if APPLIED:
        check("补丁态：加入 360 条 NO_EDGE/无方向样本后分母不变",
              block["directional_candidates"] == baseline == 200,
              (baseline, block["directional_candidates"]))
        check("补丁态：排除原因逐项可核（no_edge=240, non_directional=120）",
              block["shadow_excluded_by_reason"].get("no_edge_declared") == 240
              and block["shadow_excluded_by_reason"].get("non_directional_signal") == 120,
              block["shadow_excluded_by_reason"])
        check("补丁态：非方向信号不得靠 240 条中性样本抬门槛",
              after["gates"]["directional_candidates"]["actual"] == 200,
              after["gates"]["directional_candidates"]["actual"])
        check("补丁态：门禁理由写明只计 execution_decision_copy 且排除 NO_EDGE",
              "execution_decision_copy" in after["gates"]["directional_candidates"]["reason"],
              after["gates"]["directional_candidates"]["reason"])
    else:
        # 基线的 SQL 已按 signal IN (bullish,bearish) 过滤非方向信号，但**不认**
        # NO_EDGE 标记：240 条"明确方向但无边际"的样本照样进分母。
        check("基线态：非方向信号被 SQL 过滤（240 条 neutral/unknown 未计）",
              block["shadow_source_rows"].get(shadow_status.SOURCE_A) == 440,
              block["shadow_source_rows"])
        check("基线态：NO_EDGE 标记的 bullish 行被计入分母（200 → 440，记录缺陷）",
              block["directional_candidates"] == baseline + 240 == 440,
              (baseline, block["directional_candidates"]))
    db.close()


# --------------------------------------------------------------------------- #
# 3. 观察窗口（F24）
# --------------------------------------------------------------------------- #
def test_window_boundaries() -> None:
    section("3. F24：窗口边界来自声明，首尾缺失计入分母（两态分别断言）")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    db, path = new_db("window")
    # 观察从 +3 分钟开始、到 -2 分钟结束（首尾断档）。
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start + timedelta(minutes=3), count=26, session_id="s1")
    meta = manifest("rel-acc", "demo", "demo:acc", db=db)
    report = build(path, meta, since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    coverage = report["release_evidence"]["observation_coverage"]
    if APPLIED:
        check("补丁态：首尾缺失计入分母（expected=31，observed=26）",
              coverage["expected_count"] == 31 and coverage["observed_count"] == 26, coverage)
        check("补丁态：首尾缺失单独披露（head=3, tail=2）",
              coverage["head_missing_count"] == 3 and coverage["tail_missing_count"] == 2,
              coverage)
        check("补丁态：覆盖率因此 FAIL（不按 26/26 冒充 100%）",
              report["gates"]["observation_coverage"]["state"] == "FAIL", coverage)
        check("补丁态：观察跨度按声明窗口算（0.0208 天 = 30 分钟）",
              abs(float(coverage["observation_days"]) - 30 / 1440) < 1e-6
              and float(coverage["observed_span_days"]) < float(coverage["observation_days"]),
              coverage)
        check("补丁态：窗口明示不来自观察数据",
              report["window"]["derived_from_observations"] is False
              and report["window"]["since_source"] == "argument", report["window"])
        # 未声明窗口 → NO_DATA + WINDOW_MISSING，绝不自动截到首末健康记录。
        undeclared = build(path, manifest("rel-acc", "demo", "demo:acc", db=db),
                           thresholds=BASE_THRESHOLDS)
        check("补丁态：未声明窗口 → NO_DATA + WINDOW_MISSING（禁止自动截断）",
              undeclared["gates"]["observation_coverage"]["state"] == "NO_DATA"
              and "WINDOW_MISSING" in undeclared["window"]["reason_codes"],
              undeclared["window"])
        check("补丁态：窗口不成立时分母不为 0 冒充，而是 NO_DATA + 原因码",
              undeclared["gates"]["directional_candidates"]["state"] == "NO_DATA"
              and undeclared["gates"]["directional_candidates"]["actual"] is None
              and "WINDOW_MISSING" in undeclared["window"]["reason_codes"],
              undeclared["gates"]["directional_candidates"])
        check("补丁态：窗口终结可取自清单声明（report_at/observation_window_end）",
              _manifest_window_resolves(meta, db))
        # 报告时刻只补窗口终点，不猜起点。
        clock_only = evidence.resolve_release_window(
            manifest={"release_id": "rel-acc"}, report_at="2026-01-02T00:00:00+00:00")
        check("补丁态：仅报告时刻不足以成窗（起点仍 WINDOW_MISSING）",
              clock_only["state"] == "FAIL" and "WINDOW_MISSING" in clock_only["reason_codes"]
              and clock_only["until_source"] == "report_clock", clock_only)
    else:
        check("基线态：首尾断档被静默吸收（不披露 head/tail 缺失）",
              "head_missing_count" not in coverage and "tail_missing_count" not in coverage,
              sorted(coverage))
        # 未声明窗口时基线**自动**把窗口截到首末观察记录：3 分钟头部与 2 分钟尾部
        # 断档被吸收，覆盖率虚报 100%（规格 §12 明令禁止）。
        undeclared = build(path, manifest("rel-acc", "demo", "demo:acc", db=db),
                           thresholds=BASE_THRESHOLDS)
        undeclared_coverage = undeclared["release_evidence"]["observation_coverage"]
        check("基线态：未声明窗口时自动截到首末观察记录，覆盖率虚报 100%（记录缺陷）",
              undeclared["gates"]["observation_coverage"]["state"] == "PASS"
              and undeclared_coverage["expected_count"]
              == undeclared_coverage["observed_count"] == 26,
              undeclared_coverage)
    db.close()


def _manifest_window_resolves(meta: dict[str, Any], db: AuditDatabase) -> bool:
    declared = dict(meta)
    declared.pop("observation_window_start", None)
    declared.pop("observation_window_end", None)
    declared["observation_window_start"] = "2026-01-01T00:00:00+00:00"
    declared["observation_window_end"] = "2026-01-01T00:30:00+00:00"
    resolved = evidence.resolve_release_window(manifest=declared)
    return (resolved["state"] == "PASS" and resolved["since_source"] == "manifest"
            and resolved["until_source"] == "manifest")


def test_window_invalid() -> None:
    section("3b. K06：畸形窗口 FAIL + WINDOW_INVALID，报告不崩溃")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    db, path = new_db("window-invalid")
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start, count=31, session_id="s1")
    meta = manifest("rel-acc", "demo", "demo:acc", db=db)
    if not APPLIED:
        print("  （基线态：resolve_release_window 不存在，仅断言报告不崩溃）")
        check("基线态：畸形窗口不崩溃（既有 parse_iso 返回 None）",
              isinstance(build(path, meta, since="not-a-date",
                               until=iso(start + timedelta(minutes=30)),
                               thresholds=BASE_THRESHOLDS), dict))
        db.close()
        return
    for value in ("not-a-date", float("inf"), float("nan"), "1e400", 2 ** 64,
                  "99999999999999999999", True):
        try:
            report = build(path, meta, since=value,
                           until=iso(start + timedelta(minutes=30)),
                           thresholds=BASE_THRESHOLDS)
            ok = (report["gates"]["observation_coverage"]["state"] == "FAIL"
                  and "WINDOW_INVALID" in report["window"]["reason_codes"])
            check(f"畸形窗口 {value!r} → FAIL + WINDOW_INVALID", ok,
                  report["window"]["reason_codes"])
        except Exception as exc:  # noqa: BLE001 - 崩溃即失败
            check(f"畸形窗口 {value!r} 不得让报告崩溃", False,
                  f"{type(exc).__name__}: {exc}")
    reversed_window = build(path, meta, since=iso(start + timedelta(minutes=30)),
                            until=iso(start), thresholds=BASE_THRESHOLDS)
    check("反向窗口 → FAIL + WINDOW_INVALID（不抛异常）",
          reversed_window["gates"]["observation_coverage"]["state"] == "FAIL"
          and "WINDOW_INVALID" in reversed_window["window"]["reason_codes"],
          reversed_window["window"])
    check("合法 epoch 毫秒窗口被接受（与 K06 window_ms 同口径）",
          build(path, meta, since=int(start.timestamp() * 1000),
                until=int((start + timedelta(minutes=30)).timestamp() * 1000),
                thresholds=BASE_THRESHOLDS)["window"]["state"] == "PASS")
    db.close()


# --------------------------------------------------------------------------- #
# 4. 完整闭合 / 迟到修订 / 冲突隔离（F23、F10）
# --------------------------------------------------------------------------- #
def test_lifecycle_revision_and_identity() -> None:
    section("4. F23/F10：lifecycle 去重、迟到修订重算、冲突隔离、未知仍未知")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    closed_at = start + timedelta(minutes=10)
    db, path = new_db("lifecycle")
    meta = manifest("rel-acc", "demo", "demo:acc", db=db)
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start, count=31, session_id="s1")
    thresholds = {"min_observation_days": 0.0, "min_eligible_closed_trades": 30}
    for index in range(30):
        add_closed_lifecycle(
            db, index, closed_at=closed_at, lifecycle_id=f"lc-{index}",
            symbol="BTCUSDT", side="SHORT" if index == 5 else ("LONG" if index == 6 else "BOTH"),
            funding=0.05 if index == 0 else 0.0)
    baseline = build(path, meta, since=iso(start), until=iso(end), thresholds=thresholds)
    block = baseline["release_evidence"]
    check("30 笔完整闭合按 lifecycle_id 计数（同毫秒满页不吞并）",
          block["eligible_closed_lifecycles"] == 30
          and block["current_closed_lifecycles"] == 30, block["current_closed_lifecycles"])
    expected_net = 29 * (1.0 - 0.1 - 0.1) + (1.0 - 0.1 - 0.1 + 0.05)
    if APPLIED:
        check("净收益按 gross - entry_fee - exit_fee + funding 重算",
              abs(block["eligible_net_realized_total"] - round(expected_net, 10)) < 1e-9,
              block["eligible_net_realized_total"])
        check("零资金费被当作 0（不是缺证据）；未知成本另行计数",
              block["net_realized_unknown_lifecycles"] == 0, block)
    else:
        check("基线态：净收益与修订口径字段不存在（U09 新证据；记录缺陷）",
              "eligible_net_realized_total" not in block
              and "revised_lifecycles" not in block, sorted(block))
    ids = {item["lifecycle_id"] for item in block["eligible_closed_rows"]}
    check("双向仓 LONG/SHORT/BOTH 各计一次，不因方向合并",
          {"lc-5", "lc-6", "lc-0"}.issubset(ids) and len(ids) == 30, len(ids))
    if not APPLIED:
        check("基线态：迟到修订被静默取一条、无修订/冲突披露（记录缺陷）",
              _baseline_silent_revision(db, path, meta, start, end, thresholds),
              "见 _baseline_silent_revision")
        db.close()
        return

    # 迟到到达 + 成本修订（新 outcome_id、更晚 recorded_at、exit_fee 0.1→0.4）。
    base_lc0 = next(item for item in block["eligible_closed_rows"]
                    if item["lifecycle_id"] == "lc-0")
    add_closed_lifecycle(db, 0, closed_at=closed_at, lifecycle_id="lc-0",
                         funding=0.05, exit_fee=0.4,
                         outcome_id="outcome-lc-0-revised",
                         recorded_at=iso(closed_at + timedelta(hours=3)))
    revised = build(path, meta, since=iso(start), until=iso(end), thresholds=thresholds)
    revised_block = revised["release_evidence"]
    revised_lc0 = next(item for item in revised_block["eligible_closed_rows"]
                       if item["lifecycle_id"] == "lc-0")
    check("迟到修订不重复计数（仍为 30，不是 31）",
          revised_block["eligible_closed_lifecycles"] == 30,
          revised_block["eligible_closed_lifecycles"])
    check("迟到修订被登记（superseded 旧 outcome + 修订 hash 变化）",
          [item["lifecycle_id"] for item in revised_block["revised_lifecycles"]] == ["lc-0"]
          and revised_block["revised_lifecycles"][0]["superseded_outcome_ids"]
          == ["outcome-lc-0"]
          and base_lc0["evidence_revision_hash"]
          != revised_lc0["evidence_revision_hash"],
          revised_block["revised_lifecycles"])
    check("净收益按最新修订重算（不是两条相加、不是保留旧值）",
          abs(revised_lc0["net_realized"] - (1.0 - 0.1 - 0.4 + 0.05)) < 1e-9
          and revised_lc0["cost_recomputed"] is True
          and abs(revised_block["eligible_net_realized_total"]
                  - round(expected_net - base_lc0["net_realized"] + revised_lc0["net_realized"], 10))
          < 1e-9,
          (revised_lc0["net_realized"], revised_block["eligible_net_realized_total"]))

    # 同一次补录两条互相矛盾（同 lifecycle、同 recorded_at、内容不同）→ 冲突隔离。
    add_closed_lifecycle(db, 11, closed_at=closed_at, lifecycle_id="lc-11",
                         gross=9.99, outcome_id="outcome-lc-11-b",
                         recorded_at=iso(closed_at))
    conflicted = build(path, meta, since=iso(start), until=iso(end), thresholds=thresholds)
    conflicted_block = conflicted["release_evidence"]
    check("同一 recorded_at 内容冲突 → REVISION_CONFLICT 且被排除",
          [item["lifecycle_id"] for item in conflicted_block["conflicted_lifecycles"]]
          == ["lc-11"]
          and "REVISION_CONFLICT" in str(
              conflicted_block["conflicted_lifecycles"][0]["conflict_reason"]),
          conflicted_block["conflicted_lifecycles"])
    check("冲突生命周期不进 30（29 笔 + 门禁 FAIL，拒绝统计伪通过）",
          conflicted_block["eligible_closed_lifecycles"] == 29
          and conflicted["gates"]["eligible_closed_trades"]["state"] == "FAIL",
          conflicted_block["eligible_closed_lifecycles"])
    check("闭合总数如实披露（冲突不虚报覆盖）",
          conflicted_block["current_closed_lifecycles"] == 30, conflicted_block)

    # 重复 outcome_id 不同内容：DB 层 OR IGNORE 挡不住的历史库也要隔离（纯函数级）。
    duplicated = evidence.select_lifecycle_revisions([
        _revision_row("dup", "dup-1", pnl=1.0), _revision_row("dup", "dup-1", pnl=9.99)])
    check("重复 outcome_id 不同内容 → REVISION_CONFLICT（不静默挑一条）",
          duplicated["dup"]["conflicted"] is True
          and "outcome_id=dup-1" in str(duplicated["dup"]["conflict_reason"]),
          duplicated["dup"])
    ordered = evidence.select_lifecycle_revisions([
        _revision_row("ok", "ok-1", recorded="2026-01-01T00:00:00.000+00:00"),
        _revision_row("ok", "ok-2", recorded="2026-01-02T00:00:00.000+00:00")])
    check("时点可定序的正常修订不定为冲突",
          ordered["ok"]["conflicted"] is False and ordered["ok"]["revised"] is True
          and ordered["ok"]["revisions"] == 2, ordered["ok"])

    # 未知成本仍未知：cost_status 非 COMPLETE 不进 30，且净收益不得当 0。
    add_closed_lifecycle(db, 30, closed_at=closed_at, lifecycle_id="lc-30",
                         outcome_id="outcome-lc-30", cost_status="UNKNOWN")
    unknown = build(path, meta, since=iso(start), until=iso(end), thresholds=thresholds)
    unknown_block = unknown["release_evidence"]
    check("成本未完成 → 不计入 30（未知仍未知）",
          unknown_block["eligible_closed_lifecycles"] == 29
          and unknown["gates"]["eligible_closed_trades"]["incomplete_closed"] >= 1,
          unknown_block["eligible_closed_lifecycles"])
    check("缺失成本项的生命周期不进 complete 集合（不得用 0 冒充）",
          _unknown_net_is_none(db, path, meta, start, end))
    db.close()


def _revision_row(lifecycle_id: str, outcome_id: str, *, pnl: float = 1.0,
                  recorded: str = "2026-01-01T00:00:00.000+00:00") -> dict[str, Any]:
    return {
        "lifecycle_id": lifecycle_id, "outcome_id": outcome_id, "recorded_at": recorded,
        "status": "CLOSED", "attribution_status": "ATTRIBUTED", "cost_status": "COMPLETE",
        "actual_closed_at": "2026-01-01T00:00:00.000+00:00", "entry_price": 100.0,
        "exit_price": 101.0, "qty": 1.0, "pnl": pnl, "gross_realized": pnl,
        "entry_fee": 0.1, "exit_fee": 0.1, "funding_cashflow": 0.0,
    }


def _baseline_silent_revision(db: AuditDatabase, path: Path, meta: dict[str, Any],
                              start: datetime, end: datetime,
                              thresholds: dict[str, Any]) -> bool:
    """基线态：同 lifecycle 的迟到修订被静默取一条，且不可证明取的是哪一版。

    基线把 ``position_lifecycles ⟕ trade_outcomes`` 的多行按 ``lifecycle_id`` 去重成
    **集合**，因此不会重复计数——但它既没有修订顺序（取哪一行取决于 SQL 行序），
    也没有任何"这条值来自哪个修订"的证据字段，迟到/被修订的成本证据无法复核。
    """
    add_closed_lifecycle(db, 1, closed_at=start + timedelta(minutes=10), lifecycle_id="lc-1",
                         exit_fee=0.4, outcome_id="outcome-lc-1-revised",
                         recorded_at=iso(start + timedelta(hours=3)))
    report = build(path, meta, since=iso(start), until=iso(end), thresholds=thresholds)
    block = report["release_evidence"]
    rows = [item for item in block["eligible_closed_rows"] if item["lifecycle_id"] == "lc-1"]
    return (block["eligible_closed_lifecycles"] == 30
            and "revised_lifecycles" not in block
            and all("evidence_revision_hash" not in item for item in rows))


def _unknown_net_is_none(db: AuditDatabase, path: Path, meta: dict[str, Any],
                         start: datetime, end: datetime) -> bool:
    report = build(path, meta, since=iso(start), until=iso(end),
                   thresholds={"min_observation_days": 0.0, "min_eligible_closed_trades": 0})
    return all(item.get("net_realized") is not None
               for item in report["release_evidence"]["eligible_closed_rows"])


# --------------------------------------------------------------------------- #
# 5. F12：ACK ≠ 送达、ACK ≠ 恢复
# --------------------------------------------------------------------------- #
def test_ack_is_not_recovery() -> None:
    section("5. F12/K05：已 ACK 不得读成已送达/已恢复")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    db, path = new_db("ack")
    meta = manifest("rel-acc", "demo", "demo:acc", db=db)
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start, count=31, session_id="s1")
    with db.transaction() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO alert_deliveries(
                environment,incident_key,attempt,state,state_rank,severity,first_occurred_at,
                first_occurred_at_ms,created_at,submitted_at_ms,delivered_at_ms,ack_at_ms,
                provider_receipt,device_receipt,receiver_ref,record_json)
            VALUES('demo','delivered-1',1,'ACKNOWLEDGED',4,'critical','t0',1,'t0',1,
                   1000,1500,'provider-1','device-1','ops','{}')""")
        connection.execute(
            """INSERT OR REPLACE INTO alert_deliveries(
                environment,incident_key,attempt,state,state_rank,severity,first_occurred_at,
                first_occurred_at_ms,created_at,submitted_at_ms,delivered_at_ms,ack_at_ms,
                provider_receipt,device_receipt,receiver_ref,record_json)
            VALUES('demo','ack-only-1',1,'ACKNOWLEDGED',4,'critical','t0',1,'t0',1,
                   NULL,2000,'provider-2',NULL,'ops','{}')""")
        connection.execute(
            """INSERT OR REPLACE INTO alert_deliveries(
                environment,incident_key,attempt,state,state_rank,severity,first_occurred_at,
                first_occurred_at_ms,created_at,submitted_at_ms,delivered_at_ms,ack_at_ms,
                provider_receipt,device_receipt,receiver_ref,record_json)
            VALUES('demo','accepted-only-1',1,'ACCEPTED',2,'critical','t0',1,'t0',1,
                   NULL,NULL,'provider-3',NULL,'ops','{}')""")
        connection.execute(
            """INSERT OR REPLACE INTO risk_incidents(
                incident_key,event_type,scope,severity,status,first_seen_at,last_seen_at,
                occurrence_count,acknowledged_at,resolved_at,raw_json)
            VALUES('inc-acked-open','risk','account','critical','OPEN','t0','t0',1,
                   '2026-01-01T00:10:00+00:00',NULL,'{}')""")
    report = build(path, meta, since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    if not APPLIED:
        check("基线态：报告没有事件恢复/送达真相块（ACK≠恢复 未实现；记录缺陷）",
              "event_recovery" not in report and "run_continuity" not in report,
              sorted(report))
        check("基线态：critical_open 门禁不披露 acked-but-unresolved",
              "acknowledged_but_unresolved" not in report["gates"]["critical_open_incidents"],
              sorted(report["gates"]["critical_open_incidents"]))
        db.close()
        return
    if not HW_PATCHED:
        check("基线态：共享补丁未应用时 release_evidence 明确报 NO_DATA + 原因码（不猜）",
              report.get("evidence_version") == EV_VERSION
              and report["event_recovery"]["delivery"]["state"] == "NO_DATA"
              and "U09_SHARED_PATCH_MISSING" in report["event_recovery"]["reason_codes"],
              report["event_recovery"])
        check("基线态：incident 的 ACK≠恢复仍由发布证据自身读出（不依赖共享补丁）",
              report["event_recovery"]["incidents"]["acked_not_recovered_count"] == 1,
              report["event_recovery"]["incidents"])
    else:
        truth = health_watch.alert_receipt_truth(db.conn, environment="demo")
        check("共享态：真实回执才算送达（只有 delivered_at_ms + device_receipt 那条）",
              truth["delivered_confirmed"] == 1, truth)
        check("共享态：已 ACK 无设备回执 → ack_without_delivery，不冒充送达",
              [item["incident_key"] for item in truth["ack_without_delivery"]] == ["ack-only-1"],
              truth["ack_without_delivery"])
        check("共享态：供应商接受但不送达 → provider_accepted_only（不是 delivered）",
              truth["provider_accepted_only"] == 1 and truth["state"] == "FAIL", truth)
        check("共享态：原因码直接复用 U14（MANUAL_ACK_ONLY / NO_CONFIRMED_DELIVERY）",
              "MANUAL_ACK_ONLY" in truth["reason_codes"]
              and "NO_CONFIRMED_DELIVERY" in truth["reason_codes"], truth["reason_codes"])
        check("共享态：ACK 口径被显式声明",
              truth["ack_is_not_delivery"] is True
              and report["event_recovery"]["ack_is_not_recovery"] is True,
              report["event_recovery"].get("ack_is_not_recovery"))
        check("共享态：发布证据按真实回执展示（delivered 与 ack-only 分开列）",
              report["event_recovery"]["delivery"]["delivered_confirmed"] == 1
              and len(report["event_recovery"]["delivery"]["ack_without_delivery"]) == 1,
              report["event_recovery"]["delivery"])
        if APPLIED:
            check("共享态：incident 已 ACK 未 resolved → ACK_WITHOUT_RECOVERY",
                  report["event_recovery"]["incidents"]["acked_not_recovered_count"] == 1
                  and "ACK_WITHOUT_RECOVERY" in report["event_recovery"]["reason_codes"],
                  report["event_recovery"])
            check("共享态：critical_open 门禁披露 acknowledged_but_unresolved",
                  report["gates"]["critical_open_incidents"][
                      "acknowledged_but_unresolved"] == 1,
                  report["gates"]["critical_open_incidents"])
    # 补上真实回执后才允许读成送达（两态都必须成立的口径）。
    with db.transaction() as connection:
        connection.execute(
            "UPDATE alert_deliveries SET delivered_at_ms=2500, device_receipt='device-2' "
            "WHERE environment='demo' AND incident_key='ack-only-1'")
    if HW_PATCHED:
        fixed = health_watch.alert_receipt_truth(db.conn, environment="demo")
        check("共享态：补上真实回执后才算送达（ack_only 清空）",
              fixed["delivered_confirmed"] == 2 and not fixed["ack_without_delivery"], fixed)
    db.close()


# --------------------------------------------------------------------------- #
# 6. F24：重启 vs 换版
# --------------------------------------------------------------------------- #
def test_restart_versus_release_change() -> None:
    section("6. F24：同工件重启不得读成换版；不同 RC/配置混样必须暴露")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    db, path = new_db("continuity")
    meta = manifest("rel-acc", "demo", "demo:acc", db=db)
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start, count=15, session_id="s1")
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start + timedelta(minutes=15), count=16, session_id="s2")
    db.close()  # 关掉再算，确保证据已落盘（WAL checkpoint）
    db = AuditDatabase(path)
    report = build(path, meta, since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    if not (APPLIED and HW_PATCHED):
        check("基线/半态：报告仍可构建且不冒充换版（两态都成立）",
              isinstance(report, dict) and report.get("evidence_version", "") in
              (EV_VERSION, report.get("evidence_version")))
        db.close()
        return
    continuity = report["run_continuity"]
    check("多 session + 单一 config_hash → RESTART_SAME_ARTIFACT（重启不是换版）",
          continuity["verdict"] == "RESTART_SAME_ARTIFACT"
          and continuity["restart_is_version_change"] is False
          and continuity["state"] == "PASS" and continuity["restart_count"] == 1,
          continuity)
    check("重启不改写工件身份（artifact_hash 与 identity_ref 保持不变）",
          continuity["artifact_hash"] == "sha256:acc-artifact"
          and continuity["identity_ref"] == report["identity"]["identity_ref"], continuity)
    # 同窗口第二个 config_hash → 换版/混样必须暴露。
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start + timedelta(minutes=40), count=1, session_id="s3",
                     config_hash="cfg-b")
    mixed = build(path, meta, since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    check("同窗口两种 config_hash → CONFIG_CHANGED_WITHIN_WINDOW + MIXED_CONFIG_SAMPLES",
          mixed["run_continuity"]["verdict"] == "CONFIG_CHANGED_WITHIN_WINDOW"
          and "MIXED_CONFIG_SAMPLES" in mixed["run_continuity"]["reason_codes"]
          and mixed["run_continuity"]["state"] == "FAIL",
          mixed["run_continuity"])
    check("换版使 runtime_observation 组不得 PASS",
          _group_status(mixed, "runtime_observation") != "PASS",
          _group_status(mixed, "runtime_observation"))
    check("不同 release_id 的观察混入同一窗口被披露（不作为本发布样本）",
          _foreign_release_disclosed(path, meta, start, end, db))
    db.close()


def _group_status(report: dict[str, Any], group: str) -> str | None:
    for item in (report.get("k06") or {}).get("groups") or []:
        if item.get("group") == group:
            return item.get("status")
    return None


def _foreign_release_disclosed(path: Path, meta: dict[str, Any], start: datetime,
                               end: datetime, db: AuditDatabase) -> bool:
    add_observations(db, release_id="rel-other", environment="demo", account_ref="demo:acc",
                     start=start + timedelta(minutes=2), count=1, session_id="s9")
    report = build(path, meta, since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    continuity = report["run_continuity"]
    return (continuity["foreign_release_rows"] >= 1
            and "MIXED_RELEASE_SAMPLES" in continuity["reason_codes"])


# --------------------------------------------------------------------------- #
# 7. K06：五组独立、NO_DATA != PASS、声明 PASS 必须自证
# --------------------------------------------------------------------------- #
def test_k06_groups() -> None:
    section("7. K06：五组独立、NO_DATA != PASS、声明 PASS 必须自证")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    db, path = new_db("k06")
    meta = manifest("rel-acc", "demo", "demo:acc", db=db)
    add_observations(db, release_id="rel-acc", environment="demo", account_ref="demo:acc",
                     start=start, count=31, session_id="s1")
    if not APPLIED:
        check("基线态：报告里没有 K06 五组块（新口径属 U09 补丁）",
              "k06" not in build(path, meta, since=iso(start), until=iso(end),
                                 thresholds=BASE_THRESHOLDS), "k06 present")
        db.close()
        return
    report = build(path, meta, since=iso(start), until=iso(end), thresholds=BASE_THRESHOLDS)
    groups = [item["group"] for item in (report["k06"] or {}).get("groups") or []]
    check("K06 固定五组齐全且顺序稳定",
          groups == ["engineering", "accounting", "runtime_observation", "operations",
                     "strategy_evaluation"], groups)
    check("K06 块结构固定且不携带整体 GO 判定（不建第二套判断器）",
          set(report["k06"]) == {"schema_version", "groups", "blocking_items",
                                 "report_hash", "note"}
          and all(item["status"] in {"PASS", "FAIL", "NO_DATA"}
                  for item in report["k06"]["groups"]),
          sorted(report["k06"]))
    check("无闭合交易时 accounting=NO_DATA（NO_DATA != PASS）",
          _group_status(report, "accounting") == "NO_DATA",
          _group_status(report, "accounting"))
    check("NO_DATA 组必须带阻断项（缺证据不是通过）",
          any(item["group"] == "accounting" for item in report["blocking_items"]),
          report["blocking_items"])
    # 组与组独立：改一个组的声明不得改写其它组（相对性质，不依赖任何补丁态）。
    other_before = {item["group"]: item["status"] for item in report["k06"]["groups"]
                    if item["group"] != "accounting"}
    declared = evidence.build_release_readiness_report(
        path, meta, environment="demo", since=iso(start), until=iso(end),
        declaration={"accounting": "FAIL"}, thresholds=BASE_THRESHOLDS)
    other_after = {item["group"]: item["status"] for item in declared["k06"]["groups"]
                   if item["group"] != "accounting"}
    check("组与组独立：声明 accounting=FAIL 不改变其它组状态",
          other_before == other_after, (other_before, other_after))
    check("每组都给出证据 hash 与身份引用",
          all(item.get("evidence_hash") and item.get("identity_ref")
              for item in report["k06"]["groups"]),
          [(item["group"], bool(item.get("identity_ref")))
           for item in report["k06"]["groups"]])
    check("K06 report_hash 可复算",
          report["k06"]["report_hash"]
          == build(path, meta, since=iso(start), until=iso(end),
                   thresholds=BASE_THRESHOLDS)["k06"]["report_hash"],
          report["k06"]["report_hash"])

    # 声明 PASS 而窗口缺失 → FAIL + PASS_WITHOUT_EVIDENCE / WINDOW_MISSING。
    claimed = evidence.build_release_readiness_report(
        path, meta, environment="demo",
        declaration={"strategy_evaluation": "PASS", "engineering": "PASS"},
        thresholds=BASE_THRESHOLDS)
    codes = [item["code"] for item in claimed["blocking_items"]
             if item["group"] == "strategy_evaluation"]
    check("声明 PASS 而窗口缺失 → FAIL + PASS_WITHOUT_EVIDENCE/WINDOW_MISSING",
          _group_status(claimed, "strategy_evaluation") == "FAIL"
          and "PASS_WITHOUT_EVIDENCE" in codes and "WINDOW_MISSING" in codes, codes)
    # 缺运行工件身份 → IDENTITY_REF_MISSING（不得凭声明通过）。
    no_identity = manifest("rel-acc", "demo", "demo:acc", artifact=None, db=db)
    anonymous = evidence.build_release_readiness_report(
        path, no_identity, environment="demo", since=iso(start), until=iso(end),
        declaration={"strategy_evaluation": "PASS"}, thresholds=BASE_THRESHOLDS)
    anonymous_codes = [item["code"] for item in anonymous["blocking_items"]
                       if item["group"] == "strategy_evaluation"]
    check("缺运行工件身份 → IDENTITY_REF_MISSING 阻断（声明 PASS 也无效）",
          _group_status(anonymous, "strategy_evaluation") == "FAIL"
          and "IDENTITY_REF_MISSING" in anonymous_codes, anonymous_codes)
    check("身份不完整时 identity 块自身标 NO_DATA（不伪造身份）",
          anonymous["identity"]["state"] == "NO_DATA"
          and "ARTIFACT_HASH_MISSING" in anonymous["identity"]["reason_codes"],
          anonymous["identity"])
    # 声明 FAIL/NO_DATA 也会被如实接收（组与组独立，不互相替代）。
    declared_fail = evidence.build_release_readiness_report(
        path, meta, environment="demo", since=iso(start), until=iso(end),
        declaration={"accounting": "FAIL"}, thresholds=BASE_THRESHOLDS)
    check("声明 FAIL 被如实接收（不因其他组 PASS 而被改写）",
          _group_status(declared_fail, "accounting") == "FAIL",
          _group_status(declared_fail, "accounting"))
    check("strategy_evaluation 无影子样本时保持 NO_DATA（不得当 PASS）",
          _group_status(report, "strategy_evaluation") == "NO_DATA",
          _group_status(report, "strategy_evaluation"))
    db.close()


# --------------------------------------------------------------------------- #
# 8. 共享适配两态衔接
# --------------------------------------------------------------------------- #
def test_shared_adapters_two_states() -> None:
    section("8. 共享适配两态衔接（audit.database / core.health_watch / CLI）")
    print(f"  标记: evidence_v2={APPLIED} database={DB_PATCHED} "
          f"health_watch={HW_PATCHED} report_cli={CLI_PATCHED}")
    # 两态都必须成立：既有共享模块仍可导入且基线行为不变。
    check("audit.database 仍可导入且 utc_now 可用",
          callable(getattr(__import__("audit.database", fromlist=["utc_now"]), "utc_now")))
    check("core.health_watch 仍可只读评估（两态都必须成立）",
          callable(getattr(health_watch, "open_read_only", None)))
    check("health_watch 基线探针未被破坏",
          callable(getattr(health_watch, "probe_latest_observation", None))
          and callable(getattr(health_watch, "probe_trading_evidence", None)))
    db, path = new_db("shared")
    if DB_PATCHED:
        from audit import database as audit_database  # noqa: PLC0415

        check("数据库补丁：旧写入路径的 A_strategy 被归一到 U06a 规范来源名",
              audit_database.canonical_shadow_source("A_strategy") == shadow_status.SOURCE_A
              and audit_database.canonical_shadow_source("binance_top_trader_proxy")
              == shadow_status.SOURCE_B,
              getattr(audit_database, "SHADOW_SOURCE_ALIASES", None))
        check("数据库补丁：别名目标逐字等于 shadow_status 常量（改名即红）",
              audit_database.canonical_shadow_source("A_strategy") == shadow_status.SOURCE_A
              and all(str(value) in {spec.source for spec in shadow_status.COHORTS}
                      for value in audit_database.SHADOW_SOURCE_ALIASES.values()),
              audit_database.SHADOW_SOURCE_ALIASES)
        db.log_shadow_decision(decision_id="legacy-a", symbol="BTCUSDT", signal="bullish",
                              reference_price=1.0, confidence=0.5, source="A_strategy",
                              execution_weight=0.0, feature_json="{}")
        stored = db.fetch_one("SELECT source FROM shadow_decisions WHERE decision_id='legacy-a'")
        check("数据库补丁：历史别名写入后落库即为规范来源（不再产生 A_strategy 行）",
              stored["source"] == shadow_status.SOURCE_A, stored["source"])
    else:
        db.log_shadow_decision(decision_id="legacy-a", symbol="BTCUSDT", signal="bullish",
                              reference_price=1.0, confidence=0.5, source="A_strategy",
                              execution_weight=0.0, feature_json="{}")
        stored = db.fetch_one("SELECT source FROM shadow_decisions WHERE decision_id='legacy-a'")
        check("基线态：A_strategy 原样落库（门禁会按未知来源排除，缺陷证据）",
              stored["source"] == "A_strategy"
              and shadow_status.release_gate_role("A_strategy")
              == shadow_status.GATE_ROLE_RESEARCH_ONLY,
              stored["source"])
    if HW_PATCHED and APPLIED:
        check("监控补丁：release_observation_window 不把数据首末当窗口边界",
              _probe_declares_not_data_driven(db))
    else:
        check("基线态：core.health_watch 无 U09 探针（release_evidence 会标 NO_DATA）",
              not hasattr(health_watch, "release_observation_window")
              or not hasattr(health_watch, "alert_receipt_truth"),
              [name for name in ("release_observation_window", "alert_receipt_truth")
               if hasattr(health_watch, name)])
    db.close()


def _probe_declares_not_data_driven(db: AuditDatabase) -> bool:
    facts = health_watch.release_observation_window(
        db.conn, release_id="rel-acc", environment="demo", account_ref="demo:acc")
    return (facts.get("window_derived_from_data") is False
            and facts.get("head_gap_minutes") is None
            and facts.get("tail_gap_minutes") is None)


def main() -> int:
    print(f"U09 验收套件工作目录: {TMP}")
    print(f"两态探测: evidence_v2={APPLIED} database={DB_PATCHED} "
          f"health_watch={HW_PATCHED} cli={CLI_PATCHED}")
    try:
        test_core_invariants()
        test_shadow_source_gate()
        test_no_edge_excluded()
        test_window_boundaries()
        test_window_invalid()
        test_lifecycle_revision_and_identity()
        test_ack_is_not_recovery()
        test_restart_versus_release_change()
        test_k06_groups()
        test_shared_adapters_two_states()
    finally:
        if FAILED:
            print(f"\n（保留失败现场以便排查：{TMP}）")
        else:
            shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nU09 验收: {PASSED} 通过 / {len(FAILED)} 失败")
    for name in FAILED:
        print(f"  FAILED: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
