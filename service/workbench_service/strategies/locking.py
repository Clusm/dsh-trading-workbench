"""Local account boundary shared by authorization writes and order dispatch.

The OS lock releases on process death. Re-entrancy is per thread, while the
process RLock prevents the POSIX per-process flock semantics hiding contention.
"""
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .validation import fail

_registry_lock = threading.Lock()
_locks = {}
_depth = threading.local()


@contextmanager
def execution_guard(db_path):
    key = str(Path(db_path).resolve()).lower()
    with _registry_lock:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        depths = getattr(_depth, "values", {})
        _depth.values = depths
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        path = Path(db_path).with_suffix(".strategy-gate.lock")
        with open(path, "a+b") as handle:
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 30
            while True:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        fail("E_STRATEGY_BUSY", "执行正在提交，请稍后重试", 503)
                    time.sleep(0.025)
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
