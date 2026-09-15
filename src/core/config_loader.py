"""极简配置加载：YAML + dotenv，供阶段 0 使用。"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


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
