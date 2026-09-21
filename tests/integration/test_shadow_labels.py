"""U06b 影子 v2 固定时点标签（K01 / K02 / F09 / F11 FULL_V2_LABELS / F17 / DL08）。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：不联网、不读 ``.env``/``data/``、不构造 Broker、不启停服务、不改风险档。

**两态可跑**：本套件只依赖
- U06b 独占模块 ``src/intel/shadow_v2/``（本任务新增，两态都在），
- 已集成 ``src/audit/database.py`` / ``src/intel/service.py``（**共享适配补丁**，
  按 ``U06B_INTEGRATION`` 标记探测是否已应用，不读补丁文件内容判定），
- 已冻结证据 ``src/intel/shadow_status.py``（U06a）与
  ``src/core/release_evidence.py``（U09，只读其公开 API）。

补丁不在 → 断言本体已提供共享适配所需的**全部符号与语义**（不让分支空转），
并把"未接线时门禁不受影响"作为事实记下；补丁在 → 追加落库/只读状态断言。

期望值来源（**禁止**复制被测实现输出自证）：
- 任务卡 U06b 交付要点：固定目标时点取价、超容差 MISSING、独立全白名单采样与
  实验版本隔离、无未来信息回填；
- F11 反例「5 分钟标签晚 8 分钟」（迟 480s）；
- F17 时钟回拨不得延长授权/不得按错误时点出标签；
- ``shadow_status.LEGACY_LIMITATIONS`` 第 2 条：legacy v1 无法区分
  未成熟/缺价/超容差/断流——缺失原因标签属 U06b。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from audit import database as database_module  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core import release_evidence as evidence  # noqa: E402
from intel import shadow_status as ss  # noqa: E402
from intel import shadow_v2 as v2  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []

TMP = Path(tempfile.mkdtemp(prefix="u06b-labels-"))
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shadow" / "labels"

#: 两态探测：只按共享补丁自报的标记判断，不猜、不读补丁文本。
DB_PATCHED = bool(getattr(database_module, "U06B_INTEGRATION", ""))
try:  # pragma: no cover - 导入失败只说明依赖缺失，按未接线处理
    from intel.service import ReadOnlyIntelService  # noqa: E402

    SERVICE_PATCHED = hasattr(ReadOnlyIntelService, "shadow_v2_status")
except Exception:  # noqa: BLE001
    ReadOnlyIntelService = None  # type: ignore[assignment]
    SERVICE_PATCHED = False


def check(name: str, condition: bool, extra: Any = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {extra!r}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def note(text: str) -> None:
    print(f"  ----  {text}")


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def minute(moment: datetime) -> str:
    return moment.replace(second=0, microsecond=0).isoformat(timespec="minutes")


# --------------------------------------------------------------------------- #
# 夹具：合成时间基准（不含真实日期语义，只做相对运算）
# --------------------------------------------------------------------------- #
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
HORIZON = 5
TARGET = T0 + timedelta(minutes=HORIZON)
#: F11 反例：5 分钟标签晚 8 分钟 = 迟 480 秒，远超 90 秒容差。
F11_LATE_SECONDS = 480


def sample(symbol: str, offset_seconds: float, price: float) -> v2.PriceSample:
    return v2.PriceSample(symbol, T0 + timedelta(seconds=offset_seconds), price)


def request(decision_id: str = "d-1", *, symbol: str = "BTCUSDT",
            signal: str = "bullish", source: str = ss.SOURCE_B,
            version: str = "v2-experiment-1", horizon: int = HORIZON,
            reference: float = 100.0, funding: float = 0.0) -> v2.LabelRequest:
    return v2.LabelRequest(decision_id, source, symbol, signal, reference, T0, horizon,
                           experiment_version=version, release_id="rel-u06b",
                           config_fingerprint="cfg-u06b", funding_rate=funding)


def labeled(request_, samples, *, now_offset_minutes: float = HORIZON + 5, **kwargs) -> v2.LabelResult:
    return v2.label_request(request_, samples,
                            now=T0 + timedelta(minutes=now_offset_minutes), **kwargs)


# --------------------------------------------------------------------------- #
# 1. 口径分离与门禁隔离（不得把 v2 冒充 legacy，不得进门禁）
# --------------------------------------------------------------------------- #
def test_caliber_separation() -> None:
    section("1. 口径分离 / 首批 F11 记录不变 / 门禁隔离")
    check("v2 口径 id 与 legacy v1 口径 id 不同（两套口径不可能混读）",
          v2.V2_CALIBER_ID != ss.LEGACY_CALIBER_ID,
          (v2.V2_CALIBER_ID, ss.LEGACY_CALIBER_ID))
    check("v2 口径 id 未被硬编码为 legacy 口径名",
          "legacy" not in v2.V2_CALIBER_ID and "v2" in v2.V2_CALIBER_ID,
          v2.V2_CALIBER_ID)
    check("legacy 口径 id 逐字取自 shadow_status（引用而非复制）",
          v2.LEGACY_CALIBER_ID == ss.LEGACY_CALIBER_ID)
    check("首批 v2 标签状态仍记录为 DEFERRED（本任务不伪称已通过）",
          ss.V2_LABELS_STATE == "DEFERRED"
          and v2.FIRST_RELEASE_F11_STATE_UNCHANGED == "DEFERRED",
          (ss.V2_LABELS_STATE, v2.FIRST_RELEASE_F11_STATE_UNCHANGED))
    check("v2 结果恒不计入发布门禁",
          v2.COUNTS_TOWARD_RELEASE_GATE is False
          and v2.gate_isolation_facts()["counts_toward_release_gate"] is False)
    check("v2 结果执行权重恒为 0 且不自动晋升",
          v2.gate_isolation_facts()["execution_weight"] == 0.0
          and v2.gate_isolation_facts()["auto_promotion"] is False)
    check("共享影子模块的组定义未被本任务改动（A 仍为 execution_decision_copy）",
          ss.release_gate_role(ss.SOURCE_A) == ss.GATE_ROLE_EXECUTION_DECISION
          and ss.release_gate_role(ss.SOURCE_B) == ss.GATE_ROLE_RESEARCH_ONLY
          and ss.release_gate_role(ss.SOURCE_C) == ss.GATE_ROLE_RESEARCH_ONLY)
    facts = v2.gate_isolation_facts()
    check("隔离事实里逐组门禁角色与 shadow_status.COHORTS 逐字一致",
          facts["gate_role_by_source"] == {spec.source: spec.gate_role for spec in ss.COHORTS})


def test_runtime_status_default_disabled() -> None:
    section("2. 后置线默认不启用（代码存在不等于改变运行行为）")
    status = v2.runtime_status(None)
    check("默认 enabled=False", status["enabled"] is False)
    check("SHADOW_V2_ENABLED_BY_DEFAULT 为 False", v2.SHADOW_V2_ENABLED_BY_DEFAULT is False)
    check("显式开启也只影响 enabled，不影响门禁与权重",
          v2.runtime_status({"shadow_v2": {"enabled": True}})["counts_toward_release_gate"] is False
          and v2.runtime_status({"shadow_v2": {"enabled": True}})["execution_weight"] == 0.0)
    check("状态里仍如实登记首批 F11 为 DEFERRED",
          status["first_release_v2_labels"] == "DEFERRED")


# --------------------------------------------------------------------------- #
# 3. 固定目标时点取价（F11 核心）
# --------------------------------------------------------------------------- #
def test_fixed_target_time() -> None:
    section("3. 固定目标时点取价")
    req = request()
    check("目标时刻 = 决策时刻 + horizon（不是最近的刷新轮）",
          req.target_at == TARGET, req.target_at)
    check("窗口以目标时刻为中心、左右等长",
          v2.window_bounds(TARGET, 90.0)
          == (TARGET - timedelta(seconds=90), TARGET + timedelta(seconds=90)))
    check("容差被夹到有效上界（超大容差不得重新引入 legacy 缺陷）",
          v2.clamp_tolerance(99999) == v2.MAX_TOLERANCE_SECONDS
          and v2.clamp_tolerance(0) > 0 and v2.clamp_tolerance(-5) > 0)

    exact = labeled(req, [sample("BTCUSDT", HORIZON * 60, 101.0)])
    check("恰在目标时刻的价 → LABELED 且偏差 0",
          exact.state == v2.STATE_LABELED and exact.deviation_seconds == 0.0,
          (exact.state, exact.deviation_seconds))

    near = labeled(req, [sample("BTCUSDT", HORIZON * 60 + 60, 101.0)])
    check("迟到 60s（< 90s 容差）→ 仍 LABELED，偏差如实记录",
          near.state == v2.STATE_LABELED and near.deviation_seconds == 60.0,
          (near.state, near.deviation_seconds))

    # F11 反例：5 分钟标签晚 8 分钟（now 足够晚，确保该样本已真实到达）。
    late = labeled(req, [sample("BTCUSDT", HORIZON * 60 + F11_LATE_SECONDS, 101.0)],
                   now_offset_minutes=15.0)
    check("F11 反例：5 分钟标签晚 8 分钟 → 超容差 MISSING（不得当合格标签）",
          late.state == v2.STATE_MISSING
          and late.missing_reason == v2.REASON_OVER_TOLERANCE,
          (late.state, late.missing_reason))
    check("超容差行不带任何收益数字（不得用错时点算出数字）",
          late.net_return_pct is None and late.gross_return_pct is None
          and late.exit_price is None)

    earlier = labeled(req, [sample("BTCUSDT", HORIZON * 60 - 30, 99.0)])
    check("略早于目标时刻（-30s）→ LABELED，偏差为负",
          earlier.state == v2.STATE_LABELED and earlier.deviation_seconds == -30.0,
          (earlier.state, earlier.deviation_seconds))

    many = [sample("BTCUSDT", HORIZON * 60 - 80, 98.0),
            sample("BTCUSDT", HORIZON * 60 + 10, 102.0),
            sample("BTCUSDT", HORIZON * 60 + 70, 103.0)]
    chosen = labeled(req, many)
    check("窗口内多个样本时取离目标时刻最近的那条",
          chosen.observed_at == T0 + timedelta(seconds=HORIZON * 60 + 10),
          chosen.observed_at)


# --------------------------------------------------------------------------- #
# 4. 缺失原因标签（legacy v1 无法区分 → U06b 必须逐条给出）
# --------------------------------------------------------------------------- #
def test_missing_reasons() -> None:
    section("4. 缺失原因标签（未成熟 / 缺价 / 超容差 / 断流）")
    req = request()

    pending = v2.label_request(req, [sample("BTCUSDT", HORIZON * 60, 101.0)],
                               now=TARGET + timedelta(seconds=10))
    check("容差窗口未关闭 → PENDING / NOT_MATURED（不得提前用未来价回填）",
          pending.state == v2.STATE_PENDING and pending.missing_reason == v2.REASON_NOT_MATURED,
          (pending.state, pending.missing_reason))

    no_price = labeled(req, [])
    check("窗口内一条样本都没有（窗口已关闭）→ NO_PRICE",
          no_price.missing_reason == v2.REASON_NO_PRICE, no_price.missing_reason)

    # 断流：目标前后都有样本，但中间空洞（700s）超过一个采样周期（300s）。
    gap = labeled(req, [sample("BTCUSDT", HORIZON * 60 - 500, 100.0),
                        sample("BTCUSDT", HORIZON * 60 + 200, 101.0)])
    check("目标两侧都有样本但中间是长空洞 → DATA_GAP（断流，不是缺价）",
          gap.missing_reason == v2.REASON_DATA_GAP, gap.missing_reason)

    # 只有目标之后（或之前）有样本且都超容差 → OVER_TOLERANCE，不是断流。
    only_after = labeled(req, [sample("BTCUSDT", HORIZON * 60 + 200, 101.0)])
    check("只有单侧样本且超容差 → OVER_TOLERANCE（与断流可区分）",
          only_after.missing_reason == v2.REASON_OVER_TOLERANCE, only_after.missing_reason)

    other_symbol = labeled(req, [sample("ETHUSDT", HORIZON * 60, 101.0)])
    check("别的标的的价不参与本标的标签（不得串样本）",
          other_symbol.missing_reason == v2.REASON_NO_PRICE)

    untrusted = v2.ClockVerdict(v2.CLOCK_UNTRUSTED, ("wall_rolled_back",))
    clock_missing = labeled(req, [sample("BTCUSDT", HORIZON * 60, 101.0)],
                            clock_verdict=untrusted)
    check("时钟不可信 → CLOCK_UNTRUSTED，即使窗口内有合格价也不出标签",
          clock_missing.state == v2.STATE_MISSING
          and clock_missing.missing_reason == v2.REASON_CLOCK_UNTRUSTED,
          (clock_missing.state, clock_missing.missing_reason))

    reasons = {v2.REASON_NOT_MATURED, v2.REASON_NO_PRICE, v2.REASON_OVER_TOLERANCE,
               v2.REASON_DATA_GAP, v2.REASON_CLOCK_UNTRUSTED}
    check("四种缺失原因 + 未成熟全部有稳定常量（对应 shadow_status 披露的缺口分类）",
          reasons == set(v2.MISSING_REASONS), v2.MISSING_REASONS)
    check("已关闭窗口的真实损失原因集合与未成熟分开",
          v2.REASON_NOT_MATURED not in v2.CLOSED_WINDOW_REASONS
          and set(v2.CLOSED_WINDOW_REASONS) <= set(v2.MISSING_REASONS))

    # 相对性质：legacy 口径只能给缺失总数，v2 必须能逐原因区分（缺口已闭合）。
    legacy = ss.missing_sample_accounting(decided=4, labeled=1)
    coverage = v2.coverage_report([no_price, gap, only_after, pending])
    check("legacy 口径只给缺失总数（无逐原因字段）",
          "by_reason" not in legacy and legacy["missing"] == 3, legacy)
    check("v2 口径逐原因计数且缺失总数与 legacy 同量纲（decided-missing 口径可比）",
          set(coverage["by_reason"]) == {v2.REASON_NO_PRICE, v2.REASON_DATA_GAP,
                                         v2.REASON_OVER_TOLERANCE, v2.REASON_NOT_MATURED}
          and coverage["missing"] == 3 and coverage["pending"] == 1, coverage)


def test_coverage_report() -> None:
    section("5. 覆盖率勾稽（未成熟不计入损失）")
    req = request()
    results = [
        labeled(req, [sample("BTCUSDT", HORIZON * 60, 101.0)]),
        labeled(req, []),
        v2.label_request(req, [], now=TARGET),
    ]
    report = v2.coverage_report(results)
    check("decided = 全部请求数", report["decided"] == 3, report["decided"])
    check("labeled + pending + missing = decided（无静默丢弃）",
          report["labeled"] + report["pending"] + report["missing"] == report["decided"],
          report)
    check("unexplained 恒为 0（缺失总数 = 逐原因之和）", report["unexplained"] == 0, report)
    check("未成熟不计入 missing（未走完窗口不是损失）",
          report["pending"] == 1 and report["missing"] == 1, report)
    check("覆盖率分母排除未成熟窗口",
          report["coverage"] == 0.5, report["coverage"])
    check("覆盖率报告不带任何门禁结论字段（避免第二套 GO 判断器）",
          "promotion_ready" not in report and "go" not in {k.lower() for k in report})
    empty = v2.coverage_report([])
    check("空输入不编造覆盖率（None 而非 1.0）", empty["coverage"] is None and empty["decided"] == 0)


# --------------------------------------------------------------------------- #
# 6. 无未来信息回填
# --------------------------------------------------------------------------- #
def test_no_future_leakage() -> None:
    section("6. 无未来信息回填")
    try:
        v2.assert_no_future([sample("BTCUSDT", 0, 100.0),
                             sample("BTCUSDT", HORIZON * 60 + 600, 100.0)], TARGET)
        leaked = False
    except v2.LeakageError:
        leaked = True
    check("截止时刻之后的价格被断言拒绝（LeakageError）", leaked)

    kept = v2.samples_before([sample("BTCUSDT", 600, 100.0),
                              sample("BTCUSDT", -60, 99.0),
                              sample("BTCUSDT", 300, 100.5)], TARGET)
    check("samples_before 只保留截止时刻（含）之前的观测且按时间升序",
          [item.price for item in kept] == [99.0, 100.5], [item.price for item in kept])
    check("截止时刻恰好等于样本时刻时仍保留（边界包含）",
          len(v2.samples_before([sample("BTCUSDT", 300, 100.5)], TARGET)) == 1)

    req = request()
    future = [sample("BTCUSDT", HORIZON * 60, 101.0),        # 退出价
              sample("BTCUSDT", HORIZON * 60 + 600, 500.0),  # 未来暴涨：绝不许进路径
              sample("BTCUSDT", HORIZON * 60 - 60, 101.5)]   # 窗口内路径点
    result = labeled(req, future)
    check("路径只含 [决策时刻, 目标时刻] 的样本 + 入场 + 退出（未来样本被排除）",
          result.path_samples == 3, result.path_samples)
    check("MFE/MAE 不被未来价推高（否则会算出偏乐观的路径极值）",
          result.mfe_pct is not None and result.mfe_pct < 10.0, result.mfe_pct)

    # 未来样本的存在不得改变标签本身。
    without_future = labeled(req, [sample("BTCUSDT", HORIZON * 60, 101.0),
                                   sample("BTCUSDT", HORIZON * 60 - 60, 101.5)])
    check("同一决策加不加未来样本，标签逐字相同（无前视依赖）",
          {k: result.to_dict()[k] for k in ("state", "exit_price", "net_return_pct", "correct")}
          == {k: without_future.to_dict()[k]
             for k in ("state", "exit_price", "net_return_pct", "correct")})


def test_replay_determinism() -> None:
    section("7. 可重放（同输入必得同输出）")
    req = request()
    samples = [sample("BTCUSDT", HORIZON * 60 - 10, 100.5)]
    first = labeled(req, samples).to_dict()
    second = labeled(req, samples).to_dict()
    check("同一 (请求, 样本) 两次标签逐字段相同", first == second)
    check("落库行口径标记正确（caliber / 不进门禁 / 组别）",
          first["caliber"] == v2.V2_CALIBER_ID
          and first["counts_toward_release_gate"] is False
          and first["group"] == "B")


# --------------------------------------------------------------------------- #
# 8. 独立全白名单采样 + 实验版本隔离
# --------------------------------------------------------------------------- #
def test_sampling_plan() -> None:
    section("8. 独立全白名单采样（有界、不按轮转裁剪）")
    whitelist = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    reqs = [request("d-btc", symbol="BTCUSDT")]
    plan = v2.build_sampling_plan(reqs, whitelist=whitelist)
    check("计划覆盖全白名单（含本轮没有任何决策的标的）",
          set(plan.symbols) == set(whitelist)
          and {item.symbol for item in plan.targets} == set(whitelist),
          plan.to_dict())
    check("每个目标时刻对每个白名单标的各有一个采样点",
          len(plan.targets) == len(whitelist) and plan.scope == "whitelist")
    check("计划里带上了产生该目标时刻的决策 id（可回溯）",
          [item.decision_ids for item in plan.targets if item.symbol == "BTCUSDT"]
          == [("d-btc",)],
          plan.targets[0].decision_ids)

    bounded = v2.build_sampling_plan(reqs, whitelist=whitelist, max_targets=1)
    check("超预算时计数披露而不静默丢弃",
          len(bounded.targets) == 1 and bounded.skipped == 2
          and bounded.skipped_by_reason.get("budget_exceeded") == 2,
          bounded.to_dict())

    plan_a = v2.build_sampling_plan(reqs, whitelist=whitelist)
    plan_b = v2.build_sampling_plan(reqs, whitelist=whitelist)
    check("同输入计划顺序稳定（可重放）", plan_a.pairs() == plan_b.pairs())

    outside = v2.label_batch([request("d-eth", symbol="ETHUSDT", horizon=15)],
                             [sample("ETHUSDT", 15 * 60, 101.0)],
                             now=T0 + timedelta(minutes=20),
                             plan=v2.build_sampling_plan(reqs, whitelist=whitelist))
    check("计划外的请求按缺价留痕（不静默消失，分母可比）",
          outside[0].missing_reason == v2.REASON_NO_PRICE, outside[0].missing_reason)


def test_experiment_isolation() -> None:
    section("9. 实验版本/发布/配置隔离（DL08）")
    identity_a = v2.ExperimentIdentity("v2-experiment-1", "rel-u06b", "cfg-u06b", HORIZON)
    identity_b = v2.ExperimentIdentity("v2-experiment-2", "rel-u06b", "cfg-u06b", HORIZON)
    identity_c = v2.ExperimentIdentity("v2-experiment-1", "rel-other", "cfg-u06b", HORIZON)
    identity_d = v2.ExperimentIdentity("v2-experiment-1", "rel-u06b", "cfg-u06b", 15)
    keys = {item.key() for item in (identity_a, identity_b, identity_c, identity_d)}
    check("四段键任一不同即为不同隔离键（版本/发布/配置/视界）", len(keys) == 4, keys)
    result_a = labeled(request(version="v2-experiment-1"),
                       [sample("BTCUSDT", HORIZON * 60, 101.0)]).to_dict()
    result_b = labeled(request(version="v2-experiment-2"),
                       [sample("BTCUSDT", HORIZON * 60, 101.0)]).to_dict()
    check("不同实验版本的同一决策产出不同 experiment_key（不得混算）",
          result_a["experiment_key"] != result_b["experiment_key"])
    check("行内同时保留隔离键三段，便于按版本复现",
          {"experiment_version", "release_id", "config_fingerprint"} <= set(result_a))


def test_clock_guard() -> None:
    section("10. 时钟可信度（F17：回拨/跳变不得按错误时点出标签）")
    previous = v2.ClockSnapshot(T0, 1000.0, boot_id="boot-1")
    stable = v2.clock_guard(previous, v2.ClockSnapshot(T0 + timedelta(minutes=5), 1300.0,
                                                       boot_id="boot-1"))
    check("正常前进 → STABLE", stable.trusted and stable.state == v2.CLOCK_STABLE)
    check("首次快照（无前值）→ STABLE（不因缺基线卡死）",
          v2.clock_guard(None, previous).trusted)

    rolled = v2.clock_guard(previous, v2.ClockSnapshot(T0 - timedelta(minutes=3), 1400.0,
                                                       boot_id="boot-1"))
    check("墙钟回拨 → UNTRUSTED 且给出原因", not rolled.trusted
          and "wall_rolled_back" in rolled.issues, rolled)
    regressed = v2.clock_guard(previous, v2.ClockSnapshot(T0 + timedelta(minutes=4), 990.0,
                                                          boot_id="boot-1"))
    check("monotonic 倒退 → UNTRUSTED", not regressed.trusted
          and "monotonic_regressed" in regressed.issues, regressed)
    rebooted = v2.clock_guard(previous, v2.ClockSnapshot(T0 + timedelta(minutes=1), 1.0,
                                                         boot_id="boot-2"))
    check("boot id 变化 → UNTRUSTED（重启不当作连续观察）",
          "boot_id_changed" in rebooted.issues, rebooted)

    # 判定是相对的：同一组快照换一组「正常」基线，结论只依赖增量而非绝对日期。
    late_previous = v2.ClockSnapshot(T0 + timedelta(days=400), 1000.0, boot_id="b")
    late_current = v2.ClockSnapshot(T0 + timedelta(days=400, minutes=5), 1300.0, boot_id="b")
    check("时钟判定不依赖绝对日期（跨日/跨年同结论）",
          v2.clock_guard(late_previous, late_current).trusted)

    untrusted = v2.ClockVerdict(v2.CLOCK_UNTRUSTED, ("wall_rolled_back",))
    batch = v2.label_batch([request()], [sample("BTCUSDT", HORIZON * 60, 101.0)],
                           now=T0 + timedelta(minutes=HORIZON + 5), clock_verdict=untrusted)
    check("整批在时钟不可信时统一落缺失，不跳过、不出标签",
          all(item.missing_reason == v2.REASON_CLOCK_UNTRUSTED for item in batch))


# --------------------------------------------------------------------------- #
# 11. 离线夹具（禁止用被测实现自证）
# --------------------------------------------------------------------------- #
def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_sample(entry: dict[str, Any]) -> v2.PriceSample:
    return v2.PriceSample(entry["symbol"], T0 + timedelta(seconds=float(entry["offset_seconds"])),
                          float(entry["price"]))


def test_fixture_cases() -> None:
    section("11. 离线夹具（期望值来自 F11 反例与任务卡要点，不由实现产出）")
    data = _load_fixture("fixed_point_cases.json")
    check("夹具自述来源（derivation 覆盖任务卡要点，禁止自证）",
          all(case.get("derivation") for case in data["cases"]),
          [case["case_id"] for case in data["cases"]])
    for case in data["cases"]:
        req = request(case["case_id"], symbol=case["symbol"], signal=case["signal"])
        samples = [_fixture_sample(item) for item in case["samples"]]
        result = v2.label_request(req, samples,
                                  now=T0 + timedelta(minutes=float(case["now_offset_minutes"])),
                                  tolerance_seconds=float(case.get("tolerance_seconds", 90.0)),
                                  expected_interval_seconds=float(
                                      case.get("expected_interval_seconds", 300.0)))
        matched = (result.state == case["expect_state"]
                   and result.missing_reason == case["expect_missing_reason"])
        check(f"夹具 {case['case_id']}：{case['intent']}", matched,
              (result.state, result.missing_reason,
               case["expect_state"], case["expect_missing_reason"]))
        if case.get("expect_return_pct_delta") is not None:
            expect = float(case["reference_price"]) * (1 + float(case["expect_return_pct_delta"]) / 100.0)
            check(f"夹具 {case['case_id']}：退出价按独立算式核对",
                  result.exit_price is not None and abs(result.exit_price - expect) < 1e-9,
                  (result.exit_price, expect))

    plan_case = _load_fixture("whitelist_plan.json")
    reqs = [v2.LabelRequest(item["decision_id"], item["source"], item["symbol"],
                            item["signal"], 100.0,
                            T0 + timedelta(minutes=float(item["decided_offset_minutes"])),
                            int(item["horizon_minutes"])) for item in plan_case["requests"]]
    plan = v2.build_sampling_plan(reqs, whitelist=plan_case["whitelist"],
                                  max_targets=int(plan_case.get("max_targets", 20000)))
    check("夹具核对：计划标的集合 = 白名单全量（与决策标的无关）",
          set(plan.symbols) == set(plan_case["whitelist"]), plan.symbols)
    check("夹具核对：计划目标数 = 白名单 × 不同目标时刻数",
          len(plan.targets) == len(plan_case["whitelist"]) * len({item["horizon_minutes"]
                                                                  for item in plan_case["requests"]}),
          len(plan.targets))


# --------------------------------------------------------------------------- #
# 12. 共享适配（两态）
# --------------------------------------------------------------------------- #
def _new_db(name: str) -> tuple[AuditDatabase, Path]:
    path = TMP / name / "audit.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = AuditDatabase(path)
    db.set_release_context(release_id="rel-u06b", environment="demo",
                           account_ref="demo:acc")
    return db, path


def test_shared_adapter_two_state() -> None:
    section("12. 共享适配两态（database.py / service.py）")
    note(f"探测：DB_PATCHED={DB_PATCHED} SERVICE_PATCHED={SERVICE_PATCHED}")
    required_fields = {"decision_id", "horizon_minutes", "experiment_key", "target_at",
                       "observed_at", "state", "missing_reason", "source", "symbol",
                       "signal", "deviation_seconds", "tolerance_seconds",
                       "gross_return_pct", "net_return_pct", "correct", "mfe_pct",
                       "mae_pct", "counts_toward_release_gate", "caliber"}
    row = labeled(request(), [sample("BTCUSDT", HORIZON * 60, 101.0)]).to_dict()
    check("本体提供的落库行字段覆盖共享适配所消费的全部键（补丁不在时分支不空转）",
          required_fields <= set(row), sorted(required_fields - set(row)))
    check("本体提供集成层所需符号",
          all(callable(getattr(v2, name, None))
              for name in ("runtime_status", "label_request", "label_batch",
                           "coverage_report", "build_sampling_plan", "gate_isolation_facts")))

    db, path = _new_db("two-state")
    try:
        migrations_before = [r["version"] for r in db.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version")]
        if DB_PATCHED:
            inserted = db.log_shadow_v2_labels([row, {**row, "decision_id": "d-2"}])
            check("落库新增行数 = 输入行数（首次）", inserted == 2, inserted)
            check("重复写同一实验版本标签 → 0 新增（幂等，不覆盖历史标签）",
                  db.log_shadow_v2_labels([row]) == 0)
            forced = {**row, "decision_id": "d-3", "counts_toward_release_gate": True}
            db.log_shadow_v2_labels([forced])
            stored = db.fetch_shadow_v2_labels(experiment_key=row["experiment_key"])
            check("调用方传入 counts_toward_release_gate=True 被强制落为 0",
                  all(int(item["counts_toward_release_gate"]) == 0 for item in stored),
                  [item["counts_toward_release_gate"] for item in stored])
            check("落库行保留实验版本键，可按版本读取",
                  all(item["experiment_key"] == row["experiment_key"] for item in stored))
            coverage = db.shadow_v2_coverage(experiment_key=row["experiment_key"])
            check("库侧覆盖率逐状态计数且声明不进门禁",
                  coverage["decided"] == 3 and coverage["labeled"] == 3
                  and coverage["counts_toward_release_gate"] is False, coverage)
            check("库侧覆盖率不产出 GO/promotion 结论（避免第二套门禁判断器）",
                  "promotion_ready" not in coverage and "verdict" not in coverage)
            check("v2 标签写在与 legacy 不同的表（不污染 shadow_outcomes）",
                  db.fetch_one("SELECT COUNT(*) AS n FROM shadow_outcomes")["n"] == 0)
            other = db.shadow_v2_coverage(experiment_key="不存在-版本")
            check("按不存在的实验版本读 → 全零而不是拿别的版本凑数",
                  other["decided"] == 0 and other["coverage"] is None, other)
        else:
            note("共享补丁未应用：跳过落库断言（本体符号已在上面断言）")
            check("未接线时本体不含库侧方法（不假装已集成）",
                  not hasattr(db, "log_shadow_v2_labels"))
        migrations_after = [r["version"] for r in db.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version")]
        check("迁移版本集合在 v2 写入前后不变（不占用 schema_migrations 版本号）",
              migrations_before == migrations_after, migrations_after[-3:])
    finally:
        db.close()

    if SERVICE_PATCHED and ReadOnlyIntelService is not None:
        db, _ = _new_db("service")
        try:
            service = ReadOnlyIntelService(db=db, config={"enabled": True}, symbols=["BTCUSDT"])
            status = service.shadow_v2_status()
            check("服务侧只读状态：默认未启用、不进门禁、零权重",
                  status["enabled"] is False
                  and status["counts_toward_release_gate"] is False
                  and status["execution_weight"] == 0.0, status)
            check("服务侧返回落库覆盖率（库侧可用时）",
                  isinstance(status["stored_coverage"], dict)
                  and status["stored_coverage"].get("counts_toward_release_gate") is False)
            before = sorted(service.snapshot().keys())
            service.shadow_v2_status()
            check("调用只读状态后 snapshot 键集合不变（既有消费者输出逐字不变）",
                  sorted(service.snapshot().keys()) == before, before)
            check("服务侧不写任何 v2 行（状态查询无副作用）",
                  service.db.fetch_one("SELECT COUNT(*) AS n FROM shadow_v2_labels")["n"] == 0)
        finally:
            db.close()
    else:
        note("共享补丁未应用：服务侧只读方法不存在，属预期（行为不因代码存在而改变）")
        check("未接线时服务类不含 v2 只读方法",
              ReadOnlyIntelService is None
              or not hasattr(ReadOnlyIntelService, "shadow_v2_status"))


# --------------------------------------------------------------------------- #
# 13. 门禁逐字不变（本任务最硬的一条）
# --------------------------------------------------------------------------- #
def _seed_gate_db(db: AuditDatabase) -> None:
    """造一个能跑出真实门禁报告的最小数据集（A 组方向候选 + 观察窗口）。"""
    for index in range(3):
        db.log_shadow_decision(
            decision_id=f"a-{index}", symbol="BTCUSDT", signal="bullish",
            reference_price=100.0, confidence=0.7, source=ss.SOURCE_A,
            execution_weight=0.0,
            feature_json=json.dumps({"opportunity_id": f"opp-{index}"}),
            release_id="rel-u06b", environment="demo")
    db.log_shadow_decision(  # 研究影子：不得计入分母
        decision_id="b-0", symbol="ETHUSDT", signal="bearish", reference_price=50.0,
        confidence=0.6, source=ss.SOURCE_B, execution_weight=0.0,
        feature_json=json.dumps({"opportunity_id": "opp-b0"}),
        release_id="rel-u06b", environment="demo")
    start = T0
    with db.transaction() as connection:
        # 影子行入库时间用的是真实墙钟；把 created_at 对齐到合成窗口（只改夹具，
        # 不改实现），否则候选窗口内一条都取不到。
        connection.execute("UPDATE shadow_decisions SET created_at=?",
                           (iso(start + timedelta(minutes=5)),))
        for index in range(60):
            moment = start + timedelta(minutes=index)
            connection.execute(
                """INSERT OR IGNORE INTO release_observations(
                   observation_id,release_id,environment,account_ref,session_id,
                   observed_at,observed_minute,health_state,component_status_json,
                   config_hash,rate_limit_json,coverage_json,entry_readiness_json,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"obs-{index}", "rel-u06b", "demo", "demo:acc", "sess-1", iso(moment),
                 minute(moment), "healthy", "{}", "cfg-a",
                 json.dumps({"responses_by_status": {}, "coordination_available": True}),
                 "{}", json.dumps({"new_entry_allowed": True}), "{}"))


def test_release_gate_unchanged() -> None:
    section("13. release_evidence 门禁结果逐字不变")
    db, path = _new_db("gate")
    try:
        _seed_gate_db(db)
        manifest = {"release_id": "rel-u06b", "environment": "demo",
                    "account_ref": "demo:acc", "schema_version": 17}
        kwargs = {"environment": "demo",
                  "since": iso(T0), "until": iso(T0 + timedelta(minutes=59)),
                  "report_at": iso(T0 + timedelta(minutes=60))}
        before = evidence.build_release_readiness_report(path, manifest, **kwargs)
        before_candidates = before["release_evidence"]["directional_candidates"]

        # 灌入大量 v2 研究标签（若共享补丁在；否则等价地直接写研究行不足以影响门禁）。
        rows = []
        for index in range(50):
            result = labeled(request(f"v2-{index}", symbol="BTCUSDT"),
                             [sample("BTCUSDT", HORIZON * 60, 101.0)])
            rows.append(result.to_dict())
        if DB_PATCHED:
            written = db.log_shadow_v2_labels(rows)
            check("灌入 v2 标签成功（用于门禁不变性验证）", written == 50, written)
        else:
            note("共享补丁未应用：无落库路径，门禁不变性只在零写入下成立")

        after = evidence.build_release_readiness_report(path, manifest, **kwargs)

        def gate_payload(report: dict[str, Any]) -> str:
            """去掉报告生成时刻（墙钟噪声），其余逐字段比较。"""
            payload = {key: value for key, value in report.items() if key != "generated_at"}
            return json.dumps(payload, sort_keys=True, default=str)

        check("v2 标签写入前后，门禁报告逐字段完全相同（除 generated_at 墙钟外）",
              gate_payload(before) == gate_payload(after))
        check("生成的报告仍带 generated_at（只排除了噪声字段，没有整段跳过）",
              "generated_at" in before and "generated_at" in after)
        check("方向候选分母不含 v2 研究行",
              after["release_evidence"]["directional_candidates"] == before_candidates,
              (before_candidates, after["release_evidence"]["directional_candidates"]))
        check("B 组研究影子仍被 U09 来源过滤排除在分母外（U06a/U09 口径未被本任务改动）",
              before_candidates == 3, before_candidates)
        check("门禁结论字段（release_readiness）未被 v2 影响",
              before["release_readiness"] == after["release_readiness"])
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 14. 模块边界（不得悄悄扩大研究侧车能力）
# --------------------------------------------------------------------------- #
def test_module_boundaries() -> None:
    section("14. 模块边界（纯函数、无网络/DB/LLM）")
    source_files = sorted((ROOT / "src" / "intel" / "shadow_v2").glob("*.py"))
    check("独占路径四个模块齐备",
          {item.name for item in source_files}
          == {"__init__.py", "models.py", "sampler.py", "labels.py"},
          [item.name for item in source_files])
    forbidden = ("import httpx", "import requests", "from data.", "import sqlite3",
                 "from audit", "import openai", "subprocess")
    offenders = {}
    for item in source_files:
        text = item.read_text(encoding="utf-8")
        hits = [token for token in forbidden if token in text]
        if hits:
            offenders[item.name] = hits
    check("研究侧模块不引入网络/DB/LLM/子进程依赖", not offenders, offenders)
    check("模块不硬编码真实 UTC 日期做时钟判定（F17 用相对增量）",
          all("2026-" not in item.read_text(encoding="utf-8") for item in source_files))
    check("公开 API 均不返回门禁结论",
          all(key not in v2.runtime_status() for key in ("promotion_ready", "go_verdict")))


def main() -> int:
    print("U06b 影子 v2 固定时点标签 —— 离线集成回归")
    print(f"fixtures: {FIXTURES}")
    try:
        test_caliber_separation()
        test_runtime_status_default_disabled()
        test_fixed_target_time()
        test_missing_reasons()
        test_coverage_report()
        test_no_future_leakage()
        test_replay_determinism()
        test_sampling_plan()
        test_experiment_isolation()
        test_clock_guard()
        test_fixture_cases()
        test_shared_adapter_two_state()
        test_release_gate_unchanged()
        test_module_boundaries()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nPASS={PASS} FAIL={FAIL}")
    if FAILURES:
        print("FAILED CASES:")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("U06B SHADOW LABELS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
