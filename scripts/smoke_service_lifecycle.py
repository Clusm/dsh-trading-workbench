"""交易服务线程生命周期回归；全离线，不构造真实Broker。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.service import TradingService  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


service = TradingService.__new__(TradingService)
service._run_thread = None
service._run_thread_lock = threading.Lock()
service._stop_event = threading.Event()
service._thread = None

entered = threading.Event()
release = threading.Event()
count_lock = threading.Lock()
call_count = 0


def fake_run_once() -> None:
    global call_count
    with count_lock:
        call_count += 1
    entered.set()
    release.wait(2.0)


service.run_once = fake_run_once  # type: ignore[method-assign]

print("\n[1] 并发request_run只启动一个慢决策")
results: list[bool] = []


def request() -> None:
    results.append(service.request_run())


callers = [threading.Thread(target=request) for _ in range(20)]
for caller in callers:
    caller.start()
for caller in callers:
    caller.join()
check("仅一个请求获得执行权", sum(bool(x) for x in results) == 1)
check("慢决策只进入一次", entered.wait(1.0) and call_count == 1)
check("运行中状态可见", service.cycle_running())

release.set()
deadline = time.monotonic() + 2.0
while service.cycle_running() and time.monotonic() < deadline:
    time.sleep(0.01)
check("线程结束后状态清理", not service.cycle_running())

print("\n[2] stop_background等待调度线程退出")
service._stop_event.clear()


def scheduler() -> None:
    service._stop_event.wait(2.0)


service._thread = threading.Thread(target=scheduler)
service._thread.start()
check("安全停止返回True", service.stop_background(wait_seconds=1.0))
check("停止事件已设置", service._stop_event.is_set())

print("\n结果: 5 通过 / 0 失败")
