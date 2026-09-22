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


class LockUnreadableError(RuntimeError):
    """锁文件存在但**读不出持有者**（空 / 截断 / JSON 坏 / 非 UTF-8 / 无可用 pid）。

    这是"读不出 ≠ 已死亡"的载体：``InstanceLock.acquire()`` 在无法确认持有者状态时
    抛本异常并**拒绝启动**——不删除锁文件、不接管。继承 ``RuntimeError``，因此既有
    ``except RuntimeError`` 调用方语义不变，上层仍可单独识别"锁文件损坏"与"服务已在
    运行"两种拒绝。

    U13-FU4：修前 ``acquire()`` 把读不出降级成 ``pid = 0``，而 ``_pid_alive(0)`` 直接
    返回 False，于是走"清理遗留锁"分支——**单方删除仍存活持有者的锁文件**，第二个
    活进程随后 O_EXCL 成功，两个实例同时对同一状态根跑交易服务（重复下单 / 控制面
    双写）。锁文件被截断为 0 字节或内容损坏即可触发。
    """

    def __init__(self, lock_path: Path, reason: str):
        self.lock_path = Path(lock_path)
        self.reason = reason
        super().__init__(
            "单实例锁文件存在但无法解析，拒绝启动（读不出持有者 PID，"
            f"不能确认它是否已死亡）：{self.lock_path}\n"
            f"原因：{reason}\n"
            "为什么不能自动清理：读不出内容 ≠ 持有者已死亡；自动删除锁文件会让两个"
            "实例同时管理同一状态根（重复下单 / 控制面双写）。\n"
            "操作者请这样做：\n"
            "  1) 先确认交易服务是否正在运行（进程列表 / 控制台 / 健康巡检的核心环）；\n"
            "  2) 确认**未**运行后，手工删除该锁文件再启动：\n"
            f'     Windows: del "{self.lock_path}"\n'
            f'     POSIX:   rm "{self.lock_path}"\n'
            "  3) 若服务确实在运行，不要删除该文件——继续使用已在运行的服务即可。"
        )


def read_lock_holder(lock_path: Path) -> tuple[int, dict[str, Any]] | None:
    """只读读出锁文件里的持有者 ``(pid, data)``；**读不出一律 fail-closed**。

    这是 ``InstanceLock._read_holder`` 的实现本体（U13-FU4 落地的唯一口径），
    抽成模块级函数是为了让**不构造、不获取锁对象**的只读消费者复用同一份判定
    —— ``core.ops.recovery.check_writer_stopped`` 需要问的正是"这个状态根的锁
    现在由谁持有"，它不该自己再写一套"读文件 + 解析 pid"的逻辑（那正是
    U13-FU5 的根因：第二套口径把"读不出"降级成了"没有写者"）。

    与 ``InstanceLock.acquire()`` 的区别只在**没有副作用**：不建目录、不写文件、
    不删除、不接管。

    返回 ``(pid, data)``：可解析且 pid 可用（``pid > 0``）；存活判定由调用方做。
    返回 ``None``：锁文件不存在——"没有持有者"，不是"损坏"。
    抛 ``LockUnreadableError``：文件存在但读不出（空 / 截断 / JSON 坏 / 非 UTF-8 /
    无可用 pid）。**绝不**返回 ``0`` 或 ``None`` 把"读不出"降级成"已死亡"。
    """
    path = Path(lock_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        # 权限/IO 失败与非 UTF-8 字节都读不出持有者 ⇒ 同一 fail-closed 口径。
        raise LockUnreadableError(
            path, f"锁文件无法读取：{type(exc).__name__}: {exc}") from exc
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"JSON 顶层不是对象（{type(data).__name__}）")
        raw_pid = data.get("pid")
        if isinstance(raw_pid, bool) or not isinstance(raw_pid, (int, str)):
            raise ValueError(f"pid 字段缺失或类型非法（{type(raw_pid).__name__}）")
        pid = int(raw_pid)
    except Exception as exc:
        raise LockUnreadableError(
            path,
            f"锁文件内容不可解析（{len(raw)} 字节，UTF-8）："
            f"{type(exc).__name__}: {exc}") from exc
    if pid <= 0:
        raise LockUnreadableError(
            path, f"锁文件里的 pid 不是有效进程号（{pid}）")
    return pid, data


class InstanceLock:
    """用 PID 状态文件阻止同一状态根启动两个交易服务实例。

    获取锁时会校验旧 PID 是否仍存活；只有确认进程不存在才接管陈旧锁。实例拥有者退出
    时才可释放文件，避免另一个进程误删正在使用的安全锁。

    三种情形严格区分（U13-FU4）：可解析 + 持有者存活 → 拒绝；可解析 + 持有者**确认**
    已死 → 回收遗留锁（真运维需要，保留）；存在但**读不出** → fail-closed，抛
    ``LockUnreadableError``，不删除、不接管。

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

    def _read_holder(self) -> tuple[int, dict[str, Any]] | None:
        """读出锁文件里的持有者 PID 与元数据；**读不出一律 fail-closed**。

        返回 ``(pid, data)``：可解析且 pid 可用（``pid > 0``），由调用方判定存活/已死。
        返回 ``None``：锁文件在竞争窗口内已被删除——那是"没有持有者"，不是"损坏"，
        调用方重新竞争 O_EXCL 即可。

        抛 ``LockUnreadableError``：文件存在但读不出（空 / 截断 / JSON 坏 / 非 UTF-8 /
        无可用 pid）。这里**绝不**返回 ``0`` 或 ``None`` 来降级成"已死亡"——那正是
        U13-FU4 的根因。

        U13-FU5：实现本体已抽到模块级 :func:`read_lock_holder`，本方法只做转发，
        好让只读消费者（``core.ops.recovery.check_writer_stopped``）复用同一口径、
        不出现第二套"锁文件可读性"判定。**语义与 U13-FU4 逐字不变**（同样的
        ``None``/异常条件、同样的报错文本），``except Exception`` 口径仍不吞
        ``KeyboardInterrupt`` / ``SystemExit``。
        """
        return read_lock_holder(self.path)

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
                holder = self._read_holder()
                if holder is None:
                    # 锁已被并发回收：没有持有者，下一轮 O_EXCL 重新竞争（不是损坏）。
                    continue
                pid, data = holder
                if self._pid_alive(pid):
                    details = {key: data.get(key) for key in (
                        "runtime_instance_id", "generation_id", "lock_scope",
                        "profile", "environment", "account_ref",
                    ) if data.get(key) is not None}
                    detail = f"，持有者={details}" if details else ""
                    raise RuntimeError(
                        f"交易服务已在运行（PID={pid}，锁文件={self.path.name}）{detail}")
                # 可解析 **且** pid 确认不存活 ⇒ 遗留锁，允许回收；下一轮 O_EXCL 仍会
                # 重新竞争，避免并发覆盖。（读不出的情形已在 _read_holder fail-closed，
                # 绝不会走到这里——读不出 ≠ 已死亡。）
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
