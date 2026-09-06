"""运行控制状态：normal / pause / close_only / emergency。持久化到本地 JSON。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

VALID_STATES = {"normal", "pause", "close_only", "emergency"}


class ControlState:
    """线程安全、可持久化的全局交易控制开关。

    状态文件损坏时当前兼容行为回退为 ``normal``；因此写入必须使用原子替换，避免服务
    崩溃留下半截 JSON。是否允许开新仓只由 ``normal`` 决定，其他状态保留减仓能力。
    """
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
        """返回当前控制状态的锁内快照。"""
        with self._lock:
            return self._state

    @property
    def allow_new_positions(self) -> bool:
        """仅在 ``normal`` 状态返回 True；不代表其他风险闸门已经放行。"""
        return self.state == "normal"

    def set_state(self, state: str) -> bool:
        """校验并原子持久化控制状态；非法状态不写文件并返回 False。"""
        if state not in VALID_STATES:
            return False
        with self._lock:
            self._state = state
            tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
            tmp.write_text(json.dumps({"state": state}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        return True
