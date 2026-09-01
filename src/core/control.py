"""运行控制状态：normal / pause / close_only / emergency。持久化到本地 JSON。"""
from __future__ import annotations

import json
from pathlib import Path

VALID_STATES = {"normal", "pause", "close_only", "emergency"}


class ControlState:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        return self._state

    @property
    def allow_new_positions(self) -> bool:
        return self._state == "normal"

    def set_state(self, state: str) -> bool:
        if state not in VALID_STATES:
            return False
        self._state = state
        self.path.write_text(json.dumps({"state": state}, ensure_ascii=False), encoding="utf-8")
        return True
