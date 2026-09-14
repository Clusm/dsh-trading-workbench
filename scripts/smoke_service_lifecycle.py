"""交易服务线程生命周期回归；全离线，不构造真实Broker。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace


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
service._lock = threading.RLock()
service._latest = {"channel_state": {"previous": True}}
service._last_fast_cycle_at = None
service._last_fast_cycle_error = None
service._last_fast_cycle_duration_ms = None
service._last_slow_cycle_started_at = None
service._last_slow_cycle_at = None
service._last_slow_cycle_error = None
service._last_slow_cycle_duration_ms = None
service._last_latest_refresh_at = None
service._last_latest_refresh_error = None
service._started_at = time.time()
service.broker = SimpleNamespace(state={"positions": []})
service.capital = SimpleNamespace(
    initial_equity=500.0, baseline=1000.0, last_reset=None,
    session_equity=lambda: 520.0,
)
service.channels = SimpleNamespace(sync=lambda *_args, **_kwargs: {"fresh": True})

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
service._stop_event.clear()
check("停止后不再接受新的慢决策", service.request_run() is False)

print("\n[3] 展示快照失败必须可观测且保留最后有效值")
service._refresh_latest_channels(state={"positions": []}, prices={})
check("成功刷新记录时间", service._last_latest_refresh_at is not None)
check("成功刷新清除错误", service._last_latest_refresh_error is None)
check("成功刷新更新通道", service._latest["channel_state"] == {"fresh": True})
service.channels = SimpleNamespace(
    sync=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
service._refresh_latest_channels(state={"positions": []}, prices={})
check("刷新失败记录错误类型", service._last_latest_refresh_error == "RuntimeError")
check("刷新失败保留上次有效通道", service._latest["channel_state"] == {"fresh": True})
status = service.runtime_status()
check("运行状态暴露快照刷新错误", status["last_latest_refresh_error"] == "RuntimeError")

print("\n结果: 12 通过 / 0 失败")
