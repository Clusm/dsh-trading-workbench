"""跨进程单实例锁，防止两个交易服务同时写状态或重复执行。"""
from __future__ import annotations

import json
import os
import threading
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path


class InstanceLock:
    """用 PID 状态文件阻止同一项目目录启动两个交易服务实例。

    获取锁时会校验旧 PID 是否仍存活；只有确认进程不存在才接管陈旧锁。实例拥有者退出
    时才可释放文件，避免另一个进程误删正在使用的安全锁。
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._owned = False

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

    def acquire(self) -> None:
        payload = json.dumps({
            "pid": os.getpid(),
            "thread": threading.get_ident(),
            "started_at": datetime.now(timezone.utc).isoformat(),
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
                    raise RuntimeError(f"交易服务已在运行（PID={pid}，锁文件={self.path.name}）")
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
