"""holder 正常持锁并存活；外部把锁文件截断为 0 字节后再让 intruder 抢。"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[6]
sys.path.insert(0, str(ROOT / "src"))
from core.instance_lock import InstanceLock
role, lock = sys.argv[1], Path(sys.argv[2])
if role == "holder":
    lk = InstanceLock(lock); lk.acquire()
    print(json.dumps({"role": "holder", "pid": lk.describe().get("pid"), "owned": True}), flush=True)
    time.sleep(2.5)
else:
    lk = InstanceLock(lock)
    try:
        lk.acquire(); print(json.dumps({"role": "intruder", "owned": True, "content": lk.path.read_text(encoding="utf-8")}), flush=True)
    except Exception as exc:
        print(json.dumps({"role": "intruder", "owned": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
