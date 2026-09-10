"""运行控制状态：normal / pause / close_only / emergency。持久化到本地 JSON。"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

VALID_STATES = {"normal", "pause", "close_only", "emergency"}


class ControlState:
    """线程安全、可持久化的全局交易控制开关。

    状态文件不存在代表首次启动，可使用 ``normal``；文件存在但无法校验时必须
    fail-closed 到 ``close_only``，避免损坏的 emergency/pause 被静默解除。
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 控制状态是资金安全开关：写入必须原子（tmp + os.replace），并且全程持锁。
        # 之前直接 write_text：崩溃时可能留下半截 JSON，重启后 emergency/pause
        # 会被静默丢弃、回到 normal 自动开仓——这是不能接受的默认行为。
        self._lock = threading.Lock()
        self._state = "normal"
        self._recovery_required = False
        self._load_error: str | None = None
        if self.path.exists():
            try:
                # PowerShell 5 等历史工具可能写入 UTF-8 BOM；这是合法编码，
                # 不能误判成控制状态损坏。内容/枚举校验仍保持 fail-closed。
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict) or data.get("state") not in VALID_STATES:
                    raise ValueError("控制状态缺失或非法")
                self._state = str(data["state"])
            except Exception as exc:
                self._state = "close_only"
                self._recovery_required = True
                self._load_error = f"{type(exc).__name__}: {exc}"

    @property
    def state(self) -> str:
        """返回当前控制状态的锁内快照。"""
        with self._lock:
            return self._state

    @property
    def allow_new_positions(self) -> bool:
        """仅在 ``normal`` 状态返回 True；不代表其他风险闸门已经放行。"""
        return self.state == "normal"

    @property
    def recovery_required(self) -> bool:
        """状态文件损坏且尚未由操作者显式写入有效状态。"""
        with self._lock:
            return self._recovery_required

    @property
    def load_error(self) -> str | None:
        """返回启动时的校验错误；仅用于诊断，不包含状态文件正文。"""
        with self._lock:
            return self._load_error

    def set_state(self, state: str) -> bool:
        """校验并原子持久化控制状态；非法状态不写文件并返回 False。"""
        if state not in VALID_STATES:
            return False
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}")
        with self._lock:
            try:
                tmp.write_text(json.dumps({"state": state}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
            finally:
                # replace 成功后 tmp 已不存在；失败时尽力清理，但保留原状态文件。
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            # 只有持久化成功后才切换内存状态，避免磁盘/内存安全状态分叉。
            self._state = state
            self._recovery_required = False
            self._load_error = None
        return True
