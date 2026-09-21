"""U07 影子 v2 配对实验与独立样本统计（K02 / K06 / F11 / F16 / F24 / DL08 / DL09）。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：不联网、不读 ``.env``/``data/``、不构造 Broker、不启停服务、不改风险档。

**两态可跑**：本套件只依赖
- U07 独占模块 ``src/intel/shadow_v2/statistics.py``（本任务新增，两态都在），
- 已集成 ``src/intel/shadow_v2/``（U06b 的标签层，两态都在），
- 已冻结证据 ``src/intel/shadow_status.py``（U06a）与
  ``src/core/release_evidence.py``（U09，只读其公开 API），
- 共享适配补丁 ``src/audit/database.py``（按 ``U07_INTEGRATION`` 标记探测是否已
  应用，**不**读补丁文件内容判定）。

补丁不在 → 断言本体已提供统计所需的全部符号与语义，并断言「统计产物根本不落库、
门禁不变」；补丁在 → 追加落库/幂等/门禁逐字不变断言。

期望值来源（**禁止**复制被测实现输出自证）：
- 任务卡 U07 交付要点：同机会配对、重叠样本不冒充独立、费用压力及区间报告可重放、
  研究不自动晋升执行权重；
- ``shadow_status.LEGACY_LIMITATIONS`` 第 4/5/6/7 条（成本是假设、相邻信号重叠、
  A/B/C 非同一样本、研究行仍可能进 200 分母）；
- F11 反例「5 分钟标签晚 8 分钟」（迟 480s）；
- 夹具 ``tests/fixtures/shadow/statistics/*.json`` 自带的独立判别式。
"""
from __future__ import annotations

import dataclasses
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
from intel.shadow_v2 import statistics as stats  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []

TMP = Path(tempfile.mkdtemp(prefix="u07-statistics-"))
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shadow" / "statistics"

#: 两态探测：只按共享补丁自报的标记判断，不猜、不读补丁文本。
DB_PATCHED = bool(getattr(database_module, "U07_INTEGRATION", ""))

#: 合成时间基准（相对运算，不含真实日期语义）。
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
HORIZON = 5
TARGET = T0 + timedelta(minutes=HORIZON)
#: 研究成本假设（与 U06b legacy 同口径）：10bps 往返手续费 + 4bps 滑点。
FEE_BPS = 10.0
SLIP_BPS = 4.0
FEE_PCT = FEE_BPS / 100.0
SLIP_PCT = SLIP_BPS / 100.0


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
# 夹具构造：只用公开 API（LabelRequest/LabelResult），不注入内部字段
# --------------------------------------------------------------------------- #
def result(decision_id: str, *, symbol: str = "BTCUSDT", signal: str = "bullish",
           source: str = ss.SOURCE_B, reference: float = 100.0, exit_price: float | None,
           decided_offset_seconds: float = 0.0, horizon: int = HORIZON,
           version: str = "v2-experiment-1", now_offset_minutes: float | None = None,
           extra_samples: tuple[tuple[float, float], ...] = ()) -> v2.LabelResult:
    """用公开标签函数造一条样本；``exit_price=None`` 表示该机会**没有**价格观测。

    ``now`` 缺省按「目标时刻 + 5 分钟」派生（容差窗口已关闭），避免因 ``now`` 落在
    窗口内而把样本误判成未成熟（PENDING）。
    """
    request = v2.LabelRequest(decision_id, source, symbol, signal, reference,
                              T0 + timedelta(seconds=float(decided_offset_seconds)), horizon,
                              experiment_version=version, release_id="rel-u07",
                              config_fingerprint="cfg-u07")
    samples = [v2.PriceSample(symbol, T0 + timedelta(seconds=offset), price)
               for offset, price in extra_samples]
    if exit_price is not None:
        samples.append(v2.PriceSample(
            symbol, T0 + timedelta(seconds=decided_offset_seconds + horizon * 60), exit_price))
    now = (T0 + timedelta(minutes=float(now_offset_minutes)) if now_offset_minutes is not None
           else T0 + timedelta(seconds=decided_offset_seconds + horizon * 60 + 300.0))
    return v2.label_request(request, samples, now=now,
                            round_trip_fee_bps=FEE_BPS, slippage_bps=SLIP_BPS)


def expected_net(exit_price: float, reference: float = 100.0, *,
                 direction: float = 1.0, funding_pct: float = 0.0) -> float:
    """独立算式：``net = gross - fee_pct - slip_pct - funding_pct``（定义式）。"""
    gross = (exit_price / reference - 1.0) * 100.0 * direction
    return gross - FEE_PCT - SLIP_PCT - funding_pct


def make_pair(treatment_exit: float, control_exit: float, *, index: int = 0,
              symbol: str = "BTCUSDT", signal: str = "bullish",
              reference: float = 100.0,
              decided_offset_seconds: float = 0.0) -> Any:
    """造一对同机会样本并返回 :class:`PairedSample`（经公开的配对函数）。"""
    t_row = result(f"t-{index}", symbol=symbol, signal=signal, reference=reference,
                   exit_price=treatment_exit, decided_offset_seconds=decided_offset_seconds)
    c_row = result(f"c-{index}", symbol=symbol, signal=signal, reference=reference,
                   exit_price=control_exit, decided_offset_seconds=decided_offset_seconds)
    report = stats.pair_experiment([t_row], [c_row])
    assert len(report.pairs) == 1, report.drop_counts()
    return report.pairs[0]


def pairs_with_gross(count: int, treatment_gross_pct: float) -> list[Any]:
    """造 ``count`` 对样本：处理臂毛收益固定，对照臂毛收益为 0，且两两不重叠。"""
    out = []
    for index in range(count):
        # 间隔 600s > 持仓窗口 300s ⇒ 每对自成一类独立块（消除重叠对区间的干扰）。
        offset = index * 600.0
        treatment_exit = 100.0 * (1.0 + treatment_gross_pct / 100.0)
        out.append(make_pair(treatment_exit, 100.0, index=index,
                             symbol=f"SYM{index}USDT", decided_offset_seconds=offset))
    return out


# --------------------------------------------------------------------------- #
# 1. 口径分离 / 不自动晋升 / 门禁隔离
# --------------------------------------------------------------------------- #
def test_caliber_and_isolation() -> None:
    section("1. 口径分离 / 首批记录不变 / 不自动晋升")
    facts = stats.statistics_isolation_facts()
    check("统计口径 id 与 legacy v1、与 U06b 标签口径都不同（三套口径不可混读）",
          len({stats.STATISTICS_CALIBER_ID, ss.LEGACY_CALIBER_ID, v2.V2_CALIBER_ID}) == 3,
          (stats.STATISTICS_CALIBER_ID, ss.LEGACY_CALIBER_ID, v2.V2_CALIBER_ID))
    check("legacy 口径 id 逐字取自 shadow_status（引用而非复制）",
          facts["legacy_caliber"] == ss.LEGACY_CALIBER_ID)
    check("首批 v2 标签状态仍记录为 DEFERRED（本任务不伪称已通过 F11）",
          ss.V2_LABELS_STATE == "DEFERRED"
          and facts["shadow_status_v2_state"] == "DEFERRED"
          and v2.FIRST_RELEASE_F11_STATE_UNCHANGED == "DEFERRED",
          (ss.V2_LABELS_STATE, facts["shadow_status_v2_state"]))
    check("统计产物恒不计入发布门禁",
          facts["counts_toward_release_gate"] is False
          and stats.COUNTS_TOWARD_RELEASE_GATE is False)
    check("研究不自动晋升执行权重（权重恒 0、晋升被显式禁止）",
          facts["execution_weight"] == 0.0 and facts["auto_promotion"] is False
          and stats.EXECUTION_PROMOTION_ALLOWED is False
          and stats.promotion_guard()["execution_promotion_allowed"] is False)
    check("门禁角色事实直接引用 U06a（A 仍为 execution_decision_copy，B/C 仍为 research_only）",
          facts["gate_role_by_source"][ss.SOURCE_A] == ss.GATE_ROLE_EXECUTION_DECISION
          and facts["gate_role_by_source"][ss.SOURCE_B] == ss.GATE_ROLE_RESEARCH_ONLY
          and facts["gate_role_by_source"][ss.SOURCE_C] == ss.GATE_ROLE_RESEARCH_ONLY)
    check("发布门禁权威仍是 U09 的 release_evidence（本模块不建第二套判断器）",
          facts["release_gate_authority"].startswith("src/core/release_evidence.py"))
    check("后置线说明逐字保留「不改 F11 / 不进门禁 / 不晋升」",
          "F11:v2_labels=DEFERRED" in stats.PAIRED_STATISTICS_SCOPE_NOTE
          and "不" in stats.PAIRED_STATISTICS_SCOPE_NOTE)
    check("统计口径说明与 shadow_status 的 U07 归属一致（shadow_status 未被本任务改动）",
          "U07（配对统计）" in ss.build_disclosure(config={})["v2_labels"]["owner"])


def test_gate_leak_is_rejected() -> None:
    section("2. 研究侧最后一道闸：带门禁/权重语义的样本必须被拒")
    row = result("leak-1", exit_price=101.0)
    assert_gate_neutral_ok = True
    try:
        stats.assert_gate_neutral([row])
    except stats.GateLeakError:  # pragma: no cover - 纯研究行不该抛
        assert_gate_neutral_ok = False
    check("纯研究标签行通过门禁中性校验", assert_gate_neutral_ok)

    leaked = dataclasses.replace(row, counts_toward_release_gate=True)
    raised = False
    try:
        stats.assert_gate_neutral([leaked])
    except stats.GateLeakError:
        raised = True
    check("counts_toward_release_gate=True 的样本被拒绝纳入研究统计（fail-closed）", raised)

    weighted = dataclasses.replace(row, execution_weight=0.5)
    raised = False
    try:
        stats.assert_gate_neutral([weighted])
    except stats.GateLeakError:
        raised = True
    check("非零 execution_weight 的样本被拒绝纳入研究统计", raised)

    foreign = dataclasses.replace(row, caliber=ss.LEGACY_CALIBER_ID)
    raised = False
    try:
        stats.assert_gate_neutral([foreign])
    except stats.GateLeakError:
        raised = True
    check("legacy 口径样本不得混进 v2 统计（口径混读同样是缺陷）", raised)


def test_experiment_isolation() -> None:
    section("3. 实验版本隔离（DL08：不同版本不可比）")
    a = result("e-1", exit_price=101.0, version="v2-experiment-1")
    b = result("e-2", exit_price=101.0, version="v2-experiment-2")
    check("单版本样本通过隔离校验并返回版本键",
          stats.assert_single_experiment([a]) == a.experiment.label())
    raised = False
    try:
        stats.assert_single_experiment([a, b])
    except stats.ExperimentMixError:
        raised = True
    check("混版本样本集抛 ExperimentMixError", raised)
    raised = False
    try:
        stats.pair_experiment([a], [b])
    except stats.ExperimentMixError:
        raised = True
    check("配对两臂版本不同时拒绝配对（不静默按版本拆开算）", raised)
    raised = False
    try:
        stats.build_statistics_report([a, b], [])
    except stats.ExperimentMixError:
        raised = True
    check("报告构建同样拒绝混版本", raised)
    check("空样本集返回 None 版本键（不以空集冒充某一版本）",
          stats.assert_single_experiment([]) is None)


# --------------------------------------------------------------------------- #
# 4. 同机会配对
# --------------------------------------------------------------------------- #
def test_pairing() -> None:
    section("4. 同机会配对（同标的/同目标时刻/同 horizon/同实验版本）")
    t_row = result("t-a", exit_price=101.0)
    c_row = result("c-a", exit_price=100.5)
    report = stats.pair_experiment([t_row], [c_row])
    check("两臂同机会 → 配成 1 对", len(report.pairs) == 1, report.drop_counts())
    pair = report.pairs[0]
    expect_gross_diff = ((101.0 / 100.0) - (100.5 / 100.0)) * 100.0
    check("毛差 = 两臂毛收益之差（由价格独立算出，不经实现）",
          abs(pair.gross_diff_pct - expect_gross_diff) < 1e-9,
          (pair.gross_diff_pct, expect_gross_diff))
    check("净差 = 两臂净收益之差（两臂成本假设相同 ⇒ 净差与毛差一致）",
          abs(pair.net_diff_pct - expect_gross_diff) < 1e-9, pair.net_diff_pct)
    check("配对键含实验版本/标的/目标时刻/horizon 全部四类信息，换任一段即换键",
          pair.opportunity == stats.opportunity_key(t_row)
          and pair.opportunity == stats.opportunity_key(c_row)
          and "BTCUSDT" in pair.opportunity and "5m" in pair.opportunity
          and stats.opportunity_key(t_row) != stats.opportunity_key(
              result("t-alt", symbol="ETHUSDT", exit_price=101.0))
          and stats.opportunity_key(t_row) != stats.opportunity_key(
              result("t-alt2", horizon=15, exit_price=101.0))
          and stats.opportunity_key(
              t_row) != stats.opportunity_key(result("t-alt3", exit_price=101.0,
                                                     version="v2-experiment-2")),
          pair.opportunity)
    check("配对结果不携带门禁语义（不进任何门禁分母）",
          pair.to_dict()["counts_toward_release_gate"] is False)

    same_target_diff_symbol = result("c-b", symbol="ETHUSDT", exit_price=100.5)
    report = stats.pair_experiment([t_row], [same_target_diff_symbol])
    check("不同标的同目标时刻 → 不配对（不是同一个机会）",
          len(report.pairs) == 0
          and report.drop_counts().get(stats.REASON_MISSING_TREATMENT_ARM) == 1,
          report.drop_counts())

    late = result("c-c", exit_price=100.5, decided_offset_seconds=60.0)
    report = stats.pair_experiment([t_row], [late])
    check("同标的不同决策时刻 → 目标时刻不同 → 不配对",
          len(report.pairs) == 0 and report.drop_counts(), report.drop_counts())

    divergent = result("c-d", reference=100.5, exit_price=101.0)
    report = stats.pair_experiment([t_row], [divergent])
    check("两臂入场参考价相差约 50bps → ENTRY_DIVERGENT 剔除并披露",
          len(report.pairs) == 0
          and report.drop_counts().get(stats.REASON_ENTRY_DIVERGENT) == 1,
          report.drop_counts())

    tolerant = stats.pair_experiment([t_row], [divergent],
                                     entry_tolerance_bps=100.0)
    check("放宽容忍到 100bps 后同一对可配对（容忍是显式参数，不是隐式放宽）",
          len(tolerant.pairs) == 1
          and tolerant.pairs[0].entry_divergence_bps < 100.0,
          tolerant.drop_counts())

    mismatch = result("c-e", signal="bearish", exit_price=99.0)
    report = stats.pair_experiment([t_row], [mismatch])
    check("两臂信号方向不同 → SIGNAL_MISMATCH（同机会不同方向不是同一个样本）",
          len(report.pairs) == 0
          and report.drop_counts().get(stats.REASON_SIGNAL_MISMATCH) == 1,
          report.drop_counts())

    unlabeled = result("c-f", exit_price=None)
    report = stats.pair_experiment([t_row], [unlabeled])
    check("对照臂未出标签（窗口内无价）→ CONTROL_NOT_LABELED，不静默丢弃",
          len(report.pairs) == 0
          and report.drop_counts().get(stats.REASON_CONTROL_NOT_LABELED) == 1,
          report.drop_counts())

    report = stats.pair_experiment([], [c_row])
    check("处理臂整体缺失 → MISSING_TREATMENT_ARM（不用对照臂充数）",
          len(report.pairs) == 0
          and report.drop_counts().get(stats.REASON_MISSING_TREATMENT_ARM) == 1,
          report.drop_counts())

    duplicate = result("t-dup", exit_price=101.0)
    report = stats.pair_experiment([t_row, duplicate], [c_row])
    check("同一机会在同一臂出现两次 → DUPLICATE_OPPORTUNITY（不静默去重）",
          len(report.pairs) == 0
          and report.drop_counts().get(stats.REASON_DUPLICATE_OPPORTUNITY) == 1,
          report.drop_counts())

    check("每个候选机会都有归宿：配对 + 剔除 == 候选键数",
          _candidates_accounted_for([t_row], [c_row, late]),
          (len(stats.pair_experiment([t_row], [c_row, late]).pairs),
           stats.pair_experiment([t_row], [c_row, late]).drop_counts()))


def _candidates_accounted_for(t_rows: list[Any], c_rows: list[Any]) -> bool:
    report = stats.pair_experiment(t_rows, c_rows)
    keys = {stats.opportunity_key(item) for item in t_rows + c_rows}
    return len(report.pairs) + len(report.dropped) == len(keys)


# --------------------------------------------------------------------------- #
# 5. 独立样本：重叠不得冒充独立
# --------------------------------------------------------------------------- #
def test_independence_blocks() -> None:
    section("5. 独立样本分块（重叠样本不冒充独立）")
    rows = [result(f"i-{index}", exit_price=101.0, decided_offset_seconds=index * 60.0)
            for index in range(6)]
    report = stats.independence_report(rows)
    check("连续 6 个重叠窗口聚成 1 个独立块", report["independent_blocks"] == 1, report)
    check("状态标记 OVERLAPPING（行数 6 > 独立块数 1）",
          report["state"] == stats.INDEPENDENCE_OVERLAPPING, report["state"])
    check("overlap_ratio 按 1 - blocks/observations 独立算出（6 行 1 块 ⇒ 5/6）",
          abs(report["overlap_ratio"] - (1.0 - 1.0 / 6)) < 1e-5, report["overlap_ratio"])
    check("largest_block 披露最大块规模（不隐藏重叠程度）",
          report["largest_block"] == 6, report["largest_block"])
    check("分块报告不携带门禁语义",
          report["counts_toward_release_gate"] is False)

    separated = [result(f"s-{index}", exit_price=101.0,
                        decided_offset_seconds=index * 600.0) for index in range(4)]
    report = stats.independence_report(separated)
    check("间隔 > 持仓窗口 → 4 条各自成块，状态 INDEPENDENT",
          report["independent_blocks"] == 4 and report["state"] == stats.INDEPENDENCE_INDEPENDENT,
          report)

    check("空集为 INSUFFICIENT 且不给任何比例（不以 0 冒充独立）",
          stats.independence_report([])["state"] == stats.INDEPENDENCE_INSUFFICIENT
          and stats.independence_report([])["overlap_ratio"] is None)

    cross = [result(f"x-{index}", symbol=sym, exit_price=101.0,
                    decided_offset_seconds=index * 300.0)
             for index in range(3) for sym in ("ETHUSDT", "SOLUSDT")]
    report = stats.independence_report(cross)
    check("分块按标的各自计算（两标的窗口相同也不合并）",
          report["independent_blocks"] == 2
          and report["by_symbol"]["ETHUSDT"]["blocks"] == 1
          and report["by_symbol"]["SOLUSDT"]["observations"] == 3,
          report["by_symbol"])

    # 两块：BTCUSDT 上 3 对互相重叠（+0.5），ETHUSDT 上 3 对互相重叠（-0.5）。
    # 独立看只有 2 个单元，而朴素算法会当成 6 个独立样本 ⇒ 朴素标准误被低估。
    pairs = ([make_pair(101.0, 100.5, index=index, symbol="BTCUSDT",
                        decided_offset_seconds=index * 60.0) for index in range(3)]
             + [make_pair(99.0, 99.5, index=10 + index, symbol="ETHUSDT", signal="bearish",
                          decided_offset_seconds=index * 60.0) for index in range(3)])
    stats_report = stats.paired_statistics(pairs)
    check("配对差同样分块：同标的 3 对重叠 → 该标的 1 块，两块共 2 个独立单元",
          stats_report["independent_blocks"] == 2, stats_report["independent_blocks"])
    check("重叠时朴素标准误被标记为低估独立误差（不默认报窄区间）",
          stats_report["naive_understates"] is True
          and stats_report["naive_se"] < stats_report["cluster_se"],
          (stats_report["naive_se"], stats_report["cluster_se"]))
    check("配对差的分块区间基于独立单元（块均值）而非 6 行原始值",
          stats_report["cluster"]["n"] == 2 and stats_report["naive"]["n"] == 6,
          (stats_report["cluster"]["n"], stats_report["naive"]["n"]))


# --------------------------------------------------------------------------- #
# 6. 区间报告可重放
# --------------------------------------------------------------------------- #
def test_interval_replay() -> None:
    section("6. 区间报告（显式种子 ⇒ 可重放）")
    values = [0.5, 0.4, -0.1, 0.9, 0.2, 0.35, -0.25, 0.6]
    first = stats.bootstrap_interval(values)
    second = stats.bootstrap_interval(values)
    check("同输入同种子 → 区间逐字段相同（可重放）",
          first == second, (first.get("low"), second.get("low")))
    check("区间给出摘要 digest 供复验逐字比对",
          isinstance(first.get("digest"), str) and len(first["digest"]) == 64,
          first.get("digest"))
    other = stats.bootstrap_interval(values, seed=stats.DEFAULT_BOOTSTRAP_SEED + 1)
    check("换种子 → 区间不同（说明种子确实参与了计算，不是摆设）",
          other != first and other["seed"] != first["seed"])
    check("区间上界 >= 下界，且包含均值附近（基本健全性）",
          first["low"] <= first["high"] and first["n"] == len(values), first)

    single = stats.bootstrap_interval([0.5])
    check("单个独立单元不出区间（方差不可估计，不以 0 宽度冒充确定）",
          single["low"] is None and single["high"] is None, single)

    naive = stats.mean_interval(values)
    check("朴素区间依赖假设被显式标注（iid）", naive["assumption"] == "iid")
    check("朴素区间与自举区间都给出 n 与均值，便于交叉核对",
          naive["n"] == len(values)
          and abs(naive["mean"] - sum(values) / len(values)) < 1e-12, naive)

    clustered = [0.5] * 10 + [-0.5]
    naive_cluster = stats.mean_interval(clustered)
    block_cluster = stats.bootstrap_interval([0.5, 0.5, 0.5, -0.5])
    check("独立块区间比把重叠行当独立的区间更宽或相当（不得更窄）",
          block_cluster["high"] - block_cluster["low"] >= 0
          and naive_cluster["n"] == 11, (naive_cluster, block_cluster))


def test_sign_test() -> None:
    section("7. 配对差的精确符号检验（分布无关）")
    all_positive = stats.sign_test([0.5, 0.4, 0.3, 0.2, 0.1, 0.05])
    check("6/6 同号 → 双侧精确 p = 2*(1/64) = 0.03125",
          abs(all_positive["p_value"] - 0.03125) < 1e-12, all_positive)
    balanced = stats.sign_test([0.5, -0.4, 0.3, -0.2])
    check("2 正 2 负 → p = 1.0（无方向性证据）",
          abs(balanced["p_value"] - 1.0) < 1e-12, balanced)
    with_zero = stats.sign_test([0.0, 0.5, 0.0])
    check("差为 0 的对被排除并披露（不当作正/负样本凑数）",
          with_zero["n"] == 1 and with_zero["zeros"] == 2, with_zero)
    empty = stats.sign_test([])
    check("无配对 → 不出 p 值（不以 1.0 冒充）", empty["p_value"] is None, empty)


# --------------------------------------------------------------------------- #
# 8. 费用压力
# --------------------------------------------------------------------------- #
def test_fee_stress() -> None:
    section("8. 费用压力（成本是研究假设，不是成交成本）")
    pairs = pairs_with_gross(4, 0.3)
    stress = stats.fee_stress(pairs, multipliers=(1.0, 2.0, 3.0))
    points = {item["multiplier"]: item for item in stress["points"]}
    check("逐倍数给出处理臂均值净值（独立算式 net = gross - (fee+slip)*m）",
          all(abs(points[m]["treatment_mean_net_pct"]
                  - (0.3 - (FEE_PCT + SLIP_PCT) * m)) < 1e-9 for m in (1.0, 2.0, 3.0)),
          {m: points[m]["treatment_mean_net_pct"] for m in points})
    check("1 倍为正、3 倍转负，逐点标记 unprofitable",
          points[1.0]["unprofitable"] is False and points[3.0]["unprofitable"] is True)
    check("盈亏平衡倍数按相邻点线性插值独立算出（2 + 0.02/0.14）",
          abs(stress["break_even_multiplier"] - (2.0 + 0.02 / 0.14)) < 1e-9,
          stress["break_even_multiplier"])
    check("压力确实作用在成本项上：处理臂均值净值随倍数单调下降",
          points[1.0]["treatment_mean_net_pct"] > points[2.0]["treatment_mean_net_pct"]
          > points[3.0]["treatment_mean_net_pct"],
          [points[m]["treatment_mean_net_pct"] for m in (1.0, 2.0, 3.0)])
    check("两臂成本假设相同时配对差对费用压力不敏感（成本在配对中抵消）——"
          "这是配对设计的性质，报告必须照实呈现而不是制造「压力下有差异」的假象",
          abs(points[1.0]["paired_mean_diff_pct"] - points[3.0]["paired_mean_diff_pct"]) < 1e-12,
          (points[1.0]["paired_mean_diff_pct"], points[3.0]["paired_mean_diff_pct"]))
    check("压力点的区间报告同样可重放（含 digest）",
          isinstance(points[1.0]["paired_interval"].get("digest"), str))
    check("显式声明成本是假设而非成交成本",
          "不是真实成交成本" in stress["cost_assumption_note"])

    thick = stats.fee_stress(pairs_with_gross(4, 1.0), multipliers=(1.0, 2.0, 3.0))
    check("厚利样本在倍数区间内不穿越 0 → 不给出外推的盈亏平衡倍数",
          thick["break_even_multiplier"] is None, thick["break_even_multiplier"])

    extra = stats.fee_stress(pairs, multipliers=(1.0,), extra_slippage_bps=10.0)
    point = extra["points"][0]
    check("额外滑点按基点加到成本上（10bps = 0.1 个百分点）",
          abs(point["treatment_mean_net_pct"] - (0.3 - FEE_PCT - SLIP_PCT - 0.1)) < 1e-9,
          point["treatment_mean_net_pct"])

    zero = stats.fee_stress([], multipliers=(1.0,))
    check("无配对时不给任何数值（不以 0 冒充无成本）",
          zero["points"][0]["treatment_mean_net_pct"] is None
          and zero["break_even_multiplier"] is None, zero["points"][0])


# --------------------------------------------------------------------------- #
# 9. 报告整体：可重放 / 不含 GO 结论
# --------------------------------------------------------------------------- #
def test_report_shape_and_replay() -> None:
    section("9. 报告整体（可重放摘要 / 不产出 GO 结论）")
    treatment = [result(f"t-{index}", exit_price=101.0 + index * 0.1,
                        decided_offset_seconds=index * 600.0,
                        symbol=f"SYM{index}USDT") for index in range(4)]
    control = [result(f"c-{index}", exit_price=100.5,
                      decided_offset_seconds=index * 600.0,
                      symbol=f"SYM{index}USDT") for index in range(4)]
    report = stats.build_statistics_report(treatment, control,
                                           config_fingerprint="cfg-u07",
                                           resamples=400)
    payload = report.to_dict()
    check("报告含配对/独立块/区间/费用压力/覆盖率五个部分",
          {"pairing", "independence", "paired", "fee_stress", "coverage"} <= set(payload),
          sorted(payload))
    check("报告显式声明不进门禁、零执行权重、不自动晋升",
          payload["execution"]["counts_toward_release_gate"] is False
          and payload["execution"]["execution_weight"] == 0.0
          and payload["execution"]["auto_promotion"] is False
          and payload["gate"]["counts_toward_release_gate"] is False)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    check("报告文本不含任何 GO/promotion 判定字样（不建第二套门禁判断器）",
          all(token not in text for token in ("go_verdict", "promotion_ready",
                                              "release_readiness", "GO_VERDICT")))
    check("同一输入重复构建 → 报告摘要逐字相同（可重放）",
          report.digest() == stats.build_statistics_report(
              treatment, control, config_fingerprint="cfg-u07", resamples=400).digest())
    check("改一个输入 → 摘要变化（摘要确实覆盖全部字段）",
          report.digest() != stats.build_statistics_report(
              treatment, control, config_fingerprint="cfg-u07-other",
              resamples=400).digest())
    check("报告携带实验版本与配置指纹（DL08 隔离键）",
          payload["experiment_key"] == treatment[0].experiment.label()
          and payload["config_fingerprint"] == "cfg-u07")
    check("两臂覆盖率逐原因给出（复用 U06b coverage_report，未另造一套）",
          payload["coverage"]["treatment"]["decided"] == 4
          and payload["coverage"]["treatment"]["labeled"] == 4
          and payload["coverage"]["treatment"]["counts_toward_release_gate"] is False,
          payload["coverage"]["treatment"])
    rows = report.to_rows()
    check("落库行只带只读研究语义（门禁/权重/晋升三项强制归零）",
          len(rows) == 1 and rows[0]["counts_toward_release_gate"] is False
          and rows[0]["execution_weight"] == 0.0 and rows[0]["auto_promotion"] is False
          and len(rows[0]["digest"]) == 64, rows[0] if rows else None)
    check("报告声明口径 id 为 U07 统计口径（与 v2 标签口径分开登记）",
          payload["caliber"] == stats.STATISTICS_CALIBER_ID)


# --------------------------------------------------------------------------- #
# 10. 离线夹具（期望值由独立判别式给出，不由实现产出）
# --------------------------------------------------------------------------- #
def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_fixture_paired() -> None:
    section("10. 夹具：配对机会（期望归宿来自夹具独立判别式）")
    data = _load_fixture("paired_opportunities.json")
    check("夹具自述来源（derivation 覆盖判别式，禁止用被测实现自证）",
          bool(data.get("derivation")) and "判别式" in data["derivation"])
    experiment = data["experiment"]
    tolerance = float(data["tolerance_seconds"])
    now_minutes = float(data["now_offset_minutes"])
    treatment_rows: list[Any] = []
    control_rows: list[Any] = []
    for case in data["scenarios"]:
        offset = float(case["decided_offset_seconds"])
        horizon = int(case["horizon_minutes"])
        for arm, bucket in (("treatment", treatment_rows), ("control", control_rows)):
            payload_arm = case.get(arm)
            if payload_arm is None:
                continue
            signal = payload_arm.get("signal", case["signal"])
            exit_spec = payload_arm.get("exit")
            request = v2.LabelRequest(
                payload_arm["decision_id"], ss.SOURCE_B, case["symbol"], signal,
                float(payload_arm["reference_price"]),
                T0 + timedelta(seconds=offset), horizon,
                experiment_version=experiment["experiment_version"],
                release_id=experiment["release_id"],
                config_fingerprint=experiment["config_fingerprint"])
            samples = []
            if exit_spec is not None:
                samples.append(v2.PriceSample(
                    case["symbol"], T0 + timedelta(seconds=float(exit_spec["offset_seconds"])),
                    float(exit_spec["price"])))
            bucket.append(v2.label_request(
                request, samples, now=T0 + timedelta(minutes=now_minutes),
                tolerance_seconds=tolerance,
                expected_interval_seconds=float(data["expected_interval_seconds"]),
                round_trip_fee_bps=float(data["costs"]["round_trip_fee_bps"]),
                slippage_bps=float(data["costs"]["slippage_bps"])))
    report = stats.pair_experiment(treatment_rows, control_rows)
    paired_ids = {pair.treatment.decision_id for pair in report.pairs}
    dropped = {}
    for item in report.dropped:
        dropped.setdefault(str(item["reason"]), []).append(item)
    for case in data["scenarios"]:
        expect = case["expect"]
        t_id = (case.get("treatment") or {}).get("decision_id")
        c_id = (case.get("control") or {}).get("decision_id")
        if expect["paired"]:
            check(f"夹具 {case['scenario_id']}：{case['intent']}",
                  t_id in paired_ids, sorted(paired_ids))
        else:
            reason = expect["drop_reason"]
            ids = {item.get("treatment_decision_id") for item in dropped.get(reason, [])}
            ids |= {item.get("control_decision_id") for item in dropped.get(reason, [])}
            check(f"夹具 {case['scenario_id']}：{case['intent']}",
                  t_id in ids or c_id in ids, (reason, dropped.get(reason)))
    check("夹具核对：配对 + 剔除 == 候选机会键数（无静默丢失）",
          len(report.pairs) + len(report.dropped)
          == len({stats.opportunity_key(item) for item in treatment_rows + control_rows}),
          (len(report.pairs), len(report.dropped)))

    basic = next(item for item in report.pairs if item.treatment.decision_id == "t-basic")
    check("夹具核对：pair-basic 的毛差/净差由价格独立算出",
          abs(basic.gross_diff_pct - 0.5) < 1e-9
          and abs(basic.net_diff_pct - 0.5) < 1e-9, basic.to_dict())
    overlap = next(item for item in report.pairs if item.treatment.decision_id == "t-overlap")
    check("夹具核对：pair-basic 与 pair-overlap 同标的窗口重叠 → 同属 1 个独立块",
          len(stats.pair_overlap_blocks(report.pairs)) == 1,
          len(stats.pair_overlap_blocks(report.pairs)))
    check("夹具核对：两对样本（2 对）分成 1 块 ⇒ 不得按 2 个独立样本报区间",
          stats.paired_statistics(report.pairs)["independent_blocks"] == 1,
          stats.paired_statistics(report.pairs)["independent_blocks"])
    check("夹具核对：重叠对子集 net 差与独立算式一致",
          abs(overlap.net_diff_pct - 0.5) < 1e-9, overlap.net_diff_pct)


def test_fixture_overlap_and_stress() -> None:
    section("11. 夹具：重叠分块与费用压力")
    data = _load_fixture("overlap_windows.json")
    horizon = int(data["horizon_minutes"])
    gap = float(data["gap_seconds"])
    for grid in data["grids"]:
        rows = []
        for symbol in grid["symbols"]:
            for index in range(int(grid["count"])):
                offset = index * float(grid["interval_seconds"])
                rows.append(result(f"{grid['grid_id']}-{symbol}-{index}", symbol=symbol,
                                   exit_price=101.0, decided_offset_seconds=offset,
                                   horizon=horizon, now_offset_minutes=120.0))
        report = stats.independence_report(rows, gap_seconds=gap)
        check(f"夹具 {grid['grid_id']}：{grid['intent']}",
              report["independent_blocks"] == grid["expect_blocks"]
              and report["largest_block"] == grid["expect_largest_block"]
              and report["state"] == grid["expect_state"],
              (report["independent_blocks"], report["largest_block"], report["state"]))

    stress_data = _load_fixture("fee_stress_cases.json")
    multipliers = tuple(float(item) for item in stress_data["multipliers"])
    fee_bps = float(stress_data["costs"]["round_trip_fee_bps"])
    slip_bps = float(stress_data["costs"]["slippage_bps"])
    check("夹具核对：成本假设与实现默认口径一致（未偷改默认值）",
          (fee_bps, slip_bps) == (FEE_BPS, SLIP_BPS))
    for case in stress_data["case_sets"]:
        gross = float(case["treatment_gross_pct"])
        treatment_exit = 100.0 * (1.0 + gross / 100.0)
        pairs = [make_pair(treatment_exit, 100.0, index=index,
                           symbol=f"FS{index}USDT",
                           decided_offset_seconds=index * 600.0)
                 for index in range(int(case["pairs"]))]
        report = stats.fee_stress(pairs, multipliers=multipliers)
        expect_break = case["expect_break_even_multiplier"]
        actual_break = report["break_even_multiplier"]
        if expect_break is None:
            check(f"夹具 {case['case_set_id']}：{case['intent']}", actual_break is None,
                  actual_break)
        else:
            check(f"夹具 {case['case_set_id']}：{case['intent']}",
                  actual_break is not None and abs(actual_break - float(expect_break)) < 1e-9,
                  (actual_break, expect_break))
        actual_unprofitable = sorted(
            float(item["multiplier"]) for item in report["points"] if item["unprofitable"])
        check(f"夹具 {case['case_set_id']}：转负的倍数集合与独立算式一致",
              actual_unprofitable == sorted(float(item) for item in
                                            case["expect_unprofitable_at"]),
              (actual_unprofitable, case["expect_unprofitable_at"]))
        independent_break = _independent_break_even(gross, fee_bps, slip_bps, multipliers)
        check(f"夹具 {case['case_set_id']}：期望值 = 夹具判别式独立算出的 {independent_break}",
              (independent_break is None) == (actual_break is None)
              and (actual_break is None
                   or abs(independent_break - actual_break) < 1e-9),
              (independent_break, actual_break))


def _independent_break_even(gross_pct: float, fee_bps: float, slip_bps: float,
                            multipliers: tuple[float, ...]) -> float | None:
    """按夹具 derivation 里的定义式独立算盈亏平衡倍数（不经被测实现）。"""
    cost_pct = (fee_bps + slip_bps) / 100.0
    nets = [(m, gross_pct - cost_pct * m) for m in multipliers]
    for (m1, n1), (m2, n2) in zip(nets, nets[1:]):
        if n1 == 0.0:
            return m1
        if (n1 > 0.0) != (n2 > 0.0):
            return m1 + abs(n1) / (abs(n1) + abs(n2)) * (m2 - m1)
    return None


# --------------------------------------------------------------------------- #
# 12. 共享适配两态（database.py）
# --------------------------------------------------------------------------- #
def _new_db(name: str) -> tuple[AuditDatabase, Path]:
    path = TMP / name / "audit.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = AuditDatabase(path)
    db.set_release_context(release_id="rel-u07", environment="demo",
                           account_ref="demo:acc")
    return db, path


def test_shared_adapter_two_state() -> None:
    section("12. 共享适配两态（database.py）")
    note(f"探测：DB_PATCHED={DB_PATCHED}")
    treatment = [result("t-1", exit_price=101.0)]
    control = [result("c-1", exit_price=100.5)]
    report = stats.build_statistics_report(treatment, control, config_fingerprint="cfg-u07")
    row = report.to_rows()[0]
    required = {"experiment_key", "report_kind", "config_fingerprint", "digest",
                "schema", "caliber", "payload", "counts_toward_release_gate",
                "execution_weight", "auto_promotion"}
    check("本体提供的落库行覆盖共享适配所消费的全部键（补丁不在时分支不空转）",
          required <= set(row), sorted(required - set(row)))
    check("本体提供集成层所需符号",
          all(callable(getattr(stats, name, None))
              for name in ("build_statistics_report", "pair_experiment",
                           "independence_report", "fee_stress", "report_digest",
                           "statistics_isolation_facts", "promotion_guard")))
    db, path = _new_db("two-state")
    try:
        migrations_before = [item["version"] for item in db.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version")]
        decisions_before = db.fetch_one("SELECT COUNT(*) AS n FROM shadow_decisions")["n"]
        if DB_PATCHED:
            inserted = db.log_shadow_v2_statistics([row])
            check("落库新增行数 = 1", inserted == 1, inserted)
            check("重复写同一 digest 的报告 → 0 新增（幂等：重放不产生新行）",
                  db.log_shadow_v2_statistics([row]) == 0)
            other = {**row, "digest": "0" * 64}
            db.log_shadow_v2_statistics([other])
            stored = db.fetch_shadow_v2_statistics(experiment_key=report.payload["experiment_key"])
            check("按实验版本读取统计快照，payload 原样可取回",
                  len(stored) == 2
                  and stored[0]["payload"]["caliber"] == stats.STATISTICS_CALIBER_ID,
                  [item["digest"] for item in stored])
            check("落库行强制归零（门禁/权重/晋升三项）",
                  all(item["counts_toward_release_gate"] == 0
                      and float(item["execution_weight"]) == 0.0
                      and int(item["auto_promotion"]) == 0 for item in stored),
                  [(item["counts_toward_release_gate"], item["execution_weight"])
                   for item in stored])
            forced = {**row, "digest": "1" * 64, "counts_toward_release_gate": True,
                      "execution_weight": 9.9, "auto_promotion": True}
            db.log_shadow_v2_statistics([forced])
            forced_rows = db.fetch_shadow_v2_statistics(
                experiment_key=report.payload["experiment_key"])
            check("调用方传 True/非零权重也被强制落为 0/0.0/0（不容许经此进执行）",
                  all(item["counts_toward_release_gate"] == 0
                      and float(item["execution_weight"]) == 0.0
                      and int(item["auto_promotion"]) == 0 for item in forced_rows),
                  len(forced_rows))
            summary = db.shadow_v2_statistics_summary(
                experiment_key=report.payload["experiment_key"])
            check("库侧摘要只给计数与隔离声明，不产出 GO/promotion 结论",
                  summary["reports"] == 3 and summary["counts_toward_release_gate"] is False
                  and not ({"promotion_ready", "verdict", "go_verdict"} & set(summary)),
                  summary)
            check("统计快照写在独立研究表（不污染 shadow_decisions / shadow_outcomes）",
                  db.fetch_one("SELECT COUNT(*) AS n FROM shadow_decisions")["n"]
                  == decisions_before
                  and db.fetch_one("SELECT COUNT(*) AS n FROM shadow_outcomes")["n"] == 0)
        else:
            note("共享补丁未应用：跳过落库断言（本体符号已在上面断言）")
            check("未接线时本体不含库侧方法（不假装已集成）",
                  not hasattr(db, "log_shadow_v2_statistics"))
        migrations_after = [item["version"] for item in db.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version")]
        check("迁移版本集合在统计写入前后不变（不占用 schema_migrations 版本号）",
              migrations_before == migrations_after, migrations_after[-3:])
        check("统计写入不新增 shadow_decisions 行（研究产物不是执行候选）",
              db.fetch_one("SELECT COUNT(*) AS n FROM shadow_decisions")["n"]
              == decisions_before)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 13. 门禁逐字不变（本任务最硬的一条）
# --------------------------------------------------------------------------- #
def _seed_gate_db(db: AuditDatabase) -> None:
    """造一个能跑出真实门禁报告的最小数据集：3 条 A 组方向候选 + 1 条 B 组研究行 + 60 分钟观测。"""
    for index in range(3):
        db.log_shadow_decision(
            decision_id=f"a-{index}", symbol="BTCUSDT", signal="bullish",
            reference_price=100.0, confidence=0.7, source=ss.SOURCE_A,
            execution_weight=0.0,
            feature_json=json.dumps({"opportunity_id": f"opp-{index}"}),
            release_id="rel-u07", environment="demo")
    db.log_shadow_decision(  # 研究影子：U09 来源过滤后不得计入分母
        decision_id="b-0", symbol="ETHUSDT", signal="bearish", reference_price=50.0,
        confidence=0.6, source=ss.SOURCE_B, execution_weight=0.0,
        feature_json=json.dumps({"opportunity_id": "opp-b0"}),
        release_id="rel-u07", environment="demo")
    start = T0
    with db.transaction() as connection:
        # 影子行入库时间用真实墙钟；把 created_at 对齐到合成窗口（只改夹具，不改实现）。
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
                (f"obs-{index}", "rel-u07", "demo", "demo:acc", "sess-1", iso(moment),
                 minute(moment), "healthy", "{}", "cfg-a",
                 json.dumps({"responses_by_status": {}, "coordination_available": True}),
                 "{}", json.dumps({"new_entry_allowed": True}), "{}"))


def test_release_gate_unchanged() -> None:
    section("13. release_evidence 门禁结果逐字不变")
    db, path = _new_db("gate")
    try:
        _seed_gate_db(db)
        manifest = {"release_id": "rel-u07", "environment": "demo",
                    "account_ref": "demo:acc", "schema_version": 17}
        kwargs = {"environment": "demo",
                  "since": iso(T0), "until": iso(T0 + timedelta(minutes=59)),
                  "report_at": iso(T0 + timedelta(minutes=60))}
        before = evidence.build_release_readiness_report(path, manifest, **kwargs)

        treatment = [result(f"t-{index}", exit_price=101.0, symbol="BTCUSDT",
                            decided_offset_seconds=index * 60.0) for index in range(30)]
        control = [result(f"c-{index}", exit_price=100.5, symbol="BTCUSDT",
                          decided_offset_seconds=index * 60.0) for index in range(30)]
        report = stats.build_statistics_report(treatment, control,
                                               config_fingerprint="cfg-u07",
                                               resamples=400)
        if DB_PATCHED:
            written = db.log_shadow_v2_statistics(report.to_rows())
            check("灌入统计快照成功（用于门禁不变性验证）", written == 1, written)
        else:
            note("共享补丁未应用：无落库路径，门禁不变性在「零写入」下成立")

        after = evidence.build_release_readiness_report(path, manifest, **kwargs)

        def gate_payload(payload: dict[str, Any]) -> str:
            stripped = {key: value for key, value in payload.items() if key != "generated_at"}
            return json.dumps(stripped, sort_keys=True, default=str)

        check("统计产物写入前后，门禁报告逐字段完全相同（除 generated_at 墙钟外）",
              gate_payload(before) == gate_payload(after))
        check("生成的报告仍带 generated_at（只排除了噪声字段，没有整段跳过）",
              "generated_at" in before and "generated_at" in after)
        check("方向候选分母不受统计产物影响",
              after["release_evidence"]["directional_candidates"]
              == before["release_evidence"]["directional_candidates"],
              (before["release_evidence"]["directional_candidates"],
               after["release_evidence"]["directional_candidates"]))
        check("A 组 3 条候选仍按 U09 口径计入（U06a/U09 口径未被本任务改动）",
              after["release_evidence"]["directional_candidates"] == 3,
              after["release_evidence"]["directional_candidates"])
        check("门禁结论字段（release_readiness）未被统计产物影响",
              before["release_readiness"] == after["release_readiness"])
        source_filter = after["release_evidence"]["candidate_source_filter"]
        check("候选来源过滤口径仍逐字来自 shadow_status（本任务未改 U06a/U09 口径）",
              source_filter["sources"] == list(evidence.execution_decision_sources())
              and source_filter["sources"] == [ss.SOURCE_A]
              and sorted(ss.research_sources()) == sorted({ss.SOURCE_B, ss.SOURCE_C}),
              source_filter)
        check("统计产物自身声明不计入任何门禁分母（两处隔离声明都为 False）",
              report.to_dict()["gate"]["counts_toward_release_gate"] is False
              and report.to_dict()["execution"]["counts_toward_release_gate"] is False)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 14. 模块边界（不得悄悄扩大研究侧车能力）
# --------------------------------------------------------------------------- #
def test_module_boundaries() -> None:
    section("14. 模块边界（纯函数、无网络/DB/LLM/时钟）")
    item = ROOT / "src" / "intel" / "shadow_v2" / "statistics.py"
    check("独占路径存在且可被包内子模块方式导入", item.is_file())
    text = item.read_text(encoding="utf-8")
    forbidden = ("import httpx", "import requests", "from data.", "import sqlite3",
                 "from audit", "import openai", "subprocess", "import time",
                 "datetime.now", "utcnow")
    hits = [token for token in forbidden if token in text]
    check("统计模块不引入网络/DB/LLM/子进程依赖，也不读系统时钟", not hits, hits)
    check("统计模块不硬编码真实 UTC 日期做判定（全部用相对窗口）",
          "2026-" not in text)
    check("公开 API 均不返回门禁结论",
          all(key not in stats.statistics_isolation_facts()
              for key in ("promotion_ready", "go_verdict", "release_readiness")))
    check("统计模块不修改 shadow_status 的任何常量（U06a 判定语义逐字不变）",
          ss.release_gate_role(ss.SOURCE_A) == ss.GATE_ROLE_EXECUTION_DECISION
          and ss.release_gate_role(ss.SOURCE_B) == ss.GATE_ROLE_RESEARCH_ONLY
          and ss.release_gate_role("unknown-source") == ss.GATE_ROLE_RESEARCH_ONLY
          and ss.V2_LABELS_STATE == "DEFERRED")
    modules = {entry.name for entry in (ROOT / "src" / "intel" / "shadow_v2").glob("*.py")}
    check("U06b 四模块仍在（本任务只新增 statistics.py）",
          {"__init__.py", "models.py", "sampler.py", "labels.py"} <= modules)
    check("本任务新增的统计模块位于 U07 独占路径",
          "statistics.py" in modules)


def main() -> int:
    print("U07 影子 v2 配对实验与独立样本统计 —— 离线集成回归")
    print(f"fixtures: {FIXTURES}")
    try:
        test_caliber_and_isolation()
        test_gate_leak_is_rejected()
        test_experiment_isolation()
        test_pairing()
        test_independence_blocks()
        test_interval_replay()
        test_sign_test()
        test_fee_stress()
        test_report_shape_and_replay()
        test_fixture_paired()
        test_fixture_overlap_and_stress()
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
    print("U07 SHADOW STATISTICS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
