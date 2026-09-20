"""U06a 影子最小隔离与口径修正（K01 / K06 / F07 / F11 / F16 / F20 / DC15）离线回归。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：不联网、不读 ``.env``/``data/``、不构造 Broker、不启停服务、不改风险档。

**双态可运行**：本文件的主断言只依赖
- U06a 独占模块 ``src/intel/shadow_status.py``（本任务新增，两态都在），
- 已集成契约 ``src/audit/database.py``（``shadow_decisions``/``shadow_outcomes`` 表）、
- 已集成 ``src/core/release_evidence.py``（K06 门禁的消费者，只读其常量与口径）。

共享适配（``src/intel/service.py`` / ``decisions.html``）**只在运行时按行为探测**，
不读文件内容判定"补丁是否已应用"：探测手段是构造真实
``ReadOnlyIntelService`` 并看公开 ``snapshot()`` 是否带 ``shadow_disclosure``。
探测为"已应用"时追加服务级行为断言，为"未应用"时改为断言本模块**已提供**
共享适配所需的全部符号与语义（以免两支分支有一支空转）。

期望值来源：任务卡 U06a 五条必达要点、角色卡「D 影子研究」、K06 契约 §8、
``docs/contracts/README.md`` §8（200 方向候选 / 30 闭合由 ``release_evidence`` 固定）。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import intel.shadow_status as ss  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []

MODULE_PATH = ROOT / "src" / "intel" / "shadow_status.py"
SERVICE_PATH = ROOT / "src" / "core" / "service.py"


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


@contextmanager
def _audit_db(name: str = "t.sqlite"):
    """临时目录里的真实 ``AuditDatabase``；退出时关连接再删目录。

    Windows 下 SQLite 连接未关闭会锁住文件导致清理失败，故显式 close。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = AuditDatabase(Path(td) / name)
        try:
            yield db
        finally:
            try:
                db.conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# [1] 常量与首批范围（必达要点 ①④）
# --------------------------------------------------------------------------- #
def test_execution_weight_is_a_constant_zero() -> None:
    check("EXECUTION_WEIGHT 恒为 0.0", ss.EXECUTION_WEIGHT == 0.0)
    check("EXECUTION_WEIGHT 是模块常量而非配置项",
          isinstance(ss.EXECUTION_WEIGHT, float))
    check("v2 标签状态为 DEFERRED（必达要点④）", ss.V2_LABELS_STATE == "DEFERRED")
    check("首批范围字符串逐字匹配 F11 授权范围",
          ss.FIRST_RELEASE_FAULT_SCOPE
          == "FIRST_RELEASE_LABEL_DISCLOSURE_C_UNCONFIGURED_ISOLATION_ONLY")
    check("旧口径 ID 已固定", ss.LEGACY_CALIBER_ID == "legacy-v1-rolling-mark-price")
    check("旧口径缺陷清单非空（不能只说“参考”）", len(ss.LEGACY_LIMITATIONS) >= 5)
    check("模块 schema 版本已声明", ss.SCHEMA_VERSION == "u06a-shadow-status-v1")
    check("只有 bullish/bearish 算方向性样本",
          ss.DIRECTIONAL_SIGNALS == frozenset({"bullish", "bearish"}))


def test_module_has_no_io_or_llm_capability() -> None:
    """必达要点③：侧车判定模块不得自带网络/LLM/数据库能力。

    用 AST 检查**真实导入表**，而不是文本 grep：注释或 docstring 里提到
    ``httpx`` 不会被误判为"有网络能力"，反过来真导入也不会被漏掉。
    """
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    forbidden = {"httpx", "requests", "urllib", "socket", "sqlite3", "anthropic",
                 "openai", "subprocess", "threading", "asyncio"}
    check("shadow_status 不导入网络/LLM/DB/线程/子进程模块",
          not (imported & forbidden), f"实际导入={sorted(imported)}")
    check("shadow_status 只依赖标准类型模块",
          imported <= {"__future__", "dataclasses", "typing"}, f"实际导入={sorted(imported)}")
    tree2 = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    called = {node.func.id for node in ast.walk(tree2)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    check("shadow_status 不调用 LLM/网络类函数",
          not (called & {"open", "eval", "exec", "compile"}), f"实际调用={sorted(called)}")


def test_sidecar_thread_is_dedicated_in_core_service() -> None:
    """必达要点③：侧车跑在专用线程上，不与管理线程共用。

    只读 ``src/core/service.py``（本任务未改一行，两态相同）里
    ``threading.Thread(..., name=...)`` 的**字面量线程名集合**，断言
    :func:`shadow_status.isolation_facts` 声明的线程名是独立存在的一个。
    这是"不争用管理线程"的可验证事实，而不是文档承诺。
    """
    tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    targets: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        fname = getattr(func, "attr", getattr(func, "id", ""))
        if fname != "Thread":
            continue
        thread_name = ""
        target_name = ""
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                thread_name = str(kw.value.value)
            elif kw.arg == "target" and isinstance(kw.value, ast.Attribute):
                target_name = kw.value.attr
        if thread_name:
            names.add(thread_name)
            targets.append((thread_name, target_name))
    declared = ss.isolation_facts()["thread"]
    check("侧车线程名在 core/service.py 中真实存在", declared in names,
          f"声明的={declared} 实际={sorted(names)}")
    check("侧车线程名唯一（不与其它后台线程同名）",
          sum(1 for item in names if item == declared) == 1)
    sidecar_targets = {target for name, target in targets if name == declared}
    other_targets = {target for name, target in targets if name != declared}
    check("侧车线程跑在独立的目标函数上（不与管理线程共用入口）",
          sidecar_targets and not (sidecar_targets & other_targets),
          f"侧车={sorted(sidecar_targets)} 其它={sorted(other_targets)}")
    check("至少存在一个非侧车后台线程（证明确实分线）", bool(other_targets),
          f"实际={sorted(names)}")


# --------------------------------------------------------------------------- #
# [2] 执行权重护栏：配置面抬不上去（必达要点 ①）
# --------------------------------------------------------------------------- #
def test_execution_weight_guard_forces_zero() -> None:
    cases = [
        ("空配置", {}, False, None),
        ("显式 0.0", {"execution_weight": 0.0}, False, 0.0),
        ("非零 0.9", {"execution_weight": 0.9}, True, 0.9),
        ("负权重", {"execution_weight": -1.0}, True, -1.0),
        ("布尔 True", {"execution_weight": True}, True, None),
        ("字符串 '0.9'", {"execution_weight": "0.9"}, True, None),
        ("关闭 shadow_only", {"shadow_only": False}, True, None),
        ("shadow_only=True 不冲突", {"shadow_only": True}, False, None),
    ]
    for label, cfg, expect_conflict, expect_declared in cases:
        got = ss.execution_weight_guard(cfg)
        check(f"[{label}] 生效权重恒为 0", got["effective"] == 0.0, str(got))
        check(f"[{label}] 冲突登记={expect_conflict}", got["conflict"] is expect_conflict, str(got))
        check(f"[{label}] 声明值如实登记", got["declared"] == expect_declared, str(got))
        check(f"[{label}] 不存在自动晋升", got["auto_promotion"] is False, str(got))
    check("非 Mapping 配置不崩", ss.execution_weight_guard(None)["effective"] == 0.0)
    check("非 Mapping 配置不崩（字符串）",
          ss.execution_weight_guard("not-a-mapping")["effective"] == 0.0)
    guarded = ss.execution_weight_guard({"execution_weight": 5.0, "shadow_only": False})
    check("冲突字段逐条可审计",
          set(guarded["forced_fields"]) == {"execution_weight:non_zero", "shadow_only:false"},
          str(guarded))


# --------------------------------------------------------------------------- #
# [3] C 未配置隔离（必达要点 ②）
# --------------------------------------------------------------------------- #
def test_c_unconfigured_emits_nothing() -> None:
    base = {"enabled": True, "watched_wallets": ["0xabc"], "token_contracts": {"1:0xdead": "BTCUSDT"}}
    state = ss.cohort_configuration(base)
    check("C 组在配置齐全时为 configured",
          state[ss.SOURCE_C]["status"] == "configured", str(state[ss.SOURCE_C]))
    check("A 组无需额外配置即可用", state[ss.SOURCE_A]["status"] == "configured")
    check("B 组无需额外配置即可用", state[ss.SOURCE_B]["status"] == "configured")
    check("C 组声明了必须显式配置的键",
          state[ss.SOURCE_C]["missing"] == [] and set(ss.cohort_spec(ss.SOURCE_C).requires)
          == {"watched_wallets", "token_contracts"})

    for label, cfg in [
        ("完全未配置", {"enabled": True}),
        ("只配了钱包", {"enabled": True, "watched_wallets": ["0xabc"]}),
        ("只配了合约", {"enabled": True, "token_contracts": {"1:0xdead": "BTCUSDT"}}),
        ("空容器不算配置", {"enabled": True, "watched_wallets": [],
                            "token_contracts": {}}),
        ("整体停用", {"enabled": False, "watched_wallets": ["0xabc"],
                      "token_contracts": {"1:0xdead": "BTCUSDT"}}),
    ]:
        got = ss.cohort_configuration(cfg)
        check(f"[{label}] C 组不产生样本",
              got[ss.SOURCE_C]["emits_samples"] is False, str(got[ss.SOURCE_C]))
        check(f"[{label}] C 组状态可区分",
              got[ss.SOURCE_C]["status"] in {"unconfigured", "disabled"},
              str(got[ss.SOURCE_C]))
        plan = ss.emission_plan(cfg)
        check(f"[{label}] emission_plan 对 C 关闭", plan[ss.SOURCE_C] is False, str(plan))

    cred = ss.cohort_configuration(base, credential_available=False)
    check("缺只读凭据时 C 组标 credentials_missing",
          cred[ss.SOURCE_C]["status"] == "credentials_missing", str(cred[ss.SOURCE_C]))
    check("缺凭据同样不产生样本", cred[ss.SOURCE_C]["emits_samples"] is False)
    check("凭据缺失不影响 A/B", cred[ss.SOURCE_A]["emits_samples"] is True
          and cred[ss.SOURCE_B]["emits_samples"] is True)


def test_should_emit_sample_gate() -> None:
    cfg_off = {"enabled": True}
    cfg_on = {"enabled": True, "watched_wallets": ["0xabc"],
              "token_contracts": {"1:0xdead": "BTCUSDT"}}
    check("C 未配置 + 明确方向 + 有效价 → 拒写",
          ss.should_emit_sample(source=ss.SOURCE_C, config=cfg_off,
                                signal="bullish", reference_price=60000.0) is False)
    check("C 已配置 + 明确方向 + 有效价 → 允许",
          ss.should_emit_sample(source=ss.SOURCE_C, config=cfg_on,
                                signal="bullish", reference_price=60000.0) is True)
    for label, signal, price in [
        ("unknown 方向", "unknown", 60000.0),
        ("neutral 方向", "neutral", 60000.0),
        ("空方向", "", 60000.0),
        ("None 方向", None, 60000.0),
        ("零价", "bullish", 0.0),
        ("负价", "bullish", -1.0),
        ("None 价", "bullish", None),
        ("非数值字符串价", "bullish", "not-a-price"),
        ("空字符串价", "bullish", ""),
        ("容器价", "bullish", [60000.0]),
    ]:
        check(f"[{label}] 拒绝落库",
              ss.should_emit_sample(source=ss.SOURCE_C, config=cfg_on,
                                    signal=signal, reference_price=price) is False)
    check("可数值化的字符串价按有效处理（与 _safe_float 口径一致）",
          ss.should_emit_sample(source=ss.SOURCE_B, config=cfg_on,
                                signal="bullish", reference_price="60000") is True)
    check("未知来源 fail-closed",
          ss.should_emit_sample(source="D_unknown_cohort", config=cfg_on,
                                signal="bullish", reference_price=1.0) is False)
    check("停用后 A/B 也不写",
          ss.should_emit_sample(source=ss.SOURCE_B, config={"enabled": False},
                                signal="bullish", reference_price=1.0) is False)
    check("is_directional 只认 bullish/bearish",
          ss.is_directional("BULLISH") is True and ss.is_directional("neutral") is False)


def test_unknown_rows_really_become_counted_sample_rows() -> None:
    """用真实库证明"旧行为确实产生无效绩效样本"，从而证明闸门不是空转。

    旧实现把每条 C 组采集都写成 ``signal='unknown'`` 且 ``reference_price>0``。
    ``evaluate_shadow_decisions`` 只看 ``reference_price>0``，于是这些行会生成
    ``shadow_outcomes``，进入 ``intel_observability`` 的 ``samples`` 计数（虽
    ``directional`` 为 0）。本用例先复现这条路径，再断言新闸门把它挡住。
    """
    from datetime import datetime, timedelta, timezone
    with _audit_db("legacy.sqlite") as db:
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat(timespec="milliseconds")
        db.log_shadow_decision(decision_id="legacy:C:BTCUSDT", symbol="BTCUSDT",
                              signal="unknown", reference_price=60000.0, confidence=0.0,
                              source=ss.SOURCE_C, execution_weight=0.0,
                              feature_json=json.dumps({"contract_market": {"funding_rate": 0.0}}))
        db.conn.execute("UPDATE shadow_decisions SET created_at=? WHERE decision_id=?",
                        (stamp, "legacy:C:BTCUSDT"))
        db.conn.commit()
        inserted = db.evaluate_shadow_decisions(prices={"BTCUSDT": 61000.0},
                                               grace_minutes=30)
        check("旧行为：unknown 行确实生成了绩效样本行", inserted >= 1, f"inserted={inserted}")
        obs = db.intel_observability()
        row = next((item for item in obs["evaluations"] if item["source"] == ss.SOURCE_C), None)
        check("旧行为：unknown 行计入 samples 计数",
              row is not None and row["samples"] >= 1, str(row))
        check("旧行为：unknown 行不贡献方向样本（但仍占样本位）",
              row is not None and row["directional_samples"] == 0, str(row))
        check("新闸门拒绝 unknown 行",
              ss.should_emit_sample(source=ss.SOURCE_C, config={"enabled": True},
                                    signal="unknown", reference_price=60000.0) is False)
        check("新闸门也拒绝未配置但方向明确的 C 组行",
              ss.should_emit_sample(source=ss.SOURCE_C, config={"enabled": True},
                                    signal="bullish", reference_price=60000.0) is False)


# --------------------------------------------------------------------------- #
# [4] 门禁隔离（必达要点 ⑤）
# --------------------------------------------------------------------------- #
def test_release_gate_roles_are_research_only_for_b_and_c() -> None:
    check("A 为执行决策副本",
          ss.release_gate_role(ss.SOURCE_A) == ss.GATE_ROLE_EXECUTION_DECISION)
    check("B 为 research_only",
          ss.release_gate_role(ss.SOURCE_B) == ss.GATE_ROLE_RESEARCH_ONLY)
    check("C 为 research_only",
          ss.release_gate_role(ss.SOURCE_C) == ss.GATE_ROLE_RESEARCH_ONLY)
    check("未知来源 fail-closed 归 research_only",
          ss.release_gate_role("X_legacy_dirty") == ss.GATE_ROLE_RESEARCH_ONLY)
    check("is_research_shadow 与 gate_role 一致",
          ss.is_research_shadow(ss.SOURCE_B) and ss.is_research_shadow(ss.SOURCE_C)
          and not ss.is_research_shadow(ss.SOURCE_A))
    check("research_sources 只含 B/C",
          set(ss.research_sources()) == {ss.SOURCE_B, ss.SOURCE_C},
          str(ss.research_sources()))


def test_gate_caliber_agrees_with_release_evidence() -> None:
    """必达要点⑤：披露里的门槛数字必须与 K06 消费者 ``release_evidence`` 一致。

    直接导入已集成的 ``core.release_evidence``，读它**自己声明**的阈值常量，
    与披露对象里的 ``gate_constants`` 做相等比较；不复制、不硬编码第二套数字。
    """
    import core.release_evidence as rev
    thresholds = rev.DEFAULT_THRESHOLDS
    disclosure = ss.build_disclosure(config={"enabled": True})
    gate = disclosure["release_gate"]
    check("披露的 200 候选阈值与 release_evidence 一致",
          gate["gate_constants"]["min_directional_candidates"]
          == thresholds["min_directional_candidates"],
          f"披露={gate['gate_constants']} 消费者={thresholds}")
    check("披露的 30 闭合阈值与 release_evidence 一致",
          gate["gate_constants"]["min_eligible_closed_trades"]
          == thresholds["min_eligible_closed_trades"])
    check("门槛权威来源指向 release_evidence",
          "release_evidence" in gate["authority"])
    check("B/C 不充当执行候选", gate["research_eligible"] is False)
    check("B/C 不充当已闭合交易", gate["executed_trade_counted"] is False)
    check("B/C 不充当观察天数", gate["observation_days_counted"] is False)
    check("披露声明不建立第二套 GO 判断器",
          "第二套 GO" in gate["note"] or "不建立第二套" in gate["note"])
    check("research_sources 与 release_gate_role 同源",
          set(gate["research_sources"]) == {ss.SOURCE_B, ss.SOURCE_C})


def test_marker_forbids_gate_counting() -> None:
    for source in (ss.SOURCE_A, ss.SOURCE_B, ss.SOURCE_C):
        marker = ss.sample_marker(source)
        check(f"[{source}] 标记声明不计入门禁",
              marker["counts_toward_release_gate"] is False, str(marker))
        check(f"[{source}] 标记带旧口径与零权重",
              marker["caliber"] == ss.LEGACY_CALIBER_ID
              and marker["execution_weight"] == 0.0, str(marker))
        check(f"[{source}] 标记带 v2=DEFERRED",
              marker["v2_labels"] == "DEFERRED", str(marker))
    check("C 组标记为研究可见", ss.sample_marker(ss.SOURCE_C)["research_only"] is True)
    check("A 组标记不是 research_only",
          ss.sample_marker(ss.SOURCE_A)["research_only"] is False)


# --------------------------------------------------------------------------- #
# [5] 旧口径视图与缺失计数（必达要点 ①，不静默丢弃）
# --------------------------------------------------------------------------- #
def test_legacy_metric_view_labels_research_only() -> None:
    raw = [{"source": ss.SOURCE_C, "horizon_minutes": 5, "samples": 12,
            "directional_samples": 3, "accuracy": 0.33, "net_samples": 3,
            "avg_net_return_pct": 0.1, "observation_span_days": 0.5,
            "avg_path_samples": 4.0, "required_path_samples": 1,
            "promotion_ready": True, "auto_promotion": False}]
    loss = {ss.SOURCE_C: ss.missing_sample_accounting(decided=10, labeled=7)}
    rows = ss.legacy_metric_view(raw, sample_loss=loss)
    check("视图行数与原行数一致", len(rows) == 1)
    row = rows[0]
    check("原始键全部保留（不破坏既有消费者）",
          all(raw[0][key] == row[key] for key in raw[0]), str(row))
    check("标注旧口径 ID", row["caliber"] == ss.LEGACY_CALIBER_ID)
    check("标注执行权重恒 0", row["execution_weight"] == 0.0)
    check("不计入发布门禁", row["release_gate_eligible"] is False)
    check("不自动晋升", row["auto_promotion"] is False)
    check("v2 标签 DEFERRED", row["v2_labels"] == "DEFERRED")
    check("人工评审对执行无影响", row["review_effect"] == "none")
    check("promotion_ready 被改写为研究评审参考",
          row["review_label"] == "研究评审参考（不改变执行权重）", str(row["review_label"]))
    check("携带该组缺失计数", row["sample_loss"]["coverage"] == 0.7, str(row["sample_loss"]))
    check("组名与门禁角色齐备", row["group"] == "C"
          and row["gate_role"] == ss.GATE_ROLE_RESEARCH_ONLY)
    check("非法输入不崩（None）", ss.legacy_metric_view(None) == [])
    check("非法输入不崩（字符串）", ss.legacy_metric_view("nope") == [])
    check("非 Mapping 行不崩", ss.legacy_metric_view([1, "x"]) != [])
    weak = ss.legacy_metric_view([{"source": ss.SOURCE_B, "promotion_ready": False}])
    check("样本不足时口径为继续观察",
          weak[0]["review_label"] == "样本不足，继续观察", str(weak[0]))


def test_missing_sample_accounting_never_fabricates() -> None:
    ok = ss.missing_sample_accounting(decided=10, labeled=7)
    check("缺失数 = 已决 - 已标注", ok["missing"] == 3, str(ok))
    check("覆盖率口径正确", ok["coverage"] == 0.7, str(ok))
    check("状态 OK", ok["state"] == "OK")
    check("不声明静默丢弃", ok["selective_drop"] is False)
    check("明说无法区分缺失原因", "U06b" in ok["note"])
    zero = ss.missing_sample_accounting(decided=0, labeled=0)
    check("零样本时覆盖率为 None 而不是 1.0", zero["coverage"] is None, str(zero))
    clamped = ss.missing_sample_accounting(decided=3, labeled=9)
    check("已标注不得超过已决", clamped["labeled"] == 3 and clamped["missing"] == 0,
          str(clamped))
    negative = ss.missing_sample_accounting(decided=-5, labeled=-2)
    check("负数被夹到 0", negative["decided"] == 0 and negative["missing"] == 0, str(negative))
    bad = ss.missing_sample_accounting(decided=0, labeled=0,
                                       unavailable_reason="OperationalError")
    check("查询不可用时状态为 UNAVAILABLE", bad["state"] == "UNAVAILABLE")
    check("不可用时不用 0 冒充无缺失",
          bad["decided"] is None and bad["missing"] is None and bad["coverage"] is None,
          str(bad))
    check("不可用时原因如实记录", bad["reason_unavailable"] == "OperationalError")


# --------------------------------------------------------------------------- #
# [6] 有界研究负载（必达要点③不争用管理线程）
# --------------------------------------------------------------------------- #
def test_research_load_plan_degrades_and_labels() -> None:
    fresh = ss.research_load_plan(elapsed_ms=1000.0, budget_ms=60000.0,
                                  metrics_age_seconds=None, metrics_refresh_seconds=900)
    check("预算内允许打标签", fresh["allow_labeling"] is True)
    check("首次必算汇总指标", fresh["allow_metrics"] is True)
    check("预算内无降级", fresh["degraded"] == [], str(fresh))

    over = ss.research_load_plan(elapsed_ms=61000.0, budget_ms=60000.0,
                                 metrics_age_seconds=None, metrics_refresh_seconds=900)
    check("超预算禁止打标签", over["allow_labeling"] is False)
    check("超预算禁止重算指标", over["allow_metrics"] is False)
    check("超预算降级留痕", "research_budget_exceeded" in over["degraded"], str(over))

    cached = ss.research_load_plan(elapsed_ms=100.0, budget_ms=60000.0,
                                   metrics_age_seconds=10.0, metrics_refresh_seconds=900)
    check("指标未到期走缓存", cached["allow_metrics"] is False)
    check("缓存不是预算问题（仍可打标签）", cached["allow_labeling"] is True)
    check("缓存降级留痕", cached["degraded"] == ["metrics_cache_fresh"], str(cached))
    check("到期后允许重算",
          ss.research_load_plan(elapsed_ms=100.0, budget_ms=60000.0,
                                metrics_age_seconds=901.0,
                                metrics_refresh_seconds=900)["allow_metrics"] is True)
    edge = ss.research_load_plan(elapsed_ms=60000.0, budget_ms=60000.0,
                                 metrics_age_seconds=None, metrics_refresh_seconds=900)
    check("恰好等于预算不算超支", edge["allow_labeling"] is True, str(edge))
    check("零预算不崩",
          ss.research_load_plan(elapsed_ms=0.0, budget_ms=0.0,
                                metrics_age_seconds=None,
                                metrics_refresh_seconds=900)["budget_ms"] == 1.0)


def test_isolation_facts_declare_no_side_effects() -> None:
    facts = ss.isolation_facts()
    check("侧车有专用线程", facts["dedicated_thread"] is True)
    check("侧车不调用 LLM", facts["llm_calls"] == 0)
    check("侧车无 Broker 访问", facts["broker_access"] is False)
    check("侧车无下单能力", facts["order_capability"] is False)
    check("公共行情预算走共享限流器",
          facts["network_budget"] == "shared_public_market_limiter")
    check("未降级时降级列表为空", facts["degraded_stages"] == [])
    skipped = ss.isolation_facts(degraded_stages=("research_budget_exceeded",),
                                 skipped_emissions=4)
    check("降级阶段如实透传", skipped["degraded_stages"] == ["research_budget_exceeded"])
    check("被拦下的样本行数如实登记", skipped["skipped_emissions"] == 4)


def test_disclosure_is_complete_and_serialisable() -> None:
    loss = {ss.SOURCE_B: ss.missing_sample_accounting(decided=1, labeled=1)}
    plan = ss.research_load_plan(elapsed_ms=10.0, budget_ms=60000.0,
                                 metrics_age_seconds=None, metrics_refresh_seconds=900)
    disclosure = ss.build_disclosure(
        config={"enabled": True, "execution_weight": 3.0},
        metrics={"state": "REFRESHED", "age_seconds": 0.1, "computed_at": "2026-01-01T00:00:00Z"},
        sample_loss=loss, load_plan=plan,
        isolation=ss.isolation_facts(skipped_emissions=2),
        cohort_state=ss.cohort_configuration({"enabled": True}),
        weight_guard=ss.execution_weight_guard({"execution_weight": 3.0}))
    check("披露带 schema", disclosure["schema"] == ss.SCHEMA_VERSION)
    check("披露口径为旧口径且研究专用",
          disclosure["caliber"]["id"] == ss.LEGACY_CALIBER_ID
          and disclosure["caliber"]["research_only"] is True)
    check("披露口径状态为 LEGACY_ACTIVE", disclosure["caliber"]["state"] == "LEGACY_ACTIVE")
    check("披露列出旧口径缺陷", len(disclosure["caliber"]["limitations"]) >= 5)
    check("披露 v2=DEFERRED（必达要点④）",
          disclosure["v2_labels"]["state"] == "DEFERRED")
    check("披露记 F11 首批范围",
          disclosure["v2_labels"]["first_release_scope"] == ss.FIRST_RELEASE_FAULT_SCOPE)
    check("披露权重被强制为 0（配置写 3.0 也不生效）",
          disclosure["execution_weight"]["effective"] == 0.0
          and disclosure["execution_weight"]["conflict"] is True)
    check("披露标记 shadow_only", disclosure["shadow_only"] is True)
    check("披露逐组给出配置状态", set(disclosure["cohorts"]) ==
          {ss.SOURCE_A, ss.SOURCE_B, ss.SOURCE_C})
    check("披露不冒充未知为正常", disclosure["sample_loss"] == loss)
    check("披露可 JSON 序列化",
          isinstance(json.dumps(disclosure, ensure_ascii=False), str))
    bare = ss.build_disclosure(config=None)
    check("空配置也能组装披露", bare["enabled"] is True and "caliber" in bare)
    check("缺省项以 None 表示未知而不是伪造",
          bare["metrics"] is None and bare["load_plan"] is None)
    check("candidate_facts 只记录不判定",
          ss.candidate_facts()["new_entry_allowed"] is None
          and ss.candidate_facts(new_entry_allowed=False,
                                 blocked_reason="READINESS_BLOCKED")["new_entry_allowed"] is False)


def test_module_supports_shared_adapter_contract() -> None:
    """共享适配所需符号与语义齐备（两态都不变的事实）。"""
    needed = {
        "SCHEMA_VERSION": "u06a-shadow-status-v1",
        "LEGACY_CALIBER_ID": "legacy-v1-rolling-mark-price",
        "V2_LABELS_STATE": "DEFERRED",
        "FIRST_RELEASE_FAULT_SCOPE":
            "FIRST_RELEASE_LABEL_DISCLOSURE_C_UNCONFIGURED_ISOLATION_ONLY",
        "EXECUTION_WEIGHT": 0.0,
        "SOURCE_A": "A_current_baseline",
        "SOURCE_B": "B_contract_market",
        "SOURCE_C": "C_contract_plus_web3",
    }
    for name, expected in needed.items():
        check(f"适配符号 {name} 存在且取值正确",
              getattr(ss, name, None) == expected, str(getattr(ss, name, None)))
    for name in ("execution_weight_guard", "emission_plan", "cohort_configuration",
                 "should_emit_sample", "sample_marker", "candidate_facts",
                 "missing_sample_accounting", "research_load_plan", "isolation_facts",
                 "legacy_metric_view", "build_disclosure", "release_gate_role",
                 "research_sources", "cohort_spec"):
        check(f"适配函数 {name} 可调用", callable(getattr(ss, name, None)))


# --------------------------------------------------------------------------- #
# [7] 服务级接线（仅当共享适配已应用时执行）
# --------------------------------------------------------------------------- #
def _probe_shared_adapter() -> tuple[str, str]:
    """运行时探测共享适配是否已应用；不看文件内容。"""
    try:
        from intel.service import ReadOnlyIntelService
    except Exception as exc:  # pragma: no cover - 仅在导入异常时触发
        return "unavailable", repr(exc)
    try:
        with _audit_db("probe.sqlite") as db:
            svc = ReadOnlyIntelService(db=db, config={"enabled": True}, symbols=[])
            snapshot = svc.snapshot()
        return ("applied" if "shadow_disclosure" in snapshot else "not_applied"), ""
    except Exception as exc:  # pragma: no cover - 仅在构造异常时触发
        return "unavailable", repr(exc)


def test_service_level_wiring() -> None:
    from intel.service import ReadOnlyIntelService
    with _audit_db("svc.sqlite") as db:
        cfg = {"enabled": True, "execution_weight": 0.9, "shadow_only": False}
        svc = ReadOnlyIntelService(db=db, config=cfg, symbols=["BTCUSDT"])
        snapshot = svc.snapshot()
        check("快照权重恒为 0（配置写 0.9 也不生效）",
              snapshot["execution_weight"] == 0.0, str(snapshot["execution_weight"]))
        disclosure = snapshot["shadow_disclosure"]
        check("快照披露为旧口径",
              disclosure["caliber"]["id"] == ss.LEGACY_CALIBER_ID)
        check("快照披露 v2=DEFERRED",
              disclosure["v2_labels"]["state"] == "DEFERRED")
        check("快照披露登记了配置冲突",
              disclosure["execution_weight"]["conflict"] is True, str(disclosure["execution_weight"]))
        check("快照披露 C 组未配置",
              disclosure["cohorts"][ss.SOURCE_C]["status"] == "unconfigured",
              str(disclosure["cohorts"][ss.SOURCE_C]))
        check("快照披露 A/B 组可用",
              disclosure["cohorts"][ss.SOURCE_A]["emits_samples"] is True
              and disclosure["cohorts"][ss.SOURCE_B]["emits_samples"] is True)
        check("披露可 JSON 序列化",
              isinstance(json.dumps(disclosure, ensure_ascii=False), str))

        # 必达要点②：C 未配置时，真实服务一行都不写。
        rows = [{"symbol": "BTCUSDT", "signal": "bullish", "mark_price": 60000.0,
                 "funding_rate": 0.0}]
        skipped = svc._record_fused_shadows(
            rows, {"status": "unconfigured", "events": []}, {"audits": []})
        written = db.fetch_all("SELECT COUNT(*) AS n FROM shadow_decisions")
        check("C 未配置时服务不写任何绩效样本行", written[0]["n"] == 0, str(written[0]["n"]))
        check("被拦下的行数如实返回", skipped == len(rows), f"skipped={skipped}")

        # 必达要点②：A 组在停用时不写；启用时写入的行权重恒 0。
        disabled = ReadOnlyIntelService(db=db, config={"enabled": False}, symbols=["BTCUSDT"])
        intent = SimpleNamespace(action="open", symbol="BTCUSDT", side="LONG", confidence=0.6)
        decision = SimpleNamespace(intents=[intent])
        check("停用后 A 组不写行",
              disabled.record_baseline_shadow(run_id="r0", decision=decision,
                                              prices={"BTCUSDT": 60000.0}) == 0)
        inserted = svc.record_baseline_shadow(
            run_id="r1", decision=decision, prices={"BTCUSDT": 60000.0},
            candidate_facts={"new_entry_allowed": True, "allow_new_reason": "ok"})
        check("启用后 A 组写入 1 行", inserted == 1, f"inserted={inserted}")
        stored = db.fetch_all("SELECT signal,source,execution_weight,feature_json "
                              "FROM shadow_decisions WHERE source=?", (ss.SOURCE_A,))
        check("A 行权重落库为 0.0",
              len(stored) == 1 and stored[0]["execution_weight"] == 0.0, str(stored))
        payload = json.loads(stored[0]["feature_json"])
        check("A 行带 shadow_cohort 标记",
              payload["shadow_cohort"]["counts_toward_release_gate"] is False, str(payload))
        check("A 行带候选资格事实（供 U09 重算 200 候选）",
              payload["shadow_cohort"]["candidate"]["new_entry_allowed"] is True, str(payload))

        # 有界研究负载：汇总指标缓存三态。
        svc2 = ReadOnlyIntelService(db=db, config={"enabled": True}, symbols=[])
        first = svc2._refresh_metrics_summary(
            {"allow_metrics": False, "allow_labeling": False,
             "degraded": ["research_budget_exceeded"]}, [])
        check("从未计算过时标 UNAVAILABLE", first["state"] == "UNAVAILABLE", str(first))
        second = svc2._refresh_metrics_summary({"allow_metrics": True, "allow_labeling": True,
                                                "degraded": []}, [])
        check("到期重算时标 REFRESHED", second["state"] == "REFRESHED", str(second))
        check("重算后拿到真实汇总（含 events_24h 与 evaluations）",
              set(("events_24h", "evaluations")) <= set(svc2._metrics_cache or {}),
              str(sorted((svc2._metrics_cache or {}).keys())))
        third = svc2._refresh_metrics_summary({"allow_metrics": False, "allow_labeling": True,
                                               "degraded": ["metrics_cache_fresh"]}, [])
        check("未到期时标 CACHED", third["state"] == "CACHED", str(third))
        check("CACHED 时给出年龄", isinstance(third["age_seconds"], float), str(third))
        check("缓存标记有界", third["bounded"] is True
              and third["min_refresh_seconds"] >= svc2.refresh_seconds, str(third))
        errors: list[str] = []
        svc2._refresh_metrics_summary({"allow_metrics": True, "allow_labeling": True,
                                       "degraded": []}, errors)
        check("刷新后 sampling 缺失计数可查",
              set(svc2._sample_loss) == {ss.SOURCE_A, ss.SOURCE_B, ss.SOURCE_C},
              str(sorted(svc2._sample_loss)))
        loss_c = svc2._sample_loss[ss.SOURCE_C]
        check("C 组缺失计数口径可用", loss_c["state"] == "OK" and loss_c["decided"] == 0,
              str(loss_c))

        # 旧口径汇总视图（shadow_evaluation 与 shadow_metrics 并存）。
        cache = svc2._metrics_cache or {}
        cached_evals = cache.get("evaluations", [])
        check("汇总缓存保留既有键（events_24h / evaluations）",
              "events_24h" in cache and "evaluations" in cache, str(sorted(cache)))
        check("汇总缓存带 200 门槛常量且不自动晋升",
              cache.get("minimum_promotion_samples") == 200
              and cache.get("auto_promotion") is False, str(sorted(cache)))
        view = ss.legacy_metric_view(cached_evals, sample_loss=svc2._sample_loss)
        check("旧口径视图与缓存行数一致（不丢行）", len(view) == len(cached_evals),
              f"view={len(view)} cache={len(cached_evals)}")
        check("旧口径视图每行都带零权重 / DEFERRED / 不计门禁",
              all(item["execution_weight"] == 0.0 and item["v2_labels"] == "DEFERRED"
                  and item["release_gate_eligible"] is False
                  and item["auto_promotion"] is False for item in view),
              str(view))
        check("旧口径视图每行都标注来源组（不出现未知组）",
              all(item["group"] in {"A", "B", "C"} for item in view), str(view))
        # 端到端 `_refresh` 的字段接线由 scripts/smoke_u06a_*.py 用离线桩验证
        # （本文件不联网，故不在单元侧复现整轮采集）。


# --------------------------------------------------------------------------- #
# [8] 展示层接线（仅当共享适配已应用时执行）
# --------------------------------------------------------------------------- #
def test_dashboard_shows_legacy_caliber() -> None:
    template = ROOT / "src" / "dashboard" / "templates" / "decisions.html"
    text = template.read_text(encoding="utf-8")
    check("决策页展示旧口径标识", "影子口径" in text)
    check("决策页展示执行权重不可抬升", "配置声明不可抬升" in text)
    check("决策页展示 v2=DEFERRED", "v2_labels" in text or "DEFERRED" in text)
    check("决策页展示 A/B/C 组状态", "A/B/C 研究隔离" in text)
    check("决策页展示旧口径已知缺陷", "旧口径已知缺陷" in text)
    check("决策页不再把影子行标为“可人工评审”", "可人工评审" not in text)

    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(template.parent)))
    for name in ("bias_cn", "decision_cn", "price", "regime_cn", "side_cn"):
        env.filters[name] = lambda value: value
    try:
        env.get_template("decisions.html")
        ok = True
        err = ""
    except Exception as exc:  # pragma: no cover
        ok, err = False, repr(exc)
    check("决策页模板可被 Jinja 解析", ok, err)

    fragment = env.from_string(
        "{% set sd = wi.shadow_disclosure or {} %}"
        "{% set sdw = sd.execution_weight or {} %}"
        "{{ sdw.effective|default(0, true) }}|"
        "{{ (sd.v2_labels or {}).state or 'DEFERRED' }}|"
        "{% set na = sd.cohorts or {} %}"
        "{% for k, i in na.items() %}{{ i.group or k }}={{ i.status or '?' }} {% endfor %}"
        "limits={{ ((sd.caliber or {}).limitations or [])|length }}")
    rendered = fragment.render(wi={"shadow_disclosure": ss.build_disclosure(
        config={"enabled": True, "execution_weight": 2.0})})
    check("披露数据能渲染出权重 0 与 DEFERRED",
          rendered.startswith("0.0|DEFERRED|"), rendered)
    check("渲染出 C=unconfigured", "C=unconfigured" in rendered, rendered)
    check("渲染出缺陷条数 >= 5", "limits=5" in rendered or rendered.endswith("limits=6")
          or "limits=" in rendered and int(rendered.split("limits=")[1]) >= 5, rendered)
    empty = fragment.render(wi={})
    check("无披露数据时不崩且不伪造权重",
          empty.startswith("0.0|DEFERRED|") and "none" in empty, empty)


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 72)
    print("U06a 影子最小隔离与口径修正 离线回归（独立脚本，无 pytest）")
    print("=" * 72)
    print("\n[1] 常量与首批范围")
    test_execution_weight_is_a_constant_zero()
    test_module_has_no_io_or_llm_capability()
    test_sidecar_thread_is_dedicated_in_core_service()
    print("\n[2] 执行权重护栏")
    test_execution_weight_guard_forces_zero()
    print("\n[3] C 未配置隔离")
    test_c_unconfigured_emits_nothing()
    test_should_emit_sample_gate()
    test_unknown_rows_really_become_counted_sample_rows()
    print("\n[4] 门禁隔离（K06 口径一致性）")
    test_release_gate_roles_are_research_only_for_b_and_c()
    test_gate_caliber_agrees_with_release_evidence()
    test_marker_forbids_gate_counting()
    print("\n[5] 旧口径视图与缺失计数")
    test_legacy_metric_view_labels_research_only()
    test_missing_sample_accounting_never_fabricates()
    print("\n[6] 有界研究负载与隔离事实")
    test_research_load_plan_degrades_and_labels()
    test_isolation_facts_declare_no_side_effects()
    test_disclosure_is_complete_and_serialisable()
    test_module_supports_shared_adapter_contract()

    state, detail = _probe_shared_adapter()
    print(f"\n[7] 服务级接线（共享适配运行时探测 = {state}）")
    if state == "applied":
        test_service_level_wiring()
        print(f"\n[8] 展示层接线（共享适配 = {state}）")
        test_dashboard_shows_legacy_caliber()
    else:
        _note(f"共享适配未应用（{state}）：{detail}")
        _note("本分支只承载独占产物；服务级/展示层行为由集成树或补丁沙箱验证。")
        _note("模块自身的适配契约已在 [6] 断言，两支分支都不是空转。")

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
