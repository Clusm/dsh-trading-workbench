"""极简配置加载：YAML + dotenv，供阶段 0 使用。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class Settings:
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
    root = Path(project_root or Path(__file__).resolve().parents[2])
    load_dotenv(root / ".env")

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
