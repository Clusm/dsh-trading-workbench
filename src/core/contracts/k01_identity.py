"""K01 RuntimeIdentity 与部署作用域契约。

身份 = 环境 + 账户引用 + 代码/依赖/配置/策略/模型路由指纹 + 运行实例。
哈希只取有效非敏感值：调用方传入结构化显式字典，含 key/secret/token 等
键名或原始文本/行列表载荷时拒绝计算。``git_commit`` 进入身份但不参与
文件内容哈希；``build_identity`` 默认强制四组覆盖清单齐全。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    SCHEMA_VERSION,
    canonical_hash,
    ensure_no_sensitive_fields,
    ensure_structured_payload,
)

DEPENDENCY_SOURCE_FILES = ("requirements-lock.txt",)
#: 策略卡来源文件：**声明标签**，不参与 ``_coverage_gaps`` 文件级校验。
#: 与 ``DEPENDENCY_SOURCE_FILES`` 的差别在于载荷形态：依赖侧传入的是
#: ``路径 -> 内容`` 映射（可校验锁文件名出现），策略侧传入的是卡片内容本身，
#: 无法从内容反查文件来源。故 ``strategy`` 组只校验“已声明且非空”，
#: 文件级来源由 U15 落地（README §12 L16，C r1 P2-6 残余处置）。
STRATEGY_SOURCE_FILES = ("knowledge/strategy_cards.json",)
MODEL_ROUTE_SOURCES = (
    "config/agents.yaml 的模型名与推理档",
    "config/settings.yaml 的 llm provider/备用路由",
    "环境覆盖：MTA_AI_DECISION_PROVIDER/MTA_AI_DECISION_FALLBACK_PROVIDER/SENSENOVA_MODEL",
    "profile 覆盖值",
)
EFFECTIVE_CONFIG_SOURCES = (
    "settings/decision_policy/agents/risk_learning/risk_profile.lock/windows_service 的有效非敏感值",
    "profile 合并后的覆盖值",
    "环境变量合并后的有效非敏感值（不含凭据）",
)

IDENTITY_COVERAGE: dict[str, tuple[str, ...]] = {
    "dependency": ("requirements_lock", "comparison_status"),
    "strategy": ("strategy_cards",),
    "model_route": ("model", "effort", "fallback_routes", "profile_overrides"),
    "effective_config": ("settings", "profile_overrides", "environment_overrides"),
}

IDENTITY_MISMATCH_IGNORED_FIELDS = ("runtime_instance_id",)


class ModelRoute(BaseModel):
    """解析后的有效模型路由；字段缺失/为空由构建方负责声明来源。

    ``fallback_routes``/``profile_overrides`` 允许显式为空（声明“无备用/
    无覆盖”），但键必须存在；这与 ``IDENTITY_COVERAGE`` 的字段清单一致。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    fallback_routes: tuple[Mapping[str, Any], ...] = ()
    profile_overrides: Mapping[str, Any] = Field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "effort": self.effort,
            "fallback_routes": [dict(item) for item in self.fallback_routes],
            "profile_overrides": dict(self.profile_overrides),
        }


class IdentityCoverageError(ValueError):
    """身份输入缺少必需的覆盖组；禁止静默生成不完整身份。"""

    def __init__(self, groups: Sequence[str]) -> None:
        self.groups = tuple(groups)
        super().__init__("identity coverage missing: " + ", ".join(self.groups))


class RuntimeIdentity(BaseModel):
    """一次运行的不可混淆身份；不得写入原始密钥或完整 .env。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    environment: str
    account_ref: str
    artifact_hash: str
    git_commit: str
    dependency_hash: str
    effective_config_hash: str
    strategy_hash: str
    model_route_hash: str
    runtime_instance_id: str

    def identity_hash(self) -> str:
        """完整记录哈希（含 runtime_instance_id），用于持久化自校验。"""
        return canonical_hash(self.model_dump(mode="json"))

    def deployment_hash(self) -> str:
        """部署作用域哈希：忽略进程级 runtime_instance_id 的身份摘要。"""
        payload = self.model_dump(mode="json")
        payload.pop("runtime_instance_id", None)
        return canonical_hash(payload)


def _structured_mapping(value: Any, *, name: str) -> dict[str, Any]:
    ensure_structured_payload(value, name=name)
    if not isinstance(value, Mapping):
        raise IdentityCoverageError([name])
    ensure_no_sensitive_fields(value)
    return dict(value)


def _structured_path_mapping(value: Any, *, name: str) -> dict[str, Any]:
    """``相对路径 -> 内容`` 映射的形态守卫。

    键是**文件路径**而非字段名，故**不**施加 :func:`ensure_no_sensitive_fields`
    —— 那条守卫按字段名口径做子串匹配，会把合法模块名
    ``src/core/management_authorization.py``（命中 ``authorization``）误判为敏感键，
    使身份采集在真实 ``src/`` 树上必然失败。文件名级的凭据守卫由调用方承担
    （``core.runtime_identity._safe_artifact_name``，拒绝 ``.env``/``*.pem``/``id_rsa``
    等凭据文件形态）。

    仍保留结构化载荷与 Mapping 形态要求（原始文本、行列表照旧拒绝），故
    "把 ``.env`` 原文或 ``KEY=value`` 行列表当哈希输入"这条路依旧走不通。
    """
    ensure_structured_payload(value, name=name)
    if not isinstance(value, Mapping):
        raise IdentityCoverageError([name])
    return dict(value)


def hash_artifact(files: Mapping[str, str]) -> str:
    """不可变代码发布目录内容哈希；输入为 相对路径 -> 内容。"""
    return canonical_hash(_structured_path_mapping(files, name="artifact_files"))


def hash_dependencies(lock_files: Mapping[str, str]) -> str:
    """依赖锁文件哈希；必须覆盖实际 requirements 锁文件。输入同为 相对路径 -> 内容。"""
    return canonical_hash(_structured_path_mapping(lock_files, name="dependency_lock_files"))


def hash_effective_config(effective_values: Mapping[str, Any]) -> str:
    """settings+profile 覆盖后的有效非敏感值哈希。"""
    return canonical_hash(_structured_mapping(effective_values, name="effective_config"))


def hash_strategy(strategy_cards: Any) -> str:
    """实际策略卡内容哈希；改卡即改身份。

    接受 Mapping（如 ``{"cards": [...]}``）或 Mapping 序列（卡片列表）；
    原始字符串与字符串列表（疑似 .env 原文/行列表）一律拒绝。
    """
    ensure_structured_payload(strategy_cards, name="strategy_cards")
    if isinstance(strategy_cards, Mapping):
        ensure_no_sensitive_fields(strategy_cards)
        return canonical_hash(dict(strategy_cards))
    if isinstance(strategy_cards, (list, tuple)):
        ensure_no_sensitive_fields(strategy_cards)
        return canonical_hash(list(strategy_cards))
    raise IdentityCoverageError(["strategy"])


def hash_model_route(route: ModelRoute | Mapping[str, Any]) -> str:
    """模型名/推理档/备用路由/profile 覆盖值哈希。"""
    if isinstance(route, ModelRoute):
        return canonical_hash(route.as_payload())
    return canonical_hash(_structured_mapping(route, name="model_route"))


def _field_gap(value: Any, *, text_required: bool = False) -> bool:
    """字段缺口判定：None、空串/空白串，或（要求文本时）非字符串。"""
    if value is None:
        return True
    if text_required:
        return not isinstance(value, str) or not value.strip()
    return isinstance(value, str) and not value.strip()


def _coverage_gaps(
    *,
    dependency_lock_files: Mapping[str, str],
    strategy_cards: Any,
    model_route: ModelRoute | Mapping[str, Any],
    effective_config: Mapping[str, Any],
) -> list[str]:
    """字段级覆盖缺口；空组是合法声明，但字段缺失/空值/未知键一律报告。

    非 Mapping 载荷（列表/字符串等）不调用 ``.get``，直接按 ``组.*`` 缺口
    结构化拒绝；Mapping 载荷中的未知键按 ``组.unknown_field:<键>`` 报告。
    """
    gaps: list[str] = []
    lock_names = {DEPENDENCY_SOURCE_FILES[0]}
    if not any(str(key).replace("\\", "/").split("/")[-1] in lock_names
               for key in dependency_lock_files):
        gaps.append("dependency.requirements_lock")
    if strategy_cards is None or (
        isinstance(strategy_cards, (Mapping, Sequence))
        and not isinstance(strategy_cards, str)
        and len(strategy_cards) == 0
    ):
        gaps.append("strategy.strategy_cards")
    route_payload: Any = (
        model_route.as_payload() if isinstance(model_route, ModelRoute) else model_route
    )
    if isinstance(route_payload, Mapping):
        for key in sorted(str(key) for key in route_payload):
            if key not in IDENTITY_COVERAGE["model_route"]:
                gaps.append(f"model_route.unknown_field:{key}")
        for field in IDENTITY_COVERAGE["model_route"]:
            value = route_payload.get(field)
            if _field_gap(value, text_required=field in ("model", "effort")):
                gaps.append(f"model_route.{field}")
    else:
        gaps.append("model_route.*")
    if isinstance(effective_config, Mapping):
        for key in sorted(str(key) for key in effective_config):
            if key not in IDENTITY_COVERAGE["effective_config"]:
                gaps.append(f"effective_config.unknown_field:{key}")
        for field in IDENTITY_COVERAGE["effective_config"]:
            if _field_gap(effective_config.get(field)):
                gaps.append(f"effective_config.{field}")
    else:
        gaps.append("effective_config.*")
    return gaps


def build_identity(
    *,
    environment: str,
    account_ref: str,
    git_commit: str,
    runtime_instance_id: str,
    artifact_files: Mapping[str, str],
    dependency_lock_files: Mapping[str, str],
    strategy_cards: Any,
    model_route: ModelRoute | Mapping[str, Any],
    effective_config: Mapping[str, Any],
    schema_version: str = SCHEMA_VERSION,
    enforce_coverage: bool = True,
) -> RuntimeIdentity:
    """由显式有效值构造身份；本函数不读取任何文件或环境变量。

    ``enforce_coverage=True``（默认）时执行字段级覆盖校验：依赖锁文件、
    策略卡、模型路由四字段（model/effort/fallback_routes/profile_overrides）
    与有效配置三字段（settings/profile_overrides/environment_overrides）
    缺失即抛 ``IdentityCoverageError``。

    Mapping 载荷与 ``ModelRoute`` 同口径：``model/effort`` 必须是非空字符串，
    其余字段空串/None 计缺口，未知键按 ``组.unknown_field:<键>`` 拒绝；
    非 Mapping 载荷按 ``组.*`` 结构化拒绝，不触发 ``AttributeError``。
    """
    if enforce_coverage:
        gaps = _coverage_gaps(
            dependency_lock_files=dependency_lock_files,
            strategy_cards=strategy_cards,
            model_route=model_route,
            effective_config=effective_config,
        )
        if gaps:
            raise IdentityCoverageError(gaps)
    return RuntimeIdentity(
        schema_version=schema_version,
        environment=environment,
        account_ref=account_ref,
        artifact_hash=hash_artifact(artifact_files),
        git_commit=git_commit,
        dependency_hash=hash_dependencies(dependency_lock_files),
        effective_config_hash=hash_effective_config(effective_config),
        strategy_hash=hash_strategy(strategy_cards),
        model_route_hash=hash_model_route(model_route),
        runtime_instance_id=runtime_instance_id,
    )


def identity_mismatches(
    expected: RuntimeIdentity,
    actual: RuntimeIdentity,
    *,
    ignore: tuple[str, ...] = IDENTITY_MISMATCH_IGNORED_FIELDS,
) -> list[str]:
    """逐字段比较身份，返回不匹配字段名（保持模型声明顺序）。

    小版本（3.x）兼容场景可显式把 ``schema_version`` 加入 ``ignore``。
    """
    ignored = set(ignore)
    return [
        name
        for name in RuntimeIdentity.model_fields
        if name not in ignored and getattr(expected, name) != getattr(actual, name)
    ]


def identity_matches(
    expected: RuntimeIdentity,
    actual: RuntimeIdentity,
    *,
    ignore: tuple[str, ...] = IDENTITY_MISMATCH_IGNORED_FIELDS,
) -> bool:
    return not identity_mismatches(expected, actual, ignore=ignore)


def validate_coverage(coverage: Mapping[str, Any]) -> list[str]:
    """检查覆盖清单是否齐全；返回 ``组.字段`` 形式的缺口列表。

    字段缺失（None）或空字符串记为缺口；显式的空集合/空字典表示
    “已声明且当前为空”（如无备用路由），不算缺口。
    """
    problems: list[str] = []
    for group, required in IDENTITY_COVERAGE.items():
        payload = coverage.get(group)
        if not isinstance(payload, Mapping):
            problems.append(f"{group}.*")
            continue
        for field in required:
            value = payload.get(field)
            if value is None or value == "":
                problems.append(f"{group}.{field}")
    return problems


class K01SchemaEnvelope(BaseModel):
    """schema 导出与冒烟校验入口；不是运行期数据结构。"""

    model_config = ConfigDict(extra="forbid")

    runtime_identity: RuntimeIdentity
    model_route: ModelRoute
