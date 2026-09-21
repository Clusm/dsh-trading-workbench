"""发布验收证据：只读读取按版本隔离的样本并计算上线门禁。

U09 在**既有计算器**上修口径、补证据，不新建第二套 GO 判断器（K06 只冻结输出
结构，最终门禁仍由 :func:`build_release_readiness_report` 与发布审查决定）。
本模块相对基线的六条修复：

1. **来源过滤（S08-SHADOW-GATE-LEAK）**：方向候选分母只计入 ``intel.shadow_status``
   判定为 ``execution_decision_copy`` 的 ``shadow_decisions.source``；B/C 研究影子
   行只在诊断字段里出现，不得冒充执行候选。来源口径**只**从
   :mod:`intel.shadow_status` 派生，本模块不持有第二份 source 名单。
2. **纯 NO_EDGE 不计分母**：只有明确方向（bullish/bearish）且未声明"无边际/被拒绝"
   的机会才进 200 分母；拒绝型/无信号不得凑数。
3. **观察窗口来自声明而非数据**：窗口边界取自**声明的发布观察窗口**（发布起始 /
   报告时刻 / 调用方显式入参）。绝不自动截到第一条/最后一条健康记录；首尾缺失
   照计进分母（规格 §12）。
4. **lifecycle 去重 + 修订重算**：完整闭合按 ``lifecycle_id`` 去重，迟到到达或
   被修订的成本证据取**最新修订**并重算净收益，不重复计数、不虚报覆盖（F23）。
5. **ACK ≠ 恢复**：外部告警"已 ACK"不得读成已送达/已恢复（K05 口径），
   incident 的 ``acknowledged_at`` 同样不等于 ``resolved_at``。
6. **重启 ≠ 换版**：同工件的进程重启（多 session、同 config_hash）不与不同
   RC/配置的换版混为一谈（F24）。

K06 五组（engineering / accounting / runtime_observation / operations /
strategy_evaluation）由冻结契约 :mod:`core.contracts.k06_release` 独立评估：
``NO_DATA != PASS``、组与组独立、缺证据必须列阻断项。报告不新增整体 GO 字段，
既有 ``release_readiness`` 字段与派生方式保持不变。
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

# U06a 是"影子样本是什么口径、能不能进发布门禁分母"的**唯一权威来源**。
# 这里只消费它的判定函数与常量，绝不在本模块复制第二份 A/B/C 名单。
from intel.shadow_status import (
    GATE_ROLE_EXECUTION_DECISION,
    DIRECTIONAL_SIGNALS,
    is_directional,
    release_gate_role,
)

from core.contracts.common import canonical_hash
from core.contracts.k06_release import (
    CODE_IDENTITY_REF_MISSING,
    CODE_MISSING_EVIDENCE,
    CODE_WINDOW_INVALID,
    CODE_WINDOW_MISSING,
    build_release_report,
)

#: U09 证据版本标记（两态验收据此判断 ``release_evidence.py` 是否已升级）。
U09_EVIDENCE_VERSION = "u09.release_evidence.v2"

#: 共享适配补丁（``core/health_watch``）缺失时的显式原因码：宁可 NO_DATA 也不猜。
CODE_SHARED_PATCH_MISSING = "U09_SHARED_PATCH_MISSING"

#: 上游只"接受/ACK"而没有真实送达回执（F12）。
CODE_ACK_WITHOUT_DELIVERY = "ACK_WITHOUT_DELIVERY"
#: incident 已 ACK 但未 resolved（ACK ≠ 恢复）。
CODE_ACK_WITHOUT_RECOVERY = "ACK_WITHOUT_RECOVERY"
#: 观察窗口内混入不同发布（release_id / 配置）的样本（F24）。
CODE_MIXED_RELEASE_SAMPLES = "MIXED_RELEASE_SAMPLES"
CODE_MIXED_CONFIG_SAMPLES = "MIXED_CONFIG_SAMPLES"
#: 成本/归因证据自相矛盾（同一生命周期同一修订时点内容不同，F23）。
CODE_REVISION_CONFLICT = "REVISION_CONFLICT"

PASS = "PASS"
FAIL = "FAIL"
NO_DATA = "NO_DATA"

WINDOW_INVALID = CODE_WINDOW_INVALID
WINDOW_MISSING = CODE_WINDOW_MISSING

#: K06 窗口毫秒必须落在 int64 内；超界一律 ``WINDOW_INVALID``（不崩溃）。
_INT64_LIMIT = 2 ** 63

WINDOW_SOURCE_ARGUMENT = "argument"
WINDOW_SOURCE_MANIFEST = "manifest"
WINDOW_SOURCE_REPORT_CLOCK = "report_clock"
WINDOW_SOURCE_NONE = "undeclared"

#: 先看显式入参，再看清单里声明的观察窗口；两边都没有时**不猜**（见 WINDOW_MISSING）。
_MANIFEST_START_KEYS = (
    "observation_window_start", "observation_started_at", "release_started_at",
    "release_start", "observation_start", "started_at",
)
_MANIFEST_END_KEYS = (
    "observation_window_end", "observation_ended_at", "release_ended_at",
    "release_end", "observation_end", "report_at",
)
#: 清单里可声明"运行工件身份"的键（K01 命名优先，其次既有 release_manifest 命名）。
_ARTIFACT_HASH_KEYS = ("artifact_hash", "content_hash", "package_hash")
_IDENTITY_KEYS = ("runtime_instance_id", "identity_ref", "instance_id")

#: 「无边际 / 被拒绝」的特征标记；命中即不得计入方向候选分母（F24「纯 NO_EDGE 凑 200」）。
_NO_EDGE_FLAG_KEYS = ("no_edge", "no_edge_reason", "rejected")
_NO_EDGE_REASON_KEYS = ("reason_code", "primary_reason", "decision_reason",
                        "decision_reasons", "reject_reason")
_NO_EDGE_REASON_VALUES = frozenset({"NO_EDGE"})
_NO_EDGE_ACTIONS = frozenset({"skip", "hold", "none", "reject", "ignore", "no_edge"})


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


# --------------------------------------------------------------------------- #
# 声明窗口解析（K06：畸形窗口 FAIL + WINDOW_INVALID，报告不崩溃）
# --------------------------------------------------------------------------- #
def _epoch_ms(value: Any) -> int | None:
    """把"整数毫秒"解析成 int；非数值 / 非有限 / 非整数 / 超 int64 返回 None。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number != int(number):
        return None
    milliseconds = int(number)
    if not -_INT64_LIMIT <= milliseconds < _INT64_LIMIT:
        return None
    return milliseconds


def _datetime_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _ms_iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc).isoformat(
        timespec="milliseconds")


def parse_window_bound(value: Any, *, field: str) -> dict[str, Any]:
    """宽松解析单个窗口边界，**永不抛异常**。

    接受 ISO 文本或 epoch **毫秒**（与 K06 ``window_start_ms`` 同口径）。返回
    ``declared``（调用方/清单是否声明了这个边界）、``ms``/``iso`` 与 ``code``
    （``WINDOW_INVALID`` 表示声明了但不可用；缺席不给 code，由调用方决定是否
    ``WINDOW_MISSING``）。
    """
    info: dict[str, Any] = {
        "field": field, "declared": False, "raw": None,
        "ms": None, "iso": None, "code": None,
    }
    if value is None:
        return info
    if isinstance(value, bool):
        info.update(declared=True, raw=str(value), code=WINDOW_INVALID)
        return info
    if isinstance(value, (int, float)):
        info.update(declared=True, raw=value)
        milliseconds = _epoch_ms(value)
        if milliseconds is None:
            info["code"] = WINDOW_INVALID
        else:
            info.update(ms=milliseconds, iso=_ms_iso(milliseconds))
        return info
    text = str(value).strip()
    if not text:
        return info
    info.update(declared=True, raw=text)
    # JSON 的 Infinity/1e400 会解析成 inf；字符串 "inf"/"1e400" 同样必须拒绝，
    # 不能让报告在 datetime.fromtimestamp 上抛异常。
    milliseconds = _epoch_ms(text)
    if milliseconds is not None:
        info.update(ms=milliseconds, iso=_ms_iso(milliseconds))
        return info
    parsed = parse_iso(text)
    if parsed is None:
        info["code"] = WINDOW_INVALID
        return info
    info.update(ms=_datetime_ms(parsed), iso=parsed.isoformat(timespec="milliseconds"))
    return info


def _first_present(source: Mapping[str, Any], keys: Sequence[str]) -> tuple[str | None, Any]:
    for key in keys:
        if key in source and source.get(key) not in (None, ""):
            return key, source.get(key)
    return None, None


def resolve_release_window(
    *, since: Any = None, until: Any = None, manifest: Mapping[str, Any] | None = None,
    report_at: Any = None,
) -> dict[str, Any]:
    """解析发布观察窗口：**声明优先，数据不自证**。

    边界来源优先级：

    1. 调用方显式 ``since``/``until``；
    2. 清单里声明的观察窗口（``observation_window_start`` / ``release_started_at`` …）；
    3. 报告时刻（仅用于窗口**结束**，即规格 §12 的"报告时刻"）。

    任何情况下都**不**把第一条/最后一条健康记录当成窗口边界（§12 明令禁止）。
    起止任一不可解析 / 非有限 / 超 int64 毫秒 → ``FAIL`` + ``WINDOW_INVALID``；
    缺少起始声明 → ``FAIL`` + ``WINDOW_MISSING``。绝不抛异常。
    """
    manifest_data = dict(manifest or {}) if isinstance(manifest, Mapping) else {}
    start = parse_window_bound(since, field="since")
    start_source = WINDOW_SOURCE_ARGUMENT if start["declared"] else WINDOW_SOURCE_NONE
    end = parse_window_bound(until, field="until")
    end_source = WINDOW_SOURCE_ARGUMENT if end["declared"] else WINDOW_SOURCE_NONE
    if not start["declared"]:
        key, value = _first_present(manifest_data, _MANIFEST_START_KEYS)
        if key is not None:
            start = parse_window_bound(value, field=key)
            start_source = WINDOW_SOURCE_MANIFEST
    if not end["declared"]:
        key, value = _first_present(manifest_data, _MANIFEST_END_KEYS)
        if key is not None:
            end = parse_window_bound(value, field=key)
            end_source = WINDOW_SOURCE_MANIFEST
    if not end["declared"]:
        clock = report_at if report_at is not None else datetime.now(timezone.utc)
        parsed_clock = clock if isinstance(clock, datetime) else parse_iso(clock)
        if parsed_clock is not None:
            if parsed_clock.tzinfo is None:
                parsed_clock = parsed_clock.replace(tzinfo=timezone.utc)
            parsed_clock = parsed_clock.astimezone(timezone.utc)
            end = {
                "field": "report_clock", "declared": True,
                "raw": parsed_clock.isoformat(), "ms": _datetime_ms(parsed_clock),
                "iso": parsed_clock.isoformat(timespec="milliseconds"), "code": None,
            }
            end_source = WINDOW_SOURCE_REPORT_CLOCK

    codes: list[str] = []
    for bound in (start, end):
        if bound.get("code"):
            codes.append(str(bound["code"]))
    if not start["declared"]:
        codes.append(WINDOW_MISSING)
    if not end["declared"]:
        codes.append(WINDOW_MISSING)
    if (start.get("ms") is not None and end.get("ms") is not None
            and int(end["ms"]) <= int(start["ms"])):
        codes.append(WINDOW_INVALID)
    ordered = tuple(dict.fromkeys(codes))
    return {
        "state": PASS if not ordered else FAIL,
        "reason_codes": ordered,
        "since": start.get("iso"),
        "until": end.get("iso"),
        "since_ms": start.get("ms"),
        "until_ms": end.get("ms"),
        "since_source": start_source,
        "until_source": end_source,
        "since_declared": bool(start["declared"]),
        "until_declared": bool(end["declared"]),
        "invalid_fields": [
            {"field": bound["field"], "code": bound["code"], "raw": bound["raw"]}
            for bound in (start, end) if bound.get("code")
        ],
        # 明示：窗口不是从观察数据里推出来的（防"自动截到首末健康记录"回归）。
        "derived_from_observations": False,
    }


# --------------------------------------------------------------------------- #
# 观察覆盖
# --------------------------------------------------------------------------- #
def observation_coverage(
    observations: Sequence[Mapping[str, Any]], *, since: Any = None, until: Any = None,
    minimum_coverage: float = 0.999, max_gap_seconds: float = 120.0,
    window_code: str | None = None,
) -> dict[str, Any]:
    """计算健康分钟样本覆盖率与最大缺口；窗口缺失时明确返回 ``NO_DATA``。

    ``since``/``until`` **必须**来自声明的发布观察窗口；本函数不再退回"第一条/
    最后一条健康记录"（规格 §12）。首尾缺失照计入分母，并以
    ``head_missing_count``/``tail_missing_count`` 单独披露。
    ``window_code`` 由调用方在窗口字段畸形时传入 ``WINDOW_INVALID``，此时直接
    ``FAIL`` 而不是抛异常。
    """
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

    base: dict[str, Any] = {
        "coverage": 0.0,
        "observed_count": 0,
        "expected_count": 0,
        "healthy_count": len(healthy),
        "observation_days": 0.0,
        "observed_span_days": 0.0,
        "max_gap_seconds": None,
        "missing_count": 0,
        "missing_minutes": [],
        "head_missing_count": 0,
        "tail_missing_count": 0,
        "since": start.isoformat() if start else None,
        "until": end.isoformat() if end else None,
        "window_code": window_code,
        "derived_from_observations": False,
    }
    if window_code:
        return {**base, "state": FAIL, "reason_codes": [str(window_code)]}
    if start is None or end is None:
        # 窗口未声明：不得用首末健康记录顶上，只能 NO_DATA + 阻断项。
        return {**base, "state": NO_DATA, "reason_codes": [WINDOW_MISSING]}
    if end < start:
        # 反向窗口属畸形声明：FAIL + WINDOW_INVALID，报告不崩溃。
        return {**base, "state": FAIL, "reason_codes": [WINDOW_INVALID]}
    if not healthy:
        return {**base, "state": NO_DATA, "reason_codes": [WINDOW_MISSING]}

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
    # 分母口径：观察跨度按**声明窗口**计算，窗口首尾缺失不得把跨度缩短（§12）。
    window_days = max(0.0, (end_minute - start_minute).total_seconds() / 86400.0)
    observed_span_days = (
        max(0.0, (valid_times[-1] - valid_times[0]).total_seconds() / 86400.0)
        if valid_times else 0.0
    )
    head_missing = 0
    if valid_times:
        head_missing = int((valid_times[0] - start_minute).total_seconds() // 60)
    tail_missing = 0
    if valid_times:
        tail_missing = int((end_minute - valid_times[-1]).total_seconds() // 60)
    return {
        "state": state,
        "coverage": round(coverage, 6),
        "observed_count": observed_count,
        "expected_count": expected_count,
        "healthy_count": len(healthy),
        "observation_days": round(window_days, 6),
        "observed_span_days": round(observed_span_days, 6),
        "max_gap_seconds": round(max_gap, 3),
        "missing_count": len(missing),
        "missing_minutes": missing[:20],
        "head_missing_count": head_missing,
        "tail_missing_count": tail_missing,
        "since": start_minute.isoformat(timespec="minutes"),
        "until": end_minute.isoformat(timespec="minutes"),
        "window_code": None,
        "reason_codes": [] if state == PASS else ["COVERAGE_BELOW_THRESHOLD"],
        "derived_from_observations": False,
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


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


# --------------------------------------------------------------------------- #
# 方向候选口径（U06a 是唯一权威）
# --------------------------------------------------------------------------- #
def execution_decision_sources() -> tuple[str, ...]:
    """返回允许计入方向候选分母的来源；口径全部来自 :mod:`intel.shadow_status`。"""
    return tuple(sorted(
        source for source in (
            getattr(spec, "source", "") for spec in _cohort_specs())
        if source and release_gate_role(source) == GATE_ROLE_EXECUTION_DECISION
    ))


def _cohort_specs() -> Sequence[Any]:
    # 惰性取 COHORTS，保持模块导入期只依赖 shadow_status 的判定函数。
    from intel.shadow_status import COHORTS
    return COHORTS


def _opportunity_key(row: Mapping[str, Any]) -> str:
    feature = _json_object(row.get("feature_json"))
    explicit = feature.get("opportunity_id") or feature.get("opportunity_key")
    if explicit:
        return str(explicit)
    return "|".join((str(row.get("symbol") or ""), str(row.get("signal") or ""),
                     str(row.get("created_at") or "")[:16]))


def _candidate_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    feature = _json_object(row.get("feature_json"))
    facts: dict[str, Any] = {}
    for key in ("candidate",):
        if isinstance(feature.get(key), Mapping):
            facts.update(dict(feature[key]))
    return facts


def _no_edge_declared(row: Mapping[str, Any]) -> bool:
    """该行是否**明确声明**"没有边际/被拒绝"（F24「纯 NO_EDGE 凑 200」）。

    只认显式标记，不猜语义：``no_edge``/``rejected`` 为真、原因码等于 ``NO_EDGE``、
    或 ``action`` 取 skip/hold/none/reject。状态类字段（如
    ``new_entry_allowed=false``）属"已看见但未开仓"，不是无信号，不计入本判据，
    另行以 ``blocked_entry_rows`` 披露。
    """
    payloads = [_json_object(row.get("feature_json")), _candidate_facts(row)]
    for payload in payloads:
        for key in _NO_EDGE_FLAG_KEYS:
            value = payload.get(key)
            if value is True:
                return True
            if isinstance(value, str) and value.strip():
                if value.strip().upper() in _NO_EDGE_REASON_VALUES:
                    return True
        for key in _NO_EDGE_REASON_KEYS:
            value = payload.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                if str(item or "").strip().upper() in _NO_EDGE_REASON_VALUES:
                    return True
        action = payload.get("action")
        if isinstance(action, str) and action.strip().lower() in _NO_EDGE_ACTIONS:
            return True
    return False


def _candidate_exclusion(row: Mapping[str, Any]) -> str | None:
    """返回该行被排除出方向候选分母的原因；``None`` 表示计入。"""
    source = str(row.get("source") or "")
    if release_gate_role(source) != GATE_ROLE_EXECUTION_DECISION:
        return "research_only_source"
    signal = row.get("signal")
    if not is_directional(signal):
        return "non_directional_signal"
    if str(signal).strip().lower() not in DIRECTIONAL_SIGNALS:  # pragma: no cover - 兜底
        return "non_directional_signal"
    if _no_edge_declared(row):
        return "no_edge_declared"
    return None


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


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


# --------------------------------------------------------------------------- #
# 完整闭合生命周期：按 lifecycle_id 去重 + 修订重算（F23）
# --------------------------------------------------------------------------- #
def _revision_stamp(row: Mapping[str, Any]) -> str:
    return str(row.get("recorded_at") or row.get("actual_closed_at") or row.get("closed_at") or "")


def _cost_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in (
        "pnl", "gross_realized", "entry_fee", "exit_fee", "funding_cashflow",
        "entry_price", "exit_price", "qty", "cost_status", "attribution_status", "status",
    ))


def _cost_complete(row: Mapping[str, Any]) -> bool:
    return all(_as_float(row.get(key)) is not None for key in
               ("entry_price", "exit_price", "qty"))


def _net_realized(row: Mapping[str, Any]) -> float | None:
    """按最新修订重算净收益：``gross - entry_fee - exit_fee + funding``。

    任一成本项缺失就返回 ``None``（未知仍未知，不用 0 冒充"零成本"）。
    """
    gross = _as_float(row.get("gross_realized"))
    if gross is None:
        gross = _as_float(row.get("pnl"))
    if gross is None:
        return None
    entry_fee = _as_float(row.get("entry_fee"))
    exit_fee = _as_float(row.get("exit_fee"))
    funding = _as_float(row.get("funding_cashflow"))
    if entry_fee is None or exit_fee is None or funding is None:
        return None
    return round(gross - entry_fee - exit_fee + funding, 10)


def select_lifecycle_revisions(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """把 ``position_lifecycles ⟕ trade_outcomes`` 的行按 ``lifecycle_id`` 归并。

    规则（F23）：

    * 同一 ``lifecycle_id`` 有多条 outcome 行 → 取 ``recorded_at`` 最新的一条，
      早期行只登记为 ``superseded``，**不重复计数**；
    * 同一 ``outcome_id`` 出现不同内容 → ``REVISION_CONFLICT``（重复 ID 不同内容）；
    * 最新修订时点相同但内容不同 → ``REVISION_CONFLICT``（无法定序，冲突隔离）；
    * 冲突的生命周期不得计入 30 完整闭合（fail-closed），但必须出现在报告里。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for raw in rows:
        item = dict(raw)
        lifecycle_id = str(item.get("lifecycle_id") or "")
        if not lifecycle_id:
            continue
        if item.get("outcome_id") is None:
            # 只有生命周期、没有 outcome 证据：登记为缺证据，不参与修订归并。
            grouped.setdefault(lifecycle_id, [])
            if lifecycle_id not in order:
                order.append(lifecycle_id)
            continue
        grouped.setdefault(lifecycle_id, []).append(item)
        if lifecycle_id not in order:
            order.append(lifecycle_id)

    selected: dict[str, dict[str, Any]] = {}
    for lifecycle_id in order:
        revisions = grouped.get(lifecycle_id) or []
        if not revisions:
            selected[lifecycle_id] = {
                "lifecycle_id": lifecycle_id, "latest": None, "revisions": 0,
                "revised": False, "conflicted": False, "conflict_reason": None,
                "superseded_outcome_ids": [], "net_realized": None,
                "evidence_revision_hash": None,
            }
            continue
        ordered = sorted(revisions, key=lambda item: (
            _revision_stamp(item), str(item.get("outcome_id") or "")))
        latest = ordered[-1]
        conflict_reason = None
        by_outcome: dict[str, set[tuple[Any, ...]]] = {}
        for item in ordered:
            by_outcome.setdefault(str(item.get("outcome_id")), set()).add(_cost_signature(item))
        for outcome_id, signatures in by_outcome.items():
            if len(signatures) > 1:
                conflict_reason = f"{CODE_REVISION_CONFLICT}:outcome_id={outcome_id}"
                break
        if conflict_reason is None:
            stamp = _revision_stamp(latest)
            same_stamp = [item for item in ordered if _revision_stamp(item) == stamp]
            signatures = {_cost_signature(item) for item in same_stamp}
            if len(signatures) > 1:
                conflict_reason = f"{CODE_REVISION_CONFLICT}:same_recorded_at={stamp}"
        selected[lifecycle_id] = {
            "lifecycle_id": lifecycle_id,
            "latest": latest,
            "revisions": len(ordered),
            "revised": len(ordered) > 1,
            "conflicted": conflict_reason is not None,
            "conflict_reason": conflict_reason,
            "superseded_outcome_ids": [str(item.get("outcome_id")) for item in ordered[:-1]],
            "net_realized": _net_realized(latest),
            "evidence_revision_hash": canonical_hash({
                "lifecycle_id": lifecycle_id,
                "outcome_id": str(latest.get("outcome_id") or ""),
                "recorded_at": _revision_stamp(latest),
                "cost": list(_cost_signature(latest)),
            }),
        }
    return selected


def _eligible_lifecycle(row: Mapping[str, Any]) -> bool:
    return (
        str(row.get("status") or "").upper() == "CLOSED"
        and bool(row.get("outcome_id"))
        and bool(row.get("actual_closed_at"))
        and str(row.get("attribution_status") or "").upper() == "ATTRIBUTED"
        and str(row.get("cost_status") or "").upper() == "COMPLETE"
        and float(row.get("entry_price") or 0) > 0
        and float(row.get("exit_price") or 0) > 0
        and float(row.get("qty") or 0) > 0
    )


# --------------------------------------------------------------------------- #
# 运行连续性（重启 ≠ 换版）与事件恢复（ACK ≠ 恢复）
# --------------------------------------------------------------------------- #
def _shared_probes() -> tuple[Any, Any]:
    """惰性取共享适配补丁里的只读探针；缺失时返回 ``(None, None)``。

    共享文件（``core/health_watch``）由 I 合入；未合入时本模块**不自行复制**一份
    判定实现，而是把对应证据块标 ``NO_DATA`` + ``U09_SHARED_PATCH_MISSING``。
    """
    try:
        from core.health_watch import (  # noqa: PLC0415 - 惰性导入以避免循环依赖
            alert_receipt_truth, release_observation_window)
    except ImportError:
        return None, None
    return release_observation_window, alert_receipt_truth


def _identity_block(manifest: Mapping[str, Any], *, release_id: str,
                    environment: str, account_ref: str) -> dict[str, Any]:
    key, artifact = _first_present(manifest, _ARTIFACT_HASH_KEYS)
    identity_key, instance = _first_present(manifest, _IDENTITY_KEYS)
    codes: list[str] = []
    if not artifact:
        codes.append("ARTIFACT_HASH_MISSING")
    if not identity_key:
        codes.append(CODE_IDENTITY_REF_MISSING)
    if not release_id or release_id == "legacy":
        codes.append("RELEASE_ID_MISSING")
    identity_ref = canonical_hash({
        "release_id": release_id, "environment": environment,
        "account_ref": account_ref, "artifact_hash": str(artifact or "UNKNOWN"),
    })[:32]
    return {
        "state": PASS if not codes else NO_DATA,
        "reason_codes": codes,
        "release_id": release_id,
        "environment": environment,
        "account_ref": account_ref,
        "artifact_hash": str(artifact) if artifact else None,
        "artifact_hash_source": key,
        "runtime_instance_id": str(instance) if instance else None,
        "runtime_instance_source": identity_key,
        "identity_ref": identity_ref,
    }


def build_run_continuity(
    *, continuity: Mapping[str, Any] | None, identity: Mapping[str, Any],
    patch_available: bool,
) -> dict[str, Any]:
    """把观察到的 session/配置事实归类成「重启」或「换版」（F24）。"""
    if not patch_available or continuity is None:
        return {
            "state": NO_DATA,
            "verdict": "UNKNOWN",
            "reason_codes": [CODE_SHARED_PATCH_MISSING] if not patch_available else [WINDOW_MISSING],
            "restart_is_version_change": False,
            "artifact_hash": identity.get("artifact_hash"),
            "identity_ref": identity.get("identity_ref"),
        }
    facts = dict(continuity)
    session_count = int(facts.get("session_count") or 0)
    config_hashes = [str(item) for item in (facts.get("config_hashes") or [])]
    reason_codes: list[str] = []
    if session_count > 1:
        # 多 session = 进程重启；只要工件/配置没变就不是换版。
        if len(config_hashes) > 1:
            verdict = "CONFIG_CHANGED_WITHIN_WINDOW"
            reason_codes.append(CODE_MIXED_CONFIG_SAMPLES)
        else:
            verdict = "RESTART_SAME_ARTIFACT"
    elif session_count == 1:
        verdict = "SINGLE_SESSION"
    else:
        verdict = "NO_DATA"
        reason_codes.append(WINDOW_MISSING)
    if int(facts.get("foreign_release_rows") or 0) > 0:
        reason_codes.append(CODE_MIXED_RELEASE_SAMPLES)
        verdict = "MIXED_RELEASE"
    state = NO_DATA if verdict in {"NO_DATA"} else (FAIL if reason_codes else PASS)
    return {
        "state": state,
        "verdict": verdict,
        "reason_codes": reason_codes,
        # 明示口径：重启本身不是换版判据。
        "restart_is_version_change": False,
        "sessions": session_count,
        "restart_count": max(0, session_count - 1),
        "config_hashes": config_hashes,
        "config_hash_count": len(config_hashes),
        "foreign_release_rows": int(facts.get("foreign_release_rows") or 0),
        "artifact_hash": identity.get("artifact_hash"),
        "identity_ref": identity.get("identity_ref"),
        "observed_first": facts.get("observed_first"),
        "observed_last": facts.get("observed_last"),
        "declared_window": {
            "start": facts.get("declared_start"), "end": facts.get("declared_end"),
            "declared": facts.get("declared"),
        },
        # 观察者**完全没有写入**观察行的断档分钟（与 coverage 的"有行但不健康"
        # 是两件事，故名字不同，避免被读成同一口径）。
        "head_gap_minutes": facts.get("head_gap_minutes"),
        "tail_gap_minutes": facts.get("tail_gap_minutes"),
        "observed_minute_count": facts.get("observed_minute_count"),
        "healthy_minute_count": facts.get("healthy_minute_count"),
    }


def build_event_recovery(
    *, receipts: Mapping[str, Any] | None, patch_available: bool,
    incidents: Mapping[str, Any],
) -> dict[str, Any]:
    """汇总「事件恢复真相」：ACK/已接受不等于已送达、不等于已恢复（F12/K05）。"""
    incident_block = dict(incidents or {})
    reason_codes: list[str] = []
    if incident_block.get("acked_not_recovered"):
        reason_codes.append(CODE_ACK_WITHOUT_RECOVERY)
    if not patch_available or receipts is None:
        delivery = {
            "state": NO_DATA,
            "reason_codes": [CODE_SHARED_PATCH_MISSING] if not patch_available else [WINDOW_MISSING],
            "ack_without_delivery": [],
            "delivered_confirmed": 0,
            "provider_accepted_only": 0,
            "pending": 0,
            "tracked_incidents": 0,
        }
        reason_codes.extend(item for item in delivery["reason_codes"]
                            if str(item) not in reason_codes)
    else:
        facts = dict(receipts)
        ack_only = list(facts.get("ack_without_delivery") or [])
        if ack_only:
            reason_codes.append(CODE_ACK_WITHOUT_DELIVERY)
        delivery = {
            "state": facts.get("state", NO_DATA),
            "reason_codes": list(facts.get("reason_codes") or []),
            "delivered_confirmed": int(facts.get("delivered_confirmed") or 0),
            "ack_without_delivery": ack_only,
            "provider_accepted_only": int(facts.get("provider_accepted_only") or 0),
            "pending": int(facts.get("pending") or 0),
            "tracked_incidents": int(facts.get("tracked_incidents") or 0),
        }
        for item in delivery["reason_codes"]:
            # 直接复用 U14 的原因码（MANUAL_ACK_ONLY / NO_CONFIRMED_DELIVERY），
            # 不在本层另造一套"送达"话术。
            if str(item) not in reason_codes:
                reason_codes.append(str(item))
    states = [delivery.get("state"), incident_block.get("state")]
    if FAIL in states or reason_codes:
        state = FAIL
    elif NO_DATA in states:
        state = NO_DATA
    else:
        state = PASS
    return {
        "state": state,
        "reason_codes": tuple(dict.fromkeys(reason_codes)),
        # 口径声明：ACK 不是恢复，供应商接受不是送达。
        "ack_is_not_recovery": True,
        "ack_is_not_delivery": True,
        "delivery": delivery,
        "incidents": incident_block,
    }


def _incident_recovery_facts(connection: sqlite3.Connection, *, environment: str) -> dict[str, Any]:
    """只读统计 incident 的 ACK 与恢复事实；未 ACK/未恢复一律分开计。"""
    if not _table_exists(connection, "risk_incidents"):
        return {"state": NO_DATA, "reason_codes": [CODE_MISSING_EVIDENCE],
                "open_total": 0, "acked_not_recovered": [], "critical_open": 0}
    columns = _table_columns(connection, "risk_incidents")
    ack_column = "acknowledged_at" if "acknowledged_at" in columns else None
    resolved_column = "resolved_at" if "resolved_at" in columns else None
    rows = connection.execute(
        "SELECT incident_key, severity, status, "
        f"{ack_column or 'NULL'} AS acknowledged_at, "
        f"{resolved_column or 'NULL'} AS resolved_at FROM risk_incidents "
        "WHERE status NOT IN ('CLOSED','RESOLVED')"
    ).fetchall()
    acked_not_recovered: list[dict[str, Any]] = []
    critical_open = 0
    for row in rows:
        if str(row["severity"] or "").lower() == "critical":
            critical_open += 1
        if row["acknowledged_at"] and not row["resolved_at"]:
            acked_not_recovered.append({
                "incident_key": str(row["incident_key"]),
                "severity": str(row["severity"] or ""),
                "status": str(row["status"] or ""),
                "acknowledged_at": str(row["acknowledged_at"]),
            })
    return {
        "state": FAIL if acked_not_recovered else PASS,
        "reason_codes": [CODE_ACK_WITHOUT_RECOVERY] if acked_not_recovered else [],
        "environment": environment,
        "open_total": len(rows),
        "critical_open": critical_open,
        "acked_not_recovered": acked_not_recovered[:20],
        "acked_not_recovered_count": len(acked_not_recovered),
    }


# --------------------------------------------------------------------------- #
# 主报告
# --------------------------------------------------------------------------- #
def _group_status(*states: Any) -> str:
    states = tuple(states)
    if FAIL in states:
        return FAIL
    if NO_DATA in states or not states:
        return NO_DATA
    return PASS


def build_release_readiness_report(
    db_path: Path, manifest: Mapping[str, Any] | Any, *, environment: str,
    since: Any = None, until: Any = None, thresholds: Mapping[str, Any] | None = None,
    report_at: Any = None, declaration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构建严格只读报告；任何缺表/坏 SQL 都抛错而不是伪造通过。

    ``declaration`` 是 K06 语义的"调用方声明状态"（``{group: PASS|FAIL|NO_DATA}``）；
    声明 ``PASS`` 而证据（hash/窗口/身份）不完整由冻结契约判 ``FAIL`` +
    ``PASS_WITHOUT_EVIDENCE``。未声明时按计算出的状态声明。
    """
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
    declared = {str(key): str(value).strip().upper()
                for key, value in dict(declaration or {}).items()}
    identity = _identity_block(manifest_data, release_id=release_id,
                               environment=expected_environment, account_ref=account_ref)
    window = resolve_release_window(since=since, until=until, manifest=manifest_data,
                                    report_at=report_at)
    window_ready = window["state"] == PASS
    window_code = ordered_window_code(window)
    window_start = parse_iso(window["since"]) if window_ready else None
    window_end = parse_iso(window["until"]) if window_ready else None
    window_start_iso = window_start.isoformat() if window_start else None
    window_end_iso = window_end.isoformat() if window_end else None
    probe_window, probe_receipts = _shared_probes()

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

        # ---- 方向候选：先全量取窗口内样本，再按 U06a 口径过滤（S08 修复） ----
        candidate_time, candidate_params = _time_clause("created_at", window_start, window_end)
        candidate_rows = connection.execute(
            f"""SELECT decision_id,created_at,symbol,signal,feature_json,source
                FROM shadow_decisions
                WHERE release_id=? AND environment=?
                  AND {candidate_time}
                ORDER BY created_at,decision_id""",
            (release_id, expected_environment, *candidate_params),
        ).fetchall()
        source_counts: dict[str, int] = {}
        excluded_counts: dict[str, int] = {}
        candidate_keys: set[str] = set()
        counted_sources: dict[str, int] = {}
        blocked_entry_rows = 0
        for row in candidate_rows:
            item = dict(row)
            source = str(item.get("source") or "")
            source_counts[source] = source_counts.get(source, 0) + 1
            facts = _candidate_facts(item)
            if facts.get("new_entry_allowed") is False and str(facts.get("blocked_reason") or ""):
                blocked_entry_rows += 1
            reason = _candidate_exclusion(item)
            if reason is not None:
                excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
                continue
            candidate_keys.add(_opportunity_key(item))
            counted_sources[source] = counted_sources.get(source, 0) + 1
        # source_counts 是**全量**诊断值（含 B/C 研究影子行）；门禁分母只认
        # candidate_keys，且候选行已按 U06a 的 gate_role 过滤，避免研究样本冒充
        # 执行候选（S08-SHADOW-GATE-LEAK）。

        # ---- 完整闭合生命周期：去重 + 最新修订重算（F23） ----
        closed_time, closed_params = _time_clause("COALESCE(o.actual_closed_at,o.closed_at)",
                                                  window_start, window_end)
        lifecycle_rows = connection.execute(
            f"""SELECT l.lifecycle_id,l.status AS lifecycle_status,l.release_id AS lifecycle_release_id,
                       l.account_ref,o.outcome_id,o.status,o.actual_closed_at,o.entry_price,
                       o.exit_price,o.qty,o.attribution_status,o.cost_status,o.pnl,
                       o.gross_realized,o.entry_fee,o.exit_fee,o.funding_cashflow,o.recorded_at
                FROM position_lifecycles l
                LEFT JOIN trade_outcomes o ON o.lifecycle_id=l.lifecycle_id
                WHERE l.release_id=? AND l.environment=? AND l.account_ref=?
                  AND l.status='CLOSED' AND {closed_time}
                ORDER BY COALESCE(o.actual_closed_at,o.closed_at),l.lifecycle_id""",
            (release_id, expected_environment, account_ref, *closed_params),
        ).fetchall()
        revisions = select_lifecycle_revisions([dict(row) for row in lifecycle_rows])
        eligible_lifecycles: set[str] = set()
        eligible_rows: list[dict[str, Any]] = []
        revised_lifecycles: list[dict[str, Any]] = []
        conflicted_lifecycles: list[dict[str, Any]] = []
        net_realized_total = 0.0
        net_realized_unknown = 0
        for lifecycle_id, record in revisions.items():
            latest = record.get("latest")
            if record["conflicted"]:
                conflicted_lifecycles.append({
                    "lifecycle_id": lifecycle_id,
                    "revisions": record["revisions"],
                    "conflict_reason": record["conflict_reason"],
                })
                continue
            if latest is None:
                continue
            if record["revised"]:
                revised_lifecycles.append({
                    "lifecycle_id": lifecycle_id,
                    "revisions": record["revisions"],
                    "superseded_outcome_ids": record["superseded_outcome_ids"],
                    "evidence_revision_hash": record["evidence_revision_hash"],
                    "net_realized": record["net_realized"],
                })
            if not _eligible_lifecycle(latest):
                continue
            eligible_lifecycles.add(lifecycle_id)
            row = dict(latest)
            row["lifecycle_id"] = lifecycle_id
            row["revisions"] = record["revisions"]
            row["evidence_revision_hash"] = record["evidence_revision_hash"]
            row["net_realized"] = record["net_realized"]
            row["cost_recomputed"] = record["net_realized"] is not None
            if record["net_realized"] is None:
                net_realized_unknown += 1
            else:
                net_realized_total += float(record["net_realized"])
            eligible_rows.append(row)
        current_closed_count = int(connection.execute(
            """SELECT COUNT(*) FROM position_lifecycles
               WHERE release_id=? AND environment=? AND account_ref=? AND status='CLOSED'""",
            (release_id, expected_environment, account_ref),
        ).fetchone()[0])
        missing_outcome_count = max(0, current_closed_count - len({
            lifecycle_id for lifecycle_id, record in revisions.items()
            if record.get("latest") is not None}))
        incomplete_closed_count = len({
            lifecycle_id for lifecycle_id, record in revisions.items()
            if record.get("latest") is not None}) - len(eligible_lifecycles)
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
            window_code=window_code,
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
                required={"http_418": 0, "http_429": 0,
                          "coordination_unavailable_samples": 0},
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

        # ---- 运行连续性 / 事件恢复 ----
        continuity_facts = None
        if callable(probe_window):
            continuity_facts = probe_window(
                connection, release_id=release_id, environment=expected_environment,
                account_ref=account_ref,
                declared_start=window_start_iso, declared_end=window_end_iso,
            )
        run_continuity = build_run_continuity(
            continuity=continuity_facts, identity=identity,
            patch_available=callable(probe_window),
        )
        receipts_facts = None
        if callable(probe_receipts):
            receipts_facts = probe_receipts(
                connection, environment=expected_environment, account_ref=account_ref)
        incident_facts = _incident_recovery_facts(connection, environment=expected_environment)
        event_recovery = build_event_recovery(
            receipts=receipts_facts, patch_available=callable(probe_receipts),
            incidents=incident_facts,
        )

        # ---- 门禁（键与派生方式保持既有语义） ----
        # 窗口不成立时分母不可成立：畸形窗口 FAIL+WINDOW_INVALID，缺窗口 NO_DATA。
        candidate_state = PASS if len(candidate_keys) >= int(
            threshold["min_directional_candidates"]) else FAIL
        if not window_ready:
            candidate_state = FAIL if window_code == WINDOW_INVALID else NO_DATA
        closed_state = PASS if len(eligible_lifecycles) >= int(threshold["min_eligible_closed_trades"]) else FAIL
        if not window_ready:
            closed_state = FAIL if window_code == WINDOW_INVALID else NO_DATA
        gates = {
            "schema_version": _gate(
                PASS if manifest_schema and schema_version == manifest_schema else FAIL,
                actual=schema_version, required=manifest_schema,
                reason="数据库迁移版本与发布清单一致" if manifest_schema and schema_version == manifest_schema
                else "数据库迁移版本与发布清单不一致",
            ),
            "directional_candidates": _gate(
                candidate_state,
                actual=(len(candidate_keys) if window_ready else None),
                required=int(threshold["min_directional_candidates"]),
                reason=("按机会键去重、仅计入 execution_decision_copy 来源且排除 NO_EDGE 的"
                        "方向性影子样本"
                        if window_ready else "观察窗口不可用，方向候选分母无法成立"),
                excluded_by_reason=excluded_counts,
                counted_sources=counted_sources,
                window_reason_codes=list(window["reason_codes"]),
            ),
            "eligible_closed_trades": _gate(
                closed_state, actual=(len(eligible_lifecycles) if window_ready else None),
                required=int(threshold["min_eligible_closed_trades"]),
                reason="仅计入完整生命周期、归因和成本证据的闭合交易（按 lifecycle_id 去重）",
                current_closed=current_closed_count, incomplete_closed=incomplete_closed_count,
                missing_outcome=missing_outcome_count, carryover_closed=carryover_count,
                revised=len(revised_lifecycles), conflicted=len(conflicted_lifecycles),
            ),
            "observation_coverage": _gate(
                str(coverage.get("state") or NO_DATA), actual=coverage.get("coverage"),
                required=float(threshold["min_observation_coverage"]),
                reason="健康分钟观察覆盖率与缺口门禁（窗口来自发布观察窗口声明）",
                details=coverage,
            ),
            "observation_days": _gate(
                str(coverage.get("state")) if coverage.get("state") == NO_DATA
                else (FAIL if window_code == WINDOW_INVALID
                      else (PASS if healthy_days >= float(threshold["min_observation_days"]) else FAIL)),
                actual=round(healthy_days, 6), required=float(threshold["min_observation_days"]),
                reason="健康观察跨度门禁（按声明窗口计算，首尾缺失不缩短跨度）",
            ),
            "network_rate_limit": network_gate,
            "critical_open_incidents": _gate(
                PASS if critical_open == 0 else FAIL, actual=critical_open, required=0,
                reason="未收口的 critical incident 必须为零",
                acknowledged_but_unresolved=incident_facts.get("acked_not_recovered_count", 0),
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

        groups_payload = {
            "engineering": _group_evidence(
                declared, "engineering",
                _group_status(gates["schema_version"]["state"], identity["state"]),
                identity=identity, window=window,
                detail={"schema_version": schema_version, "manifest_schema": manifest_schema,
                        "identity_reason_codes": list(identity["reason_codes"])},
            ),
            "accounting": _group_evidence(
                declared, "accounting", _accounting_group_state(
                    gates["eligible_closed_trades"], eligible_count=len(eligible_lifecycles),
                    has_outcomes=bool(revisions)),
                identity=identity, window=window,
                detail={"eligible_closed_lifecycles": len(eligible_lifecycles),
                        "net_realized_total": round(net_realized_total, 10),
                        "net_realized_unknown": net_realized_unknown,
                        "revised_lifecycles": len(revised_lifecycles),
                        "conflicted_lifecycles": len(conflicted_lifecycles),
                        "current_closed_lifecycles": current_closed_count},
            ),
            "runtime_observation": _group_evidence(
                declared, "runtime_observation",
                _group_status(gates["observation_coverage"]["state"],
                              gates["observation_days"]["state"], run_continuity["state"]),
                identity=identity, window=window,
                detail={"coverage": coverage.get("coverage"),
                        "expected_count": coverage.get("expected_count"),
                        "head_missing_count": coverage.get("head_missing_count"),
                        "tail_missing_count": coverage.get("tail_missing_count"),
                        "run_continuity_verdict": run_continuity.get("verdict"),
                        "run_continuity_reason_codes": list(run_continuity["reason_codes"])},
            ),
            "operations": _group_evidence(
                declared, "operations",
                _group_status(gates["network_rate_limit"]["state"], entry_gate["state"],
                              gates["critical_open_incidents"]["state"],
                              gates["unknown_orders"]["state"],
                              gates["submitting_orders"]["state"],
                              event_recovery["state"]),
                identity=identity, window=window,
                detail={"network_rate_limit": gates["network_rate_limit"]["state"],
                        "entry_readiness": entry_gate["state"],
                        "event_recovery_state": event_recovery["state"],
                        "event_recovery_reason_codes": list(event_recovery["reason_codes"]),
                        "acked_not_recovered": incident_facts.get("acked_not_recovered_count", 0),
                        "ack_without_delivery": len(
                            (event_recovery.get("delivery") or {}).get("ack_without_delivery") or [])},
            ),
            "strategy_evaluation": _group_evidence(
                declared, "strategy_evaluation", _group_status(
                    gates["directional_candidates"]["state"]
                    if candidate_rows else NO_DATA),
                identity=identity, window=window,
                detail={"directional_candidates": len(candidate_keys),
                        "shadow_rows": len(candidate_rows),
                        "excluded_by_reason": excluded_counts,
                        "counted_sources": counted_sources},
            ),
        }
        k06 = build_release_report(groups_payload, identity_ref=identity["identity_ref"])
        blocking_items = [item.model_dump(mode="json") for item in k06.blocking_items]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "read_only": True,
            "evidence_version": U09_EVIDENCE_VERSION,
            "database": Path(db_path).resolve().name,
            "environment": expected_environment,
            "account_ref": account_ref,
            "release_id": release_id,
            "manifest": manifest_data,
            "window": window,
            "identity": identity,
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
                "shadow_counted_sources": counted_sources,
                "shadow_excluded_by_reason": excluded_counts,
                "candidate_source_filter": {
                    "authority": "intel.shadow_status",
                    "gate_role": GATE_ROLE_EXECUTION_DECISION,
                    "sources": list(execution_decision_sources()),
                    "research_only_sources": sorted(
                        source for source in source_counts
                        if release_gate_role(source) != GATE_ROLE_EXECUTION_DECISION),
                },
                "blocked_entry_rows": blocked_entry_rows,
                "eligible_closed_lifecycles": len(eligible_lifecycles),
                "eligible_closed_rows": eligible_rows,
                "eligible_net_realized_total": round(net_realized_total, 10),
                "net_realized_unknown_lifecycles": net_realized_unknown,
                "revised_lifecycles": revised_lifecycles,
                "conflicted_lifecycles": conflicted_lifecycles,
                "current_closed_lifecycles": current_closed_count,
                "incomplete_closed_lifecycles": incomplete_closed_count,
                "missing_outcome_lifecycles": missing_outcome_count,
                "carryover_closed_lifecycles": carryover_count,
                "observations": len(observations),
                "observation_coverage": coverage,
            },
            "run_continuity": run_continuity,
            "event_recovery": event_recovery,
            "runtime": {
                "critical_open_incidents": critical_open,
                "unknown_orders": unknown_orders,
                "submitting_orders": submitting_orders,
            },
            "schema_version": schema_version,
            "gates": gates,
            "k06": {
                "schema_version": k06.schema_version,
                "groups": [item.model_dump(mode="json") for item in k06.groups],
                "blocking_items": blocking_items,
                "report_hash": k06.report_hash,
                "note": ("五组独立评估；NO_DATA != PASS；本块不产生第二套 GO 判断器。"),
            },
            "blocking_items": blocking_items,
            "blocking_item_count": len(blocking_items),
            "release_readiness": "GO" if release_passed else "NO_GO",
            "release_gates_passed": release_passed,
            "release_readiness_note": (
                "既有字段保持不变（仅由 gates 汇总，语义与升级前一致）；"
                "K06 五组与 blocking_items 是独立的证据门禁，不得互相替代。"),
        }
    finally:
        connection.close()


def ordered_window_code(window: Mapping[str, Any]) -> str | None:
    """从窗口解析结果里抽出该报给覆盖门禁的代码：畸形优先于缺失。"""
    codes = tuple(window.get("reason_codes") or ())
    if WINDOW_INVALID in codes:
        return WINDOW_INVALID
    if WINDOW_MISSING in codes:
        return None  # 缺失交给 observation_coverage 自己判 NO_DATA
    return None


def _accounting_group_state(gate: Mapping[str, Any], *, eligible_count: int,
                            has_outcomes: bool) -> str:
    if not has_outcomes:
        return NO_DATA
    return str(gate.get("state") or NO_DATA)


def _group_evidence(
    declared: Mapping[str, str], group: str, computed: str, *,
    identity: Mapping[str, Any], window: Mapping[str, Any], detail: Mapping[str, Any],
) -> dict[str, Any]:
    """构造 K06 单组证据载荷：声明状态 + 证据 hash + 窗口 + 身份引用。

    身份不完整（缺运行工件 hash / 发布标识）时**不**传 ``identity_ref``：K06 要求
    "PASS 必须同时具备证据 hash、有效窗口与匹配身份"，凭空造一个引用会让
    "声明 PASS 但身份不明"逃过 ``IDENTITY_REF_MISSING``。
    """
    status = declared.get(group, computed)
    identity_complete = identity.get("state") == PASS
    payload = {
        "status": status,
        "evidence_hash": canonical_hash({"group": group, "detail": detail}),
        "window_start_ms": window.get("since_ms"),
        "window_end_ms": window.get("until_ms"),
        "identity_ref": identity.get("identity_ref") if identity_complete else None,
        "detail": dict(detail),
    }
    return payload
