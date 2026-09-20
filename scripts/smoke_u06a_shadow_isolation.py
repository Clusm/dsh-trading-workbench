"""U06a 影子最小隔离冒烟：真实侧车轮次的离线端到端（含共享适配接线）。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：**网络层用模块内桩替换**（``get_marks`` / ``_fetch_ratios``），
不联网、不读 ``.env``/``data/``、不构造 Broker、不启停服务、不改风险档。

**双态可运行**：共享适配（``src/intel/service.py`` / ``decisions.html``）未应用时，
本脚本退化为"独占模块 + 旧口径兼容"断言（仍 exit 0）；已应用时追加整轮
``_refresh`` 的端到端断言。状态用**运行时行为**探测（``snapshot()`` 是否带
``shadow_disclosure``），不读共享文件内容。

运行：
    PYTHONUTF8=1 <repo>/.venv/Scripts/python.exe scripts/smoke_u06a_shadow_isolation.py

SCRIPTS 登记行（由 I 加入 ``scripts/run_offline_regression.py`` 的 ``SCRIPTS`` 元组）：
    "scripts/smoke_u06a_shadow_isolation.py",
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import intel.service as isvc  # noqa: E402
import intel.shadow_status as ss  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []

SYMBOL = "BTCUSDT"
MARK = 60000.0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {extra}")


def _note(text: str) -> None:
    print(f"  ----  {text}")


def _install_offline_stubs() -> None:
    """把侧车的网络入口换成确定性桩；不触碰真实传输层实现。"""
    now_ms = 1_700_000_000_000

    def fake_marks(symbols, max_age: float = 30.0):  # noqa: ANN001, ARG001
        return [{"symbol": SYMBOL, "markPrice": str(MARK), "indexPrice": "59990",
                 "lastFundingRate": "0.0001", "time": now_ms,
                 "nextFundingTime": now_ms + 28_800_000}]

    def fake_ratios(self, symbol: str):  # noqa: ANN001, ARG002
        # 三个比率都 > 1.05 → _signal() 判为 bullish（>= 2 个看多）。
        values = {"top_account_ratio": 1.2, "top_position_ratio": 1.2,
                  "taker_buy_sell_ratio": 1.2}
        return values, now_ms, []

    isvc.get_marks = fake_marks
    isvc.ReadOnlyIntelService._fetch_ratios = fake_ratios


def _make_service(db, config):  # noqa: ANN001
    return isvc.ReadOnlyIntelService(db=db, config=config, symbols=[SYMBOL])


def test_author_state_compatibility(db) -> None:  # noqa: ANN001
    """两态都成立：独占模块可用 + 旧口径汇总字段语义不变（既有看板消费者）。

    本段**不假定**共享适配是否已应用：``shadow_evaluation`` 在两态都由整轮刷新
    产出（未应用时来自 ``intel_observability()``，已应用时来自汇总缓存），
    两种实现都必须保留 ``events_24h``（``decisions.html`` 的既有消费者）。
    """
    svc = _make_service(db, {"enabled": True})
    check("侧车快照声明 shadow_only", svc.snapshot()["shadow_only"] is True)
    check("侧车快照的执行权重为 0", svc.snapshot()["execution_weight"] == 0.0)
    check("模块常量：权重 0 / v2 DEFERRED / 首批范围",
          ss.EXECUTION_WEIGHT == 0.0 and ss.V2_LABELS_STATE == "DEFERRED"
          and ss.FIRST_RELEASE_FAULT_SCOPE
          == "FIRST_RELEASE_LABEL_DISCLOSURE_C_UNCONFIGURED_ISOLATION_ONLY")
    check("C 未配置时闸门关闭",
          ss.should_emit_sample(source=ss.SOURCE_C, config={"enabled": True},
                                signal="bullish", reference_price=MARK) is False)

    snapshot = svc.maybe_refresh(force=True)
    check("整轮刷新产出快照", snapshot["status"] in {"ok", "degraded"},
          str(snapshot.get("errors")))
    check("刷新后 shadow_evaluation 存在（decisions.html 依赖）",
          "shadow_evaluation" in snapshot, str(sorted(snapshot)))
    check("shadow_evaluation 保留 events_24h（看板兼容）",
          "events_24h" in snapshot["shadow_evaluation"],
          str(snapshot["shadow_evaluation"]))
    check("shadow_evaluation 保留 evaluations 键",
          "evaluations" in snapshot["shadow_evaluation"])
    check("刷新后执行权重仍为 0", snapshot["execution_weight"] == 0.0)
    b_rows = [dict(row) for row in db.fetch_all(
        "SELECT signal,execution_weight FROM shadow_decisions WHERE source=?",
        (ss.SOURCE_B,))]
    check("B 组写出方向样本且权重为 0",
          len(b_rows) == 1 and b_rows[0]["signal"] == "bullish"
          and b_rows[0]["execution_weight"] == 0.0, str(b_rows))


def test_full_refresh_end_to_end(db) -> None:  # noqa: ANN001
    cfg = {"enabled": True, "execution_weight": 0.9, "shadow_only": False,
           "watched_wallets": [], "token_contracts": {}, "research_budget_ms": 60000}
    svc = _make_service(db, cfg)

    first = svc.maybe_refresh(force=True)
    check("整轮刷新成功产出快照", first["status"] in {"ok", "degraded"}, str(first["status"]))
    check("快照带影子披露", "shadow_disclosure" in first, str(sorted(first)))
    disclosure = first["shadow_disclosure"]
    check("披露口径为旧口径 v1", disclosure["caliber"]["id"] == ss.LEGACY_CALIBER_ID)
    check("披露执行权重被强制为 0（配置写 0.9 也不生效）",
          disclosure["execution_weight"]["effective"] == 0.0
          and disclosure["execution_weight"]["conflict"] is True,
          str(disclosure["execution_weight"]))
    check("披露 v2 标签 DEFERRED（必达要点④）",
          disclosure["v2_labels"]["state"] == "DEFERRED")
    check("披露 C 组 unconfigured",
          disclosure["cohorts"][ss.SOURCE_C]["status"] == "unconfigured",
          str(disclosure["cohorts"][ss.SOURCE_C]))
    check("披露 A/B 组可产生样本",
          disclosure["cohorts"][ss.SOURCE_A]["emits_samples"] is True
          and disclosure["cohorts"][ss.SOURCE_B]["emits_samples"] is True)
    check("披露计入门禁为否", disclosure["release_gate"]["research_eligible"] is False)
    check("披露隔离事实：无 LLM / 无 Broker / 无下单",
          disclosure["isolation"]["llm_calls"] == 0
          and disclosure["isolation"]["broker_access"] is False
          and disclosure["isolation"]["order_capability"] is False,
          str(disclosure["isolation"]))
    check("披露侧车线程名独立",
          disclosure["isolation"]["dedicated_thread"] is True
          and disclosure["isolation"]["thread"] == "readonly-intel-sidecar")

    rows = [dict(item) for item in db.fetch_all(
        "SELECT decision_id,source,signal,execution_weight,feature_json "
        "FROM shadow_decisions ORDER BY source")]
    sources = [row["source"] for row in rows]
    check("B 组写出方向样本（bullish）",
          sources.count(ss.SOURCE_B) == 1
          and all(row["signal"] == "bullish" for row in rows if row["source"] == ss.SOURCE_B),
          str(rows))
    check("C 未配置 → 一行都不写（必达要点②）",
          sources.count(ss.SOURCE_C) == 0, f"sources={sources}")
    check("所有影子行权重落库为 0.0",
          all(row["execution_weight"] == 0.0 for row in rows), str(rows))
    check("影子行带 cohort 标记且声明不计门禁",
          all(json.loads(row["feature_json"])["shadow_cohort"]
              ["counts_toward_release_gate"] is False for row in rows), str(rows))
    check("被拦下的 C 组行数如实登记在披露里",
          disclosure["isolation"]["skipped_emissions"] >= 1,
          str(disclosure["isolation"]))

    check("快照带 shadow_metrics（研究参考视图）", "shadow_metrics" in first)
    metrics = first["shadow_metrics"]
    check("shadow_metrics 标旧口径 + 不计门禁",
          metrics["caliber"] == ss.LEGACY_CALIBER_ID
          and metrics["auto_promotion"] is False
          and metrics["execution_weight"] == 0.0
          and metrics["v2_labels"] == "DEFERRED", str(metrics))
    check("shadow_metrics 行都带零权重 / DEFERRED / 不计门禁",
          all(item["execution_weight"] == 0.0 and item["v2_labels"] == "DEFERRED"
              and item["release_gate_eligible"] is False for item in metrics["evaluations"]),
          str(metrics["evaluations"]))

    # 旧字段兼容：shadow_evaluation 仍由 intel_observability 的完整结果支撑。
    check("shadow_evaluation 保留 events_24h（看板兼容）",
          "events_24h" in first["shadow_evaluation"], str(first["shadow_evaluation"]))
    check("shadow_evaluation 保留 evaluations 键",
          "evaluations" in first["shadow_evaluation"])
    check("首轮汇总指标为 REFRESHED", metrics["state"] == "REFRESHED", str(metrics))

    # 第二轮：预算内但指标未到期 → 走缓存（不每轮全表聚合，减少与管理线程争用）。
    second = svc.maybe_refresh(force=True)
    check("第二轮不再重算汇总（CACHED）",
          second["shadow_metrics"]["state"] == "CACHED",
          str(second["shadow_metrics"]))
    check("第二轮仍给出缓存年龄", isinstance(second["shadow_metrics"]["age_seconds"], float))
    check("缓存到期前不重复聚合",
          "metrics_cache_fresh" in second["shadow_disclosure"]["load_plan"]["degraded"],
          str(second["shadow_disclosure"]["load_plan"]))
    check("降级阶段同步写入隔离事实",
          second["shadow_disclosure"]["isolation"]["degraded_stages"]
          == ["metrics_cache_fresh"],
          str(second["shadow_disclosure"]["isolation"]))
    check("降级时旧字段仍有数据（不清空）",
          "events_24h" in second["shadow_evaluation"], str(second["shadow_evaluation"]))
    # 旧口径已知缺陷（不在 U06a 范围，但必须**如实披露**而不是被静默忽略）：
    # 每轮刷新按 symbol 各写一行、decision_id 用新 uuid，因此重叠样本会累积。
    # 这是既有行为，U06b/U07 才做固定时点与独立样本；本批次只保证它写在披露里。
    all_rows = [dict(item) for item in db.fetch_all(
        "SELECT source,decision_id FROM shadow_decisions")]
    repeats = len([row for row in all_rows if row["source"] == ss.SOURCE_B])
    check("每轮刷新各写一行 B 组样本（既有行为，不静默）", repeats == 2, f"repeats={repeats}")
    check("decision_id 每行唯一（不覆盖既有样本）",
          len({row["decision_id"] for row in all_rows}) == len(all_rows))
    check("披露明说重叠样本不等于独立交易",
          any("独立" in item for item in disclosure["caliber"]["limitations"]),
          str(disclosure["caliber"]["limitations"]))


def test_metrics_failure_is_not_reported_fresh(db) -> None:  # noqa: ANN001
    """P2-1：聚合失败后**不得**报 REFRESHED / age≈0（陈旧当新值 = 假新鲜）。

    把聚合入口 ``intel_observability`` 换成抛错桩，并让"距上次成功"超过重算间隔
    （直接推进成功时刻，不 sleep 900s），再整轮刷新。
    """
    svc = _make_service(db, {"enabled": True})
    first = svc.maybe_refresh(force=True)
    check("首轮汇总为 REFRESHED", first["shadow_metrics"]["state"] == "REFRESHED",
          str(first["shadow_metrics"]))
    original = svc.db.intel_observability

    def boom(*_a, **_k):
        raise RuntimeError("aggregate failed")

    try:
        svc.db.intel_observability = boom  # type: ignore[method-assign]
        # 推进"上次成功"时刻，令本轮到期重算（不 sleep）。
        svc._metrics_success_at = (
            (svc._metrics_success_at or 0.0) - (svc.metrics_refresh_seconds + 60.0))
        second = svc.maybe_refresh(force=True)
    finally:
        svc.db.intel_observability = original  # type: ignore[method-assign]

    metrics = second["shadow_metrics"]
    check("聚合失败后 state 为 FAILED（不是 REFRESHED）",
          metrics["state"] == "FAILED", str(metrics))
    check("聚合失败后 age 未被重置为 0（说的是距上次成功）",
          isinstance(metrics["age_seconds"], float)
          and metrics["age_seconds"] >= svc.metrics_refresh_seconds,
          str(metrics))
    check("聚合失败后仍带旧缓存（events_24h 不清空）",
          "events_24h" in second["shadow_evaluation"], str(second["shadow_evaluation"]))
    check("失败原因进入本轮错误列表",
          any(item.startswith("metrics:") for item in second["errors"]),
          str(second["errors"]))
    check("披露仍声明未按 source 过滤的残留缺口（不因失败而消失）",
          second["shadow_disclosure"]["release_gate"]["residual_gap"]["fixed_here"] is False,
          str(second["shadow_disclosure"]["release_gate"]["residual_gap"]))


def test_over_budget_degrades_labeling(db) -> None:  # noqa: ANN001
    """超预算 → 降级可选阶段并留痕（必达要点③：不拖住管理线程）。"""
    original = ss.research_load_plan

    def forced(**kwargs):  # noqa: ANN003
        plan = original(**kwargs)
        return {**plan, "allow_labeling": False, "allow_metrics": False,
                "degraded": ["research_budget_exceeded"]}

    ss.research_load_plan = forced
    try:
        svc = _make_service(db, {"enabled": True})
        snapshot = svc.maybe_refresh(force=True)
        plan = snapshot["shadow_disclosure"]["load_plan"]
        check("披露记录预算超支降级",
              plan["degraded"] == ["research_budget_exceeded"], str(plan))
        check("降级时禁用打标签", plan["allow_labeling"] is False)
        check("降级时禁用汇总重算", plan["allow_metrics"] is False)
        check("首轮即降级 → 汇总标 UNAVAILABLE（不用 0 冒充）",
              snapshot["shadow_metrics"]["state"] == "UNAVAILABLE",
              str(snapshot["shadow_metrics"]))
        check("降级不产生无效绩效样本（未打标签 → 无 outcomes）",
              int(db.fetch_all("SELECT COUNT(*) AS n FROM shadow_outcomes")[0]["n"]) == 0)
        check("降级仍不写 C 组样本",
              int(db.fetch_all("SELECT COUNT(*) AS n FROM shadow_decisions "
                               "WHERE source=?", (ss.SOURCE_C,))[0]["n"]) == 0)
        check("隔离事实同步降级阶段",
              snapshot["shadow_disclosure"]["isolation"]["degraded_stages"]
              == ["research_budget_exceeded"])
    finally:
        ss.research_load_plan = original


def main() -> int:
    print("=" * 72)
    print("U06a 影子最小隔离冒烟（离线端到端，无 pytest / 无网络）")
    print("=" * 72)
    _install_offline_stubs()
    def run_case(label: str, fn) -> None:  # noqa: ANN001
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            db = AuditDatabase(Path(td) / "smoke.sqlite")
            try:
                print(f"\n{label}")
                fn(db)
            finally:
                try:
                    db.conn.close()
                except Exception:
                    pass

    run_case("[1] 两态兼容：模块契约 + 旧字段语义", test_author_state_compatibility)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        probe_db = AuditDatabase(Path(td) / "probe.sqlite")
        try:
            applied = "shadow_disclosure" in _make_service(probe_db, {"enabled": True}).snapshot()
        finally:
            try:
                probe_db.conn.close()
            except Exception:
                pass
    print(f"\n[2] 整轮刷新端到端（共享适配运行时探测 = "
          f"{'applied' if applied else 'not_applied'}）")
    if applied:
        run_case("[2] 整轮刷新端到端", test_full_refresh_end_to_end)
        run_case("[3] 超预算降级", test_over_budget_degrades_labeling)
        run_case("[4] 聚合失败不得报新值", test_metrics_failure_is_not_reported_fresh)
    else:
        _note("共享适配未应用：本分支只承载独占产物，整轮接线由集成树/补丁沙箱验证。")
        _note("模块契约与旧字段兼容已在 [1] 断言，本脚本在两态都非空转。")
    print("\n" + "=" * 72)
    print(f"结果：PASS {PASS} / FAIL {FAIL}")
    if FAILURES:
        print("失败项：")
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
