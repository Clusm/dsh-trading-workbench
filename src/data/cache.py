"""本地 JSON 快照缓存：用于计算 DefiLlama/稳定币等无历史接口数据的 24h 变化。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SnapshotCache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def save(self, key: str, payload: dict[str, Any]) -> None:
        self._path(key).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
