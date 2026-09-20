"""跨进程单实例锁，防止两个交易服务同时写状态或重复执行。"""
from __future__ import annotations

import json
import os
import threading
import ctypes
from collections.abc import Mapping
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 锁文件里由本类自己维护的保留键，调用方不能覆盖。
_RESERVED_KEYS = ("pid", "thread", "started_at")
#: 允许写入锁文件的元数据形状；其余类型一律丢弃（不静默 str() 未知对象）。
_ALLOWED_METADATA_TYPES = (str, int, float, bool)
_MAX_METADATA_TEXT = 200


def _sanitise_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """只保留可审计的非敏感标量：拒绝敏感键名、截断超长文本、丢弃复杂对象。

    锁文件是跨进程可见的明文（曾被用来证明"同一账户同一执行者"），因此绝不
    把凭据/密钥写进去。敏感键名判定复用 U15 ``is_sensitive_name``，不另立口径。
    """
    if not metadata:
        return {}
    from core.runtime_identity import is_sensitive_name

    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        name = str(key)
        if name in _RESERVED_KEYS or is_sensitive_name(name):
            continue
        if value is None or isinstance(value, bool):
            cleaned[name] = value
        elif isinstance(value, _ALLOWED_METADATA_TYPES):
            cleaned[name] = value[:_MAX_METADATA_TEXT] if isinstance(value, str) else value
    return cleaned


class InstanceLock:
    """用 PID 状态文件阻止同一状态根启动两个交易服务实例。

    获取锁时会校验旧 PID 是否仍存活；只有确认进程不存在才接管陈旧锁。实例拥有者退出
    时才可释放文件，避免另一个进程误删正在使用的安全锁。

    U13（diff-6）：锁路径必须由 **状态根 + profile** 决定（``RuntimePaths.lock_file``），
    这样换代码目录不会换锁——两套代码根无法各持一把锁管理同一账户（F06/F18/R12）。
    可选 ``metadata`` 把运行代/账户作用域写进锁文件，事故复盘时可回答"当时是谁在
    写这份账本"。
    """
    def __init__(self, path: Path, *, metadata: Mapping[str, Any] | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._owned = False
        self.metadata = _sanitise_metadata(metadata)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # Windows 的 os.kill(pid, 0) 不是 POSIX 式无害探测：Python 会调用
            # TerminateProcess，signal=0 仍会把目标进程直接结束。使用只读进程
            # 句柄 + 零等待检查退出状态，绝不向目标发送信号。
            synchronize = 0x00100000
            wait_timeout = 0x00000102
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            open_process.restype = wintypes.HANDLE
            wait_for_single = kernel32.WaitForSingleObject
            wait_for_single.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            wait_for_single.restype = wintypes.DWORD
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            handle = open_process(synchronize, False, int(pid))
            if not handle:
                # ERROR_ACCESS_DENIED 表示进程存在但当前用户无权打开。
                return int(ctypes.get_last_error()) == 5
            try:
                return int(wait_for_single(handle, 0)) == wait_timeout
            finally:
                close_handle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def describe(self) -> dict[str, Any]:
        """只读读取锁文件内容（不判断存活、不改文件）；损坏或缺失返回空字典。"""
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def update_metadata(self, metadata: Mapping[str, Any] | None) -> bool:
        """在**持有锁**的前提下补充元数据（运行代/账户作用域）。

        启动顺序决定了先拿锁、后才知道账户：拿锁时只能写 profile/锁作用域，
        解析出账户与运行代后再回填。只在 ``self._owned`` 为真时写，且保留
        pid/thread/started_at 与已有键——不会把别人的锁文件改掉。
        """
        if not self._owned:
            return False
        cleaned = _sanitise_metadata(metadata)
        if not cleaned:
            return False
        payload = self.describe()
        if not payload or int(payload.get("pid") or 0) != os.getpid():
            return False
        payload.update(cleaned)
        self.metadata = {**self.metadata, **cleaned}
        temp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)
        return True

    def acquire(self) -> None:
        payload = json.dumps({
            "pid": os.getpid(),
            "thread": threading.get_ident(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            # 运行代/账户作用域：仅可审计标量，详见 _sanitise_metadata。
            **self.metadata,
        }, ensure_ascii=False).encode("utf-8")
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                self._owned = True
                return
            except FileExistsError:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    pid = int(data.get("pid") or 0)
                except Exception:
                    pid = 0
                if self._pid_alive(pid):
                    holder = {key: data.get(key) for key in (
                        "runtime_instance_id", "generation_id", "lock_scope",
                        "profile", "environment", "account_ref",
                    ) if data.get(key) is not None}
                    detail = f"，持有者={holder}" if holder else ""
                    raise RuntimeError(
                        f"交易服务已在运行（PID={pid}，锁文件={self.path.name}）{detail}")
                # 仅清理确认已死亡的遗留锁；下一轮 O_EXCL 仍会重新竞争，避免并发覆盖。
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        raise RuntimeError(f"无法获得交易服务单实例锁：{self.path}")

    def release(self) -> None:
        if not self._owned:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if int(data.get("pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        except Exception:
            pass
        self._owned = False

    def __del__(self) -> None:
        # 构造TradingService中途失败时也尽力释放本进程刚创建的锁。
        try:
            self.release()
        except Exception:
            pass
