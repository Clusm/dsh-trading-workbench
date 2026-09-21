"""U06b 影子 v2 固定时点标签（后置研究线）。

**这不是首批功能。** 首批 F11 的记录保持 ``v2_labels=DEFERRED``（见
``src/intel/shadow_status.py``）；本包是 W4+ 后置线交付，实现被推迟的 v2 标签能力，
且**不参与**发布门禁、**不**给执行权重、**不**自动晋升。

对外 API 一览：

* :func:`~intel.shadow_v2.sampler.target_at` / ``window_bounds``：固定目标时点与容差窗口；
* :func:`~intel.shadow_v2.sampler.build_sampling_plan`：独立全白名单采样计划（有界）；
* :func:`~intel.shadow_v2.sampler.clock_guard`：时钟可信度（F17）；
* :func:`~intel.shadow_v2.labels.label_request` / ``label_batch`` / ``build_batch``：
  逐请求标签与批量产物；
* :func:`~intel.shadow_v2.labels.coverage_report`：逐原因覆盖率（未成熟/缺价/超容差/断流）；
* :func:`runtime_status`：给集成层（service.py）的只读状态；
* :func:`gate_isolation_facts`：机器可读的「不进门禁」事实。

两态说明：``SHADOW_V2_ENABLED_BY_DEFAULT`` 恒为 ``False``——打开它是**显式配置**
行为，而不是本包默认打开；集成层据此在「未接线」与「已接线但禁用」两支上都能断言。
"""
from __future__ import annotations

from typing import Any, Mapping

from intel import shadow_status as _ss

from intel.shadow_v2.labels import (  # noqa: F401
    DEFAULT_ROUND_TRIP_FEE_BPS,
    DEFAULT_SLIPPAGE_BPS,
    LabelBatch,
    build_batch,
    coverage_report,
    label_batch,
    label_request,
)
from intel.shadow_v2.models import (  # noqa: F401
    CLOSED_WINDOW_REASONS,
    COUNTS_TOWARD_RELEASE_GATE,
    DEFAULT_EXPECTED_INTERVAL_SECONDS,
    DEFAULT_TOLERANCE_SECONDS,
    LEGACY_CALIBER_ID,
    MAX_TOLERANCE_SECONDS,
    MISSING_REASONS,
    POST_RELEASE_SCOPE_NOTE,
    REASON_CLOCK_UNTRUSTED,
    REASON_DATA_GAP,
    REASON_NO_PRICE,
    REASON_NOT_MATURED,
    REASON_OVER_TOLERANCE,
    SCHEMA_VERSION,
    STATE_LABELED,
    STATE_MISSING,
    STATE_PENDING,
    V2_CALIBER_ID,
    V2_CALIBER_LABEL,
    ExperimentIdentity,
    LabelRequest,
    LabelResult,
    LeakageError,
    PriceSample,
    gate_isolation_facts,
    parse_ts,
)
from intel.shadow_v2.sampler import (  # noqa: F401
    CLOCK_STABLE,
    CLOCK_UNTRUSTED,
    ClockSnapshot,
    ClockVerdict,
    SamplingPlan,
    SamplingTarget,
    assert_no_future,
    build_sampling_plan,
    clamp_tolerance,
    clock_guard,
    group_samples,
    samples_before,
    select_sample,
    target_at,
    window_bounds,
)

#: 本包默认**不**启用：后置研究线不得凭「代码已存在」改变运行行为。
SHADOW_V2_ENABLED_BY_DEFAULT = False

#: 本包**不**宣称首批 F11 已验收；集成/文档必须保留 DEFERRED 记录。
FIRST_RELEASE_F11_STATE_UNCHANGED = _ss.V2_LABELS_STATE

__all__ = [
    "CLOCK_STABLE",
    "CLOCK_UNTRUSTED",
    "CLOSED_WINDOW_REASONS",
    "COUNTS_TOWARD_RELEASE_GATE",
    "DEFAULT_EXPECTED_INTERVAL_SECONDS",
    "DEFAULT_ROUND_TRIP_FEE_BPS",
    "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_TOLERANCE_SECONDS",
    "ExperimentIdentity",
    "FIRST_RELEASE_F11_STATE_UNCHANGED",
    "LEGACY_CALIBER_ID",
    "LabelBatch",
    "LabelRequest",
    "LabelResult",
    "LeakageError",
    "MAX_TOLERANCE_SECONDS",
    "MISSING_REASONS",
    "POST_RELEASE_SCOPE_NOTE",
    "PriceSample",
    "REASON_CLOCK_UNTRUSTED",
    "REASON_DATA_GAP",
    "REASON_NOT_MATURED",
    "REASON_NO_PRICE",
    "REASON_OVER_TOLERANCE",
    "SCHEMA_VERSION",
    "SHADOW_V2_ENABLED_BY_DEFAULT",
    "STATE_LABELED",
    "STATE_MISSING",
    "STATE_PENDING",
    "SamplingPlan",
    "SamplingTarget",
    "V2_CALIBER_ID",
    "V2_CALIBER_LABEL",
    "assert_no_future",
    "build_batch",
    "build_sampling_plan",
    "clamp_tolerance",
    "clock_guard",
    "coverage_report",
    "gate_isolation_facts",
    "group_samples",
    "label_batch",
    "label_request",
    "parse_ts",
    "runtime_status",
    "samples_before",
    "select_sample",
    "target_at",
    "window_bounds",
]


def runtime_status(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """给集成层的**只读**状态：v2 是否启用、口径、以及它不进门禁的事实。

    Args:
        config: ``web3_intel`` 配置节；只读取 ``shadow_v2.enabled``，不修改任何值。

    Returns:
        可 JSON 序列化的状态字典。``enabled`` 恒为 ``False``，除非配置里
        ``shadow_v2.enabled`` 显式为真；即使启用，``counts_toward_release_gate``
        与 ``execution_weight`` 仍分别为 ``False`` / ``0.0``。
    """
    raw = dict(config) if isinstance(config, Mapping) else {}
    section = raw.get("shadow_v2")
    declared = False
    if isinstance(section, Mapping):
        declared = bool(section.get("enabled", False))
    return {
        "schema": SCHEMA_VERSION,
        "caliber": V2_CALIBER_ID,
        "caliber_label": V2_CALIBER_LABEL,
        "legacy_caliber": LEGACY_CALIBER_ID,
        "enabled": declared,
        "enabled_by_default": SHADOW_V2_ENABLED_BY_DEFAULT,
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        "execution_weight": _ss.EXECUTION_WEIGHT,
        "auto_promotion": False,
        "first_release_v2_labels": FIRST_RELEASE_F11_STATE_UNCHANGED,
        "note": POST_RELEASE_SCOPE_NOTE,
    }
