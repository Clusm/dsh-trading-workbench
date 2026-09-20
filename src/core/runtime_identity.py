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
（依赖锁、策略卡、git 元数据）与环境变量字典快照。
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
    IdentityCoverageError,
    ModelRoute,
    RuntimeIdentity,
    SensitiveInputError,
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
    """键名是否命中 K01 敏感标记（大小写不敏感的子串匹配）。"""
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


def collect_artifact_files(root: Path | str, *, relatives: Sequence[str] | None = None) -> dict[str, str]:
    """收集不可变代码工件内容（``相对路径 -> 内容``）。

    ``relatives`` 缺省为 ``src/**/*.py``；脚本等入口文件由调用方显式传入，
    避免把临时诊断文件卷进身份。内容为 UTF-8 文本；读取失败按缺失处理。
    """
    base = Path(root)
    if relatives is None:
        files = sorted(
            path for path in (base / "src").rglob("*.py")
            if not any(part in IDENTITY_ARTIFACT_IGNORED_PARTS for part in path.parts)
        )
        names = [path.relative_to(base).as_posix() for path in files]
    else:
        names = [Path(name).as_posix() for name in relatives]
    payload: dict[str, str] = {}
    for name in names:
        text = _read_text(base, name)
        if text is not None:
            payload[name] = text
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
    return StrategyCardSource(source_file, payload, text, _count_cards(payload))


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route.as_payload(),
            "provider": self.provider,
            "mock": self.mock,
            "execution_mode": self.execution_mode,
            "sources": dict(self.sources),
            "blockers": list(self.blockers),
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


def _fallback_route(
    fallback_cfg: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    provider: str,
) -> tuple[Mapping[str, Any], ...]:
    """按环境声明的 provider 构造备用路由；非法参数抛 ``FallbackRouteError``。"""
    if provider != "opencode":
        return ()
    primary = {
        "provider": "opencode",
        "model": _text(spec_env(env, fallback_cfg, "model_env")) or _text(fallback_cfg.get("default_model")),
        "base_url": _text(spec_env(env, fallback_cfg, "base_url_env")) or _text(fallback_cfg.get("default_base_url")),
        "credential_present": bool(_text(spec_env(env, fallback_cfg, "api_key_env"))),
        "min_remaining_seconds": fallback_cfg.get("min_remaining_seconds"),
        "max_retries": fallback_cfg.get("max_retries"),
    }
    normalised = validate_fallback_route(primary)
    return ({k: v for k, v in normalised.items() if k != "scheme"},)


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
) -> ResolvedModelRoute:
    """解析有效模型路由并给出 Mock 判定与阻断原因。

    覆盖优先级（与现行实现一致，但**显式记录来源**）：

    1. provider：``MTA_AI_DECISION_PROVIDER=workbuddy`` 只作用于 ``ai_trader``；
    2. model：``agents.yaml[agent].model`` > 环境变量 ``settings.llm.model_env``
       > ``settings.llm.default_model``（**只在真实设置时才算覆盖**）；
    3. effort：``agents.yaml[agent].reasoning_effort`` > ``settings.llm.reasoning_effort``
       > ``"default"``；
    4. fallback_routes：仅 ``MTA_AI_DECISION_FALLBACK_PROVIDER=opencode`` 且
       agent 为 ai_trader 时构造；否则显式空元组（声明“无备用”）。
    5. profile_overrides：由调用方传入的 profile 生效值 + provider/mock 判定证据。

    ``credentials_present`` 只接受 **布尔**（由调用方用 ``bool(os.getenv(name))``
    拍平），本函数不接受、也不会记录任何凭据值或凭据变量名。
    """
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
    if provider == "workbuddy":
        wb = llm.get("workbuddy") or {}
        wb = wb if isinstance(wb, Mapping) else {}
        model = _text(wb.get("model")) or "deepseek-v4.1-flash"
        effort = _text(wb.get("effort")) or "max"
        sources.update(model="settings.llm.workbuddy.model", effort="settings.llm.workbuddy.effort")
        mock = False
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

    fallback_provider = env_map.get("MTA_AI_DECISION_FALLBACK_PROVIDER", "").strip().lower()
    fallback_routes = _fallback_route(llm.get("decision_fallback") or {}, env_map, provider=fallback_provider)

    overrides: dict[str, Any] = {
        "provider": provider,
        "mock": mock,
        "model_source": sources.get("model", ""),
        "effort_source": sources.get("effort", ""),
        "mock_source": sources.get("mock", "credential_present" if not mock else "unknown"),
    }
    if _text(execution_mode):
        overrides["execution_mode"] = _text(execution_mode).lower()
    profile_effective = {
        str(key): value for key, value in dict(profile_overrides or {}).items()
        if not is_sensitive_name(key)
    }
    if profile_effective:
        overrides["profile"] = profile_effective

    route = ModelRoute(
        model=model or "",
        effort=effort or "",
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
    return ResolvedModelRoute(
        route=route, provider=provider, mock=mock,
        execution_mode=execution_mode, sources=sources, blockers=blockers,
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
) -> EffectiveConfig:
    """采集 settings + profile 覆盖 + 环境覆盖的有效非敏感值。

    - ``settings`` 只取白名单点分路径 **且实际存在** 的值；
    - 命中敏感标记的键（如 ``llm.api_key_env``）被丢弃并计入 ``dropped_sensitive``；
    - ``profile_overrides`` 只有显式选择了 demo/live profile 时才收录该段的实际覆盖值；
    - ``environment_overrides`` 只收录白名单内确实设置的非空变量。
    """
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

    return EffectiveConfig(
        settings=effective,
        profile_overrides=overrides,
        environment_overrides=environment_override_values(env),
        dropped_sensitive=tuple(sorted(set(dropped))),
        sources={
            "settings": "config/settings.yaml (whitelisted, actually present)",
            "profile_overrides": f"config/settings.yaml profiles.{profile}" if profile else "no profile selected",
            "environment_overrides": "allowlist, set-and-non-empty only",
        },
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
    """
    ref = account_ref if isinstance(account_ref, AccountRef) else AccountRef(
        _text(account_ref), _text(environment).lower()[:32] or "unknown", False, "explicit",
    )
    instance = _text(runtime_instance_id)
    if not instance:
        raise MissingRuntimeInputError(["runtime_instance_id"])
    if not ref.value:
        raise MissingRuntimeInputError(["account_ref"])
    if credential_epoch < 0:
        raise RuntimeIdentityError("credential_epoch must be >= 0")
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

    @property
    def entry_allowed(self) -> bool:
        """是否存在阻断新仓的身份类原因（空 = 无身份类阻断）。"""
        return not self.blockers

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
    raw_settings: Mapping[str, Any] | None = None,
    raw_agents: Mapping[str, Any] | None = None,
    agent_name: str = "ai_trader",
    expected_git_commit: str | None = None,
    extra_profile_overrides: Mapping[str, Any] | None = None,
    artifact_relatives: Sequence[str] | None = None,
    require_lock_file: bool = True,
) -> IdentityBundle:
    """从仓库事实装配 K01 身份（release_manifest / service 的单一入口）。

    ``environment`` 取实际执行作用域（profile 优先），``account_ref`` 与密钥
    轮换无关。所有覆盖组齐全才返回；缺失时 ``build_identity`` 抛
    ``IdentityCoverageError``（禁止静默生成不完整身份）。
    """
    root = Path(project_root)
    env_map = {str(k): str(v) for k, v in (env if env is not None else {}).items()}
    scope = resolve_execution_scope(
        profile=profile if profile is not None else env_map.get("MTA_PROFILE"),
        execution_mode=execution_mode if execution_mode is not None else env_map.get("EXECUTION_MODE"),
        default_mode=_dig(raw_settings or {}, "execution.default_mode") or "demo",
    )
    declared = declared_account_ref if declared_account_ref is not None else env_map.get("MTA_ACCOUNT_REF")
    if isinstance(account_ref, AccountRef):
        ref = account_ref
    elif account_ref:
        ref = derive_account_ref(scope.mode, declared_account_ref=str(account_ref))
    else:
        ref = derive_account_ref(scope.mode, declared_account_ref=declared)

    lock_files = collect_dependency_lock_files(root, require_lock=require_lock_file)
    strategy = load_strategy_cards(root)
    verify_strategy_cards_source(strategy, root)
    effective = collect_effective_config(
        raw_settings=raw_settings, profile=scope.profile, env=env_map,
        extra_profile_overrides=extra_profile_overrides,
    )
    guard_no_raw_env_payload(effective.as_payload(), name="effective_config")
    route = resolve_model_route(
        raw_settings=raw_settings, raw_agents=raw_agents, env=env_map,
        agent_name=agent_name, credentials_present=credentials_present,
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
    scope_blockers: tuple[str, ...] = ()
    if not scope.locked and scope.mode == "live":
        # decl-7：未显式选择 profile 却要跑实盘 —— 状态根无法唯一确定
        # （config_loader.runtime_path 退回公共 data/），必须阻断而不是静默放行。
        scope_blockers = (EXECUTION_SCOPE_UNLOCKED,)
    blockers = tuple(dict.fromkeys((
        *route.blockers, *scope_blockers, *git_state.hard_reasons(expected_git_commit),
    )))
    advisories = tuple(dict.fromkeys((*git_state.advisory_reasons(), *scope.warnings)))
    return IdentityBundle(
        identity=identity, model_route=route, execution_scope=scope,
        effective_config=effective, strategy_source=strategy,
        git_state=git_state, blockers=blockers, advisories=advisories,
    )


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

    接受 ``127.0.0.1`` / ``::1`` / ``localhost`` 与 IPv4-mapped IPv6
    （``::ffff:127.0.0.1``）；``0.0.0.0``、私有网段与任意域名一律拒绝。
    """
    text = _text(host).strip().lower()
    if not text:
        return False
    text = text.strip("[]")
    if text.startswith("::ffff:") and text.count(":") == 3:
        text = text[len("::ffff:"):]
    return text in LOOPBACK_HOSTS


def _role_allows(role: str, action: str) -> bool:
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
    """写后回读校验；“写入成功”必须被回读证据证明。"""
    reasons: list[str] = []
    if not read_back_ok:
        reasons.append("CONTROL_READBACK_FAILED")
    if read_back_state is None or _text(read_back_state) == "":
        reasons.append("CONTROL_READBACK_MISSING")
    elif _text(read_back_state) != _text(requested_state):
        reasons.append("CONTROL_WRITE_UNVERIFIED")
    return ControlWriteVerdict(not reasons, tuple(reasons))


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
    - 未知动作/状态 → 400（fail-closed，不做默认放行）；
    - ``auto`` 流程不得把人工禁用的状态放宽 → 409
      （``AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE``）；
    - 人工从更严状态恢复需要显式 ``confirm_manual_reenable``，``emergency``
      还需要 ``confirm_secret == "emergency"``（沿用既有二次确认语义）。
    """
    role_text = _text(role).lower()
    action_text = _text(action)
    if not is_loopback_host(client_host):
        return PermissionDecision(False, ("NON_LOOPBACK_REJECTED",), 403)
    if role_text not in CONTROL_ROLES and role_text != AUTO_ACTOR:
        return PermissionDecision(False, ("UNKNOWN_ROLE",), 403)
    if action_text not in CONTROL_ACTION_ROLES:
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

    if not _role_allows(role_text, action_text):
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
FINGERPRINT_R1_DISPOSITION: dict[str, dict[str, str]] = {
    "diff-1": {"status": "fixed", "note": "git 状态含 dirty/缺失原因码（resolve_git_state + mismatch_reasons）；factory 侧 WorkBuddy 实盘专属由 model_route_blockers 表达", "owner": "U15"},
    "diff-2": {"status": "fixed", "note": "依赖锁纳入 requirements-lock.txt 且缺失即拒（collect_dependency_lock_files）", "owner": "U15"},
    "diff-3": {"status": "fixed", "note": "effective_config_hash 三字段采集（白名单+仅实际设置值）", "owner": "U15"},
    "diff-4": {"status": "fixed", "note": "strategy_hash 覆盖 knowledge/strategy_cards.json 并做文件级来源复核", "owner": "U15"},
    "diff-5": {"status": "fixed", "note": "model_route_hash 取解析后有效路由（含环境/备用/profile 覆盖）", "owner": "U15"},
    "diff-6": {"status": "registered", "note": "runtime_instance_id 持久化与实例锁绑定属 U13（RuntimePaths/instance_lock 集成）", "owner": "U13"},
    "diff-7": {"status": "fixed", "note": "account_ref 由声明值派生、与密钥解耦；environment/状态根一致性校验接口已提供，接线归 U13", "owner": "U15/U13"},
    "decl-1": {"status": "fixed", "note": "WorkBuddy 非 live 使用→WORKBUDDY_OUTSIDE_LIVE 阻断（默认不允许 demo）", "owner": "U15"},
    "decl-3": {"status": "fixed", "note": "settings.llm.mock=true 现在真正生效（模型路由强制 Mock 并阻断）", "owner": "U15"},
    "decl-4": {"status": "registered", "note": "agents.yaml strategy_trader 声明 model 但主图用确定性执行：属图侧语义，U15 不改变执行路径", "owner": "U03/U15"},
    "decl-5": {"status": "fixed", "note": "同 diff-2", "owner": "U15"},
    "decl-6": {"status": "registered", "note": "Windows SCM 双 profile 托管（port/profile 参数化）属 U13", "owner": "U13"},
    "decl-7": {"status": "fixed", "note": "resolve_execution_scope fail-closed：未知模式/profile 拒绝，profile/EXECUTION_MODE 冲突记入 ignored", "owner": "U15"},
    "decl-12": {"status": "fixed", "note": "K01 字段映射：本模块直接从事实装配 RuntimeIdentity，不再复用 ReleaseManifest 命名", "owner": "U15"},
    "decl-13": {"status": "registered", "note": "观测表 config_hash 字段名沿用现状（写库 schema 变更归 U09/U12）", "owner": "U09"},
    "decl-14": {"status": "fixed", "note": "静默 Mock 改为显式判定 + 阻断原因码（策略层）；执行层接线见共享 diff", "owner": "U15"},
    "decl-15": {"status": "registered", "note": "供应商模型/effort 枚举无法离线核实（UNKNOWN），不做断言", "owner": "-"},
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
    "SCOPE_HASH_UNBOUND",
    "SCOPE_HASH_MISMATCH",
    "EXECUTION_PROFILE_NOT_DECLARED",
    "EXECUTION_SCOPE_UNLOCKED",
    "fingerprint_disposition",
    "identity_mismatch_reasons",
    "available_environment_snapshot",
    "spec_env",
])
