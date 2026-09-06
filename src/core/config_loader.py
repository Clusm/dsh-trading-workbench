"""极简配置加载：YAML + dotenv，供阶段 0 使用。"""
from __future__ import annotations

from dataclasses import dataclass
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

    @property
    def stage(self) -> str:
        return str(self.raw_settings.get("stage", "phase0"))

    @property
    def data_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("data_dir", "data")
        return self.project_root / rel

    @property
    def db_file(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("db_file", "data/agent_trader.sqlite")
        return self.project_root / rel

    @property
    def artifacts_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("artifacts_dir", "data/artifacts")
        return self.project_root / rel

    @property
    def logs_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("logs_dir", "data/logs")
        return self.project_root / rel

    @property
    def reviews_dir(self) -> Path:
        rel = self.raw_settings.get("paths", {}).get("reviews_dir", "data/reviews")
        return self.project_root / rel

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


def load_settings(project_root: Path | None = None) -> Settings:
    """加载仓库配置，并允许未跟踪的 ``.env.local`` 覆盖基础环境变量。

    ``.env`` 保留机器级通用配置；供应商切换或短期凭据写入 ``.env.local``。
    两个文件都被 Git 忽略，且 local 文件后加载并显式覆盖，避免在原文件中留下
    多个同名键导致实际生效值不清晰。
    """
    root = Path(project_root or Path(__file__).resolve().parents[2])
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
    )
    settings.ensure_dirs()
    return settings
