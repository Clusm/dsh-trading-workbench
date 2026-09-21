"""U15 运行身份 / 有效配置 / 控制权限边界（K01、K04、K06 落地）。

本模块把仓库里的运行事实（依赖锁、策略卡、解析后的模型路由、有效配置、
git 提交、账户引用、运行代）收敛成 **可复核且不含凭据** 的身份输入，并向
U02 提供账户 / 运行代 / 权限验证接口。

三条不可退让的边界：

1. **凭据不外泄**：只接受显式的非敏感有效值；环境变量按白名单取值；任何
   键名命中 K01 敏感标记（key/secret/token/...）的字段一律丢弃并计数，绝不
   把值写入身份或日志。**不对原始 ``.env`` 整文件哈希**（``.env`` 文本不是
   结构化载荷，K01 守卫会拒绝，本模块另加显式守卫）。
2. **不把配置默认值当有效值**：环境覆盖只记录“确实被设置”的变量；未设置的
   变量不会用代码默认值填充进 ``environment_overrides``。
3. **Mock / 缺 key 显式阻断新仓**：解析路由后给出 ``mock`` 判定与阻断原因，
   Demo/live 下不得冒充成功（DC14 / F13 / DL01）。

同时提供控制面纯策略（回环、验证写入、operator/health/risk 分离、人工禁用
优先于自动恢复），供看板控制接口绑定（R11）。

本模块是纯函数库：不读 ``.env``/``data``/密钥，不发网络请求，不启动服务。
唯一的外部读取是调用方显式传入的 ``project_root`` 下的 **受信非敏感文件**
（依赖锁、策略卡、git 元数据、K01 六类配置来源）与环境变量字典快照。

**U02 / U13 的交接边界（谁必须自建什么）**

本模块**只**提供下列三件东西，其余一律由下游自建（规格 §9「U15 仅提供身份/
权限验证接口，K04 管理授权的实现归 U02」）：

- :func:`collect_runtime_identity` → ``IdentityBundle``：一次运行的身份事实
  （``identity`` 的 K01 哈希、``model_route``、``execution_scope``、
  ``effective_config``、``strategy_source``、``git_state``）+ ``blockers``/
  ``advisories`` + ``account_ref`` 溯源。**不含** TTL、时钟、nonce。
- :func:`mint_runtime_generation` / :func:`rotate_credential` → ``RuntimeGeneration``：
  ``account_ref`` / ``environment`` / ``runtime_instance_id`` / ``generation_id`` /
  ``scope_hash`` / ``credential_epoch`` / ``key_fingerprint``；轮换不改变前四项。
- :func:`verify_account_binding` / :func:`authorize_new_entry`：绑定与开仓门禁，
  返回 ``allowed`` + ``reason_codes`` + 建议 ``http_status``（**fail-closed**：
  200 只可能来自身份、运行代、readiness 三者都被真正校验过的路径）。

U02 必须自建（K04 ``ManagementAuthorization`` 的 12 字段中本模块只提供 3 个）：
``nonce``、``issued_at_ms``/``expires_at_ms``（时效 —— 本模块没有任何 TTL 概念，
也不校验「运行代是否仍有效」）、``revision``、``max_reduce_qty``、``lifecycle_id``、
``position_side``、``target_protection``，以及授权→意图→命令的关联 ID。
U02 必须显式传 ``expected``（自己的授权记录里的运行代）、``readiness_allowed``
（既有 ``entry_readiness`` 结论）与 ``scope_hash``；这三者省略即拒绝。

U13 必须自建：``RuntimePaths``/状态根生成、``runtime_instance_id`` 持久化与实例锁
绑定、启动期拦截（见 :func:`verify_environment_state_root` 与处置表 ``fu-*``）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.contracts import (
    ModelRoute,
    RuntimeIdentity,
    build_identity,
    hash_model_route,
    hash_strategy,
    identity_mismatches,
)
from core.contracts.common import SENSITIVE_KEY_MARKERS

__all__ = [
    "ACCOUNT_REF_KEYS",
    "AUTO_ACTOR",
    "CONTROL_ACTION_ROLES",
    "CONTROL_ACTIONS",
    "CONTROL_ROLES",
    "CONTROL_STATE_SEVERITY",
    "DEPENDENCY_LOCK_FILES",
    "ENVIRONMENT_OVERRIDE_WHITELIST",
    "FINGERPRINT_R1_DISPOSITION",
    "IDENTITY_ARTIFACT_IGNORED_PARTS",
    "MODE_ALLOWED",
    "REQUIRED_STRATEGY_SOURCE",
    "SETTINGS_WHITELIST",
    "AccountBinding",
    "AccountRef",
    "ControlWriteVerdict",
    "CredentialLeakError",
    "EffectiveConfig",
    "ExecutionModeError",
    "ExecutionScope",
    "FallbackRouteError",
    "GitState",
    "IdentityBundle",
    "MissingRuntimeInputError",
    "ModelRouteConfigError",
    "PermissionDecision",
    "ResolvedModelRoute",
    "RuntimeGeneration",
    "RuntimeIdentityError",
    "StrategyCardSource",
    "StrategySourceMismatchError",
    "authorize_control_request",
    "authorize_new_entry",
    "collect_artifact_files",
    "collect_dependency_lock_files",
    "collect_effective_config",
    "collect_runtime_identity",
    "derive_account_ref",
    "environment_override_values",
    "guard_no_raw_env_payload",
    "is_loopback_host",
    "is_sensitive_name",
    "key_fingerprint",
    "load_strategy_cards",
    "mint_runtime_generation",
    "model_route_blockers",
    "resolve_execution_scope",
    "resolve_git_state",
    "resolve_model_route",
    "rotate_credential",
    "verify_account_binding",
    "verify_control_write",
    "verify_strategy_cards_source",
]


# --------------------------------------------------------------------------
# 常量与错误
# --------------------------------------------------------------------------

#: 部署依赖锁：``requirements-lock.txt`` 必须在场（K01 字段级覆盖要求）。
DEPENDENCY_LOCK_FILES: tuple[str, ...] = (
    "requirements-lock.txt",
    "requirements-dev-lock.txt",
)
#: 策略卡来源文件（K01 ``STRATEGY_SOURCE_FILES`` 的文件级落地目标）。
REQUIRED_STRATEGY_SOURCE = "knowledge/strategy_cards.json"
#: 有效代码工件目录（相对 project_root）。
IDENTITY_ARTIFACT_IGNORED_PARTS = ("__pycache__",)

#: 允许进入 ``environment_overrides`` 的环境变量名（**显式白名单**）。
#: 非白名单变量一律忽略，因此 ``.env`` 里的凭据永远不可能被哈希。
ENVIRONMENT_OVERRIDE_WHITELIST: tuple[str, ...] = (
    "MTA_PROFILE",
    "EXECUTION_MODE",
    "MTA_AI_DECISION_PROVIDER",
    "MTA_AI_DECISION_FALLBACK_PROVIDER",
    "MTA_EGRESS_ID",
    "MTA_MARKET",
    "MTA_DRY_RUN",
)

#: settings.yaml 中允许进入 ``settings`` 子哈希的点分路径（有效非敏感值）。
SETTINGS_WHITELIST: tuple[str, ...] = (
    "app_name",
    "stage",
    "log_level",
    "execution.default_mode",
    "execution.allow_paper_fallback",
    "paths.data_dir",
    "paths.db_file",
    "paths.artifacts_dir",
    "paths.logs_dir",
    "paths.reviews_dir",
    "paths.knowledge_dir",
    "llm.provider",
    "llm.default_model",
    "llm.model_env",
    "llm.base_url_env",
    "llm.default_base_url",
    "llm.mock",
    "llm.reasoning_effort",
    "channels.strategy_weight",
    "channels.ai_weight",
    "channels.channel_max_margin_usage",
    "channels.ai_max_leverage",
    "channels.ai_strong_max_leverage",
)

#: ``account_ref`` 允许的显式声明来源（与密钥材料**无关**）。
ACCOUNT_REF_KEYS: tuple[str, ...] = ("MTA_ACCOUNT_REF",)

#: K01 ``IDENTITY_COVERAGE["dependency"]`` 第二项 ``comparison_status`` 的取值
#: （U15 r3 NEW-E：此前只在发布清单补丁里存在，模块内从未产出）。
DEPENDENCY_COMPARISON_MATCH = "MATCH"
DEPENDENCY_COMPARISON_MISMATCH = "MISMATCH"
DEPENDENCY_COMPARISON_UNAVAILABLE = "UNAVAILABLE"

# --------------------------------------------------------------------------
# 门禁原因码（fail-closed 默认值）
# --------------------------------------------------------------------------
#: 调用方未提供被测运行代 / 未提供期望运行代：**无法证明绑定**，一律拒绝。
#: 省略 `expected` 不再等价于“跳过绑定校验”，200 只可能来自真正校验过的路径。
RUNTIME_GENERATION_UNPROVEN = "RUNTIME_GENERATION_UNPROVEN"
IDENTITY_BINDING_UNPROVEN = "IDENTITY_BINDING_UNPROVEN"
#: 调用方未声明 readiness（旧签名默认放行）→ 视为“未证明”，不得开新仓。
READINESS_UNPROVEN = "READINESS_UNPROVEN"
#: 入参形状不合法（非本模块的数据结构）→ 结构化拒绝，不抛裸 AttributeError。
IDENTITY_MALFORMED = "IDENTITY_MALFORMED"
RUNTIME_GENERATION_MALFORMED = "RUNTIME_GENERATION_MALFORMED"
#: 身份 bundle 与其要授权的运行代**指向不同环境/账户**（U15 r3 NEW-A）：
#: 二者各自自洽，但互不一致 —— 说明“本次运行的身份”与“被授权的运行代”不是
#: 同一回事，禁止静默 200。
IDENTITY_GENERATION_MISMATCH = "IDENTITY_GENERATION_MISMATCH"
#: 调用方要求了作用域绑定但运行代没有 scope_hash（或反之不一致）。
SCOPE_HASH_UNBOUND = "SCOPE_HASH_UNBOUND"
SCOPE_HASH_MISMATCH = "SCOPE_HASH_MISMATCH"
#: account_ref 来自 ``<env>:local`` 回退（未声明账户）：无法区分同一环境的多个账户。
ACCOUNT_REF_UNPROVEN = "ACCOUNT_REF_UNPROVEN"
ACCOUNT_REF_LOCAL_FALLBACK = "ACCOUNT_REF_LOCAL_FALLBACK"
#: 未显式选择 profile：执行作用域未锁定（live 下为阻断项）。
EXECUTION_PROFILE_NOT_DECLARED = "EXECUTION_PROFILE_NOT_DECLARED"
EXECUTION_SCOPE_UNLOCKED = "EXECUTION_SCOPE_UNLOCKED"
#: 模型路由 / 备用路由证据不足（不得静默当作“无备用”或“可用”）。
PROVIDER_CAPABILITY_UNPROVEN = "PROVIDER_CAPABILITY_UNPROVEN"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
FALLBACK_PROVIDER_UNRECOGNISED = "FALLBACK_PROVIDER_UNRECOGNISED"
#: 有效配置来源文件缺失（六类来源之一无法采集）。
CONFIG_SOURCE_MISSING = "CONFIG_SOURCE_MISSING"
#: environment ↔ 状态根一致性（diff-7 缺口②）。
ENVIRONMENT_UNBOUND = "ENVIRONMENT_UNBOUND"
STATE_ROOT_UNBOUND = "STATE_ROOT_UNBOUND"
STATE_ROOT_PROFILE_UNLOCKED = "STATE_ROOT_PROFILE_UNLOCKED"
ENVIRONMENT_STATE_ROOT_MISMATCH = "ENVIRONMENT_STATE_ROOT_MISMATCH"
LIVE_SHARED_STATE_ROOT = "LIVE_SHARED_STATE_ROOT"

#: 执行模式合法取值（``paper`` 仅测试用；profile 只允许 demo/live）。
MODE_ALLOWED: tuple[str, ...] = ("paper", "demo", "testnet", "live")
PROFILE_ALLOWED: tuple[str, ...] = ("demo", "live")

#: 控制面角色与动作（R11：operator/health/risk 权限分离）。
CONTROL_ROLES: tuple[str, ...] = ("operator", "health", "risk")
AUTO_ACTOR = "auto"

#: 控制状态严重度：数值越大越严；自动恢复只能“不变或放宽到更严的反面”——
#: 即自动流程**不得**把人工禁用的状态放宽。
CONTROL_STATE_SEVERITY: dict[str, int] = {
    "normal": 0,
    "close_only": 1,
    "paused": 2,
    "emergency": 3,
}

#: ``emergency_stop`` 的语义就是“进入 emergency”：显式给出别的目标态即拒绝
#: （U15 r3 NEW-G P3-1：此前 ``emergency_stop + close_only`` 被放行，审计会记成
#: “紧急停机”却只进 close_only）。
EMERGENCY_STOP_TARGET_MISMATCH = "EMERGENCY_STOP_TARGET_MISMATCH"
#: 回读/请求状态不是本模块已知的控制状态 → 结构化拒绝，不按“文本相等”判成功
#: （U15 r3 NEW-G P3-3：``('bogus','bogus')`` 此前 ok=True）。
CONTROL_STATE_ILLEGAL = "CONTROL_STATE_ILLEGAL"
#: ``auto`` 流程允许的动作（只读 + 更严方向；放宽方向在上游已 409）。
AUTO_PERMITTED_ACTIONS: tuple[str, ...] = (
    "read_status",
    "acknowledge_incident",
    "set_control",
    "set_control_severity_up",
    "emergency_stop",
)

#: 每个角色可执行的控制动作（未列出即拒绝）。
CONTROL_ACTION_ROLES: dict[str, tuple[str, ...]] = {
    "read_status": ("operator", "health", "risk"),
    "set_control": ("operator", "risk"),
    "set_control_severity_up": ("operator", "risk"),
    "set_control_severity_down": ("operator",),
    "emergency_stop": ("operator", "risk"),
    "acknowledge_incident": ("operator", "health", "risk"),
    "resolve_incident": ("operator", "risk"),
    "unspecified": (),
}

CONTROL_ACTIONS: tuple[str, ...] = tuple(CONTROL_ACTION_ROLES)

#: 环回地址白名单；控制接口只允许本机来源。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

#: 探测标记：载荷键名出现这些子串即判定为“疑似整份 .env 载荷”。
_RAW_ENV_KEY_MARKERS = (".env", "env_file", "env_text", "raw_env", "dotenv")

#: **文件名**级凭据守卫：只认“凭据容器词”，与 K01 键名标记
#: （``SENSITIVE_KEY_MARKERS``）是两条**互不相通**的口径。契约 L1 的语义是
#: “敏感守卫只查键名/载荷形态”，因此这里**不得**复用键名标记表：那是“键名/字段名”
#: 口径，拿它做文件名子串匹配会把普通**代码模块**（如
#: ``management_authorization.py`` 命中 ``authorization``）误判为凭据文件，
#: 属过度匹配（U12-BUILD-F1）。只拒绝真正的凭据**容器**文件名。
CREDENTIAL_FILE_NAME_MARKERS: tuple[str, ...] = (
    "secret",
    "credential",
    "apikey",
    "api_key",
    "private_key",
    "password",
    "passwd",
    "passphrase",
)
#: 凭据/私钥文件后缀（文件名以这些结尾即为凭据容器）。
CREDENTIAL_FILE_NAME_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".ppk",
    ".jks",
    ".keystore",
    ".env",
)
#: 免后缀也一眼是凭据容器的前缀（``.env`` / ``.env.*`` / SSH 私钥等）。
CREDENTIAL_FILE_NAME_PREFIXES: tuple[str, ...] = (
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
)


def _looks_like_credential_file(name: Any) -> bool:
    """**文件名**是否呈“凭据文件形态”（独立的文件名守卫，见上）。

    与 :func:`is_sensitive_name` 的区别：后者是**键名/字段名**口径（K01
    ``SENSITIVE_KEY_MARKERS``，含 ``key``/``token``/``authorization`` 等宽标记），
    拿来做文件名子串匹配会把 ``management_authorization.py`` 这类**代码模块**
    误杀。这里只认凭据**容器**词、凭据后缀与 ``.env``/SSH 私钥前缀。
    """
    lowered = str(name).lower().replace("\\", "/").rsplit("/", 1)[-1]
    if not lowered:
        return False
    if any(lowered == prefix or lowered.startswith(f"{prefix}.")
           for prefix in CREDENTIAL_FILE_NAME_PREFIXES):
        return True
    if any(marker in lowered for marker in CREDENTIAL_FILE_NAME_MARKERS):
        return True
    return any(lowered.endswith(suffix) for suffix in CREDENTIAL_FILE_NAME_SUFFIXES)


class RuntimeIdentityError(RuntimeError):
    """本模块所有结构化拒绝的基类。"""


class MissingRuntimeInputError(RuntimeIdentityError):
    """必需的非敏感输入缺失（依赖锁、策略卡、git 元数据等）。"""

    def __init__(self, items: Sequence[str]) -> None:
        self.items = tuple(items)
        super().__init__("runtime identity inputs missing: " + ", ".join(self.items))


class CredentialLeakError(RuntimeIdentityError):
    """试图把疑似凭据（键名命中敏感标记）送入身份哈希。"""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = tuple(sorted(set(names)))
        # 只报键名路径，绝不回报值。
        super().__init__("credential-bearing fields rejected: " + ", ".join(self.names))


class ModelRouteConfigError(RuntimeIdentityError):
    """模型路由配置不合法（非法枚举、缺失必需字段等）。"""


class FallbackRouteError(ModelRouteConfigError):
    """备用路由参数非法（DL01 负例：备用非法参数必须拒绝而非静默跳过）。"""


class ExecutionModeError(RuntimeIdentityError):
    """执行模式 / profile 非法或来源冲突（DL01 负例：CLI 误选必须拒绝）。"""


class StrategySourceMismatchError(RuntimeIdentityError):
    """策略卡内容与声明的来源文件不一致（L16 文件级来源校验）。"""


def is_sensitive_name(name: Any) -> bool:
    """键名是否命中 K01 敏感标记（大小写不敏感的子串匹配）。

    已知限制（已登记：``docs/contracts/README.md`` §12 ``L1``，U15 r3 NEW-B）：
    只做 ``lower()`` + 子串匹配，**不做 Unicode 归一化（NFKC）**。因此全角/同形字
    键名（如 ``ｌｌｍ.ａｐｉ＿ｋｅｙ``）不会被识别为敏感键，其值可能进入
    ``model_route_hash`` 与 ``ResolvedModelRoute.as_dict()``。补这一层要引入
    ``unicodedata.normalize`` 并重新界定“敏感键”口径（含各语言同形字），属
    K01 敏感标记的共享口径变更（``src/core/contracts/common.py``），不在 U15
    独占路径内；调用方不应把该守卫当作“值级防泄漏”。
    """
    lowered = str(name).lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def guard_no_raw_env_payload(payload: Any, *, name: str, prefix: str = "") -> None:
    """显式拒绝“整份 .env”形态的载荷（递归遍历 leaf 值）。

    K01 守卫拒绝原始文本；这里再挡一层 **Mapping 形态** 的整文件载荷
    （键名如 ``.env``/``env_file``，或值里塞进多行 ``KEY=VALUE`` 文本）。
    命中即抛 ``CredentialLeakError``，错误信息只含键路径、不含值。
    """
    if isinstance(payload, Mapping):
        for key in payload:
            path = f"{name}.{prefix}{key}" if prefix else f"{name}.{key}"
            lowered = str(key).lower()
            if any(marker in lowered for marker in _RAW_ENV_KEY_MARKERS):
                raise CredentialLeakError([path])
            if is_sensitive_name(key):
                raise CredentialLeakError([path])
            guard_no_raw_env_payload(payload[key], name=name, prefix=f"{prefix}{key}.")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            guard_no_raw_env_payload(item, name=name, prefix=f"{prefix}[{index}].")
    elif isinstance(payload, str) and _looks_like_env_text(payload):
        raise CredentialLeakError([f"{name}.{prefix.rstrip('.')} (raw env text)"])


def _looks_like_env_text(value: str) -> bool:
    """多行 ``NAME=...`` 文本判定：>=2 行且过半行是 ``NAME=`` 形态。"""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    assignments = sum(
        1 for line in lines
        if "=" in line and not line.startswith("#") and line.split("=", 1)[0].strip().replace("_", "").isalnum()
    )
    return assignments * 2 >= len(lines)


# --------------------------------------------------------------------------
# git 状态（K01 ``git_commit`` 是独立身份字段）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GitState:
    """一次运行实际绑定的代码提交；无 git 时显式降级而不是静默。"""

    commit: str
    available: bool
    dirty: bool
    dirty_paths: tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "available": self.available,
            "dirty": self.dirty,
            "dirty_paths": list(self.dirty_paths),
            "detail": self.detail,
        }

    def advisory_reasons(self) -> tuple[str, ...]:
        """提示类原因（不阻断，但必须可见，替代旧的静默 ``unknown``）。"""
        reasons: list[str] = []
        if not self.available or self.commit in ("", "unknown"):
            reasons.append("GIT_COMMIT_UNAVAILABLE")
        if self.dirty:
            reasons.append("GIT_WORKTREE_DIRTY")
        return tuple(reasons)

    def hard_reasons(self, expected_commit: str | None) -> tuple[str, ...]:
        """阻断类原因：仅在显式声明了预期提交且实际不一致时产生。"""
        if expected_commit and self.commit != expected_commit:
            return ("GIT_COMMIT_MISMATCH",)
        return ()

    def mismatch_reasons(self, expected_commit: str | None) -> tuple[str, ...]:
        """提示 + 阻断原因的合并视图（审计用，供 U09 证据消费）。"""
        return tuple(dict.fromkeys((*self.hard_reasons(expected_commit), *self.advisory_reasons())))


def _run_git(root: Path, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(root), check=False, timeout=timeout,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None


def resolve_git_state(root: Path | str) -> GitState:
    """解析 HEAD 与工作区脏状态；不改变工作区，不联网。"""
    path = Path(root)
    head = _run_git(path, "rev-parse", "HEAD")
    if head is None:
        return GitState("unknown", False, False, detail="git executable unavailable")
    if head.returncode != 0:
        return GitState("unknown", False, False, detail="not a git worktree")
    commit = head.stdout.strip()
    if not commit:
        return GitState("unknown", False, False, detail="empty HEAD")
    status = _run_git(path, "status", "--porcelain")
    dirty = bool(status is not None and status.returncode == 0 and status.stdout.strip())
    dirty_paths: tuple[str, ...] = ()
    if dirty and status is not None:
        dirty_paths = tuple(sorted(
            line[3:].strip() for line in status.stdout.splitlines() if line.strip()
        ))[:64]
    return GitState(commit, True, dirty, dirty_paths, detail="ok")


# --------------------------------------------------------------------------
# 文件级身份输入（依赖锁 / 策略卡 / 代码工件）
# --------------------------------------------------------------------------


def _read_text(root: Path, relative: str) -> str | None:
    target = root / relative
    if not target.is_file():
        return None
    return target.read_text(encoding="utf-8")


def collect_dependency_lock_files(
    root: Path | str, *, require_lock: bool = True,
) -> dict[str, str]:
    """读取实际部署锁文件（``路径 -> 内容``，供 K01 ``hash_dependencies``）。

    默认要求 ``requirements-lock.txt`` 在场：缺失即结构化拒绝，而不是退化成
    只哈希范围约束文件（指纹报告 §1.2 / 不一致清单 #5）。
    """
    base = Path(root)
    payload: dict[str, str] = {}
    for relative in DEPENDENCY_LOCK_FILES:
        text = _read_text(base, relative)
        if text is not None:
            payload[relative] = text
    if require_lock and DEPENDENCY_LOCK_FILES[0] not in payload:
        raise MissingRuntimeInputError([DEPENDENCY_LOCK_FILES[0]])
    return payload


def compare_dependency_lock(
    root: Path | str, *, declared: Sequence[str] | None = None,
) -> str:
    """K01 ``IDENTITY_COVERAGE["dependency"]`` 的 ``comparison_status``（NEW-E）。

    指纹报告 ``diff-2`` 缺口②要求“比对结果另存”：只把锁文件内容拿进
    ``dependency_hash`` 还不够，还要**显式产出一个可复核的对照结论**，
    否则“名单漂移（清单声明集合漏掉身份要求的锁文件 → 锁文件改动漏出指纹）”
    这种复发形态没有任何证据位可查。取值与语义：

    - ``MATCH``：身份模块要求的每个部署锁文件都在 ``declared`` 里，且确实在盘上；
    - ``MISMATCH``：两份名单已漂移（``declared`` 漏了身份要求的锁文件 → 该文件的
      改动不会体现在清单指纹里）；
    - ``UNAVAILABLE``：必需锁文件缺失（身份不可证明），或 ``declared`` 形状非法。

    ``declared=None`` 表示调用方**没有**自己的声明集合：此时只报告身份侧锁文件
    是否齐备（齐备 ``MATCH``／缺失 ``UNAVAILABLE``），不制造虚假对照。

    只读：不改路径、不写文件、不读 ``.env``。发布清单侧的字段落地见
    ``docs/dispatch/patches/U15/<基线>/01_release_manifest.diff``
    （``manifest.dependency_comparison_status``，接线归 I）。
    """
    base = Path(root)
    if declared is not None and (
            not isinstance(declared, Sequence) or isinstance(declared, (str, bytes))):
        return DEPENDENCY_COMPARISON_UNAVAILABLE
    required = tuple(DEPENDENCY_LOCK_FILES)
    declared_set = {str(name) for name in (required if declared is None else declared)}
    if any(name not in declared_set for name in required):
        return DEPENDENCY_COMPARISON_MISMATCH
    if not all((base / name).is_file() for name in required):
        return DEPENDENCY_COMPARISON_UNAVAILABLE
    return DEPENDENCY_COMPARISON_MATCH


#: 单个工件文件的大小上限（源文件级别足够）；超出即拒绝，避免把数据文件卷进身份。
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def _safe_artifact_name(base: Path, name: Any) -> str:
    """工件相对路径必须落在 ``project_root`` 内，且不是凭据/环境文件。

    U15 r2 P1-2：此前绝对路径（或足够深的 ``../``）能把 root 之外的任意文件
    ——包括 ``.env``——读进 ``artifact_hash``。现在三类都结构化拒绝：
    绝对路径 / 逃逸到 root 之外 / **文件名**呈凭据容器形态或命中 ``.env`` 标记。

    U12-BUILD-F1：第三类**只**用 :func:`_looks_like_credential_file` 这条**文件名**
    口径（凭据容器词/后缀/``.env``·SSH 私钥前缀），**不再**复用 K01 键名标记表
    ``SENSITIVE_KEY_MARKERS``——那是键名口径，用作文件名子串匹配会把
    ``src/core/management_authorization.py``（普通代码模块）误杀，使真实 ``src/``
    树恒采集失败、发布门禁永不 GO。
    """
    raw = str(name).replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or ".." in parts:
        raise MalformedInputError([f"artifact_relatives[{name}] (absolute or escaping path)"])
    if not parts:
        raise MalformedInputError([f"artifact_relatives[{name}] (empty path)"])
    for part in parts:
        # U12-BUILD-F1：文件名只用**文件名**口径守卫（凭据容器形态），
        # 不得复用 K01 键名标记表——否则 ``management_authorization.py``
        # 因含 ``authorization`` 被误杀，真实 src/ 树永远采不到身份。
        if _looks_like_credential_file(part) or any(
                marker in part.lower() for marker in _RAW_ENV_KEY_MARKERS):
            raise CredentialLeakError([f"artifacts.{raw}"])
    if not (base / raw).resolve().is_relative_to(base.resolve()):
        raise MalformedInputError([f"artifact_relatives[{name}] (outside project_root)"])
    return raw


def collect_artifact_files(root: Path | str, *, relatives: Sequence[str] | None = None) -> dict[str, str]:
    """收集不可变代码工件内容（``相对路径 -> 内容``）。

    ``relatives`` 缺省为 ``src/**/*.py``；脚本等入口文件由调用方显式传入，
    避免把临时诊断文件卷进身份。内容为 UTF-8 文本；读取失败按缺失处理。

    每个路径都过 :func:`_safe_artifact_name`（root 边界 + 凭据/``.env`` 名拒绝），
    单文件超过 ``_MAX_ARTIFACT_BYTES`` 也结构化拒绝。
    """
    base = Path(root)
    if relatives is None:
        files = sorted(
            path for path in (base / "src").rglob("*.py")
            if not any(part in IDENTITY_ARTIFACT_IGNORED_PARTS for part in path.parts)
        )
        names = [path.relative_to(base).as_posix() for path in files]
    else:
        names = [Path(str(name)).as_posix() for name in relatives]
    payload: dict[str, str] = {}
    for name in names:
        safe = _safe_artifact_name(base, name)
        target = base / safe
        if target.is_file() and target.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise MalformedInputError([
                f"artifact_relatives[{safe}] (file larger than {_MAX_ARTIFACT_BYTES} bytes)"
            ])
        text = _read_text(base, safe)
        if text is not None:
            payload[safe] = text
    return payload


@dataclass(frozen=True)
class StrategyCardSource:
    """策略卡内容及其来源文件证据（L16 文件级来源校验载体）。"""

    source_file: str
    payload: Any
    source_content: str
    card_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "strategy_hash": hash_strategy(self.payload),
            "card_count": self.card_count,
        }


def _count_cards(payload: Any) -> int:
    if isinstance(payload, Mapping):
        cards = payload.get("cards")
        if isinstance(cards, (list, tuple)):
            return len(cards)
        return 0
    if isinstance(payload, (list, tuple)):
        return len(payload)
    return 0


def load_strategy_cards(
    root: Path | str, *, source_file: str = REQUIRED_STRATEGY_SOURCE,
) -> StrategyCardSource:
    """加载策略卡内容并保留来源文件原文，供后续文件级复核。

    缺失文件或非法 JSON 一律结构化拒绝：身份宁可不可用，也不接受“无策略卡”
    的静默降级（改卡即改身份，``strategy_hash`` 必须覆盖实际卡片）。

    U15 r2 P1-3：``{"cards": []}``（以及被截断成 ``{"version": 3}`` 的文件）
    同样拒绝——零卡不是“已声明且非空”的策略集，删光卡片不得成为可发布身份。
    """
    base = Path(root)
    text = _read_text(base, source_file)
    if text is None:
        raise MissingRuntimeInputError([source_file])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - 由负例测试覆盖
        raise MissingRuntimeInputError([f"{source_file} (invalid json: {exc.msg})"]) from exc
    if not isinstance(payload, (Mapping, list)):
        raise MissingRuntimeInputError([f"{source_file} (payload must be mapping or list)"])
    if len(payload) == 0:
        raise MissingRuntimeInputError([f"{source_file} (empty payload)"])
    card_count = _count_cards(payload)
    if card_count <= 0:
        raise MissingRuntimeInputError([
            f"{source_file} (no strategy cards: expected a non-empty 'cards' list)"
        ])
    return StrategyCardSource(source_file, payload, text, card_count)


def verify_strategy_cards_source(source: StrategyCardSource, root: Path | str) -> str:
    """文件级来源复核：重读声明文件并重算卡片哈希（L16）。

    比对两件事：① 文件原文与加载时原文一致（加载后被改动即失败）；
    ② 由文件原文重算的卡片哈希与 ``source.payload`` 的哈希一致（内存篡改即
    失败）。任一不一致 → ``StrategySourceMismatchError``：调用方手里的策略卡
    并非来自所声明的文件，因此身份里的 ``strategy_hash`` 不可采信。
    """
    base = Path(root)
    current = _read_text(base, source.source_file)
    if current is None:
        raise MissingRuntimeInputError([source.source_file])
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != \
            hashlib.sha256(source.source_content.encode("utf-8")).hexdigest():
        raise StrategySourceMismatchError(
            f"strategy cards file changed after load: {source.source_file}"
        )
    try:
        recomputed = json.loads(current)
    except json.JSONDecodeError as exc:  # pragma: no cover - 由负例测试覆盖
        raise StrategySourceMismatchError(
            f"strategy cards file is not valid json: {source.source_file} ({exc.msg})"
        ) from exc
    if hash_strategy(recomputed) != hash_strategy(source.payload):
        raise StrategySourceMismatchError(
            f"strategy cards payload differs from declared source {source.source_file}"
        )
    return hash_strategy(source.payload)


# --------------------------------------------------------------------------
# 执行作用域（DL01：CLI 误选必须拒绝）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionScope:
    """解析后的执行作用域：profile（冻结语义）与模式（broker 实际模式）。"""

    profile: str | None
    mode: str
    source: str
    ignored: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def locked(self) -> bool:
        return self.profile is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "mode": self.mode,
            "source": self.source,
            "locked": self.locked,
            "ignored": list(self.ignored),
            "warnings": list(self.warnings),
        }


def _normalise_mode(value: Any, *, field_name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ExecutionModeError(f"{field_name} must be a string, got {type(value).__name__}")
    text = value.strip()
    if text == "":
        # 空串是“显式给出的空值”，与“未提供”不同：静默回退默认值会掩盖配置错误。
        raise ExecutionModeError(f"{field_name} is an empty string (use None to mean unset)")
    return text.lower()


def resolve_execution_scope(
    *,
    profile: Any = None,
    execution_mode: Any = None,
    default_mode: Any = "demo",
) -> ExecutionScope:
    """按 ``profile > EXECUTION_MODE > default`` 解析执行作用域。

    与旧实现（``exchange/factory.py``）的差别是 **fail-closed**：

    - 未知模式/未知 profile 直接拒绝（不再静默落回默认值，DL01 负例“CLI 误选”）；
    - profile 为空串与未提供是两种语义（空串拒绝）；
    - profile 与 EXECUTION_MODE 同时给出且不一致时，把被忽略的一方记入
      ``ignored``（可审计，而不是无声丢失）；
    - 未显式选择 profile（``locked is False``）时给出 ``warnings``
      （``EXECUTION_PROFILE_NOT_DECLARED``）而不是静默：无 profile 的实盘作用域
      无法锁定状态根（decl-7），调用方须自行判断是否阻断。
    """
    raw_profile = _normalise_mode(profile, field_name="profile")
    if raw_profile and raw_profile not in PROFILE_ALLOWED:
        raise ExecutionModeError(
            f"unknown profile {raw_profile!r}; allowed: {', '.join(PROFILE_ALLOWED)}"
        )
    selected_profile: str | None = raw_profile or None

    raw_mode = _normalise_mode(execution_mode, field_name="execution_mode")
    if raw_mode and raw_mode not in MODE_ALLOWED:
        raise ExecutionModeError(
            f"unknown execution mode {raw_mode!r}; allowed: {', '.join(MODE_ALLOWED)}"
        )
    fallback_mode = _normalise_mode(default_mode, field_name="default_mode")
    if fallback_mode and fallback_mode not in MODE_ALLOWED:
        raise ExecutionModeError(f"unknown default mode {fallback_mode!r}")

    if selected_profile is not None:
        ignored = ("EXECUTION_MODE",) if raw_mode else ()
        return ExecutionScope(selected_profile, selected_profile, "profile", ignored)
    unlocked = (EXECUTION_PROFILE_NOT_DECLARED,)
    if raw_mode:
        return ExecutionScope(None, raw_mode, "EXECUTION_MODE", (), warnings=unlocked)
    if fallback_mode:
        return ExecutionScope(None, fallback_mode, "default_mode", (), warnings=unlocked)
    raise ExecutionModeError("no execution mode could be resolved")


# --------------------------------------------------------------------------
# 模型路由（DL01：覆盖优先级 / Mock 可见 / 备用非法参数）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedModelRoute:
    """解析后的有效模型路由 + 判定证据（不含任何凭据值）。"""

    route: ModelRoute
    provider: str
    mock: bool
    execution_mode: str | None
    sources: Mapping[str, str]
    blockers: tuple[str, ...] = ()
    #: 被递归丢弃的 profile 覆盖键路径（命中敏感标记）；只记路径不记值。
    dropped_overrides: tuple[str, ...] = ()
    #: provider 可用性证据：``unproven`` / ``declared_available`` /
    #: ``declared_unavailable``（WorkBuddy 依赖本机 CLI runtime，不是 API key）。
    provider_evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.as_payload(),
            "provider": self.provider,
            "mock": self.mock,
            "execution_mode": self.execution_mode,
            "sources": dict(self.sources),
            "blockers": list(self.blockers),
            "dropped_overrides": list(self.dropped_overrides),
            "provider_evidence": self.provider_evidence,
        }

    def identity_hash(self) -> str:
        return hash_model_route(self.route)


def _dig(payload: Mapping[str, Any], dotted: str) -> Any:
    node: Any = payload
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def validate_fallback_route(spec: Mapping[str, Any]) -> dict[str, Any]:
    """备用路由合法性校验（DL01 负例：非法备用参数必须报错）。

    拒绝：缺 key / 空模型 / 模型为 ``none`` / 非法 URL（非 http(s)、无主机名、
    URL 内嵌用户名密码）/ 未知键。返回脱敏后的规范描述（**不含 key 值**）。
    """
    if not isinstance(spec, Mapping):
        raise FallbackRouteError("fallback route spec must be a mapping")
    allowed = {
        "provider", "model", "base_url", "credential_present",
        "timeout_seconds", "min_remaining_seconds", "max_retries",
        "primary_max_retries", "default_429_cooldown_seconds",
    }
    unknown = sorted(str(key) for key in spec if str(key) not in allowed)
    if unknown:
        raise FallbackRouteError("unknown fallback fields: " + ", ".join(unknown))

    provider = _text(spec.get("provider")).lower()
    model = _text(spec.get("model"))
    base_url = _text(spec.get("base_url"))
    if not provider:
        raise FallbackRouteError("fallback provider is required")
    if not model or model.lower() == "none":
        raise FallbackRouteError("fallback model is required and must not be 'none'")
    if not base_url:
        raise FallbackRouteError("fallback base_url is required")
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise FallbackRouteError("fallback base_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise FallbackRouteError("fallback base_url must not embed credentials")
    if spec.get("credential_present") is False:
        raise FallbackRouteError("fallback credential is not present")
    if spec.get("credential_present") is None:
        raise FallbackRouteError("fallback credential presence must be declared explicitly")
    if not isinstance(spec.get("credential_present"), bool):
        raise FallbackRouteError("fallback credential_present must be a boolean")
    return {"provider": provider, "model": model, "base_url_host": parsed.hostname,
            "scheme": parsed.scheme}


def spec_env(env: Mapping[str, str], cfg: Mapping[str, Any], name_key: str) -> str:
    """从 ``cfg[name_key]`` 取环境变量名，再取该变量的 **值**。

    仅用于非敏感判定（模型名/env 开关），凭据字段只取“是否设置”，不取值。
    """
    var = _text(cfg.get(name_key))
    if not var:
        return ""
    return _text(env.get(var))


#: 备用路由状态（进 ``ResolvedModelRoute.sources["fallback"]``，禁止静默）。
FALLBACK_STATUS_ABSENT = "absent"
FALLBACK_STATUS_SKIPPED_AGENT = "skipped:agent-not-ai-trader"
FALLBACK_STATUS_UNRECOGNISED = "unrecognised-provider"


def _fallback_route(
    fallback_cfg: Mapping[str, Any],
    env: Mapping[str, str],
    creds: Mapping[str, bool],
    *,
    provider: str,
    agent_name: str = "ai_trader",
) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...], str]:
    """构造备用路由，返回 ``(routes, reason_codes, status)``。

    - 未声明 provider → ``("", "absent")``；
    - 声明的 provider 不认识（如 ``anthropic``）→ **不再静默当作“无备用”**：
      返回 ``FALLBACK_PROVIDER_UNRECOGNISED`` 与状态码，让身份里如实记下
      “运维声明了备用但运行时不支持”（DL01 故障降级承诺）；
    - agent 不是 ``ai_trader`` → 不构造备用（与 ``src/llm/factory.py:61``
      ``agent_name == "ai_trader" and fallback_provider == "opencode"`` 同语义），
      状态记 ``skipped:agent-not-ai-trader``，避免身份多报不存在的备用；
    - provider=opencode 但参数非法 → ``FallbackRouteError``（DL01 负例）。
    """
    text = _text(provider).lower()
    if not text:
        return (), (), FALLBACK_STATUS_ABSENT
    if _text(agent_name) != "ai_trader":
        return (), (), FALLBACK_STATUS_SKIPPED_AGENT
    if text != "opencode":
        return (), (FALLBACK_PROVIDER_UNRECOGNISED,), f"{FALLBACK_STATUS_UNRECOGNISED}:{text}"
    key_env = _text(fallback_cfg.get("api_key_env")) or "OPENCODE_API_KEY"
    declared = creds.get(key_env)
    key_present = (bool(_text(spec_env(env, fallback_cfg, "api_key_env")))
                   if declared is None else bool(declared))
    primary = {
        "provider": "opencode",
        "model": _text(spec_env(env, fallback_cfg, "model_env")) or _text(fallback_cfg.get("default_model")),
        "base_url": _text(spec_env(env, fallback_cfg, "base_url_env")) or _text(fallback_cfg.get("default_base_url")),
        "credential_present": key_present,
        "min_remaining_seconds": fallback_cfg.get("min_remaining_seconds"),
        "max_retries": fallback_cfg.get("max_retries"),
    }
    normalised = validate_fallback_route(primary)
    return (
        ({k: v for k, v in normalised.items() if k != "scheme"},), (),
        f"declared:opencode:{normalised['model']}",
    )


def resolve_model_route(
    *,
    raw_settings: Mapping[str, Any] | None = None,
    raw_agents: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    agent_name: str = "ai_trader",
    credentials_present: Mapping[str, bool] | None = None,
    execution_mode: str | None = None,
    profile_overrides: Mapping[str, Any] | None = None,
    allow_workbuddy_in_demo: bool = False,
    provider_available: Mapping[str, bool] | None = None,
) -> ResolvedModelRoute:
    """解析有效模型路由并给出 Mock 判定与阻断原因。

    覆盖优先级（与现行实现一致，但**显式记录来源**）：

    1. provider：``MTA_AI_DECISION_PROVIDER=workbuddy`` 只作用于 ``ai_trader``；
    2. model：``agents.yaml[agent].model`` > 环境变量 ``settings.llm.model_env``
       > ``settings.llm.default_model``（**只在真实设置时才算覆盖**）；
    3. effort：``agents.yaml[agent].reasoning_effort`` > ``settings.llm.reasoning_effort``
       > ``"default"``；
    4. fallback_routes：仅 ``MTA_AI_DECISION_FALLBACK_PROVIDER=opencode`` 且
       agent 为 ai_trader 时构造；未声明 → 显式空元组（声明“无备用”）；
       声明了不认识的 provider → ``FALLBACK_PROVIDER_UNRECOGNISED``（不静默）；
    5. profile_overrides：由调用方传入的 profile 生效值 + provider/mock 判定证据。
       **递归**丢弃命中敏感标记的键（嵌套 ``{"llm": {"api_key": ...}}`` 同样拒绝），
       丢掉的键路径记入 ``dropped_overrides``。

    ``credentials_present`` 只接受 **布尔**（由调用方用 ``bool(os.getenv(name))``
    拍平），本函数不接受、也不会记录任何凭据值或凭据变量名。

    ``provider_available`` 是调用方对 provider **能力**的显式声明（布尔映射），
    用于 WorkBuddy：WorkBuddy 依赖本机 CLI runtime（``MTA_WORKBUDDY_CLI`` /
    node），不是 API key，本模块在 ``project_root`` 之外不做文件系统探测，
    因此**不猜**：未声明 → ``PROVIDER_CAPABILITY_UNPROVEN``（live/demo 阻断），
    声明 False → ``PROVIDER_UNAVAILABLE``，声明 True → 记录 ``declared_available``。
    绝不再把未验证的事实写进 ``mock_source="credential_present"``。
    """
    if raw_settings is not None and not isinstance(raw_settings, Mapping):
        raise MalformedInputError(["raw_settings must be a mapping"])
    if raw_agents is not None and not isinstance(raw_agents, Mapping):
        raise MalformedInputError(["raw_agents must be a mapping"])
    if env is not None and not isinstance(env, Mapping):
        raise MalformedInputError(["env must be a mapping"])
    if credentials_present is not None and not isinstance(credentials_present, Mapping):
        raise MalformedInputError(["credentials_present must be a mapping"])
    if profile_overrides is not None and not isinstance(profile_overrides, Mapping):
        raise MalformedInputError(["profile_overrides must be a mapping"])
    if provider_available is not None and not isinstance(provider_available, Mapping):
        raise MalformedInputError(["provider_available must be a mapping"])
    settings = raw_settings or {}
    agents = raw_agents or {}
    env_map = {str(k): str(v) for k, v in (env if env is not None else {}).items()}
    creds = {str(k): bool(v) for k, v in (credentials_present or {}).items()}

    llm = settings.get("llm") if isinstance(settings, Mapping) else {}
    llm = llm if isinstance(llm, Mapping) else {}
    agent_cfg: Mapping[str, Any] = {}
    for item in (agents.get("agents") or []) if isinstance(agents, Mapping) else []:
        if isinstance(item, Mapping) and item.get("name") == agent_name:
            agent_cfg = item
            break

    decision_provider = env_map.get("MTA_AI_DECISION_PROVIDER", "").strip().lower()
    provider = "workbuddy" if (agent_name == "ai_trader" and decision_provider == "workbuddy") else "sensenova"

    sources: dict[str, str] = {}
    provider_reasons: tuple[str, ...] = ()
    provider_evidence = ""
    if provider == "workbuddy":
        wb = llm.get("workbuddy") or {}
        wb = wb if isinstance(wb, Mapping) else {}
        model = _text(wb.get("model")) or "deepseek-v4.1-flash"
        effort = _text(wb.get("effort")) or "max"
        sources.update(model="settings.llm.workbuddy.model", effort="settings.llm.workbuddy.effort")
        mock = False
        # 能力证据必须由调用方声明：本模块不猜“本机 CLI 是否可用”。
        available = None if provider_available is None else provider_available.get("workbuddy")
        if available is None:
            provider_evidence = "unproven"
            provider_reasons = (PROVIDER_CAPABILITY_UNPROVEN,)
        elif available:
            provider_evidence = "declared_available"
        else:
            provider_evidence = "declared_unavailable"
            provider_reasons = (PROVIDER_UNAVAILABLE,)
    else:
        model_env = _text(llm.get("model_env")) or "SENSENOVA_MODEL"
        agent_model = _text(agent_cfg.get("model"))
        env_model = _text(env_map.get(model_env))
        default_model = _text(llm.get("default_model"))
        if agent_model:
            model, sources["model"] = agent_model, f"agents.yaml[{agent_name}].model"
        elif env_model:
            model, sources["model"] = env_model, f"environment:{model_env}"
        else:
            model, sources["model"] = default_model, "settings.llm.default_model"
        effort = _text(agent_cfg.get("reasoning_effort")) or _text(llm.get("reasoning_effort")) or "default"
        sources["effort"] = (
            f"agents.yaml[{agent_name}].reasoning_effort" if _text(agent_cfg.get("reasoning_effort"))
            else "settings.llm.reasoning_effort"
        )
        key_env = _text(llm.get("api_key_env")) or "SENSENOVA_API_KEY"
        # 默认：缺 key 即 Mock（与现行 factory 一致），但把结论显式暴露。
        key_present = creds.get(key_env)
        if key_present is None:
            key_present = bool(_text(env_map.get(key_env)))
        force_mock = llm.get("mock") is True
        mock = force_mock or (not key_present) or (not model) or model.lower() == "none"
        if force_mock:
            sources["mock"] = "settings.llm.mock"
        elif not key_present:
            sources["mock"] = "credential_absent"
        elif not model or model.lower() == "none":
            sources["mock"] = "model_unset"

    fallback_cfg = llm.get("decision_fallback") or {}
    fallback_cfg = fallback_cfg if isinstance(fallback_cfg, Mapping) else {}
    # NEW-F：备用 provider 的**环境变量名本身**是可配置的
    # （``decision_fallback.provider_env``）；此前硬编码读
    # ``MTA_AI_DECISION_FALLBACK_PROVIDER``，配置改名后备用会被静默丢弃
    # （status='absent'），运维以为有备用、身份里却什么都没有。
    fallback_env_name = _text(fallback_cfg.get("provider_env")) \
        or "MTA_AI_DECISION_FALLBACK_PROVIDER"
    # 先按声明名读；声明名未设置时回退到既有默认名（向后兼容：老配置里
    # provider_env 就写着默认名，两者等价；改名后旧名仍生效不至于静默丢备用）。
    fallback_provider = _text(env_map.get(fallback_env_name)).lower()
    if not fallback_provider and fallback_env_name != "MTA_AI_DECISION_FALLBACK_PROVIDER":
        fallback_provider = _text(
            env_map.get("MTA_AI_DECISION_FALLBACK_PROVIDER")).lower()
    sources["fallback_provider_env"] = fallback_env_name
    fallback_routes, fallback_reasons, fallback_status = _fallback_route(
        fallback_cfg, env_map, creds, provider=fallback_provider, agent_name=agent_name)
    sources["fallback"] = fallback_status

    # mock_source 必须只陈述**真正发生过**的判定；WorkBuddy 全程不检查 API key，
    # 因此不再复用 "credential_present" 这个标签（U15 r2 P1-6 伪证据）。
    if sources.get("mock"):
        mock_source = sources["mock"]
    elif mock:
        mock_source = "unknown"
    elif provider == "workbuddy":
        mock_source = "provider_local_cli"
    else:
        mock_source = "credential_present"
    overrides: dict[str, Any] = {
        "provider": provider,
        "mock": mock,
        "model_source": sources.get("model", ""),
        "effort_source": sources.get("effort", ""),
        "mock_source": mock_source,
        "fallback_status": fallback_status,
    }
    if provider_evidence:
        overrides["provider_evidence"] = provider_evidence
    if _text(execution_mode):
        overrides["execution_mode"] = _text(execution_mode).lower()
    override_dropped: list[str] = []
    override_payloads: list[str] = []
    profile_effective = _sanitise_source_payload(
        dict(profile_overrides or {}), prefix="profile_overrides",
        dropped=override_dropped, dropped_payloads=override_payloads,
    )
    if profile_effective:
        overrides["profile"] = profile_effective

    if not _text(model):
        # 结构化拒绝而不是让 ModelRoute 的 min_length 抛出第三方 ValidationError
        # （调用方拿到的是本模块异常，原因码 MODEL_ROUTE_UNSET 仍可从
        # model_route_blockers 取到）。
        raise ModelRouteConfigError(
            "MODEL_ROUTE_UNSET: no effective model could be resolved "
            "(agents.yaml[agent].model / env model_var / settings.llm.default_model all empty)"
        )
    route = ModelRoute(
        model=model,
        effort=effort or "default",
        fallback_routes=fallback_routes,
        profile_overrides=overrides,
    )
    blockers = model_route_blockers(
        route, mock=mock,
        environment=_text(execution_mode) or None,
        execution_mode=_text(execution_mode) or None,
        provider=provider,
        mock_source=sources.get("mock"),
        allow_workbuddy_in_demo=allow_workbuddy_in_demo,
    )
    # 备用路由不可用 / provider 能力未证明：与其它模型路由阻断同口径（paper 豁免）。
    extra = (*provider_reasons, *fallback_reasons)
    if extra and _text(execution_mode).lower() != "paper":
        blockers = tuple(dict.fromkeys((*blockers, *extra)))
    return ResolvedModelRoute(
        route=route, provider=provider, mock=mock,
        execution_mode=execution_mode, sources=sources, blockers=blockers,
        dropped_overrides=tuple(sorted(set(override_dropped))),
        provider_evidence=provider_evidence,
    )


def model_route_blockers(
    route: ModelRoute | Mapping[str, Any],
    *,
    mock: bool | None = None,
    environment: str | None = None,
    execution_mode: str | None = None,
    provider: str | None = None,
    mock_source: str | None = None,
    allow_workbuddy_in_demo: bool = False,
) -> tuple[str, ...]:
    """Demo/live 下“主模型无效”必须阻断新仓（DC14 / F13 / S05 冻结接口）。

    返回原因码（空元组 = 可用于真实交易）：
    ``MODEL_ROUTE_MOCK``（Mock/缺 key）、``MODEL_ROUTE_UNSET``（模型为空）、
    ``MODEL_ROUTE_FORCED_MOCK``（配置 ``llm.mock=true`` 显式强制）、
    ``WORKBUDDY_OUTSIDE_LIVE``（WorkBuddy 声明为实盘专属）、
    ``FALLBACK_DEGRADED_PRIMARY_MOCK``（主路 Mock 却声明了备用路由）。
    ``paper`` 模式不阻断（仅测试用途）。

    **模式未知按最严处理**：``execution_mode``/``environment`` 均为空时
    ``effective_mode`` 为空串，此时按“非 live”判定（而不是跳过 WorkBuddy
    阻断）。``resolve_model_route`` 的 ``execution_mode`` 默认正是 ``None``，
    旧实现在该默认下永不阻断，属 fail-open。
    """
    payload = route.as_payload() if isinstance(route, ModelRoute) else dict(route)
    model = _text(payload.get("model"))
    overrides = payload.get("profile_overrides")
    overrides = overrides if isinstance(overrides, Mapping) else {}
    is_mock = mock
    if is_mock is None:
        is_mock = overrides.get("mock") is True
    forced = is_mock and (mock_source or str(overrides.get("mock_source") or "")) == "settings.llm.mock"
    resolved_provider = provider if provider is not None else str(overrides.get("provider") or "")
    effective_mode = (_text(execution_mode) or _text(environment)).lower()
    reasons: list[str] = []
    if is_mock:
        reasons.append("MODEL_ROUTE_MOCK")
    if not model:
        reasons.append("MODEL_ROUTE_UNSET")
    if forced:
        reasons.append("MODEL_ROUTE_FORCED_MOCK")
    if resolved_provider == "workbuddy" and effective_mode != "live" \
            and not allow_workbuddy_in_demo:
        reasons.append("WORKBUDDY_OUTSIDE_LIVE")
    if is_mock and tuple(payload.get("fallback_routes") or ()):
        reasons.append("FALLBACK_DEGRADED_PRIMARY_MOCK")
    if effective_mode == "paper":
        return tuple()
    return tuple(dict.fromkeys(reasons))


# --------------------------------------------------------------------------
# 有效配置（只取有效非敏感值；不得把默认值当有效值）
# --------------------------------------------------------------------------


#: K01 ``EFFECTIVE_CONFIG_SOURCES`` 的六类来源落地映射。
#: 顺序即文档顺序：settings / decision_policy / agents / risk_learning /
#: risk_profile.lock / windows_service。每一项都必须真实进入
#: ``effective_config_hash``：改风险档、止损线、执行端点、白名单或服务定义
#: 都必须改变身份（U15 r2 P0-2）。
#: ``settings.yaml`` 既按 ``SETTINGS_WHITELIST`` 显式取值，也整份非敏感落盘，
#: 因为白名单之外的键（``channels.ai_daily_loss_stop`` / ``capital.*`` /
#: ``execution.base_url_map`` / ``market_universe.*`` / ``stability.*`` 等）
#: 同样是风险有效值。
CONFIG_SOURCE_FILES: tuple[tuple[str, str], ...] = (
    ("settings", "config/settings.yaml"),
    ("decision_policy", "config/decision_policy.yaml"),
    ("agents", "config/agents.yaml"),
    ("risk_learning", "config/risk_learning.yaml"),
    ("risk_profile.lock", "config/risk_profile.lock.json"),
    ("windows_service", "config/windows_service.json"),
)

#: 六类来源在 ``settings`` 里的保留命名空间（不会与点分白名单键冲突）。
CONFIG_SOURCES_KEY = "config_sources"

#: 递归遍历的安全上限（防止深嵌套/自引用载荷触发 RecursionError）。
_MAX_SOURCE_DEPTH = 24


class MalformedInputError(RuntimeIdentityError):
    """入参形状或载荷类型不合法（非 Mapping 的配置、非法 JSON/YAML 等）。

    结构化拒绝而不是把畸形输入静默当成空配置（U15 r2 P1-3）。
    """

    def __init__(self, items: Sequence[str]) -> None:
        self.items = tuple(items)
        super().__init__("malformed runtime identity input: " + ", ".join(self.items))


def _json_scalar(value: Any) -> Any:
    """把 YAML 标量收敛成 ``canonical_json`` 可序列化的类型。

    ``yaml.safe_load`` 会把 ISO 日期解析成 ``datetime.date``，直接进
    ``canonical_hash`` 会抛 ``TypeError``；这里统一转文本。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _sanitise_source_payload(
    value: Any, *, prefix: str, dropped: list[str], dropped_payloads: list[str],
    depth: int = 0,
) -> Any:
    """递归复制配置载荷，丢弃命中敏感标记的键与疑似 ``.env`` 文本值。

    只登记键路径/计数值，绝不把值写进任何输出。返回的对象可安全进入
    ``hash_effective_config``（无敏感键名、无非 JSON 标量、无原始 env 文本）。
    """
    if depth > _MAX_SOURCE_DEPTH:
        dropped_payloads.append(f"{prefix or '<root>'} (max depth exceeded)")
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            path = f"{prefix}.{name}" if prefix else name
            if is_sensitive_name(name):
                dropped.append(path)
                continue
            result[name] = _sanitise_source_payload(
                item, prefix=path, dropped=dropped, dropped_payloads=dropped_payloads,
                depth=depth + 1,
            )
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitise_source_payload(item, prefix=f"{prefix}[{index}]", dropped=dropped,
                                     dropped_payloads=dropped_payloads, depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str) and _looks_like_env_text(value):
        dropped_payloads.append(f"{prefix} (raw env text)")
        return None
    return _json_scalar(value)


def _load_structured_file(path: Path) -> Any:
    """读取 YAML/JSON 配置文件（``.json`` 直接 json，其余走 yaml）。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedInputError([f"{path.name} (invalid json: {exc.msg})"]) from exc
    try:
        import yaml  # noqa: PLC0415 - 延迟导入：本模块的纯函数面不依赖 yaml
    except ImportError as exc:  # pragma: no cover - 运行环境已装 pyyaml
        raise MalformedInputError([f"{path.name} (yaml reader unavailable)"]) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MalformedInputError([f"{path.name} (invalid yaml: {exc.__class__.__name__})"]) from exc


def collect_source_config_files(
    root: Path | str, *, sources: Sequence[tuple[str, str]] = CONFIG_SOURCE_FILES,
    include_settings: bool = False,
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """采集 K01 ``EFFECTIVE_CONFIG_SOURCES`` 的来源文件非敏感有效值。

    返回 ``(dumps, dropped_sensitive, dropped_payloads, missing)``：

    - ``dumps``：``来源标签 -> 脱敏后的结构化载荷``；文件缺失记
      ``{"__missing__": <相对路径>}``——**缺失本身也改变哈希**，不会被静默忽略；
    - ``dropped_sensitive``：被丢弃的敏感键路径（只记路径，不记值）；
    - ``dropped_payloads``：被丢弃的整份 ``.env`` 形态文本 / 超深节点；
    - ``missing``：确实不存在的来源文件相对路径。

    ``include_settings=False``（默认）时跳过 ``config/settings.yaml``：该来源的
    **有效值**由调用方实际使用的 ``raw_settings`` 直接采集（见
    :func:`collect_effective_config`），避免磁盘文件与内存值两份“有效值”互相
    矛盾。其余五类来源（decision_policy / agents / risk_learning /
    risk_profile.lock / windows_service）只能从文件采集。

    本函数只读 ``root`` 下的受信配置文件，不读 ``.env``/``data``/密钥。
    """
    base = Path(root)
    dumps: dict[str, Any] = {}
    dropped: list[str] = []
    dropped_payloads: list[str] = []
    missing: list[str] = []
    for label, relative in sources:
        if label == "settings" and not include_settings:
            continue
        target = base / relative
        if not target.is_file():
            missing.append(relative)
            dumps[label] = {"__missing__": relative}
            continue
        payload = _load_structured_file(target)
        dumps[label] = _sanitise_source_payload(
            payload, prefix=label, dropped=dropped, dropped_payloads=dropped_payloads)
    return (dumps, tuple(sorted(set(dropped))), tuple(sorted(set(dropped_payloads))),
            tuple(missing))





def _flatten_into(
    target: dict[str, Any], prefix: str, value: Mapping[str, Any],
    dropped: list[str], *, origin: str = "",
) -> None:
    """把嵌套非敏感标量扁平化为点分键；命中敏感标记的键只登记不取值。"""
    for key, item in value.items():
        name = str(key)
        path = f"{prefix}{name}" if not prefix else f"{prefix}.{name}"
        label = path if not origin else f"{origin}.{path}"
        if is_sensitive_name(name):
            dropped.append(label)
            continue
        if isinstance(item, Mapping):
            _flatten_into(target, path, item, dropped, origin=origin)
            continue
        if isinstance(item, (list, tuple, set)):
            raise CredentialLeakError([label if origin else f"effective_config.{label} (non-scalar)"])
        if item is None or item == "":
            continue
        target[path] = item


@dataclass(frozen=True)
class EffectiveConfig:
    """K01 ``effective_config`` 三字段载荷 + 采集证据。"""

    settings: Mapping[str, Any]
    profile_overrides: Mapping[str, Any]
    environment_overrides: Mapping[str, Any]
    dropped_sensitive: tuple[str, ...] = ()
    sources: Mapping[str, str] = field(default_factory=dict)
    dropped_payloads: tuple[str, ...] = ()
    missing_sources: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "settings": dict(self.settings),
            "profile_overrides": dict(self.profile_overrides),
            "environment_overrides": dict(self.environment_overrides),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.as_payload(),
            "dropped_sensitive": list(self.dropped_sensitive),
            "dropped_payloads": list(self.dropped_payloads),
            "missing_sources": list(self.missing_sources),
            "sources": dict(self.sources),
        }


def environment_override_values(env: Mapping[str, str] | None) -> dict[str, str]:
    """按白名单提取 **确实被设置** 的环境覆盖值。

    未设置的变量不出现（不得用默认值填充）；非白名单变量（含任何凭据类名字）
    一律忽略，因此原始 ``.env`` 不可能进入哈希。
    """
    payload: dict[str, str] = {}
    for name in ENVIRONMENT_OVERRIDE_WHITELIST:
        raw = (env or {}).get(name)
        if raw is None:
            continue
        text = str(raw).strip()
        if text == "":
            continue
        payload[name] = text
    return payload


def collect_effective_config(
    *,
    raw_settings: Mapping[str, Any] | None = None,
    profile: str | None = None,
    env: Mapping[str, str] | None = None,
    extra_profile_overrides: Mapping[str, Any] | None = None,
    source_configs: Mapping[str, Any] | None = None,
    source_dropped_sensitive: Sequence[str] = (),
    source_dropped_payloads: Sequence[str] = (),
    source_missing: Sequence[str] = (),
) -> EffectiveConfig:
    """采集 settings + profile 覆盖 + 环境覆盖的有效非敏感值。

    - ``settings`` 只取白名单点分路径 **且实际存在** 的值；
    - 命中敏感标记的键（如 ``llm.api_key_env``）被丢弃并计入 ``dropped_sensitive``；
    - ``profile_overrides`` 只有显式选择了 demo/live profile 时才收录该段的实际覆盖值；
    - ``environment_overrides`` 只收录白名单内确实设置的非空变量；
    - ``source_configs``（来自 :func:`collect_source_config_files`）是 K01
      ``EFFECTIVE_CONFIG_SOURCES`` 六类来源的脱敏载荷，嵌入
      ``settings[CONFIG_SOURCES_KEY]``；**白名单之外的键因此也进入身份哈希**
      （U15 r2 P0-2：风险档/止损线/执行端点/服务定义漂移必须改变身份）。

    ``raw_settings`` 必须是 Mapping；传入其它类型（如列表）**结构化拒绝**而不是
    静默当成空配置。
    """
    if raw_settings is not None and not isinstance(raw_settings, Mapping):
        raise MalformedInputError(["raw_settings must be a mapping"])
    settings = raw_settings or {}
    dropped: list[str] = []
    effective: dict[str, Any] = {}
    for dotted in SETTINGS_WHITELIST:
        if is_sensitive_name(dotted.split(".")[-1]):
            dropped.append(dotted)
            continue
        value = _dig(settings, dotted)
        if value is None or value == "":
            continue
        if isinstance(value, Mapping):
            _flatten_into(effective, dotted, value, dropped)
            continue
        if isinstance(value, (list, tuple, set)):
            raise CredentialLeakError([f"effective_config.settings.{dotted} (non-scalar)"])
        effective[dotted] = value

    overrides: dict[str, Any] = {}
    if profile is not None:
        if profile not in PROFILE_ALLOWED:
            raise ExecutionModeError(f"unknown profile {profile!r}")
        section = _dig(settings, f"profiles.{profile}")
        if isinstance(section, Mapping):
            _flatten_into(overrides, "", section, dropped, origin=f"profiles.{profile}")
    if env is not None and not isinstance(env, Mapping):
        raise MalformedInputError(["env must be a mapping"])
    if extra_profile_overrides is not None and not isinstance(extra_profile_overrides, Mapping):
        raise MalformedInputError(["extra_profile_overrides must be a mapping"])
    for key, value in dict(extra_profile_overrides or {}).items():
        if is_sensitive_name(key):
            dropped.append(f"extra_profile_overrides.{key}")
            continue
        if isinstance(value, Mapping):
            _flatten_into(overrides, "", value, dropped, origin=f"extra_profile_overrides.{key}")
            continue
        if isinstance(value, (list, tuple, set)):
            raise CredentialLeakError([f"effective_config.profile_overrides.{key} (non-scalar)"])
        overrides[str(key)] = value

    # K01 EFFECTIVE_CONFIG_SOURCES 六类来源都必须进入有效配置哈希。
    # 每个来源都显式落一个条目：已采集=脱敏载荷 / 缺失=__missing__ /
    # 未采集=__not_collected__。三种状态互相可区分，且都改变哈希，
    # 不存在“来源消失而身份不变”的静默路径（U15 r2 P0-2）。
    source_dumps: dict[str, Any] = {
        label: {"__not_collected__": relative}
        for label, relative in CONFIG_SOURCE_FILES if label != "settings"
    }
    source_dumps.update({
        str(label): payload for label, payload in dict(source_configs or {}).items()
    })
    # settings.yaml：以调用方实际使用的 raw_settings 为准（有效值而非磁盘副本）。
    effective_source_dropped: list[str] = list(source_dropped_payloads)
    source_dumps["settings"] = _sanitise_source_payload(
        settings, prefix="settings", dropped=dropped,
        dropped_payloads=effective_source_dropped,
    )
    effective[CONFIG_SOURCES_KEY] = source_dumps
    return EffectiveConfig(
        settings=effective,
        profile_overrides=overrides,
        environment_overrides=environment_override_values(env),
        dropped_sensitive=tuple(sorted(set(dropped) | set(source_dropped_sensitive))),
        sources={
            "settings": "config/settings.yaml (whitelisted, actually present)",
            "profile_overrides": f"config/settings.yaml profiles.{profile}" if profile else "no profile selected",
            "environment_overrides": "allowlist, set-and-non-empty only",
            "source_configs": (
                ", ".join(label for label, _ in CONFIG_SOURCE_FILES)
                if source_configs is not None else "not collected"
            ),
        },
        dropped_payloads=tuple(sorted(set(effective_source_dropped))),
        missing_sources=tuple(source_missing),
    )


# --------------------------------------------------------------------------
# 账户引用 / 运行代（与密钥轮换解耦）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountRef:
    """稳定账户引用：与 API key 材料解耦，轮换密钥不改变它。"""

    value: str
    environment: str
    stable: bool
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "environment": self.environment,
                "stable": self.stable, "source": self.source}


def derive_account_ref(
    environment: str,
    *,
    declared_account_ref: str | None = None,
    fallback_local: bool = True,
) -> AccountRef:
    """派生 ``account_ref``：**不使用密钥材料**。

    优先级：显式声明的账户引用（``MTA_ACCOUNT_REF`` 或配置值）> ``<env>:local``。
    绝不从 API key 计算，因此轮换密钥时 ``account_ref`` 保持不变（要求 4）。
    需要密钥级区分时，用 ``key_fingerprint``/``credential_epoch`` 单独表达。
    """
    env = _text(environment).lower()[:32] or "unknown"
    declared = _text(declared_account_ref)
    if declared:
        value = declared if declared.startswith(f"{env}:") else f"{env}:{declared}"
        return AccountRef(value[:160], env, True, "declared")
    if not fallback_local:
        raise MissingRuntimeInputError(["account_ref (declared)"])
    return AccountRef(f"{env}:local", env, False, "local-fallback")


def key_fingerprint(api_key: str | None, *, length: int = 16) -> str | None:
    """密钥指纹：不可逆短摘要，**仅作轮换审计**，不参与身份哈希。

    不接受空值（返回 ``None``），返回的摘要不包含、也不能还原密钥。
    """
    text = _text(api_key)
    if not text:
        return None
    size = max(8, min(int(length), 64))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:size]


@dataclass(frozen=True)
class RuntimeGeneration:
    """一次运行的“代”：账户 + 运行实例 + 凭据纪元（轮换解耦）。"""

    environment: str
    account_ref: str
    runtime_instance_id: str
    generation_id: str
    credential_epoch: int = 0
    key_fingerprint: str | None = None
    scope_hash: str | None = None
    started_at: float | None = None
    account_ref_stable: bool = False
    account_ref_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        """脱敏表示：指纹可留（不可逆），原始密钥永不出现。"""
        return {
            "environment": self.environment,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "generation_id": self.generation_id,
            "credential_epoch": self.credential_epoch,
            "key_fingerprint": self.key_fingerprint,
            "scope_hash": self.scope_hash,
            "started_at": self.started_at,
            "account_ref_stable": self.account_ref_stable,
            "account_ref_source": self.account_ref_source,
        }


def mint_runtime_generation(
    *,
    environment: str,
    account_ref: AccountRef | str,
    runtime_instance_id: str,
    credential_epoch: int = 0,
    key_fingerprint_value: str | None = None,
    scope_hash: str | None = None,
    started_at: float | None = None,
) -> RuntimeGeneration:
    """铸造运行代：``generation_id`` 只由环境/账户/实例/起始时间决定。

    凭据纪元与密钥指纹 **不参与** ``generation_id``，所以轮换密钥不会产生
    新的运行代（账本连续归集不被轮换打断）。

    U15 r2 收紧三处构造口径：

    - ``environment`` 必须非空且属于 ``MODE_ALLOWED``（与
      ``resolve_execution_scope`` 同口径，不再接受 ``prod``/空串/``unknown``）；
    - 传 ``AccountRef`` 时 ``environment`` 形参**不得被静默忽略**：与
      ``AccountRef.environment`` 不一致即结构化拒绝（否则会产出绑定到另一
      环境的运行代，K04 只比对 environment 字段，等于 live 用 demo 授权放行）；
    - 传裸字符串时一律经 :func:`derive_account_ref` 归一化（补 ``env:`` 前缀），
      同一账户不会因构造入口不同得到两个 ``account_ref``。

    U15 r3 类型健壮性（NEW-D）：``environment``/``runtime_instance_id`` 必须是
    字符串，``credential_epoch`` 必须是非负整数，``started_at`` 必须是实数或
    ``None``，``scope_hash``/``key_fingerprint_value``/``account_ref`` 必须是
    字符串或 ``None``（``account_ref`` 也接受 ``AccountRef``）。形状不对一律
    ``MalformedInputError``（结构化拒绝），**不再**泄漏
    ``TypeError``/``ValueError`` 之裸异常（此前 ``credential_epoch='3'`` 抛
    ``'<' not supported between instances of 'str' and 'int'``）。
    """
    if not isinstance(environment, str):
        raise MalformedInputError(["environment must be a string"])
    env = _text(environment).lower()
    if not env:
        raise MissingRuntimeInputError(["environment"])
    if env not in MODE_ALLOWED:
        raise ExecutionModeError(
            f"unknown environment {env!r}; allowed: {', '.join(MODE_ALLOWED)}"
        )
    if isinstance(account_ref, AccountRef):
        if _text(account_ref.environment) != env:
            raise ExecutionModeError(
                f"environment {env!r} conflicts with AccountRef.environment "
                f"{_text(account_ref.environment)!r}; refusing to silently prefer one"
            )
        ref = account_ref
    elif isinstance(account_ref, str):
        ref = derive_account_ref(
            env, declared_account_ref=_text(account_ref), fallback_local=False,
        )
    else:
        # 非 AccountRef/非字符串（dict、list、None 等）：不静默 str() 成一个
        # 看似合法的账户引用，直接结构化拒绝。
        raise MalformedInputError(
            [f"account_ref must be AccountRef or str, got {type(account_ref).__name__}"]
        )
    if not isinstance(runtime_instance_id, str):
        raise MalformedInputError(["runtime_instance_id must be a string"])
    instance = _text(runtime_instance_id)
    if not instance:
        raise MissingRuntimeInputError(["runtime_instance_id"])
    if isinstance(credential_epoch, bool) or not isinstance(credential_epoch, int):
        raise MalformedInputError(
            [f"credential_epoch must be an int, got {type(credential_epoch).__name__}"]
        )
    if credential_epoch < 0:
        raise RuntimeIdentityError("credential_epoch must be >= 0")
    if started_at is not None and (
            isinstance(started_at, bool) or not isinstance(started_at, (int, float))):
        raise MalformedInputError(
            [f"started_at must be a real number or None, got {type(started_at).__name__}"]
        )
    if scope_hash is not None and not isinstance(scope_hash, str):
        raise MalformedInputError(
            [f"scope_hash must be a string or None, got {type(scope_hash).__name__}"]
        )
    if key_fingerprint_value is not None and not isinstance(key_fingerprint_value, str):
        raise MalformedInputError(
            ["key_fingerprint_value must be a string or None, "
             f"got {type(key_fingerprint_value).__name__}"]
        )
    seed = "|".join((ref.environment, ref.value, instance, "" if started_at is None else f"{started_at:.6f}"))
    generation_id = "gen-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return RuntimeGeneration(
        environment=ref.environment,
        account_ref=ref.value,
        runtime_instance_id=instance,
        generation_id=generation_id,
        credential_epoch=int(credential_epoch),
        key_fingerprint=key_fingerprint_value,
        scope_hash=scope_hash,
        started_at=started_at,
        account_ref_stable=ref.stable,
        account_ref_source=ref.source,
    )


def rotate_credential(
    generation: RuntimeGeneration, *, new_key_fingerprint: str | None,
) -> RuntimeGeneration:
    """轮换凭据：纪元 +1、指纹更新，**账户引用与运行代保持不变**。"""
    if not isinstance(generation, RuntimeGeneration):
        raise MalformedInputError(["generation must be a RuntimeGeneration"])
    if new_key_fingerprint is not None and (
            not isinstance(new_key_fingerprint, str) or not new_key_fingerprint.strip()):
        raise RuntimeIdentityError("new key fingerprint must be a non-empty string or None")
    from dataclasses import replace

    return replace(
        generation,
        credential_epoch=generation.credential_epoch + 1,
        key_fingerprint=new_key_fingerprint,
    )


@dataclass(frozen=True)
class AccountBinding:
    """账户 / 运行代 / 权限验证结果（U02 绑定接口）。"""

    allowed: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason_codes": list(self.reason_codes)}


def verify_account_binding(
    expected: RuntimeGeneration,
    actual: RuntimeGeneration | None,
    *,
    environment: str | None = None,
    account_ref: str | None = None,
    runtime_instance_id: str | None = None,
    scope_hash: str | None = None,
) -> AccountBinding:
    """验证“同一账户同一运行代”的绑定（U02 用于决策/命令前置校验）。

    比较项：``environment`` / ``account_ref`` / ``runtime_instance_id`` /
    ``generation_id`` / ``scope_hash``（``credential_epoch`` 与密钥指纹 **只做
    审计，不参与判定**——轮换不应否决在途管理动作）。

    ``scope_hash`` 是双向补齐的 fail-closed 判定：调用方在 ``scope_hash`` 里
    提出了域绑定要求而运行代没有该字段 → ``SCOPE_HASH_UNBOUND``（不是静默跳过）；
    ``expected`` 与 ``actual`` 的 ``scope_hash`` 也必须逐字相等，否则
    ``SCOPE_HASH_MISMATCH``。只有两边都为 None（未声明域）时才不比较。
    """
    reasons: list[str] = []
    if actual is None:
        return AccountBinding(False, (RUNTIME_GENERATION_UNPROVEN,))
    if not isinstance(actual, RuntimeGeneration) or not isinstance(expected, RuntimeGeneration):
        # 入参不是本模块的运行代：结构化拒绝，不抛裸 AttributeError（500）。
        return AccountBinding(False, (RUNTIME_GENERATION_MALFORMED,))
    if _text(environment) and _text(actual.environment) != _text(environment).lower():
        reasons.append("ENVIRONMENT_MISMATCH")
    if _text(account_ref) and _text(actual.account_ref) != _text(account_ref):
        reasons.append("ACCOUNT_REF_MISMATCH")
    if _text(runtime_instance_id) and _text(actual.runtime_instance_id) != _text(runtime_instance_id):
        reasons.append("RUNTIME_INSTANCE_MISMATCH")
    if actual.generation_id != expected.generation_id:
        reasons.append("RUNTIME_GENERATION_MISMATCH")
    if actual.account_ref != expected.account_ref:
        reasons.append("ACCOUNT_REF_MISMATCH")
    if actual.environment != expected.environment:
        reasons.append("ENVIRONMENT_MISMATCH")
    demanded_scope = _text(scope_hash)
    if demanded_scope:
        if not _text(actual.scope_hash):
            reasons.append(SCOPE_HASH_UNBOUND)
        elif actual.scope_hash != demanded_scope:
            reasons.append(SCOPE_HASH_MISMATCH)
    if _text(expected.scope_hash) != _text(actual.scope_hash):
        reasons.append(SCOPE_HASH_MISMATCH)
    return AccountBinding(not reasons, tuple(dict.fromkeys(reasons)))


# --------------------------------------------------------------------------
# 身份装配（release_manifest / service 的集成入口）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityBundle:
    """一次运行的全部身份输入与判定证据。"""

    identity: RuntimeIdentity
    model_route: ResolvedModelRoute
    execution_scope: ExecutionScope
    effective_config: EffectiveConfig
    strategy_source: StrategyCardSource
    git_state: GitState
    blockers: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    #: 账户引用溯源。``identity.account_ref`` 是字符串值，这里保留
    #: ``stable``/``source``，使 U02/看板不必二次派生，也让 ``<env>:local``
    #: 回退可被检测（stable=False）。r2 起不再只存在于被丢弃的局部变量里。
    account_ref_ref: AccountRef | None = None
    #: K01 ``IDENTITY_COVERAGE["dependency"]`` 的 ``comparison_status``
    #: （NEW-E）：锁文件声明的对照结论，见 :func:`compare_dependency_lock`。
    dependency_comparison_status: str = DEPENDENCY_COMPARISON_UNAVAILABLE

    @property
    def entry_allowed(self) -> bool:
        """是否存在阻断新仓的身份类原因（空 = 无身份类阻断）。"""
        return not self.blockers

    @property
    def account_ref(self) -> AccountRef | None:
        """账户引用溯源（不是字符串；字符串见 ``identity.account_ref``）。"""
        return self.account_ref_ref

    @property
    def account_ref_stable(self) -> bool:
        """``account_ref`` 是否来自显式声明（``<env>:local`` 回退时为 False）。"""
        return bool(self.account_ref_ref and self.account_ref_ref.stable)

    @property
    def account_ref_source(self) -> str:
        return self.account_ref_ref.source if self.account_ref_ref else "unavailable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.model_dump(mode="json"),
            "identity_hash": self.identity.identity_hash(),
            "deployment_hash": self.identity.deployment_hash(),
            "git_state": self.git_state.as_dict(),
            "execution_scope": self.execution_scope.as_dict(),
            "model_route": self.model_route.as_dict(),
            "strategy": self.strategy_source.as_dict(),
            "effective_config": self.effective_config.as_dict(),
            "account_ref_provenance": (
                self.account_ref_ref.as_dict() if self.account_ref_ref else None
            ),
            "account_ref_stable": self.account_ref_stable,
            "dependency_comparison_status": self.dependency_comparison_status,
            "blockers": list(self.blockers),
            "advisories": list(self.advisories),
            "entry_allowed": self.entry_allowed,
        }


def collect_runtime_identity(
    project_root: Path | str,
    *,
    environment: str,
    account_ref: AccountRef | str | None = None,
    declared_account_ref: str | None = None,
    runtime_instance_id: str,
    profile: str | None = None,
    execution_mode: str | None = None,
    env: Mapping[str, str] | None = None,
    credentials_present: Mapping[str, bool] | None = None,
    provider_available: Mapping[str, bool] | None = None,
    raw_settings: Mapping[str, Any] | None = None,
    raw_agents: Mapping[str, Any] | None = None,
    agent_name: str = "ai_trader",
    expected_git_commit: str | None = None,
    extra_profile_overrides: Mapping[str, Any] | None = None,
    artifact_relatives: Sequence[str] | None = None,
    require_lock_file: bool = True,
    require_source_configs: bool = False,
    fallback_local: bool | None = None,
) -> IdentityBundle:
    """从仓库事实装配 K01 身份（release_manifest / service 的单一入口）。

    ``environment`` 取实际执行作用域（profile 优先），``account_ref`` 与密钥
    轮换无关。所有覆盖组齐全才返回；缺失时 ``build_identity`` 抛
    ``IdentityCoverageError``（禁止静默生成不完整身份）。

    K01 ``EFFECTIVE_CONFIG_SOURCES`` 六类来源（settings / decision_policy /
    agents / risk_learning / risk_profile.lock / windows_service）的非敏感值
    始终进入 ``effective_config_hash``；来源文件缺失会改变哈希并记入
    ``effective_config.missing_sources`` 与 advisory。生产入口应传
    ``require_source_configs=True`` 把“六类来源必须齐备”升级为结构化拒绝
    （U15 r2 P0-2）。

    ``fallback_local`` 控制 ``account_ref`` 的本地回退：

    - ``None``（默认）→ 允许回退，但 live 下回退是**阻断项**
      （``ACCOUNT_REF_UNPROVEN``）：身份工件仍可审计，`entry_allowed` 为假；
    - ``True`` → 显式允许（live 下仍记 advisory，不再阻断）；
    - ``False`` → 显式要求已声明账户引用，缺失即 ``MissingRuntimeInputError``
      （生产入口建议用这一档）。

    未声明账户时身份会把“哪个账户”退化成 ``<env>:local``：同环境所有账户共用
    同一 ``account_ref``（跨账户碰撞）。r2 起这一事实通过
    ``IdentityBundle.account_ref_stable``/``account_ref_source`` 与
    advisory/blocker 暴露，不再只存在于被丢弃的局部变量里（U15 r2 P1-2）。
    """
    root = Path(project_root)
    if not isinstance(env, Mapping) and env is not None:
        raise MalformedInputError(["env must be a mapping"])
    if credentials_present is not None and not isinstance(credentials_present, Mapping):
        raise MalformedInputError(["credentials_present must be a mapping"])
    if not isinstance(runtime_instance_id, str) or not runtime_instance_id.strip():
        # 与 mint_runtime_generation 同口径：运行代必须有值（空值会让身份退化为常量）
        raise MissingRuntimeInputError(["runtime_instance_id"])
    env_map = {str(k): str(v) for k, v in (env if env is not None else {}).items()}
    scope = resolve_execution_scope(
        profile=profile if profile is not None else env_map.get("MTA_PROFILE"),
        execution_mode=execution_mode if execution_mode is not None else env_map.get("EXECUTION_MODE"),
        default_mode=_dig(raw_settings or {}, "execution.default_mode") or "demo",
    )
    declared = declared_account_ref if declared_account_ref is not None \
        else env_map.get(ACCOUNT_REF_KEYS[0])
    if isinstance(account_ref, AccountRef):
        if _text(account_ref.environment) != _text(scope.mode).lower():
            raise ExecutionModeError(
                f"AccountRef.environment {_text(account_ref.environment)!r} does not match "
                f"resolved execution scope {scope.mode!r}"
            )
        ref = account_ref
    else:
        # 未声明账户时是否允许回退到 <env>:local：
        #   None（默认）= 允许回退，但 live 下由 ACCOUNT_REF_UNPROVEN 阻断
        #                 （保留可审计的身份工件，绝不静默放行）；
        #   False       = 直接要求已声明账户，缺失即结构化拒绝。
        fallback_ok = True if fallback_local is None \
            else bool(fallback_local)
        explicit = account_ref if account_ref else declared
        ref = derive_account_ref(
            scope.mode, declared_account_ref=str(explicit) if explicit else None,
            fallback_local=fallback_ok,
        )

    lock_files = collect_dependency_lock_files(root, require_lock=require_lock_file)
    strategy = load_strategy_cards(root)
    verify_strategy_cards_source(strategy, root)
    source_dumps, source_dropped, source_dropped_payloads, source_missing = \
        collect_source_config_files(root)
    if require_source_configs and source_missing:
        raise MissingRuntimeInputError([f"{name} (config source)" for name in source_missing])
    effective = collect_effective_config(
        raw_settings=raw_settings, profile=scope.profile, env=env_map,
        extra_profile_overrides=extra_profile_overrides,
        source_configs=source_dumps,
        source_dropped_sensitive=source_dropped,
        source_dropped_payloads=source_dropped_payloads,
        source_missing=source_missing,
    )
    guard_no_raw_env_payload(effective.as_payload(), name="effective_config")
    route = resolve_model_route(
        raw_settings=raw_settings, raw_agents=raw_agents, env=env_map,
        agent_name=agent_name, credentials_present=credentials_present,
        provider_available=provider_available,
        execution_mode=scope.mode, profile_overrides=effective.profile_overrides,
    )
    artifacts = collect_artifact_files(root, relatives=artifact_relatives)
    git_state = resolve_git_state(root)

    identity = build_identity(
        environment=scope.mode,
        account_ref=ref.value,
        git_commit=git_state.commit,
        runtime_instance_id=runtime_instance_id,
        artifact_files=artifacts,
        dependency_lock_files=lock_files,
        strategy_cards=strategy.payload,
        model_route=route.route,
        effective_config=effective.as_payload(),
    )
    scope_blockers: list[str] = []
    if not scope.locked and scope.mode == "live":
        # decl-7：未显式选择 profile 却要跑实盘 —— 状态根无法唯一确定
        # （config_loader.runtime_path 退回公共 data/），必须阻断而不是静默放行。
        scope_blockers.append(EXECUTION_SCOPE_UNLOCKED)
    account_advisories: tuple[str, ...] = ()
    if not ref.stable:
        # 未声明账户 → <env>:local：同环境所有账户共用同一 account_ref。
        # 隐式回退（fallback_local 未显式声明）在 live 下阻断；
        # 调用方显式 True 表示已知情并接受，此时只留 advisory。
        account_advisories = (ACCOUNT_REF_LOCAL_FALLBACK,)
        if scope.mode == "live" and fallback_local is not True:
            scope_blockers.append(ACCOUNT_REF_UNPROVEN)
    blockers = tuple(dict.fromkeys((
        *route.blockers, *scope_blockers, *git_state.hard_reasons(expected_git_commit),
    )))
    missing_advisories = tuple(
        f"{CONFIG_SOURCE_MISSING}:{name}" for name in source_missing
    )
    advisories = tuple(dict.fromkeys((
        *git_state.advisory_reasons(), *scope.warnings, *missing_advisories,
        *account_advisories,
    )))
    return IdentityBundle(
        identity=identity, model_route=route, execution_scope=scope,
        effective_config=effective, strategy_source=strategy,
        git_state=git_state, blockers=blockers, advisories=advisories,
        account_ref_ref=ref,
        # NEW-E：diff-2 缺口②的「比对结果另存」——结论随身份工件一起产出，
        # 调用方不必重新推导（锁文件缺失时上面已抛 MissingRuntimeInputError，
        # 这里保留 best-effort 语义：拿不到就如实报 UNAVAILABLE）。
        dependency_comparison_status=compare_dependency_lock(root),
    )


def _identity_generation_mismatches(
    identity: RuntimeIdentity, generation: RuntimeGeneration,
) -> tuple[str, ...]:
    """``IdentityBundle.identity`` ↔ ``RuntimeGeneration`` 的交叉一致性（NEW-A）。

    只比较“这个身份指向哪个环境/哪个账户”：
    ``environment`` 与 ``account_ref``。两者任一不等即
    ``IDENTITY_GENERATION_MISMATCH`` —— 因为此时“本次运行的身份”与“被授权的
    运行代”在事实层就不是同一回事，任何 reason_codes 都无法区分“真的绑定成功”
    与“身份跟运行代根本不是一回事”。

    刻意**不**比较 ``runtime_instance_id``：``IdentityBundle`` 里没有该字段
    （``RuntimeIdentity`` 的 ``runtime_instance_id`` 与运行代实例号同为一次运行
    的实例标识，但两者来源不同，逐字比较会误报）；调用方要绑定实例号请用
    ``authorize_new_entry(runtime_instance_id=...)``。
    """
    reasons: list[str] = []
    if _text(identity.environment).lower() != _text(generation.environment).lower():
        reasons.append(IDENTITY_GENERATION_MISMATCH)
    elif _text(identity.account_ref) != _text(generation.account_ref):
        reasons.append(IDENTITY_GENERATION_MISMATCH)
    return tuple(reasons)


@dataclass(frozen=True)
class PermissionDecision:
    """U02 绑定的权限判定（含建议 HTTP 状态，禁止伪造成功）。"""

    allowed: bool
    reason_codes: tuple[str, ...]
    http_status: int = 200
    advisories: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason_codes": list(self.reason_codes),
                "http_status": self.http_status, "advisories": list(self.advisories)}


def authorize_new_entry(
    bundle: IdentityBundle | None,
    generation: RuntimeGeneration | None,
    *,
    expected: RuntimeGeneration | None = None,
    environment: str | None = None,
    account_ref: str | None = None,
    runtime_instance_id: str | None = None,
    scope_hash: str | None = None,
    expected_git_commit: str | None = None,
    readiness_allowed: bool | None = None,
) -> PermissionDecision:
    """新开仓身份门禁（F13 / DC14）：身份不符或 Mock 一律拒绝，而非降级成功。

    **fail-closed 契约（U15 r2 收紧）**：返回 200 只可能来自“身份、运行代、
    readiness 三者都被真正校验过”的路径。

    - ``generation`` 缺失 → ``RUNTIME_GENERATION_UNPROVEN``；
    - ``expected`` 缺失 → ``IDENTITY_BINDING_UNPROVEN``：省略它**不再**等于
      “跳过绑定校验”，而是“无法证明本次运行与期望账户/环境/运行代绑定”。
      U02 必须显式传入它要绑定到的运行代（通常来自自己的授权记录）；
    - ``readiness_allowed`` 默认 ``None``（未声明）→ ``READINESS_UNPROVEN``；
      既有 ``entry_readiness`` 结论必须由调用方显式传入（``True``/``False``），
      本函数不重复实现那套事实判定，只叠加身份类阻断；
    - 入参形状不是本模块数据结构 → ``IDENTITY_MALFORMED`` /
      ``RUNTIME_GENERATION_MALFORMED``（结构化 409，而不是未捕获的 AttributeError）。

    ``expected`` 与 ``generation`` 分别可传 ``environment``/``account_ref``/
    ``runtime_instance_id``/``scope_hash`` 作为额外的显式绑定要求；``scope_hash``
    的缺失/不一致按 ``SCOPE_HASH_UNBOUND``/``SCOPE_HASH_MISMATCH`` 拒绝。

    **bundle ↔ generation 交叉校验（U15 r3 NEW-A）**：``bundle`` 存活时，它的
    ``identity.environment``/``identity.account_ref`` 必须与 ``generation`` 的
    同名字段一致，否则 ``IDENTITY_GENERATION_MISMATCH``（409）。缺少这层校验
    时，把 ``expected``/``environment``/``account_ref``/``runtime_instance_id``/
    ``scope_hash`` 全部显式绑定到一个**异账户 live 运行代**、却拿 demo 身份的
    bundle 来授权，同样会得到 ``allowed=True / 200 / reason_codes=()``：调用方
    能证明“我打算授权的确实是那个运行代”，但证明不了“**本次运行**就是那个
    运行代”。两个方向（demo bundle + live gen、live bundle + demo gen）都拒绝。
    """
    reasons: list[str] = []
    advisories: list[str] = []
    if bundle is None:
        reasons.append("IDENTITY_UNPROVEN")
    elif not isinstance(bundle, IdentityBundle):
        reasons.append(IDENTITY_MALFORMED)
    else:
        reasons.extend(bundle.blockers)
        reasons.extend(bundle.git_state.hard_reasons(expected_git_commit))
        advisories.extend(bundle.advisories)
        advisories.extend(bundle.execution_scope.warnings)
    if generation is None:
        reasons.append(RUNTIME_GENERATION_UNPROVEN)
    elif not isinstance(generation, RuntimeGeneration):
        reasons.append(RUNTIME_GENERATION_MALFORMED)
    elif expected is None:
        # 省略 expected 即“无法证明绑定”，不再静默跳过全部账户/环境/运行代校验。
        reasons.append(IDENTITY_BINDING_UNPROVEN)
    elif not isinstance(expected, RuntimeGeneration):
        reasons.append(RUNTIME_GENERATION_MALFORMED)
    else:
        reasons.extend(verify_account_binding(
            expected, generation, environment=environment, account_ref=account_ref,
            runtime_instance_id=runtime_instance_id, scope_hash=scope_hash,
        ).reason_codes)
    if isinstance(bundle, IdentityBundle) and isinstance(generation, RuntimeGeneration):
        # NEW-A：身份 bundle 与被授权运行代必须指向同一环境与同一账户。
        # 这一步不看调用方传了哪些绑定参数 —— 上面那组只证明“expected 与
        # generation 一致”，本组证明“本次运行的身份与 generation 一致”。
        reasons.extend(_identity_generation_mismatches(bundle.identity, generation))
    if readiness_allowed is None:
        reasons.append(READINESS_UNPROVEN)
    elif not readiness_allowed:
        reasons.append("READINESS_BLOCKED")
    unique = tuple(dict.fromkeys(reasons))
    return PermissionDecision(
        not unique, unique, 200 if not unique else 409,
        tuple(dict.fromkeys(advisories)),
    )


# --------------------------------------------------------------------------
# 控制面边界（R11 / 回环 / 验证写入 / 人工禁用优先）
# --------------------------------------------------------------------------


def is_loopback_host(host: str | None) -> bool:
    """来源是否为本机环回（控制接口只允许环回）。

    接受 ``127.0.0.1`` / ``::1`` / ``localhost`` 与**缩写形式**的 IPv4-mapped
    IPv6（``::ffff:127.0.0.1``）；``0.0.0.0``、私有网段与任意域名一律拒绝。

    已知限制（U15 r3 NEW-H，**已登记、方向 fail-closed，不修**）：完整展开形式的
    IPv4-mapped 回环（``0:0:0:0:0:ffff:127.0.0.1``）不被识别 → 返回 ``False``。
    本函数的唯一消费者是 :func:`authorize_control_request`，其用法为
    ``if not is_loopback_host(host): return 403``，即 ``False`` = **拒绝**；
    因此该形态只会让合法的本机管理请求被 403（可用性问题），**不会**放行任何
    非回环来源 —— 是保守方向的误拒，不是安全缺陷。放宽它需要引入 IP 解析并
    重新界定白名单（例如 ``127.0.0.2`` 属 ``127/8`` 环回但当前被刻意排除，
    见 ``tests/acceptance/test_control_boundary.py``），收益不等于风险，故按
    「已如实登记」处理。
    """
    text = _text(host).strip().lower()
    if not text:
        return False
    text = text.strip("[]")
    if text.startswith("::ffff:") and text.count(":") == 3:
        text = text[len("::ffff:"):]
    return text in LOOPBACK_HOSTS


def _role_allows(role: str, action: str, *, lowering: bool = False) -> bool:
    """角色能否执行该动作。

    ``auto`` 不是 ``CONTROL_ROLES`` 成员（无人工权限），但它是自动恢复流程的
    执行者：文档语义是“只能不变或放宽到更严的反面”。r3 NEW-G P3-4 之前它
    **什么都做不了**（连 ``read_status`` 都 403），与自身文档矛盾，也让自动
    恢复在需要收紧时无路可走。现在：只读类动作放行；状态类动作放行**更严**
    方向（``lowering=False``），放宽方向在上游已按
    ``AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE`` 409 拦下。
    """
    if role == AUTO_ACTOR:
        return action in AUTO_PERMITTED_ACTIONS and not lowering
    return role in CONTROL_ACTION_ROLES.get(action, ())


@dataclass(frozen=True)
class ControlWriteVerdict:
    """控制写入验证结果：写后必须回读一致，否则视为未生效。"""

    ok: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason_codes": list(self.reason_codes)}


def verify_control_write(
    *, requested_state: str, read_back_state: str | None, read_back_ok: bool = True,
) -> ControlWriteVerdict:
    """写后回读校验；“写入成功”必须被回读证据证明。

    r3 NEW-G P3-3：回读一致性**不足以**证明写入有意义 —— ``('bogus','bogus')``
    文本相等但两者都不是本模块已知的控制状态，此前会得到 ``ok=True``。现在
    先做状态合法性判定：任一非空状态不在 ``CONTROL_STATE_SEVERITY`` 内即
    ``CONTROL_STATE_ILLEGAL``（fail-closed，ok=False），再比较一致性。
    """
    reasons: list[str] = []
    if not read_back_ok:
        reasons.append("CONTROL_READBACK_FAILED")
    requested_text = _text(requested_state)
    read_back_text = _text(read_back_state)
    if requested_text and requested_text not in CONTROL_STATE_SEVERITY:
        reasons.append(CONTROL_STATE_ILLEGAL)
    if read_back_text and read_back_text not in CONTROL_STATE_SEVERITY:
        reasons.append(CONTROL_STATE_ILLEGAL)
    if read_back_state is None or read_back_text == "":
        reasons.append("CONTROL_READBACK_MISSING")
    elif read_back_text != requested_text:
        reasons.append("CONTROL_WRITE_UNVERIFIED")
    unique = tuple(dict.fromkeys(reasons))
    return ControlWriteVerdict(not unique, unique)


def authorize_control_request(
    *,
    role: str,
    action: str,
    client_host: str | None,
    requested_state: str | None = None,
    current_state: str | None = None,
    actor: str = "operator",
    confirm_manual_reenable: bool = False,
    confirm_secret: str | None = None,
) -> PermissionDecision:
    """控制请求授权（R11 权限分离 + 环回 + 人工禁用优先）。

    规则：

    - 非环回来源一律 403（``NON_LOOPBACK_REJECTED``）；
    - 角色/动作不匹配 → 403（``ROLE_NOT_PERMITTED``）；
    - 未知动作/状态 → 400（fail-closed，不做默认放行）；``unspecified`` 这类
      “注册表里没有角色”的伪动作也按未知动作 400（r3 NEW-G P3-2）；
    - ``emergency_stop`` 只能进 ``emergency``：显式给出别的目标态 →
      400 ``EMERGENCY_STOP_TARGET_MISMATCH``（r3 NEW-G P3-1）；
    - ``auto`` 流程不得把人工禁用的状态放宽 → 409
      （``AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE``）；只读与**更严**方向
      放行（r3 NEW-G P3-4，此前 auto 连 ``read_status`` 都 403）；
    - 人工从更严状态恢复需要显式 ``confirm_manual_reenable``，``emergency``
      还需要 ``confirm_secret == "emergency"``（沿用既有二次确认语义）。
    """
    role_text = _text(role).lower()
    action_text = _text(action)
    if not is_loopback_host(client_host):
        return PermissionDecision(False, ("NON_LOOPBACK_REJECTED",), 403)
    if role_text not in CONTROL_ROLES and role_text != AUTO_ACTOR:
        return PermissionDecision(False, ("UNKNOWN_ROLE",), 403)
    if action_text not in CONTROL_ACTION_ROLES or not CONTROL_ACTION_ROLES[action_text]:
        # 注册表里存在但没有任何角色 = 不是一个可执行动作（``unspecified`` 即此形态）：
        # 按**未知动作** 400 拒绝。r3 NEW-G P3-2 之前它会落到 ROLE_NOT_PERMITTED/403，
        # 把“动作不存在”误报成“权限不足”（口径不一致，且 403 更适合已认证越权）。
        return PermissionDecision(False, ("UNKNOWN_CONTROL_ACTION",), 400)

    state_actions = ("set_control", "emergency_stop", "set_control_severity_up",
                     "set_control_severity_down")
    target = ""
    current = ""
    lowering = False
    if action_text in state_actions:
        target = _text(requested_state) or ("emergency" if action_text == "emergency_stop" else "")
        if not target:
            return PermissionDecision(False, ("CONTROL_STATE_REQUIRED",), 400)
        if target not in CONTROL_STATE_SEVERITY:
            return PermissionDecision(False, ("UNKNOWN_CONTROL_STATE",), 400)
        if action_text == "emergency_stop" and target != "emergency":
            # 紧急停机只能进 emergency：显式给别的目标态会被审计记成“紧急停机”
            # 却只进 close_only（r3 NEW-G P3-1）。
            return PermissionDecision(False, (EMERGENCY_STOP_TARGET_MISMATCH,), 400)
        current = _text(current_state) or "normal"
        if current not in CONTROL_STATE_SEVERITY:
            return PermissionDecision(False, ("UNKNOWN_CONTROL_STATE",), 400)
        lowering = CONTROL_STATE_SEVERITY[target] < CONTROL_STATE_SEVERITY[current]
        if lowering:
            # 放宽方向：自动流程永远不得自动恢复（人工禁用优先）；
            # 只有 operator 能经显式二次确认放宽。
            if role_text == AUTO_ACTOR:
                return PermissionDecision(
                    False, ("AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE",), 409)
            if role_text != "operator":
                return PermissionDecision(False, ("ROLE_NOT_PERMITTED",), 403)

    if not _role_allows(role_text, action_text, lowering=lowering):
        return PermissionDecision(False, ("ROLE_NOT_PERMITTED",), 403)

    reasons: list[str] = []
    status = 403
    if target == "emergency" and _text(confirm_secret) != "emergency":
        reasons.append("EMERGENCY_CONFIRM_REQUIRED")
        status = 400
    if lowering:
        if not confirm_manual_reenable:
            reasons.append("MANUAL_REENABLE_NOT_CONFIRMED")
            status = 409
        if CONTROL_STATE_SEVERITY[current] == CONTROL_STATE_SEVERITY["emergency"] \
                and _text(confirm_secret) != "emergency":
            reasons.append("EMERGENCY_EXIT_NOT_CONFIRMED")
            status = 409
    if reasons:
        return PermissionDecision(False, tuple(dict.fromkeys(reasons)), status)
    return PermissionDecision(True, ())


# --------------------------------------------------------------------------
# 指纹报告（W0 F_fingerprint_r1）声明/实现不一致处置登记
# --------------------------------------------------------------------------
#: 逐项处置：``fixed``=本任务已用代码/策略修复；``registered``=显式登记为
#: 未修复残余（附归属）；``aligned``=核对后确认实现与声明一致（无动作）。
#: 禁止静默忽略：每一项都必须出现在这里。
#: 键 = ``docs/dispatch/reviews/U00/w0/F_fingerprint_r1.md`` 的 ``diff-N``（§1
#: 身份输入清单）/``decl-N``（§3 声明与实现不一致清单）；``fu-N`` 为 §4
#: 「需 U15/E 后续验证」第 N 项，``fu-paths`` 为 §2 RuntimePaths 漂移清单整体。
#: **status 只描述本模块（及本目录补丁）里真实存在的东西**，生产接线是否完成
#: 一律写进 note，不得用 fixed 掩盖未接线的部分。
FINGERPRINT_R1_DISPOSITION: dict[str, dict[str, str]] = {
    "diff-1": {"status": "fixed", "note": "git 状态含 dirty/缺失原因码（resolve_git_state + mismatch_reasons）；factory 侧 WorkBuddy 实盘专属由 model_route_blockers 表达；启动门禁接线属 U13（模块内零消费者）", "owner": "U15/U13"},
    "diff-2": {"status": "fixed", "note": "缺口①依赖锁纳入 requirements-lock.txt 且缺失即拒（collect_dependency_lock_files）；缺口②（比对结果另存）r3 起由 compare_dependency_lock(root, declared) 真正产出 MATCH/MISMATCH/UNAVAILABLE（K01 IDENTITY_COVERAGE['dependency'].comparison_status），并随 IdentityBundle.dependency_comparison_status 一起交付；发布清单字段 dependency_comparison_status 的接线在补丁 01，合入前清单侧仍为空值", "owner": "U15/I"},
    "diff-3": {"status": "fixed", "note": "r2：effective_config_hash 覆盖 K01 EFFECTIVE_CONFIG_SOURCES 六类来源（settings 全量非敏感值 + decision_policy/agents/risk_learning/risk_profile.lock/windows_service），缺失/未采集三态可区分", "owner": "U15"},
    "diff-4": {"status": "fixed", "note": "strategy_hash 覆盖 knowledge/strategy_cards.json 并做文件级来源复核；r2 起零卡/截断文件拒绝（不得成为可发布身份）", "owner": "U15"},
    "diff-5": {"status": "fixed", "note": "model_route_hash 取解析后有效路由（含环境/备用/profile 覆盖）；r2 起非 ai_trader 不构造备用、不识别的备用 provider 给 FALLBACK_PROVIDER_UNRECOGNISED", "owner": "U15"},
    "diff-6": {"status": "registered", "note": "runtime_instance_id 持久化与实例锁绑定属 U13（RuntimePaths/instance_lock 集成）；本模块只提供 RuntimeGeneration 数据结构与 generate 口径", "owner": "U13"},
    "diff-7": {"status": "registered", "note": "account_ref 由声明值派生、与密钥解耦，且 live 隐式回退被 ACCOUNT_REF_UNPROVEN 阻断（r2 已修）。状态根一致性只到**纯函数判定** verify_environment_state_root(environment, state_root, profile)（ENVIRONMENT_UNBOUND/STATE_ROOT_UNBOUND/STATE_ROOT_PROFILE_UNLOCKED/ENVIRONMENT_STATE_ROOT_MISMATCH/LIVE_SHARED_STATE_ROOT）+ config_loader 补丁的 assert_environment_matches_state_root；RuntimePaths 生成、启动期拦截与双控制台串账本的端到端验证**未做**，归 U13", "owner": "U13"},
    "decl-1": {"status": "fixed", "note": "WorkBuddy 非 live 使用→WORKBUDDY_OUTSIDE_LIVE 阻断（默认不允许 demo）；r2 起模式未知（None/空/空白）也按非 live 判定，并新增 provider 能力证据要求", "owner": "U15"},
    "decl-2": {"status": "aligned", "note": "WorkBuddy 只作用于 ai_trader：与 factory.py:30 一致，非 ai_trader 也不生成备用路由（r2 对齐）", "owner": "-"},
    "decl-3": {"status": "fixed", "note": "settings.llm.mock=true 现在真正生效（模型路由强制 Mock 并阻断）；字符串 'true' 仍按未设置处理（易误读，属已知限制）", "owner": "U15"},
    "decl-4": {"status": "registered", "note": "agents.yaml strategy_trader 声明 model 但主图用确定性执行：属图侧语义，U15 不改变执行路径", "owner": "U03/U15"},
    "decl-5": {"status": "fixed", "note": "同 diff-2", "owner": "U15"},
    "decl-6": {"status": "registered", "note": "Windows SCM 双 profile 托管（port/profile 参数化）属 U13；本模块只把 windows_service.json 纳入有效配置哈希", "owner": "U13"},
    "decl-7": {"status": "fixed", "note": "r2 如实修复：未知模式/profile/空串拒绝；未选 profile 记 EXECUTION_PROFILE_NOT_DECLARED；EXECUTION_MODE=live 且 locked=False 由 EXECUTION_SCOPE_UNLOCKED 阻断（此前该组合仍 locked=False 放行且无告警）", "owner": "U15"},
    "decl-8": {"status": "aligned", "note": "不读 .env 内容：与实现一致，且 r2 加固（工件路径 root 边界、来源文件敏感键递归丢弃、凭据只记键路径）", "owner": "-"},
    "decl-9": {"status": "aligned", "note": "WorkBuddy 不使用 SenseNova 共享请求协调器：与 factory.py:30-37 一致，属 LLM 客户端选择，U15 不改", "owner": "-"},
    "decl-10": {"status": "aligned", "note": "WorkBuddy 子进程遵守主决策 deadline：与 workbuddy_client.py:199-203 一致，不属 U15 路径", "owner": "-"},
    "decl-11": {"status": "registered", "note": "发布包 SAFE_DOCS 白名单缺最新规格、tracked 诊断脚本入包：文件在 scripts/create_release_package.py（非 U15 独占路径），归属待 I 指派", "owner": "-"},
    "decl-12": {"status": "fixed", "note": "K01 字段映射：本模块直接从事实装配 RuntimeIdentity，不再复用 ReleaseManifest 命名", "owner": "U15"},
    "decl-13": {"status": "registered", "note": "观测表 config_hash 字段名沿用现状（写库 schema 变更归 U09/U12）", "owner": "U09"},
    "decl-14": {"status": "fixed", "note": "静默 Mock 改为显式判定 + 阻断原因码（策略层）；执行层接线见本目录补丁 01/04，合入前运行系统仍无强制力", "owner": "U15/I"},
    "decl-15": {"status": "registered", "note": "供应商模型/effort 枚举无法离线核实（UNKNOWN），不做断言", "owner": "-"},
    "fu-1": {"status": "registered", "note": "更换 worktree/代码目录后实例锁与账户作用域是否互斥（K01:72、F18）：本模块无状态根/实例锁语义，归 U13", "owner": "U13"},
    "fu-2": {"status": "registered", "note": "MTA_RELEASE_ID 可整体覆盖 release_id 且与哈希无绑定（service.py:135）：该变量在共享文件读取，U15 独占路径不可改；本模块不读它，也无法为它提供绑定接口", "owner": "U13"},
    "fu-3": {"status": "registered", "note": "release_observations 的 release_id 绑定是否足以在身份不符时拒绝开仓（release_evidence.py:328-391）：属报告期一致性，非启动门禁；归 U09/U13", "owner": "U09"},
    "fu-4": {"status": "registered", "note": "Windows SCM 双 profile 托管（同 decl-6）", "owner": "U13"},
    "fu-5": {"status": "fixed", "note": "DC14/F13 与「指纹必变」的可执行负例：本任务交付 tests/acceptance/test_runtime_identity.py 与 test_control_boundary.py；r3 实测 233 PASS / exit 0 与 63 PASS / exit 0（含 NEW-A/D/E/F/G/H 的新增用例）；执行层是否真的拒绝仍待 I 合入补丁后验证", "owner": "U15/I"},
    "fu-paths": {"status": "registered", "note": "F 报告 §2 的 RuntimePaths 漂移清单（knowledge/review_lessons.json 随代码根漂移、binance_futures.py 兜底 data/ 路径、dashboard 读 data/ 而 service 写 data/live）整体归 U13；RuntimePaths 类尚不存在，U15 不提供也不改动这些共享文件", "owner": "U13"},
}


def fingerprint_disposition() -> dict[str, dict[str, str]]:
    """返回指纹清单处置表的副本（供报告与回归脚本引用）。"""
    return {key: dict(value) for key, value in FINGERPRINT_R1_DISPOSITION.items()}


def identity_mismatch_reasons(
    expected: RuntimeIdentity, actual: RuntimeIdentity,
    *, ignore: tuple[str, ...] = ("runtime_instance_id",),
) -> tuple[str, ...]:
    """K01 身份逐字段比较的薄包装（供服务侧门禁复用）。"""
    return tuple(identity_mismatches(expected, actual, ignore=ignore))


def verify_environment_state_root(
    environment: str | None,
    state_root: str | Path | None,
    *,
    profile: str | None = None,
    project_root: str | Path | None = None,
) -> tuple[str, ...]:
    """``environment`` ↔ 状态根一致性判定（纯函数；空元组 = 一致）。

    落地指纹清单 ``diff-7`` 缺口②：``profile=None`` 且 ``EXECUTION_MODE=live``
    时 ``Settings.runtime_path`` 退回公共 ``data/`` 根，实盘账本可能与 demo 或
    另一台控制台共用，此前无任何校验。判定规则：

    - ``environment`` 未声明或不在 ``MODE_ALLOWED`` → ``ENVIRONMENT_UNBOUND``；
    - ``state_root`` 未声明/空白 → ``STATE_ROOT_UNBOUND``；
    - ``environment == "live"`` 但未显式选择 profile →
      ``STATE_ROOT_PROFILE_UNLOCKED``（``Settings.runtime_path`` 只有 live 会
      分区到 ``data/live``，无 profile 时实盘退回公共根）；
    - profile 与 environment 不一致 → ``ENVIRONMENT_STATE_ROOT_MISMATCH``；
    - ``environment == "live"`` 且状态根解析到公共 ``<project_root>/data`` 或
      ``project_root`` 本身 → ``LIVE_SHARED_STATE_ROOT``。

    demo/paper/testnet 不产生 live 专属原因码：它们沿用既有共享根布局，不是
    不一致（``STATE_ROOT_PROFILE_UNLOCKED`` 只对 live 报告）。

    只判定路径语义：不改路径、不读文件、不创建目录。``RuntimePaths`` 的生成与
    启动期接线（终止进程）归 U13；``docs/dispatch/patches/U15/a136cb5/
    02_config_loader.diff`` 提供 ``Settings.assert_environment_matches_state_root``
    作为现成接线点（该补丁只在 environment=live 时收紧，其余组合行为不变）。
    """
    reasons: list[str] = []
    env = _text(environment).lower()
    if not env or env not in MODE_ALLOWED:
        reasons.append(ENVIRONMENT_UNBOUND)
    if state_root is None or not _text(state_root):
        reasons.append(STATE_ROOT_UNBOUND)
    declared_profile = _text(profile).lower()
    if declared_profile and env and declared_profile != env:
        reasons.append(ENVIRONMENT_STATE_ROOT_MISMATCH)
    if env == "live":
        if not declared_profile:
            reasons.append(STATE_ROOT_PROFILE_UNLOCKED)
        if _text(state_root):
            raw = _text(state_root).replace("\\", "/").rstrip("/").lower()
            try:
                resolved = Path(state_root) if project_root is None \
                    else Path(project_root) / Path(state_root)
                resolved = resolved.resolve()
            except OSError:  # pragma: no cover - 取决于文件系统
                resolved = None
            shared = {"data", ".", "data/", "./data"}
            if raw in shared or (
                resolved is not None and project_root is not None
                and resolved in {(Path(project_root) / "data").resolve(),
                                 Path(project_root).resolve()}
            ):
                reasons.append(LIVE_SHARED_STATE_ROOT)
    return tuple(dict.fromkeys(reasons))


def available_environment_snapshot() -> dict[str, str]:
    """只读快照：仅白名单内 **已设置** 的环境变量（不含任何凭据）。"""
    return environment_override_values(os.environ)


__all__.extend([
    "IdentityCoverageError",
    "SensitiveInputError",
    # 门禁原因码（U02 / 看板按这些常量比较，不要硬编码字面量）。
    "RUNTIME_GENERATION_UNPROVEN",
    "IDENTITY_BINDING_UNPROVEN",
    "READINESS_UNPROVEN",
    "IDENTITY_MALFORMED",
    "RUNTIME_GENERATION_MALFORMED",
    "IDENTITY_GENERATION_MISMATCH",
    "SCOPE_HASH_UNBOUND",
    "SCOPE_HASH_MISMATCH",
    "EXECUTION_PROFILE_NOT_DECLARED",
    "EXECUTION_SCOPE_UNLOCKED",
    "CONFIG_SOURCE_FILES",
    "CONFIG_SOURCES_KEY",
    "CONFIG_SOURCE_MISSING",
    # 控制面原因码（r3 NEW-G）。
    "EMERGENCY_STOP_TARGET_MISMATCH",
    "CONTROL_STATE_ILLEGAL",
    "AUTO_PERMITTED_ACTIONS",
    # 依赖锁对照结论（r3 NEW-E，K01 IDENTITY_COVERAGE['dependency']）。
    "DEPENDENCY_COMPARISON_MATCH",
    "DEPENDENCY_COMPARISON_MISMATCH",
    "DEPENDENCY_COMPARISON_UNAVAILABLE",
    "MalformedInputError",
    "collect_source_config_files",
    "compare_dependency_lock",
    "fingerprint_disposition",
    "identity_mismatch_reasons",
    "available_environment_snapshot",
    "spec_env",
])
