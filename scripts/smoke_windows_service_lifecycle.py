"""统一 Web 入口的托管停止回归。

通过替换 Uvicorn/TradingService 的隔离桩验证 SCM stop_event 会触发有界 server 退出，
并最终调用 ``TradingService.close``。全程不创建真实服务、不访问网络、不打开项目状态库。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import run_web  # noqa: E402


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  PASS  {name}")


class FakeSettings:
    raw_settings = {"decision_cycle": {"interval_minutes": 10}}


class FakeService:
    instances: list["FakeService"] = []

    def __init__(self, _settings: FakeSettings) -> None:
        self.started = False
        self.closed = False
        self.__class__.instances.append(self)

    def start_background(self, _interval: float) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True


class FakeConfig:
    def __init__(self, app: object, **kwargs: object) -> None:
        self.app = app
        self.kwargs = kwargs


class FakeServer:
    instances: list["FakeServer"] = []

    def __init__(self, config: FakeConfig) -> None:
        self.config = config
        self.should_exit = False
        self.started = threading.Event()
        self.exited = threading.Event()
        self.__class__.instances.append(self)

    def run(self) -> None:
        self.started.set()
        while not self.should_exit:
            time.sleep(0.005)
        self.exited.set()


original_service = run_web.TradingService
original_settings = run_web.load_settings
original_create_app = run_web.create_app
original_config = run_web.uvicorn.Config
original_server = run_web.uvicorn.Server
run_web.TradingService = FakeService
run_web.load_settings = lambda _root: FakeSettings()
run_web.create_app = lambda _service: object()
run_web.uvicorn.Config = FakeConfig
run_web.uvicorn.Server = FakeServer

try:
    print("\n[1] SCM stop_event驱动统一Web入口有界退出")
    stop_event = threading.Event()
    worker = threading.Thread(
        target=run_web.run_web_app,
        kwargs={"host": "127.0.0.1", "port": 18000, "stop_event": stop_event},
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 2
    while not FakeServer.instances and time.monotonic() < deadline:
        time.sleep(0.005)
    check("统一入口已创建服务和Uvicorn实例", bool(FakeService.instances and FakeServer.instances))
    server = FakeServer.instances[-1]
    check("托管运行调用后台循环", FakeService.instances[-1].started)
    check("server进入运行态", server.started.wait(timeout=1))
    stop_event.set()
    worker.join(timeout=2)
    check("stop_event触发server退出", server.exited.is_set() and not worker.is_alive())
    check("Web退出后最终关闭TradingService", FakeService.instances[-1].closed)
finally:
    run_web.TradingService = original_service
    run_web.load_settings = original_settings
    run_web.create_app = original_create_app
    run_web.uvicorn.Config = original_config
    run_web.uvicorn.Server = original_server

print(f"\n结果: {passed} 通过 / 0 失败")
