"""U13 运行路径单一事实源（RuntimePaths）：代码根 / 状态根 / 共享出口根。

本模块解决的核心问题是 **换代码根不得换状态根**（规格 R12、F18）：

- 现状里几乎所有运行状态都由 ``Settings.runtime_path`` 从 ``project_root``
  派生（``config_loader.py``），因此把同一账户的不可变代码发布到 **另一个
  目录**（worktree / 新版本目录）会得到 **另一套状态根**——包括实例锁本身。
  后果是两台控制台可以同时管理同一账户（F06/F18），限流冷却与 LLM 跨进程
  准入锁被绕过（F08），复盘经验库被读成空库。
- 本模块把三类根显式区分出来，并给出一个 **与代码根解耦** 的状态基座：

  ==================  =========================================  ==============
  根                  语义                                        随代码根漂移
  ==================  =========================================  ==============
  ``code_root``       不可变代码发布目录（只读资产、模板、卡）    是（预期）
  ``state_base``      账户状态基座（锁、账本、备份、恢复包）      **否**（可注入）
  ``shared_egress_root`` 跨 profile 共享的出口状态（限流冷却、    **否**（唯一）
                      LLM 跨进程准入）
  ==================  =========================================  ==============

``state_base`` 由 ``MTA_STATE_ROOT``（优先级最高）或 ``paths.state_root``
配置声明；**未声明时退回 ``<code_root>/data``**，与升级前的行为逐字节一致
（兼容迁移的零漂移基线）。因此：

- 未设置 ``MTA_STATE_ROOT``：所有路径与今天完全相同，无行为变化。
- 设置了 ``MTA_STATE_ROOT``：换个代码目录（或新版本目录）启动，实例锁、
  账本、备份、恢复包、限流冷却都仍然指向同一份状态——单账户互斥与退避
  不再被"换目录"绕过。

``shared_egress_root`` 只由 ``state_base`` 决定，**不按 profile 分区**：
demo 与 live 共享同一个限流冷却与 LLM 准入目录（这是有意为之：它们共用
出口 IP 与供应商额度）。``shared_egress_root_is_unique()`` 是它的可执行
证明——两个 profile 必须解析到同一个目录。

**唯一判定源**：``environment`` ↔ 状态根的一致性判定不在本模块重复实现，
一律委托 :func:`core.runtime_identity.verify_environment_state_root`
（U15 已冻结），本模块只负责 **生成路径** 与 **启动期拦截**。

本模块是纯函数库 + 只读盘点：不读 ``.env``/密钥/账本内容，不联网，不启动
服务；``apply_compatibility_migration`` 只在显式调用时做 **复制**（绝不
移动/删除/覆盖），并默认 ``dry_run=True``。
"""
from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 判定唯一源（U15 已冻结）：不在这里重复实现 environment↔状态根规则。
from core.runtime_identity import verify_environment_state_root

__all__ = [
    "CODE_ROOT_ASSETS",
    "DEFAULT_STATE_ROOT_ENV",
    "LEGACY_STATE_BASE_RELATIVE",
    "PROFILE_ALLOWED",
    "PROFILE_NOT_DECLARED",
    "REQUIRED_STATE_FILES",
    "SHARED_EGRESS_CONSUMERS",
    "SHARED_EGRESS_ROOT_RELPATH",
    "STATE_ROOT_ESCAPE",
    "STATE_ROOT_ENV_VAR",
    "STATE_ROOT_MISSING",
    "U13_FINGERPRINT_DISPOSITION",
    "RuntimePaths",
    "RuntimePathsError",
    "StateRootUnresolvedError",
]

#: profile 只允许这两个值（与 ``config_loader.Settings`` 同口径）。
PROFILE_ALLOWED = ("demo", "live")

#: 状态基座的显式注入点：**换代码根不改状态根**（R12）的唯一开关。
STATE_ROOT_ENV_VAR = "MTA_STATE_ROOT"
#: 兼容别名（历史脚本可能用 MTA_DATA_ROOT 指同一件事）。
DEFAULT_STATE_ROOT_ENV = STATE_ROOT_ENV_VAR
_LEGACY_ALIAS_ENV_VARS = ("MTA_STATE_ROOT", "MTA_DATA_ROOT")

#: 未声明状态根时的旧布局：``<code_root>/data``（逐字节兼容）。
LEGACY_STATE_BASE_RELATIVE = "data"

#: 共享出口根相对 ``state_base`` 的固定位置（跨 profile 唯一）。
SHARED_EGRESS_ROOT_RELPATH = "shared"

#: 恢复包必须包含的关键状态（与 ``create_recovery_bundle.REQUIRED_STATE_FILES``
#: 同源；本模块把它升级为"同 epoch 恢复包"的必需成员清单）。
REQUIRED_STATE_FILES = (
    "capital_state.json",
    "channel_state.json",
    "control_state.json",
    "account_risk_state.json",
)

PROFILE_NOT_DECLARED = "PROFILE_NOT_DECLARED"
STATE_ROOT_MISSING = "STATE_ROOT_MISSING"
STATE_ROOT_ESCAPE = "STATE_ROOT_ESCAPE"


class RuntimePathsError(RuntimeError):
    """运行路径不可安全解析（fail-closed）。"""


class StateRootUnresolvedError(RuntimePathsError):
    """状态根无法唯一确定。"""

    code = STATE_ROOT_MISSING


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_profile(profile: str | None) -> str | None:
    raw = _text(profile).lower()
    if not raw:
        return None
    if raw not in PROFILE_ALLOWED:
        raise RuntimePathsError(
            f"profile 只允许 {PROFILE_ALLOWED}，收到 {profile!r}"
        )
    return raw


def resolve_state_base(
    code_root: Path | str,
    *,
    declared: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str, str]:
    """解析状态基座，返回 ``(绝对路径, 声明的原始值, 来源)``。

    优先级：显式 ``declared`` 形参 > 环境变量（``MTA_STATE_ROOT``/兼容别名）
    > 旧布局 ``<code_root>/data``。只做路径规范化，不创建目录、不读文件内容。

    第三个元素 ``source`` 取值 ``declared``（显式声明，锁与状态不随代码根漂移）
    或 ``fallback``（未声明，退回旧布局，锁随代码根——升级前的行为）。

    **P1-4**：``fallback`` 分支返回的第二个元素是**旧布局的相对路径** ``"data"``
    （不是"调用方声明的值"）。它对既有消费者必须逐字节保持不变（例如
    ``RuntimePaths.state_base_declared`` 在旧布局下就是 ``"data"``）。因此
    "是否已声明状态根"这一判断**只能**用 ``source``（:attr:`RuntimePaths.
    state_base_explicit` / :meth:`RuntimePaths.state_root_drift_reasons`），
    **不得**用"声明值是否为非空字符串"——后者在旧布局下恒为真，
    ``STATE_ROOT_MISSING`` 会永不可达。
    """
    root = Path(code_root).resolve()
    # 显式形参接受 str 或 Path（``_text`` 只处理 str，Path 必须先归一化）。
    source = _text(str(declared)) if declared is not None else ""
    origin = "declared" if source else "fallback"
    if not source:
        environ = os.environ if env is None else env
        for name in _LEGACY_ALIAS_ENV_VARS:
            candidate = _text(environ.get(name))
            if candidate:
                source = candidate
                origin = "declared"
                break
    if not source:
        return ((root / LEGACY_STATE_BASE_RELATIVE).resolve(),
                LEGACY_STATE_BASE_RELATIVE, "fallback")
    declared_path = Path(source)
    resolved = declared_path if declared_path.is_absolute() else (root / declared_path)
    return resolved.resolve(), source, origin


@dataclass(frozen=True)
class ConsumerEntry:
    """RuntimePaths 的一个消费者：给恢复/迁移用的可审计条目。

    ``status`` 取值（禁止静默忽略任何一项）：

    - ``migrated``：已由 :class:`RuntimePaths` 提供规范路径，消费者可切过来；
    - ``aligned``：本来就与规范布局一致（无动作）；
    - ``compat``：既有落盘位置被保留，同时提供规范路径与兼容回退（读写都可）；
    - ``planned``：**尚未**迁移（消费者不在本任务可改文件内），显式登记。
    """

    key: str
    owner: str
    canonical: str
    status: str
    note: str


@dataclass(frozen=True, eq=False)
class RuntimePaths:
    """运行路径的单一事实源。

    ``profile`` **必须显式**才能得到"已锁定"的路径：``profile=None`` 保留
    旧调用方的语义（状态落在公共根、不分区），但会通过
    :attr:`profile_declared` / :meth:`validate` 明确报告 ``PROFILE_NOT_DECLARED``，
    供启动期与看板 fail-closed 使用。
    """

    code_root: Path
    state_base: Path
    state_base_declared: str
    profile: str | None = None
    #: ``declared`` = 显式声明状态根（锁不随代码根漂移）；``fallback`` = 旧布局。
    state_base_source: str = "fallback"
    #: 旧布局的公共 data 目录（兼容迁移的只读来源）。
    _env: Mapping[str, str] = field(default_factory=dict, repr=False)

    # ---------------- 构造 ----------------

    @classmethod
    def resolve(
        cls,
        code_root: Path | str,
        *,
        profile: str | None = None,
        state_root: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "RuntimePaths":
        """按显式输入解析路径；不做任何文件系统写操作。"""
        root = Path(code_root).resolve()
        base, declared, origin = resolve_state_base(root, declared=state_root, env=env)
        return cls(
            code_root=root,
            state_base=base,
            state_base_declared=declared,
            profile=_normalise_profile(profile),
            state_base_source=origin,
            _env=dict(env or {}),
        )

    @classmethod
    def from_settings(cls, settings: Any) -> "RuntimePaths":
        """从 ``core.config_loader.Settings`` 适配（不反向依赖它，避免循环导入）。

        只读 ``project_root`` / ``profile_name`` 与 ``paths.state_root`` 三个
        属性；缺省即旧行为。
        """
        raw = getattr(settings, "raw_settings", None) or {}
        declared = None
        if isinstance(raw, Mapping):
            declared = (raw.get("paths") or {}).get("state_root")
        return cls.resolve(
            getattr(settings, "project_root"),
            profile=getattr(settings, "profile_name", None),
            state_root=declared,
            env=os.environ,
        )

    def with_profile(self, profile: str | None) -> "RuntimePaths":
        return RuntimePaths(
            code_root=self.code_root, state_base=self.state_base,
            state_base_declared=self.state_base_declared,
            profile=_normalise_profile(profile),
            state_base_source=self.state_base_source, _env=dict(self._env),
        )

    # ---------------- 根 ----------------

    @property
    def profile_declared(self) -> bool:
        return self.profile is not None

    @property
    def state_base_explicit(self) -> bool:
        """状态根是否被 **显式声明**（决定"换代码根是否换锁"）。

        ``False`` = 旧布局（``<code_root>/data``）：同一账户换一个代码目录启动会得到
        **另一把锁与另一套运行代**，这正是 F18 的漂移面。该性质必须**可被检出**
        （见 :meth:`state_root_drift` / :meth:`validate`），不能只在脚本里可读。
        """
        return self.state_base_source == "declared"

    def state_root_drift(self) -> dict[str, Any]:
        """未声明状态根时的漂移报告（**只读、不落盘**）。

        默认部署（未设置 ``MTA_STATE_ROOT`` / ``paths.state_root``）是真实存在的，
        本方法不把它变成不可用，而是把"锁与运行代随代码根漂移"这一事实**检出并
        报告**出来，供启动期检查与看板消费（fail-closed 的**可观测**）：

        - ``declared``：是否显式声明了状态根；
        - ``code``：告警码（``STATE_ROOT_MISSING``）或空串；
        - ``lock_follows_code_root``：旧布局下恒为 ``True``（锁随代码根）；
        - ``remediation``：可选修复动作（声明 ``MTA_STATE_ROOT``）。
        """
        explicit = self.state_base_explicit
        return {
            "declared": explicit,
            "state_base": str(self.state_base),
            "state_base_declared": self.state_base_declared,
            "state_base_source": self.state_base_source,
            "profile": self.profile,
            "lock_file": str(self.lock_file),
            "run_registry_file": str(self.run_registry_file),
            "lock_follows_code_root": not explicit,
            "code": "" if explicit else STATE_ROOT_MISSING,
            "remediation": (
                "" if explicit else
                f"声明 {STATE_ROOT_ENV_VAR}=<账户状态基座>（或 paths.state_root），"
                "使锁/账本/运行代不随代码根漂移（R12/F18）"),
        }

    def state_root_drift_reasons(self) -> tuple[str, ...]:
        """漂移告警码元组（未声明 → ``(STATE_ROOT_MISSING,)``，否则空）。"""
        return () if self.state_base_explicit else (STATE_ROOT_MISSING,)

    @property
    def state_root(self) -> Path:
        """当前 profile 的状态根（live 分区到 ``state_base/live``，与旧语义一致）。"""
        if self.profile == "live":
            return self.state_base / "live"
        return self.state_base

    @property
    def shared_egress_root(self) -> Path:
        """跨 profile 唯一的共享出口根（限流冷却 + LLM 跨进程准入）。

        **不由 profile 决定**：demo 与 live 必须解析到同一个目录，否则 418/429
        冷却与供应商额度会在两个进程间各算一套（F08）。
        """
        return self.state_base / SHARED_EGRESS_ROOT_RELPATH

    def shared_egress_root_is_unique(self) -> bool:
        """两个 profile 的共享出口根必须完全相同（可执行证明）。"""
        return (self.with_profile("demo").shared_egress_root
                == self.with_profile("live").shared_egress_root)

    @property
    def rate_limit_state(self) -> Path:
        return self.shared_egress_root / "public_rate_limit_state.json"

    @property
    def legacy_rate_limit_state(self) -> Path:
        """升级前位置：``<state_base>/public_rate_limit_state.json``。"""
        return self.state_base / "public_rate_limit_state.json"

    @property
    def coordination_dir(self) -> Path:
        """LLM 跨进程准入目录（U05 的 ``MTA_LLM_COORDINATION_DIR`` 目标位置）。"""
        return self.shared_egress_root / "llm_coordination"

    def shared_egress_state_file(self) -> Path:
        """限流冷却的**实际**读写路径：优先既有文件，避免升级即丢冷却（F08）。"""
        canonical = self.rate_limit_state
        if canonical.exists():
            return canonical
        legacy = self.legacy_rate_limit_state
        if legacy.exists():
            return legacy
        return canonical

    # ---------------- 状态路径 ----------------

    def path(self, relative: str | Path) -> Path:
        """profile 感知的状态路径；语义与 ``Settings.runtime_path`` 一致。"""
        raw = str(relative).replace("\\", "/").lstrip("/")
        target = (self.state_root / raw).resolve()
        base = self.state_root.resolve()
        if target != base and base not in target.parents:
            raise RuntimePathsError(f"{STATE_ROOT_ESCAPE}: {relative} 逃出状态根 {base}")
        return target

    @property
    def data_dir(self) -> Path:
        return self.state_root

    @property
    def db_file(self) -> Path:
        return self.path("agent_trader.sqlite")

    @property
    def artifacts_dir(self) -> Path:
        return self.path("artifacts")

    @property
    def logs_dir(self) -> Path:
        return self.path("logs")

    @property
    def reviews_dir(self) -> Path:
        return self.path("reviews")

    @property
    def cache_dir(self) -> Path:
        return self.path("cache")

    @property
    def backups_dir(self) -> Path:
        return self.path("backups")

    @property
    def recovery_bundles_dir(self) -> Path:
        return self.path("recovery_bundles")

    @property
    def release_manifest_file(self) -> Path:
        return self.path("release_manifest.json")

    # ---------------- 单实例 / 运行代 ----------------

    @property
    def lock_file(self) -> Path:
        """单实例锁：只由 **状态根 + profile** 决定，与代码根无关（R12、F06/F18）。

        旧布局下等于 ``<code_root>/data[/live]/trading_service.lock``——与
        ``Settings.runtime_path("data/trading_service.lock")`` 逐字节一致，因此
        在未声明 ``MTA_STATE_ROOT`` 时无行为变化。
        """
        return self.path("trading_service.lock")

    @property
    def run_registry_file(self) -> Path:
        """运行代持久化（diff-6）：``runtime_instance_id``/运行代跨重启稳定。"""
        return self.path("runtime_generation.json")

    @property
    def supervision_ledger_file(self) -> Path:
        return self.path("ops/supervision_ledger.json")

    @property
    def recovery_gate_file(self) -> Path:
        return self.path("ops/recovery_gate.json")

    @property
    def recovery_state_dir(self) -> Path:
        return self.path("ops/recovery_state")

    def lock_scope(self) -> str:
        """锁作用域摘要（写进锁文件，便于证明"同一账户同一执行者"）。"""
        material = "|".join([
            str(self.state_base).replace("\\", "/").casefold(),
            self.profile or "legacy",
        ])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    # ---------------- 只读资产（代码根） ----------------

    @property
    def strategy_cards_file(self) -> Path:
        """策略卡：属**代码发布物**（只读），随代码根迁移属预期。"""
        return self.code_root / "knowledge" / "strategy_cards.json"

    @property
    def legacy_review_lessons_file(self) -> Path:
        """旧位置：``<code_root>/knowledge/review_lessons.json``（运行期写入）。

        随代码根漂移（F 报告 §2）——换目录后读到空经验库且跨 profile 共用。
        """
        return self.code_root / "knowledge" / "review_lessons.json"

    @property
    def review_lessons_file(self) -> Path:
        """规范位置：``<state_root>/review_lessons.json``（写入侧迁移见 planned 条目）。

        读取侧提供兼容回退：规范位置不存在但旧位置存在时仍可读到内容，
        使迁移 **可回滚**（F18）。
        """
        canonical = self.state_root / "review_lessons.json"
        if canonical.exists():
            return canonical
        legacy = self.legacy_review_lessons_file
        if legacy.exists():
            return legacy
        return canonical

    # ---------------- 启动期拦截 ----------------

    def validate(self, environment: str | None = None) -> tuple[str, ...]:
        """环境 ↔ 状态根一致性；空元组 = 通过。

        判定 **全部委托** :func:`verify_environment_state_root`（唯一判定源），
        本方法只做本地输入归一化 + 补一条"状态基座必须可解析"的前置检查。
        """
        reasons: list[str] = []
        # P1-4：这里必须判"是否**显式声明**"，而不是"声明的字面量是否非空"。
        # ``resolve_state_base`` 的 fallback 分支会把旧布局的相对路径 ``"data"``
        # 当作声明值返回（对既有消费者保持逐字节兼容），于是按"非空即已声明"
        # 判定会让 ``STATE_ROOT_MISSING`` **永不可达**——未声明状态根时换代码根
        # 得到不同锁与不同运行代（静默新建错误基线）却没有任何原因码。
        reasons.extend(self.state_root_drift_reasons())
        declared_value: str | Path
        if self.state_base_declared:
            raw = _text(self.state_base_declared)
            # 交给唯一判定源的必须是"相对项目根"或绝对路径，二者都不能是空串。
            declared_value = raw
        else:  # pragma: no cover - resolve_state_base 已保证非空
            declared_value = str(self.state_base)
        reasons.extend(verify_environment_state_root(
            environment,
            declared_value,
            profile=self.profile,
            project_root=self.code_root,
        ))
        return tuple(dict.fromkeys(reasons))

    def assert_environment_bound(self, environment: str | None) -> None:
        """fail-closed 版：不一致直接抛错（启动期使用）。"""
        reasons = self.validate(environment)
        if reasons:
            raise RuntimePathsError(
                f"状态根与环境不一致: environment={environment!r} "
                f"profile={self.profile!r} reasons={','.join(reasons)}"
            )

    # ---------------- 消费者清单 ----------------

    def consumer_report(self) -> list[dict[str, str]]:
        """全消费者清单（路径 + 归属 + 迁移状态）。"""
        rows: list[dict[str, str]] = []
        for entry in CONSUMER_INVENTORY:
            resolved = getattr(self, entry.canonical, None)
            if callable(resolved):
                resolved = resolved()
            rows.append({
                **entry.__dict__,
                "resolved": str(resolved) if resolved is not None else "",
            })
        return rows

    def unresolved_consumers(self) -> list[str]:
        """仍未迁移（``planned``）的消费者 key——显式登记，不得静默忽略。"""
        return [row["key"] for row in self.consumer_report()
                if row["status"] == "planned"]

    def escape_check(self) -> list[str]:
        """所有规范路径必须落在 ``code_root`` 或 ``state_base`` 内。"""
        problems: list[str] = []
        roots = (self.code_root.resolve(), self.state_base.resolve())
        for row in self.consumer_report():
            value = row.get("resolved") or ""
            if not value:
                continue
            path = Path(value).resolve()
            if not any(path == root or root in path.parents for root in roots):
                problems.append(f"{row['key']} -> {path}")
        return problems

    # ---------------- 兼容迁移 ----------------

    def compatibility_migration_plan(self) -> list[dict[str, str]]:
        """只读盘点：旧位置 → 规范位置的复制计划（不执行任何写操作）。

        ``action`` 取值：``copy``（旧有新无，建议复制）、``keep``（已在规范
        位置）、``skip-missing``（旧位置也不存在）、``manual``（需人工决定，
        例如运行期写入点迁移）。
        """
        plan: list[dict[str, str]] = []
        candidates = (
            ("rate_limit_state", self.legacy_rate_limit_state, self.rate_limit_state),
            ("review_lessons", self.legacy_review_lessons_file, self.state_root / "review_lessons.json"),
        )
        for key, legacy, canonical in candidates:
            if canonical.exists():
                action = "keep"
            elif legacy.exists():
                action = "copy"
            else:
                action = "skip-missing"
            plan.append({
                "key": key, "legacy": str(legacy), "canonical": str(canonical),
                "action": action,
            })
        for key in self.unresolved_consumers():
            plan.append({"key": key, "legacy": "(当前代码根派生)",
                         "canonical": "(见 consumer inventory)", "action": "manual"})
        return plan

    def apply_compatibility_migration(self, *, dry_run: bool = True) -> list[dict[str, str]]:
        """执行兼容迁移：**只复制，不移动、不删除、不覆盖**。

        可回滚性由"旧文件原地保留"保证（F18：迁移可回滚）。``dry_run=False``
        时才真正复制，且目标已存在时一律不覆盖（返回 ``keep``）。
        """
        plan = self.compatibility_migration_plan()
        applied: list[dict[str, str]] = []
        for row in plan:
            if row["action"] != "copy":
                applied.append({**row, "applied": "no"})
                continue
            if dry_run:
                applied.append({**row, "applied": "dry-run"})
                continue
            target = Path(row["canonical"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                applied.append({**row, "applied": "no"})
                continue
            shutil.copy2(row["legacy"], target)
            applied.append({**row, "applied": "yes"})
        return applied


#: 全消费者清单：每一个"谁把状态写到哪"都必须出现在这里。
#: ``canonical`` 是 :class:`RuntimePaths` 的属性名（可调用）。
CONSUMER_INVENTORY: tuple[ConsumerEntry, ...] = (
    ConsumerEntry("trading_service_lock", "src/core/service.py:85", "lock_file",
                  "migrated", "锁只由状态根+profile 决定；换代码根不再换锁（R12/F06/F18）"),
    ConsumerEntry("run_registry", "src/core/service.py（新）", "run_registry_file",
                  "migrated", "runtime_instance_id/运行代持久化（diff-6），重启沿用同一运行代"),
    ConsumerEntry("supervision_ledger", "src/core/ops/supervision.py", "supervision_ledger_file",
                  "migrated", "重启退避账本（30/60/120 秒、10 分钟 3 次）落状态根"),
    ConsumerEntry("recovery_gate", "src/core/ops/recovery.py", "recovery_gate_file",
                  "migrated", "恢复后「先对账再开仓」的门禁状态落状态根（F14）"),
    ConsumerEntry("database", "src/core/config_loader.py:db_file", "db_file",
                  "aligned", "SQLite 账本；旧布局下与 Settings.runtime_path 一致"),
    ConsumerEntry("artifacts", "src/core/config_loader.py:artifacts_dir", "artifacts_dir",
                  "aligned", "智能体产物"),
    ConsumerEntry("logs", "src/core/config_loader.py:logs_dir", "logs_dir", "aligned", "JSONL 日志"),
    ConsumerEntry("reviews", "src/core/config_loader.py:reviews_dir", "reviews_dir", "aligned", "复盘目录"),
    ConsumerEntry("kline_cache", "src/core/service.py:98", "cache_dir", "aligned", "K 线缓存"),
    ConsumerEntry("backups", "src/core/db_backup.py", "backups_dir", "aligned",
                  "SQLite 在线备份；同盘备份不等于灾难恢复（R21），演练见 ops/recovery"),
    ConsumerEntry("recovery_bundles", "scripts/create_recovery_bundle.py", "recovery_bundles_dir",
                  "aligned", "DB+JSON 同 epoch 恢复包输出目录"),
    ConsumerEntry("release_manifest", "src/core/service.py:148", "release_manifest_file",
                  "aligned", "脱敏发布清单"),
    ConsumerEntry("rate_limit_cooldown", "src/core/config_loader.py:rate_limit_state",
                  "shared_egress_state_file", "compat",
                  "418/429 冷却；规范位置在 shared_egress_root，旧位置保留回退（F08）"),
    ConsumerEntry("llm_coordination", "src/llm/request_coordinator.py", "coordination_dir",
                  "planned", "跨进程 LLM 准入目录仍按代码根 data/shared 派生 → 归 U05 适配"),
    ConsumerEntry("review_lessons_write", "src/agents/lessons.py:12", "review_lessons_file",
                  "planned", "写入侧仍落代码根 knowledge/；读取侧已提供规范位置+回退"),
    ConsumerEntry("futures_source_ledger", "src/exchange/binance_futures.py:201", "data_dir",
                  "planned", "直接构造 Broker 的脚本仍走代码根 data/ 兜底 → 归 U04/U05"),
    ConsumerEntry("dashboard_signal_state", "src/dashboard/app.py:1020", "data_dir",
                  "planned", "看板读 settings.project_root 而 service 写 runtime_path（F 报告 §2）"),
    ConsumerEntry("health_watch_state", "src/core/health_watch.py:82", "data_dir",
                  "planned", "health_watch 自实现 profile 分支，第二套解析逻辑 → 归 U14"),
    ConsumerEntry("strategy_cards", "src/signal_engine/scanner.py", "strategy_cards_file",
                  "aligned", "策略卡是代码发布物，随代码根迁移属预期（只读）"),
    # r2（P2-9）：F 报告指出的三项遗漏消费者，按"显式登记、不静默忽略"补入。
    ConsumerEntry("binance_public_cache", "src/data/binance_public.py:20", "cache_dir",
                  "planned", "公共行情快照缓存 CACHE_DIR = parents[2]/data/cache，"
                             "按代码根派生 → 跨 profile/跨代码根共用同一份缓存，"
                             "与 kline_cache 不是同一消费者；归 U05/U04 适配"),
    ConsumerEntry("env_file_read", "src/core/net_env.py:65", "state_base",
                  "planned", "代理/直连配置从 <code_root>/.env 读取（代码根只读输入，"
                             "不是状态），U13 不改其语义；仅登记以免被当成"
                             "「已由 RuntimePaths 收口」"),
    ConsumerEntry("legacy_phase1_entry", "scripts/run_phase1.py:41", "data_dir",
                  "planned", "旧入口直接用 settings.project_root 拼 "
                             "data/signal_scanner_state.json；`scripts/run_web.py` "
                             "才是 U13 对齐的入口，旧脚本归 U05/I 决定下线或适配"),
)

#: 共享出口根的消费者：必须都落在 ``shared_egress_root`` 之下（唯一性证明）。
SHARED_EGRESS_CONSUMERS: tuple[str, ...] = (
    "rate_limit_cooldown",
    "llm_coordination",
)

#: 代码根下的只读资产（迁移恢复包**不应**包含它们：它们随代码发布，不属于状态）。
CODE_ROOT_ASSETS: tuple[str, ...] = ("strategy_cards",)


# --------------------------------------------------------------------------
# 指纹报告（W0 F_fingerprint_r1）里**归 U13** 的差异处置登记
# --------------------------------------------------------------------------
#: 与 ``core.runtime_identity.FINGERPRINT_R1_DISPOSITION`` 同构。U15 的处置表
#: 只写"本模块"范围内的结论，归 U13 的条目在那里是 ``registered``；本表给出
#: U13 侧的真实落地情况。**禁止静默忽略**：每一项都要出现，且 ``status`` 只
#: 描述本任务真实存在的东西，未接线部分必须写进 note。
U13_FINGERPRINT_DISPOSITION: dict[str, dict[str, str]] = {
    "diff-6": {
        "status": "fixed",
        "note": "runtime_instance_id 持久化（RuntimePaths.run_registry_file + "
                "ops.supervision.RunRegistry，沿用 U15 mint_runtime_generation 口径）"
                "与实例锁绑定（锁路径=状态根+profile，锁文件记录 runtime_instance_id/"
                "generation_id/scope_hash，见 instance_lock 补丁）；换代码根不改锁（R12）。"
                "生产接线在 service.py 补丁，合入前运行系统仍用旧锁路径。",
        "owner": "U13",
    },
    "decl-6": {
        "status": "fixed",
        "note": "WindowsServiceConfig 增加显式 profile（demo/live）并校验控制台端口"
                "（demo=8000 / live=8001，与 config_loader.console_port 对齐）、"
                "限定 live 日志落在 data/live/、服务名按 profile 限定"
                "（<base>-Live）；run_arguments() 追加 --profile，run_web.py 原样支持。"
                "profile=None 时行为与升级前逐字节一致。**残余**：真正注册两个 SCM "
                "实例需要两份配置与管理员操作，属 U12 OPS/D01，不在离线范围内。",
        "owner": "U13/U12",
    },
    "fu-1": {
        "status": "fixed",
        "note": "换 worktree/代码目录后实例锁与账户作用域互斥：lock_file 只由 "
                "state_base+profile 决定，ops.supervision.lock_scope_report() 与 "
                "tests/acceptance/test_supervision.py 用两个不同 code_root 指向同一 "
                "state_base 验证得到同一锁路径与同一 scope_hash。",
        "owner": "U13",
    },
    "fu-2": {
        "status": "registered",
        "note": "MTA_RELEASE_ID 可整体覆盖 release_id 且与哈希无绑定。U13 不改 "
                "release_manifest 语义（不属本任务集成目标）；本模块只保证清单 **落盘"
                "位置** 由 RuntimePaths.release_manifest_file 决定。残余归属 I/U09。",
        "owner": "U09/I",
    },
    "fu-4": {
        "status": "fixed",
        "note": "同 decl-6：SCM 双 profile 参数化（port/profile/service_name/log 路径），"
                "真实双实例注册与宿主验证归 U12 OPS。",
        "owner": "U13/U12",
    },
    "fu-paths": {
        "status": "fixed",
        "note": "RuntimePaths 落地 + 全消费者清单（CONSUMER_INVENTORY **22 项**，含 "
                "review_lessons 漂移、binance_futures 兜底 data/、dashboard 读 "
                "project_root、health_watch 自实现分支，r2 补入 "
                "binance_public.CACHE_DIR / net_env 的 .env 读取 / 旧入口 "
                "scripts/run_phase1.py）；迁移为只复制不删除（可回滚），"
                "状态根可由 MTA_STATE_ROOT 注入（换代码根不改状态根）。**残余**："
                "review_lessons 写入侧、request_coordinator、binance_futures 兜底、"
                "dashboard、health_watch、binance_public 缓存、net_env、"
                "run_phase1 共 8 项标 planned（文件不在 U13 可改范围），"
                "已在清单中逐条登记，未静默忽略。",
        "owner": "U13/U05/U14",
    },
}


def fingerprint_disposition() -> dict[str, dict[str, str]]:
    """U13 侧指纹处置表副本（供报告与回归脚本引用）。"""
    return {key: dict(value) for key, value in U13_FINGERPRINT_DISPOSITION.items()}


def consumer_inventory_keys() -> tuple[str, ...]:
    return tuple(entry.key for entry in CONSUMER_INVENTORY)


def code_root_asset_keys() -> tuple[str, ...]:
    return CODE_ROOT_ASSETS


__all__.extend([
    "CONSUMER_INVENTORY",
    "ConsumerEntry",
    "consumer_inventory_keys",
    "code_root_asset_keys",
    "fingerprint_disposition",
    "resolve_state_base",
    "state_base_explicit",
])
