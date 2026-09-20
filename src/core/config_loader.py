"""极简配置加载：YAML + dotenv，供阶段 0 使用。"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from core.runtime_identity import (
    collect_effective_config,
    collect_source_config_files,
    verify_environment_state_root,
)


#: 原因码 / 异常标识：未显式选择 profile 却要跑实盘。
LIVE_WITHOUT_PROFILE_STATE_ROOT = "LIVE_WITHOUT_PROFILE_STATE_ROOT"


class LiveWithoutProfileStateRootError(ValueError):
    """``profile=None`` 且 ``environment == "live"``：状态根无法唯一确定。

    无 profile 时 ``runtime_path`` 会退回公共 ``data/`` 目录，两台控制台
    （或换代码根不换状态根）会共用同一实盘账本。这里 fail-closed 拒绝启动，
    而不是静默按公共路径运行。
    """

    code = LIVE_WITHOUT_PROFILE_STATE_ROOT

    def __init__(self, message: str = LIVE_WITHOUT_PROFILE_STATE_ROOT) -> None:
        super().__init__(message)


@dataclass
class Settings:
    """配置文件的轻量只读视图，并负责解析项目内各运行目录。"""
    project_root: Path
    raw_settings: dict[str, Any]
    raw_agents: dict[str, Any]
    raw_risk: dict[str, Any]
    profile_name: str | None = None

    def __post_init__(self) -> None:
        if self.profile_name not in (None, "demo", "live"):
            raise ValueError("MTA_PROFILE 只允许 demo 或 live")
        if self.profile_name == "live":
            # 只允许覆盖实盘通道分配；风险档和杠杆仍来自共同的风险配置。
            override = (self.raw_settings.get("profiles", {}).get("live", {})
                        .get("channels", {}))
            if override:
                self.raw_settings = dict(self.raw_settings)
                self.raw_settings["channels"] = {
                    **self.raw_settings.get("channels", {}), **override,
                }

    def runtime_path(self, relative: str) -> Path:
        """实盘状态固定写入 data/live；Demo 保留原账本，禁止路径逃逸。

        未指定 profile 的旧调用方保持原语义。显式 profile 在启动时冻结，
        不随后续环境变量或控制台修改而切换，避免同一账本绑定另一账户。
        """
        path = self.project_root / relative
        if self.profile_name is None:
            return path
        base = (self.project_root / "data").resolve()
        resolved = path.resolve()
        if not resolved.is_relative_to(base):
            raise ValueError(f"运行状态路径必须位于 data 内: {relative}")
        suffix = resolved.relative_to(base)
        if suffix.parts and suffix.parts[0] == "live":
            raise ValueError("配置使用公共相对路径，由 profile 自动分配 live 子目录")
        target = base / "live" / suffix if self.profile_name == "live" else resolved
        if self.profile_name == "live" and not target.resolve().is_relative_to(base / "live"):
            raise ValueError("实盘目录不能通过链接指向其他账本")
        return target

    def assert_environment_matches_state_root(self, environment: str | None) -> None:
        """校验 ``environment`` 与状态根是否一致（fail-closed）。

        判定委托给 ``core.runtime_identity.verify_environment_state_root``
        （唯一判定源，避免两套口径）。被拒绝的组合（都只在 environment 为
        live 时成立）：

        - 未显式选择 profile（``STATE_ROOT_PROFILE_UNLOCKED``）：``runtime_path``
          只有 live 会分区到 ``data/live``，无 profile 时实盘退回公共根；
        - 状态根就是公共根 / 代码根（``LIVE_SHARED_STATE_ROOT``）；
        - 除此之外的 live 不一致（``ENVIRONMENT_STATE_ROOT_MISMATCH`` /
          ``STATE_ROOT_UNBOUND`` 等）同样按 fail-closed 拒绝。

        ``environment`` 未传或为 demo/testnet/paper 时**直接返回**，不改变任何
        既有行为（存在性触发，缺省即向后兼容）。
        """
        text = str(environment or "").strip().lower()[:32]
        if text != "live":
            return
        data_rel = str(self.raw_settings.get("paths", {}).get("data_dir", "data"))
        reasons = verify_environment_state_root(
            text, data_rel, profile=self.profile_name, project_root=self.project_root,
        )
        if reasons:
            raise LiveWithoutProfileStateRootError(
                f"{LIVE_WITHOUT_PROFILE_STATE_ROOT}: environment={text} "
                f"profile={self.profile_name} reasons={','.join(reasons)}"
            )

    @property
    def effective_config_payload(self) -> dict[str, Any]:
        """K01 ``effective_config`` 三字段载荷（settings / profile_overrides /
        environment_overrides）。

        只收录白名单点分路径中 **实际存在** 的值，命中敏感标记的键被丢弃；
        环境覆盖只取白名单内确实设置的变量，因此 ``.env`` 里的凭据不可能
        进入有效配置哈希，代码默认值也不会被当成有效值。

        r2：额外采集 K01 ``EFFECTIVE_CONFIG_SOURCES`` 的其余五类来源
        （decision_policy / agents / risk_learning / risk_profile.lock /
        windows_service），使风险档、止损线、执行端点与服务定义的漂移都会
        改变 ``effective_config_hash``；来源文件缺失同样改变哈希并在
        ``missing_sources`` 可见。
        """
        dumps, dropped, dropped_payloads, missing = collect_source_config_files(
            self.project_root,
        )
        return collect_effective_config(
            raw_settings=self.raw_settings,
            profile=self.profile_name,
            env=os.environ,
            source_configs=dumps,
            source_dropped_sensitive=dropped,
            source_dropped_payloads=dropped_payloads,
            source_missing=missing,
        ).as_payload()

    @property
    def console_port(self) -> int:
        return 8001 if self.profile_name == "live" else 8000

    @property
    def rate_limit_state(self) -> Path:
        """沿用原公共文件，保留升级前已登记的冷却及跨进程额度。"""
        return self.project_root / "data" / "public_rate_limit_state.json"

    @property
    def stage(self) -> str:
        return str(self.raw_settings.get("stage", "phase0"))

    @property
    def data_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("data_dir", "data")
        return self.runtime_path(rel)

    @property
    def db_file(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("db_file", "data/agent_trader.sqlite")
        return self.runtime_path(rel)

    @property
    def artifacts_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("artifacts_dir", "data/artifacts")
        return self.runtime_path(rel)

    @property
    def logs_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("logs_dir", "data/logs")
        return self.runtime_path(rel)

    @property
    def reviews_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("reviews_dir", "data/reviews")
        return self.runtime_path(rel)

    @property
    def knowledge_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("knowledge_dir", "knowledge")
        return self.project_root / rel

    @property
    def llm_config(self) -> dict[str, Any]:
        return self.raw_settings.get("llm", {})

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.artifacts_dir, self.logs_dir, self.reviews_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_settings(project_root: Path | None = None, *, profile: str | None = None) -> Settings:
    """加载仓库配置，并允许未跟踪的 ``.env.local`` 覆盖基础环境变量。

    ``.env`` 保留机器级通用配置；供应商切换或短期凭据写入 ``.env.local``。
    两个文件都被 Git 忽略，且 local 文件后加载并显式覆盖，避免在原文件中留下
    多个同名键导致实际生效值不清晰。
    """
    root = Path(project_root or Path(__file__).resolve().parents[2])
    selected_profile = profile if profile is not None else os.getenv("MTA_PROFILE")
    load_dotenv(root / ".env")
    load_dotenv(root / ".env.local", override=True)

    def _load(name: str) -> dict[str, Any]:
        raw = yaml.safe_load((root / "config" / name).read_text(encoding="utf-8")) or {}
        return raw

    settings = Settings(
        project_root=root,
        raw_settings=_load("settings.yaml"),
        raw_agents=_load("agents.yaml"),
        raw_risk=_load("risk_learning.yaml"),
        profile_name=selected_profile,
    )
    settings.ensure_dirs()
    return settings
