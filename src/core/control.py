"""运行控制状态：normal / pause / close_only / emergency。持久化到本地 JSON。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

VALID_STATES = {"normal", "pause", "close_only", "emergency"}


class ControlState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 控制状态是资金安全开关：写入必须原子（tmp + os.replace），并且全程持锁。
        # 之前直接 write_text：崩溃时可能留下半截 JSON，重启后 emergency/pause
        # 会被静默丢弃、回到 normal 自动开仓——这是不能接受的默认行为。
        self._lock = threading.Lock()
        self._state = "normal"
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("state") in VALID_STATES:
                    self._state = data["state"]
            except Exception:
                pass

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def allow_new_positions(self) -> bool:
        return self.state == "normal"

    def set_state(self, state: str) -> bool:
        if state not in VALID_STATES:
            return False
        with self._lock:
            self._state = state
            tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
            tmp.write_text(json.dumps({"state": state}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        return True
