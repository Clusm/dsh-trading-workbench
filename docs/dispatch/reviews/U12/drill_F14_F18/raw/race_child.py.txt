"""跨进程单实例锁竞态：holder 在 O_EXCL 建文件后延迟写入，intruder 抢占。

用法: race_child.py <holder|intruder> <lock_path>
只写 TEMP，不联网，不下单。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.instance_lock import InstanceLock  # noqa: E402


def main() -> int:
    role = sys.argv[1]
    lock = Path(sys.argv[2])
    if role == "holder":
        # 真实顺序：O_EXCL 建文件 → 写 payload；这里把两步之间的间隔放大成
        # 300ms 以稳定命中窗口（真实窗口宽度取决于磁盘/AV/负载）。
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        time.sleep(0.3)
        os.write(fd, json.dumps({"pid": os.getpid(), "thread": 1,
                                 "started_at": "x", "runtime_instance_id": "holder"}).encode())
        os.close(fd)
        print(json.dumps({"role": "holder", "pid": os.getpid(), "owned": True}))
        time.sleep(1.0)
        return 0
    # intruder
    lk = InstanceLock(lock)
    try:
        lk.acquire()
    except RuntimeError as exc:
        print(json.dumps({"role": "intruder", "owned": False, "error": str(exc)}))
        return 0
    print(json.dumps({"role": "intruder", "pid": os.getpid(), "owned": True,
                      "observed_content": lk.path.read_text(encoding="utf-8")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
