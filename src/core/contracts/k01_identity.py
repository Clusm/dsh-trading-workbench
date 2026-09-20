"""K01 RuntimeIdentity 与部署作用域契约。

身份 = 环境 + 账户引用 + 代码/依赖/配置/策略/模型路由指纹 + 运行实例。
哈希只取有效非敏感值：调用方传入显式字典，含 key/secret/token 等键名
时拒绝计算。``git_commit`` 进入身份但不参与文件内容哈希。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from .common import (
    SCHEMA_VERSION,
    canonical_hash,
    ensure_no_sensitive_fields,
)

DEPENDENCY_SOURCE_FILES = ("requirements-lock.txt",)
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
        """身份整体哈希，便于持久化时绑定全部字段。"""
        return canonical_hash(self.model_dump(mode="json"))


def hash_artifact(files: Mapping[str, str]) -> str:
    """不可变代码发布目录内容哈希；输入为 相对路径 -> 内容。"""
    ensure_no_sensitive_fields(files)
    return canonical_hash(dict(files))


def hash_dependencies(lock_files: Mapping[str, str]) -> str:
    """依赖锁文件哈希；必须覆盖实际 requirements 锁文件。"""
    ensure_no_sensitive_fields(lock_files)
    return canonical_hash(dict(lock_files))


def hash_effective_config(effective_values: Mapping[str, Any]) -> str:
    """settings+profile 覆盖后的有效非敏感值哈希。"""
    ensure_no_sensitive_fields(effective_values)
    return canonical_hash(dict(effective_values))


def hash_strategy(strategy_cards: Any) -> str:
    """实际策略卡内容哈希；改卡即改身份。"""
    if isinstance(strategy_cards, Mapping):
        ensure_no_sensitive_fields(strategy_cards)
    return canonical_hash(strategy_cards)


def hash_model_route(route: Mapping[str, Any]) -> str:
    """模型名/推理档/备用路由/profile 覆盖值哈希。"""
    ensure_no_sensitive_fields(route)
    return canonical_hash(dict(route))


def build_identity(
    *,
    environment: str,
    account_ref: str,
    git_commit: str,
    runtime_instance_id: str,
    artifact_files: Mapping[str, str],
    dependency_lock_files: Mapping[str, str],
    strategy_cards: Any,
    model_route: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    schema_version: str = SCHEMA_VERSION,
) -> RuntimeIdentity:
    """由显式有效值构造身份；本函数不读取任何文件或环境变量。"""
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
    """逐字段比较身份，返回不匹配字段名（保持模型声明顺序）。"""
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
    """检查覆盖清单是否齐全；返回 ``组.字段`` 形式的缺口列表。"""
    problems: list[str] = []
    for group, required in IDENTITY_COVERAGE.items():
        payload = coverage.get(group)
        if not isinstance(payload, Mapping):
            problems.append(f"{group}.*")
            continue
        for field in required:
            if payload.get(field) in (None, "", (), [], {}):
                problems.append(f"{group}.{field}")
    return problems


class K01SchemaEnvelope(BaseModel):
    """schema 导出与冒烟校验入口；不是运行期数据结构。"""

    model_config = ConfigDict(extra="forbid")

    runtime_identity: RuntimeIdentity
