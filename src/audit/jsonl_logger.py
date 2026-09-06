"""按天滚动的 JSONL 日志：每个 Agent 一个文件，便于复盘。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlLogger:
    """按 UTC 日期追加结构化 JSONL 日志，作为 SQLite 审计的原始旁路记录。"""
    def __init__(self, logs_dir: Path):
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, agent_name: str, suffix: str = "") -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d = self.logs_dir / day
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{agent_name}{suffix}.jsonl"

    def log_agent(self, agent_name: str, event: dict[str, Any]) -> Path:
        p = self._path_for(agent_name)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "agent": agent_name,
            **event,
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return p

    def log_llm(self, agent_name: str, event: dict[str, Any]) -> Path:
        p = self._path_for(agent_name, suffix="_llm_raw")
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return p
