"""U06a 影子研究状态：旧口径披露、C 未配置隔离与有界研究负载。

本模块是 D 影子研究线对「阴影样本是什么口径、能不能被信任、是否影响执行」
的唯一权威描述来源。它**只做判定与描述**，不写数据库、不发网络请求、不调用
LLM、不持有执行权重，也不实现 v2 标签（U06b）与配对统计（U07）。

三条硬不变量（对应任务卡 U06a 必达要点）：

1. **零执行权重**：任何影子组的执行权重恒为 ``0.0``；即使配置里写了非零
   ``execution_weight`` 或 ``shadow_only=false``，也只有“被忽略”并如实登记的
   结果（:func:`execution_weight_guard`），不存在“默默参与决策”的路径。
2. **旧口径可见**：A/B/C 三组的结果一律标记为旧口径（legacy v1）研究参考，
   附上已知口径缺陷与 ``v2_labels=DEFERRED``；人工评审标记不再暗示任何
   自动晋升（:func:`legacy_metric_view`）。
3. **门禁隔离**：B/C 为 ``research_only``，禁止计入 K06 发布门禁的
   「200 方向候选」与「30 完整闭合」（:func:`release_gate_role`）。200 门槛的
   业务口径由 U09 在 ``src/core/release_evidence.py`` 固定并版本化；本模块
   只提供按**证据来源**分组的事实，不做跨组替代。

第一批次（FIRST_RELEASE）范围仅限
``FIRST_RELEASE_LABEL_DISCLOSURE_C_UNCONFIGURED_ISOLATION_ONLY``：
旧口径标识 + C 未配置隔离 + 零执行权重，不含 v2 标签。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

SCHEMA_VERSION = "u06a-shadow-status-v1"

#: 旧口径标识：取价时点绑定在采集轮转刷新上，不是固定目标时刻。
LEGACY_CALIBER_ID = "legacy-v1-rolling-mark-price"
LEGACY_CALIBER_LABEL = "旧口径 v1（轮转刷新取价，仅研究参考）"

#: v2 固定时点标签未交付；首批只做标识与隔离，不得暗示已支持。
V2_LABELS_STATE = "DEFERRED"

#: F11 首批验收范围（U12 必须逐字记录 ``F11:v2_labels=DEFERRED``）。
FIRST_RELEASE_FAULT_SCOPE = "FIRST_RELEASE_LABEL_DISCLOSURE_C_UNCONFIGURED_ISOLATION_ONLY"

#: 影子组执行权重；常量而非配置，杜绝配置面把它抬起来。
EXECUTION_WEIGHT = 0.0

#: 只有明确方向才算“方向性样本”；neutral/unknown 不是绩效样本。
DIRECTIONAL_SIGNALS = frozenset({"bullish", "bearish"})

#: 门禁角色：执行决策副本 vs 纯研究。
GATE_ROLE_EXECUTION_DECISION = "execution_decision_copy"
GATE_ROLE_RESEARCH_ONLY = "research_only"

SOURCE_A = "A_current_baseline"
SOURCE_B = "B_contract_market"
SOURCE_C = "C_contract_plus_web3"


@dataclass(frozen=True)
class CohortSpec:
    """影子组的静态定义。

    Attributes:
        source: ``shadow_decisions.source`` 列的取值，也是唯一的分组键。
        group: 展示用的组名（A/B/C）。
        gate_role: :data:`GATE_ROLE_EXECUTION_DECISION` 或
            :data:`GATE_ROLE_RESEARCH_ONLY`；决定该组能否进入发布门禁分母。
        requires: 该组产生方向样本所必须显式配置的 ``web3_intel`` 键。
        description: 人类可读的一句话口径说明。
    """

    source: str
    group: str
    gate_role: str
    requires: tuple[str, ...]
    description: str


COHORTS: tuple[CohortSpec, ...] = (
    CohortSpec(
        source=SOURCE_A, group="A", gate_role=GATE_ROLE_EXECUTION_DECISION, requires=(),
        description="当前系统真实开仓意图的零权重副本；只作审计，不人为增造开仓样本。",
    ),
    CohortSpec(
        source=SOURCE_B, group="B", gate_role=GATE_ROLE_RESEARCH_ONLY, requires=(),
        description="币安合约公开比率规则（top trader/taker）的描述性因子研究。",
    ),
    CohortSpec(
        source=SOURCE_C, group="C", gate_role=GATE_ROLE_RESEARCH_ONLY,
        requires=("watched_wallets", "token_contracts"),
        description="合约情报叠加只读钱包方向的融合研究；未显式配置时不产生样本。",
    ),
)

_BY_SOURCE: dict[str, CohortSpec] = {spec.source: spec for spec in COHORTS}

#: 旧口径的已知缺陷；逐条写进披露对象，避免“看起来是官方绩效”。
LEGACY_LIMITATIONS: tuple[str, ...] = (
    "取价时点=轮转刷新时的当前标记价，可能明显晚于目标时刻（F11：5 分钟标签晚 8 分钟）。",
    "轮转/非优先标的在刷新时可能缺价，该样本直接消失；旧口径无法区分未成熟、缺价、"
    "超容差与数据断流（缺失原因标签属 U06b）。",
    "历史行未按实验版本/策略版本/配置/环境隔离，不同发布期的逻辑混在同一张表里。",
    "费用、滑点与资金费是固定研究假设（round_trip_fee_bps/slippage_bps/horizon÷480），"
    "不是真实成交成本。",
    "同轮相邻信号高度重叠，数千条样本不等于数千次独立交易；不能按平均收益排名。",
    "A/B/C 三组的交易时点、覆盖标的与机会集合不同，不是同一样本的随机实验，禁止互相证明。",
)


def cohort_spec(source: str) -> CohortSpec | None:
    """按 ``source`` 取影子组定义；未知来源返回 ``None``。

    Args:
        source: ``shadow_decisions.source`` 的取值。

    Returns:
        对应的 :class:`CohortSpec`；未知来源（历史脏数据）返回 ``None``。
    """
    return _BY_SOURCE.get(str(source or ""))


def is_directional(signal: Any) -> bool:
    """判断信号是否为明确方向（bullish/bearish）。

    Args:
        signal: 待判定的信号文本。

    Returns:
        ``True`` 表示可产生方向性绩效样本；``neutral``/``unknown``/空值均为 ``False``。
    """
    return str(signal or "").strip().lower() in DIRECTIONAL_SIGNALS


def release_gate_role(source: str) -> str:
    """返回该来源在发布门禁中的角色，供 U09 固定 200 候选口径时消费。

    Args:
        source: ``shadow_decisions.source`` 的取值。

    Returns:
        :data:`GATE_ROLE_RESEARCH_ONLY`（B/C，明确不得计入 200 方向候选与 30 完整闭合）
        或 :data:`GATE_ROLE_EXECUTION_DECISION`（A，真实开仓决策副本，其是否计入由
        U09 固定并版本化）。**未知来源一律按** :data:`GATE_ROLE_RESEARCH_ONLY`
        **处理**（fail-closed：来源不明的研究行不得充数）。
    """
    spec = cohort_spec(source)
    if spec is None:
        return GATE_ROLE_RESEARCH_ONLY
    return spec.gate_role


def is_research_shadow(source: str) -> bool:
    """``True`` 表示该来源是纯研究影子，不得计入发布门禁或执行候选。"""
    return release_gate_role(source) == GATE_ROLE_RESEARCH_ONLY


def execution_weight_guard(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """返回影子组**实际生效**的执行权重与配置声明冲突。

    影子侧车在任何情况下都不接受把执行权重抬起来的配置；冲突只登记、不生效。

    Args:
        config: ``web3_intel`` 配置节（缺失或非 Mapping 按空配置处理）。

    Returns:
        含 ``declared``/``declared_shadow_only``/``effective``/``conflict``/
        ``forced_fields``/``reason`` 的字典，可直接 JSON 序列化。
    """
    raw = dict(config) if isinstance(config, Mapping) else {}
    declared = raw.get("execution_weight")
    declared_shadow_only = raw.get("shadow_only")
    forced: list[str] = []
    declared_value: float | None = None
    if isinstance(declared, bool):
        forced.append("execution_weight:non_numeric")
    elif isinstance(declared, (int, float)):
        declared_value = float(declared)
        if declared_value != EXECUTION_WEIGHT:
            forced.append("execution_weight:non_zero")
    elif declared is not None:
        forced.append("execution_weight:non_numeric")
    if declared_shadow_only is not None and not bool(declared_shadow_only):
        forced.append("shadow_only:false")
    return {
        "declared": declared_value,
        "declared_shadow_only": (None if declared_shadow_only is None
                                 else bool(declared_shadow_only)),
        "effective": EXECUTION_WEIGHT,
        "auto_promotion": False,
        "conflict": bool(forced),
        "forced_fields": forced,
        "reason": ("配置声明的影子权重/模式被强制忽略，实际执行权重恒为 0"
                   if forced else "配置未声明非零影子权重，实际执行权重恒为 0"),
    }


def _enabled(config: Mapping[str, Any]) -> bool:
    return bool(config.get("enabled", True))


def _configured_keys(config: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    """返回 ``keys`` 中已显式配置（非空容器）的键名。"""
    present: list[str] = []
    for key in keys:
        value = config.get(key)
        if isinstance(value, Mapping):
            if value:
                present.append(key)
        elif isinstance(value, (list, tuple, set)):
            if value:
                present.append(key)
        elif value not in (None, ""):
            present.append(key)
    return present


def cohort_configuration(config: Mapping[str, Any] | None, *,
                         credential_available: bool | None = None) -> dict[str, dict[str, Any]]:
    """逐组判定配置状态，供 UI 显示 ``UNCONFIGURED`` 与覆盖指标。

    Args:
        config: ``web3_intel`` 配置节。
        credential_available: 只读链上数据凭据是否可用（``None`` 表示不判定）；
            显式 ``False`` 时 C 组标 ``credentials_missing``，同样不产生样本。

    Returns:
        以 ``source`` 为键的字典，每项含 ``group``/``status``/``missing``/
        ``description``/``emits_samples``。``status`` 取值：
        ``disabled``/``unconfigured``/``credentials_missing``/``configured``。
    """
    raw = dict(config) if isinstance(config, Mapping) else {}
    enabled = _enabled(raw)
    out: dict[str, dict[str, Any]] = {}
    for spec in COHORTS:
        present = _configured_keys(raw, spec.requires)
        missing = [key for key in spec.requires if key not in present]
        if not enabled:
            status = "disabled"
        elif missing:
            status = "unconfigured"
        elif spec.source == SOURCE_C and credential_available is False:
            status = "credentials_missing"
        else:
            status = "configured"
        out[spec.source] = {
            "group": spec.group,
            "status": status,
            "missing": missing,
            "configured_keys": present,
            "gate_role": spec.gate_role,
            "emits_samples": enabled and not missing and status == "configured",
            "description": spec.description,
        }
    return out


def emission_plan(config: Mapping[str, Any] | None) -> dict[str, bool]:
    """返回本轮允许写样本行的组（静态配置口径，不含运行期事件判定）。

    未知方向、缺价以及未配置的组一律 ``False``：研究侧车宁可少写样本，也不拿
    空/无效数据充样本。

    Args:
        config: ``web3_intel`` 配置节。

    Returns:
        以 ``source`` 为键的布尔字典。
    """
    state = cohort_configuration(config)
    return {source: bool(item["emits_samples"]) for source, item in state.items()}


def should_emit_sample(*, source: str, config: Mapping[str, Any] | None,
                       signal: Any, reference_price: Any) -> bool:
    """判定一条影子样本行是否允许落库。

    三个条件同时成立才允许写入：组已配置且启用、信号方向明确、参考价有效。
    这是「C 未配置不产生无效绩效样本」的最后一道闸门。

    Args:
        source: ``shadow_decisions.source``。
        config: ``web3_intel`` 配置节。
        signal: 该行信号。
        reference_price: 该行参考价（``None``/非正/非数值均视为无效）。

    Returns:
        允许写入返回 ``True``，否则 ``False``。
    """
    try:
        price = float(reference_price)
    except (TypeError, ValueError):
        return False
    if price <= 0 or not is_directional(signal):
        return False
    spec = cohort_spec(source)
    if spec is None:
        return False
    return bool(emission_plan(config).get(source, False))


def sample_marker(source: str) -> dict[str, Any]:
    """返回写入 ``feature_json`` 的 ``shadow_cohort`` 标记。

    标记是**只追加**的诊断键：既有消费者（``evaluate_shadow_decisions`` 读
    ``contract_market``/``funding_rate``、``release_evidence`` 读
    ``opportunity_id``/``opportunity_key``）不受影响。

    Args:
        source: ``shadow_decisions.source``。

    Returns:
        含组别、口径、门禁角色与 v2 标签状态的字典。
    """
    spec = cohort_spec(source)
    return {
        "schema": SCHEMA_VERSION,
        "source": str(source or ""),
        "group": spec.group if spec else "?",
        "gate_role": release_gate_role(source),
        "research_only": is_research_shadow(source),
        "caliber": LEGACY_CALIBER_ID,
        "execution_weight": EXECUTION_WEIGHT,
        "v2_labels": V2_LABELS_STATE,
        "counts_toward_release_gate": False,
    }


def candidate_facts(*, new_entry_allowed: bool | None = None,
                    allow_new_reason: str | None = None,
                    blocked_reason: str | None = None) -> dict[str, Any]:
    """规范化 A 组样本的“候选资格事实”，只记录、不判定。

    口径归属 U09：本函数只负责把管理周期的既有事实（是否允许新开仓、原因码）
    随样本一起留痕，便于按证据来源重算 200 候选。

    Args:
        new_entry_allowed: 该轮是否允许新开仓（``None`` 表示未知）。
        allow_new_reason: 允许/阻断的原因码文本。
        blocked_reason: 阻断原因（若有）。

    Returns:
        可直接放进 ``feature_json`` 的字典。
    """
    return {
        "new_entry_allowed": (None if new_entry_allowed is None
                              else bool(new_entry_allowed)),
        "allow_new_reason": str(allow_new_reason or "")[:200],
        "blocked_reason": str(blocked_reason or "")[:200],
    }


def missing_sample_accounting(*, decided: int, labeled: int,
                              unavailable_reason: str | None = None) -> dict[str, Any]:
    """计算旧口径的标签覆盖率与缺失计数（不做静默丢弃）。

    Args:
        decided: 已写入的方向性影子决策数（窗口内）。
        labeled: 其中已生成过任一 horizon 结果的决策数。
        unavailable_reason: 查询不可用时的原因（此时只回状态，不编造数字）。

    Returns:
        含 ``state``/``decided``/``labeled``/``missing``/``coverage`` 与
        ``reason_unavailable`` 的字典；``state`` 为 ``OK``/``UNAVAILABLE``。
    """
    if unavailable_reason:
        return {
            "state": "UNAVAILABLE",
            "decided": None,
            "labeled": None,
            "missing": None,
            "coverage": None,
            "reason_unavailable": str(unavailable_reason)[:200],
            "selective_drop": False,
            "note": "缺失计数不可用时如实标 UNAVAILABLE，不用 0 冒充“无缺失”。",
        }
    total = max(0, int(decided))
    done = max(0, min(int(labeled), total))
    missing = total - done
    return {
        "state": "OK",
        "decided": total,
        "labeled": done,
        "missing": missing,
        "coverage": (round(done / total, 6) if total else None),
        "reason_unavailable": None,
        "selective_drop": False,
        "note": ("旧口径只能给出缺失总数，无法区分未成熟/缺价/超容差/断流；"
                 "原因标签属 U06b。缺失样本按计数披露，不做静默丢弃。"),
    }


def research_load_plan(*, elapsed_ms: float, budget_ms: float,
                       metrics_age_seconds: float | None,
                       metrics_refresh_seconds: float) -> dict[str, Any]:
    """把研究负载约束成有界决策：超预算时降级可选阶段，且留痕。

    Args:
        elapsed_ms: 本轮采集已耗时（毫秒）。
        budget_ms: 研究侧车单轮预算（毫秒）。
        metrics_age_seconds: 上一次汇总指标距今秒数（``None`` 表示从未计算）。
        metrics_refresh_seconds: 汇总指标最小重算间隔（秒）。

    Returns:
        含 ``allow_labeling``/``allow_metrics``/``degraded``/``budget_ms`` 的字典；
        ``degraded`` 为空表示本轮未降级。
    """
    budget = max(1.0, float(budget_ms))
    spent = max(0.0, float(elapsed_ms))
    over = spent > budget
    metrics_due = metrics_age_seconds is None or (
        float(metrics_age_seconds) >= max(0.0, float(metrics_refresh_seconds)))
    degraded: list[str] = []
    if over:
        degraded.append("research_budget_exceeded")
    if not metrics_due:
        degraded.append("metrics_cache_fresh")
    return {
        "allow_labeling": not over,
        "allow_metrics": bool(metrics_due and not over),
        "metrics_due": bool(metrics_due),
        "degraded": degraded,
        "elapsed_ms": round(spent, 3),
        "budget_ms": round(budget, 3),
    }


def isolation_facts(*, degraded_stages: tuple[str, ...] = (),
                    llm_calls: int = 0, skipped_emissions: int = 0,
                    thread_name: str = "readonly-intel-sidecar") -> dict[str, Any]:
    """返回侧车隔离事实（线程/LLM/写权），供 UI 与故障报告引用。

    Args:
        degraded_stages: 本轮被降级的研究阶段名。
        llm_calls: 本轮侧车发起的 LLM 调用次数（恒为 0）。
        skipped_emissions: 因未配置或非方向而**未**写库的样本行数。
        thread_name: 侧车线程名。

    Returns:
        含线程、LLM、Broker、网络预算与降级信息的字典。
    """
    return {
        "thread": thread_name,
        "dedicated_thread": True,
        "llm_calls": int(llm_calls),
        "broker_access": False,
        "order_capability": False,
        "network_budget": "shared_public_market_limiter",
        "degraded_stages": list(degraded_stages),
        "skipped_emissions": int(skipped_emissions),
    }


def legacy_metric_view(evaluations: Any, *,
                       sample_loss: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """把旧口径的聚合结果规范成“研究参考”视图（执行权重恒 0）。

    Args:
        evaluations: ``AuditDatabase.intel_observability()`` 的 ``evaluations``
            列表；非列表输入按空处理。
        sample_loss: ``{source: 覆盖率字典}``，来自 :func:`missing_sample_accounting`。

    Returns:
        与输入同序的行列表；每行新增 ``caliber``/``caliber_label``/
        ``execution_weight``/``release_gate_eligible``/``v2_labels``/
        ``gate_role``/``review_effect``/``review_label``/``sample_loss``。
        ``promotion_ready`` 保留原值但语义降级为“研究评审参考”。
    """
    rows: list[dict[str, Any]] = []
    loss = dict(sample_loss) if isinstance(sample_loss, Mapping) else {}
    for raw in evaluations if isinstance(evaluations, list) else []:
        item = dict(raw) if isinstance(raw, Mapping) else {}
        source = str(item.get("source") or "")
        research = is_research_shadow(source)
        rows.append({
            **item,
            "caliber": LEGACY_CALIBER_ID,
            "caliber_label": LEGACY_CALIBER_LABEL,
            "group": (cohort_spec(source).group if cohort_spec(source) else "?"),
            "gate_role": release_gate_role(source),
            "research_only": research,
            "execution_weight": EXECUTION_WEIGHT,
            "auto_promotion": False,
            "release_gate_eligible": False,
            "v2_labels": V2_LABELS_STATE,
            "review_effect": "none",
            "review_label": ("研究评审参考（不改变执行权重）"
                             if item.get("promotion_ready") else "样本不足，继续观察"),
            "sample_loss": dict(loss.get(source) or {}),
        })
    return rows


def build_disclosure(*, config: Mapping[str, Any] | None,
                     metrics: Mapping[str, Any] | None = None,
                     sample_loss: Mapping[str, Any] | None = None,
                     isolation: Mapping[str, Any] | None = None,
                     cohort_state: Mapping[str, Any] | None = None,
                     weight_guard: Mapping[str, Any] | None = None,
                     load_plan: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """组装给 UI 与下游消费者共用的影子披露对象。

    Args:
        config: ``web3_intel`` 配置节。
        metrics: 汇总指标缓存信息（``state``/``computed_at``/``age_seconds``）。
        sample_loss: 按来源的标签覆盖率字典。
        isolation: :func:`isolation_facts` 的结果。
        cohort_state: :func:`cohort_configuration` 的结果（缺省时重新计算）。
        weight_guard: :func:`execution_weight_guard` 的结果（缺省时重新计算）。
        load_plan: :func:`research_load_plan` 的结果。

    Returns:
        可 JSON 序列化的披露字典，含口径、组配置、门禁角色、v2 标签状态与
        隔离事实；缺省项用 ``None`` 表示“未知”，不以空值冒充正常。
    """
    raw = dict(config) if isinstance(config, Mapping) else {}
    return {
        "schema": SCHEMA_VERSION,
        "caliber": {
            "id": LEGACY_CALIBER_ID,
            "label": LEGACY_CALIBER_LABEL,
            "state": "LEGACY_ACTIVE",
            "research_only": True,
            "limitations": list(LEGACY_LIMITATIONS),
        },
        "execution_weight": dict(weight_guard or execution_weight_guard(raw)),
        "v2_labels": {
            "state": V2_LABELS_STATE,
            "owner": "U06b（固定时点标签）/ U07（配对统计）",
            "first_release_scope": FIRST_RELEASE_FAULT_SCOPE,
            "note": ("v2 固定时点标签与配对统计为后置任务，首批不实现、不验收；"
                     "U12 必须记录 F11:v2_labels=DEFERRED。"),
        },
        "release_gate": {
            "shadow_counted": False,
            "counted_toward": {
                "directional_candidates": False,
                "eligible_closed_trades": False,
                "observation_days": False,
            },
            "role_by_source": {spec.source: spec.gate_role for spec in COHORTS},
            "note": ("影子样本不得计入 K06 发布门槛；200 方向候选的业务口径由 U09 "
                     "在 src/core/release_evidence.py 固定并版本化，禁止跨组替代。"),
        },
        "cohorts": dict(cohort_state or cohort_configuration(raw)),
        "sample_loss": dict(sample_loss or {}),
        "metrics": (dict(metrics) if isinstance(metrics, Mapping) else None),
        "load_plan": (dict(load_plan) if isinstance(load_plan, Mapping) else None),
        "isolation": dict(isolation or isolation_facts()),
        "enabled": _enabled(raw),
        "shadow_only": True,
    }
