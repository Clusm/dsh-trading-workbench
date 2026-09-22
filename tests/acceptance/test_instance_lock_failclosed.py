"""U13-FU4 + U13-FU5 验收：锁文件**存在但不可解析**时全仓必须 fail-closed。

独立可运行（本仓库无 pytest）：

    PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_instance_lock_failclosed.py

全程离线：只在临时目录造锁文件，不读真实 ``.env``/``data``/密钥，不联网、
不启动交易服务、不下单。跨进程：锁持有者是**真实子进程**（PID 真实存活），
本进程扮演"第二个服务"，因此"两个活持有者"是真实的两进程共存，而不是同进程
多对象的近似。逐项打印 PASS/FAIL，任一失败以退出码 1 结束。

U13-FU4 —— 单实例锁 ``InstanceLock.acquire()`` 侧，三种情形：

  [1] 可解析 + 持有者存活      → 拒绝（行为必须逐字不变）
  [2] 可解析 + 持有者确定已死  → 回收遗留锁（真实运维需要，必须没被修坏）
  [3] 存在但不可解析（空 / 截断 / JSON 坏 / 非 UTF-8 / 无 pid / pid<=0）
      → fail-closed：抛可区分的 ``LockUnreadableError``、**不删除**锁文件、不接管
  [4] 正常路径：首拿 / 次拒 / 释放后可再拿

U13-FU5 —— **恢复包创建**侧的同一根因（第二种口径），在本文件被扩展为：

  [5] **真实存活写者 + 锁文件损坏** → ``check_writer_stopped`` / ``create_epoch_bundle``
      必须**拒绝**（原因码 ``RECOVERY_LOCK_UNREADABLE``），写者仍活、锁原样保留、
      全程**不发布任何恢复包**。修复前这里会成功造出 ``writer_stopped: True`` 的包
      并被 ``verify_epoch_bundle`` 接受（撕裂恢复点被伪称"同一时刻静止"）。
  [5b] 反向证据：**锁文件不存在** 与 **遗留锁（持有者确认已死）** 两条合法运维
      路径必须**仍然允许**建包并通过 ``verify_epoch_bundle``（证明没修坏）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.instance_lock import InstanceLock  # noqa: E402
from core.ops.recovery import (  # noqa: E402
    RECOVERY_WRITER_RUNNING,
    check_writer_stopped,
    create_epoch_bundle,
    verify_epoch_bundle,
)
from core.runtime_paths import REQUIRED_STATE_FILES, RuntimePaths  # noqa: E402

try:
    # 修复后引入的可区分错误类型；修复前不存在（那时"读不出"被降级成 pid=0）。
    from core.instance_lock import LockUnreadableError  # noqa: E402

    _HAS_TYPED_ERROR = True
except ImportError:
    LockUnreadableError = None  # type: ignore[assignment]
    _HAS_TYPED_ERROR = False

try:
    # U13-FU5 新增：恢复包侧的"锁读不出"原因码；修复前不存在。
    from core.ops.recovery import RECOVERY_LOCK_UNREADABLE as _FU5_CODE  # noqa: E402
except ImportError:  # 修复前的树上没有这个原因码：按字面量比对，照样能判 FAIL。
    _FU5_CODE = "RECOVERY_LOCK_UNREADABLE"  # type: ignore[assignment]

_FAILURES: list[str] = []

#: 真实持有者子进程：拿住若干锁路径并保持存活，直到被父进程终止。
#: 必须把 lock 对象留在列表里，否则临时对象被回收时 ``__del__`` 会释放锁。
_HOLDER_PROGRAM = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
from core.instance_lock import InstanceLock
held = []
for raw in sys.argv[2:]:
    lock = InstanceLock(raw)
    lock.acquire()
    held.append(lock)
print("HELD %d" % os.getpid(), flush=True)
time.sleep(120)
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    """打印一项判定；失败登记以便最终汇总。"""
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f' — {detail}' if detail else ''}")
    if not ok:
        _FAILURES.append(name)


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _spawn_holder(paths: list[Path]) -> tuple[subprocess.Popen, int]:
    """启动真实持有者子进程，等它宣告 HELD；返回 (进程, 锁文件里那个真实 PID)。

    Windows 的 venv ``python.exe`` 启动器可能再起一层真实解释器，因此持有者自报的
    ``os.getpid()`` 才等于锁文件里的 PID，清理也必须按它来杀。
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_PROGRAM, str(ROOT / "src"), *map(str, paths)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=_child_env(),
    )
    line = (proc.stdout.readline() if proc.stdout else "") or ""
    if "HELD" not in line:
        stderr = (proc.stderr.read() if proc.stderr else "")[-2000:]
        proc.kill()
        raise AssertionError(f"持有者子进程未就绪: {line!r} / {stderr}")
    return proc, int(line.split()[1])


def _kill_holder(proc: subprocess.Popen, real_pid: int) -> None:
    import signal

    for target in (real_pid, proc.pid):
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def _dead_pid() -> int:
    """取一个**确定已死**的真实 PID（真跑一个短命子进程，再等它退出）。"""
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"], env=_child_env())
    pid = proc.pid
    proc.wait(timeout=30)
    return pid


def _attempt(path: Path) -> tuple[InstanceLock | None, BaseException | None]:
    """尝试获取锁；返回 (成功时的 lock 对象, 失败时的异常)。"""
    lock = InstanceLock(path)
    try:
        lock.acquire()
    except BaseException as exc:
        return None, exc
    return lock, None


def _corrupt_payloads() -> dict[str, bytes]:
    """"存在但不可解析"的六种形态（内容一律不可用，但文件确实存在）。"""
    return {
        "空文件（被截断为 0 字节）": b"",
        "坏 JSON（写入中途被截断）": b'{"pid": 1234,',
        "JSON 顶层非对象": b'["pid", 1234]',
        "非 UTF-8 字节": b'{"pid": 1234}\xff\xfe',
        "无 pid 字段": b'{"thread": 5}',
        "pid 为 0（不可用）": b'{"pid": 0}',
    }


# --------------------------------------------------------------------------
# U13-FU5：恢复包创建侧的同一根因
# --------------------------------------------------------------------------


def _attempt_code(fn, *args, **kwargs) -> str:
    """调用 ``fn``；返回异常的 ``code`` 属性（未抛异常则返回空串）。"""
    try:
        fn(*args, **kwargs)
    except BaseException as exc:  # noqa: BLE001 —— 只取原因码，不吞给调用方
        return str(getattr(exc, "code", "") or type(exc).__name__)
    return ""


def _try_bundle(paths, bundles) -> tuple[Path | None, BaseException | None]:
    """尝试建包；返回 (包目录, None) 或 (None, 异常)。"""
    try:
        return create_epoch_bundle(paths, bundles, environment="demo"), None
    except BaseException as exc:  # noqa: BLE001
        return None, exc


def _make_state_fixture(base: Path) -> RuntimePaths:
    """最小可用状态根：SQLite（含 ``schema_migrations``）+ 全部关键 JSON。

    JSON 名单取自 ``REQUIRED_STATE_FILES``（**不**硬编码名单/迁移版本集合）。
    """
    paths = RuntimePaths.resolve(base, profile=None)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(paths.db_file)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO schema_migrations VALUES(1)")
    connection.commit()
    connection.close()
    for name in REQUIRED_STATE_FILES:
        (paths.data_dir / name).write_text(json.dumps({"fixture": name}), encoding="utf-8")
    return paths


def _manifest_of(bundle: Path) -> dict:
    return json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))


def check_recovery_writer_stopped_fail_closed() -> None:
    """[5] 写者真实存活 + 锁文件损坏 → 恢复包必须拒绝；[5b] 合法路径仍允许。"""
    print("\n[5] 写者存活 + 锁文件损坏 → 恢复包必须拒绝（U13-FU5）")
    with tempfile.TemporaryDirectory(prefix="fu5-accept-") as tmp:
        base = Path(tmp)
        paths = _make_state_fixture(base / "host")
        bundles = base / "bundles"
        payloads = _corrupt_payloads()
        holder, holder_pid = _spawn_holder([paths.lock_file])
        try:
            lock = paths.lock_file
            check("前提：锁文件记录的是真实存活的写者 PID",
                  lock.is_file() and holder_pid > 0
                  and InstanceLock._pid_alive(holder_pid))
            check("前提：锁完好且写者存活 → RECOVERY_WRITER_RUNNING（行为不变）",
                  _attempt_code(check_writer_stopped, paths) == RECOVERY_WRITER_RUNNING)

            for label, payload in payloads.items():
                lock.write_bytes(payload)
                code = _attempt_code(check_writer_stopped, paths)
                check(f"{label}：check_writer_stopped 拒绝且原因码可区分",
                      code == _FU5_CODE, f"code={code or '（未拒绝）'}")
                bundle, exc = _try_bundle(paths, bundles)
                check(f"{label}：create_epoch_bundle 拒绝建包", bundle is None,
                      "未拒绝：造出了伪称静止的恢复包" if bundle is not None
                      else f"{type(exc).__name__}")
                check(f"{label}：拒绝原因码可区分（非 WRITER_RUNNING）",
                      _attempt_code(check_writer_stopped, paths) == _FU5_CODE)
                check(f"{label}：写者仍真实存活（读不出 ≠ 已停）",
                      InstanceLock._pid_alive(holder_pid))
                check(f"{label}：锁文件被原样保留",
                      lock.exists() and lock.read_bytes() == payload)
                check(f"{label}：报错可行动（先确认服务、再手工删锁）",
                      exc is not None and "确认" in str(exc) and "手工删除" in str(exc))
            published = sorted(bundles.iterdir()) if bundles.is_dir() else []
            check("全程未发布任何恢复包", not published,
                  ", ".join(p.name for p in published))
        finally:
            _kill_holder(holder, holder_pid)

        print("\n[5b] 反向证据：合法运维路径必须仍然允许建包")
        legal = _make_state_fixture(base / "host2")

        check("(a) 前提：锁文件不存在", not legal.lock_file.exists())
        check("(a) 锁文件不存在 → check_writer_stopped 不拒绝",
              _attempt_code(check_writer_stopped, legal) == "")
        bundle, exc = _try_bundle(legal, base / "bundles-a")
        check("(a) 锁文件不存在 → 允许建包（合法路径未被修坏）",
              bundle is not None, f"{type(exc).__name__}: {exc}")
        if bundle is not None:
            check("(a) 包声明停写且 verify_epoch_bundle 接受",
                  _manifest_of(bundle)["writer_stopped"] is True
                  and verify_epoch_bundle(bundle, expected_environment="demo")["schema"] == 3)

        dead = _dead_pid()
        check("(b) 前提：构造出的 PID 确定已死", not InstanceLock._pid_alive(dead))
        legal.lock_file.write_text(
            json.dumps({"pid": dead, "thread": 3}), encoding="utf-8")
        check("(b) 遗留锁（持有者已死）→ check_writer_stopped 不拒绝",
              _attempt_code(check_writer_stopped, legal) == "")
        stale_bundle, stale_exc = _try_bundle(legal, base / "bundles-b")
        check("(b) 遗留锁（持有者已死）→ 允许建包（真实运维路径未被修坏）",
              stale_bundle is not None, f"{type(stale_exc).__name__}: {stale_exc}")
        if stale_bundle is not None:
            check("(b) 包声明停写且 verify_epoch_bundle 接受",
                  _manifest_of(stale_bundle)["writer_stopped"] is True
                  and verify_epoch_bundle(
                      stale_bundle, expected_environment="demo")["schema"] == 3)


def main() -> int:
    print("U13-FU4 单实例锁 fail-closed 验收")
    with tempfile.TemporaryDirectory(prefix="fu4-accept-") as tmp:
        base = Path(tmp)
        intact = base / "intact.lock"
        corrupt = {label: base / f"c{index}.lock"
                   for index, label in enumerate(_corrupt_payloads())}
        payloads = _corrupt_payloads()

        holder, holder_pid = _spawn_holder([intact, *corrupt.values()])
        try:
            print("\n[1] 可解析 + 持有者存活 → 拒绝（行为不变）")
            lock, exc = _attempt(intact)
            check("第二实例被拒绝", lock is None and isinstance(exc, RuntimeError),
                  type(exc).__name__)
            check("拒绝文案保持原样（写明持有者 PID 与锁文件）",
                  "已在运行" in str(exc) and str(holder_pid) in str(exc))
            check("拒绝时未删除持有者的锁文件", intact.exists())
            check("持有者进程真实存活（跨进程）", InstanceLock._pid_alive(holder_pid))

            print("\n[3] 存在但不可解析 → fail-closed（不删除、不接管）")
            for label, path in corrupt.items():
                payload = payloads[label]
                path.write_bytes(payload)
                lock, exc = _attempt(path)
                if lock is not None:
                    lock.release()
                check(f"{label}：拒绝且不接管", lock is None)
                typed = _HAS_TYPED_ERROR and isinstance(exc, LockUnreadableError)
                check(f"{label}：抛可区分的 LockUnreadableError",
                      bool(typed), type(exc).__name__ if exc is not None else "无异常")
                check(f"{label}：报错可行动（先确认、再手工删除）",
                      "手工删除" in str(exc) and "确认" in str(exc))
                preserved = path.exists() and path.read_bytes() == payload
                check(f"{label}：锁文件被原样保留", preserved,
                      "" if path.exists() else "锁文件已被删除（fail-open）")
                check(f"{label}：持有者未被误判为已死亡",
                      InstanceLock._pid_alive(holder_pid))
        finally:
            _kill_holder(holder, holder_pid)

        print("\n[2] 可解析 + 持有者确定已死 → 回收（合法路径不能被修坏）")
        stale = base / "stale.lock"
        dead = _dead_pid()
        check("前提：构造出的 PID 确定已死", not InstanceLock._pid_alive(dead))
        stale.write_text(
            json.dumps({"pid": dead, "thread": 7,
                        "started_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        lock, exc = _attempt(stale)
        check("遗留锁被回收（第二个进程可接管）", lock is not None,
              type(exc).__name__ if exc is not None else "")
        if lock is not None:
            check("接管后锁文件记录本进程 PID",
                  int(json.loads(stale.read_text(encoding="utf-8"))["pid"]) == os.getpid())
            lock.release()
        check("释放后锁文件被删除", not stale.exists())

        print("\n[4] 正常路径：首拿 / 次拒 / 释放后可再拿")
        normal = base / "normal.lock"
        first = InstanceLock(normal)
        first.acquire()
        check("第一个实例拿到锁", first._owned and normal.exists())
        second = InstanceLock(normal)
        refused = False
        try:
            second.acquire()
        except RuntimeError:
            refused = True
        check("第二个实例被拒绝", refused and not second._owned)
        first.release()
        check("持有者释放后锁文件被删除", not normal.exists())
        third = InstanceLock(normal)
        third.acquire()
        check("释放后可再拿", third._owned)
        third.release()

    check_recovery_writer_stopped_fail_closed()

    if _FAILURES:
        print(f"\nU13-FU4/FU5 FAILED: {len(_FAILURES)} 项 —— {', '.join(_FAILURES[:5])}")
        return 1
    print("\nU13-FU4/FU5 LOCK + RECOVERY FAIL-CLOSED OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
