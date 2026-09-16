"""发布证据采样回归：分钟对齐、时效判定、以及“停摆不得记为健康”。

背景（两个可复现缺陷）：

1. 采样挂在交易快循环上（``_loop`` 顶部调用 ``_record_release_observation``），
   任一轮快循环变慢就整分钟没有采样。生产实测出现过 123 秒间隔，直接缺掉
   ``2026-09-15T16:06`` 整分钟：90 条观测只覆盖 90/91 个分钟桶，覆盖率 0.989011。
   按该缺失率投影 7 天约缺 111 分钟（约 98.9%），而门禁要求 ≥0.999
   （10080 分钟里最多缺 10 分钟）——即门禁在结构上不可达。
2. 健康判定只看心跳字段“是否存在”，不看时间。快循环停摆十分钟仍写入
   ``health_state='healthy'``，而覆盖率门禁只统计 healthy 分钟，于是停摆会被
   记成有效证据，违反“缺失分钟不得记为健康”。

本用例全离线：不构造真实 Broker，不联网，不写项目数据库。
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.service import TradingService  # noqa: E402


def check(name: str, condition: bool, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{name} -> {detail!r}")
    print(f"  PASS  {name}")


class FakeDB:
    """只实现证据写入所需接口；不落盘。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.lock = threading.Lock()

    def fetch_one(self, _sql: str, _params: object = None) -> tuple[int]:
        return (1,)

    def log_release_observation(self, **kwargs) -> bool:
        with self.lock:
            self.rows.append(kwargs)
        return True

    def count(self) -> int:
        with self.lock:
            return len(self.rows)


def build_service() -> TradingService:
    """搭一个最小可用的 TradingService；不发网络请求。"""
    service = TradingService.__new__(TradingService)
    service._lock = threading.RLock()
    service._run_thread = None
    service._run_thread_lock = threading.Lock()
    service._stop_event = threading.Event()
    service._thread = None
    service._intel_thread = None
    service._review_thread = None
    service._evidence_thread = None
    service._closed = False
    service._accepting_work = True
    service._started_at = time.time()
    service._last_release_observation_minute = None
    service._last_fast_cycle_at = time.time()
    service._last_fast_cycle_error = None
    service._last_slow_cycle_at = time.time()
    service._last_slow_cycle_error = None
    service.runtime_instance_id = "smoke-instance"
    service.release_id = "smoke-release"
    service.release_manifest = {"content_hash": "smoke-hash", "account_ref": "smoke-account"}
    service.broker = SimpleNamespace(environment="demo")
    service.settings = SimpleNamespace(raw_settings={
        "stability": {"startup_grace_seconds": 120, "fast_cycle_stale_seconds": 120},
        "decision_cycle": {"fast_interval_seconds": 30},
    })
    service.db = FakeDB()
    return service


print("\n[1] 采样边界对齐到 UTC 整分")
for probe in ("2026-09-16T00:00:00.000+00:00", "2026-09-16T00:00:00.500+00:00",
              "2026-09-16T00:00:59.999+00:00", "2026-09-16T12:34:56.789+00:00"):
    now = datetime.fromisoformat(probe)
    boundary = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    wait = TradingService._next_evidence_boundary(now)
    land = now.timestamp() + wait
    # 落点必须不早于下一个整分，且不晚于它（下限保护最多允许 0.05 秒）。
    offset = land - boundary.timestamp()
    check(f"边界对齐 {probe[-15:]} wait={wait:.3f}s",
          0.0 < wait <= 60.0 and 0.0 <= offset < 0.06,
          {"wait": wait, "offset": offset})

print("\n[2] 快循环时效判定复用 stability 阈值")
service = build_service()
now = time.time()
check("新鲜心跳 -> healthy", service._fast_cycle_freshness(now, None) == "healthy")
check("超阈值心跳 -> stale",
      service._fast_cycle_freshness(now - 300, None) == "stale")
check("边界内心跳 -> healthy",
      service._fast_cycle_freshness(now - 119, None) == "healthy")
check("有错误 -> degraded",
      service._fast_cycle_freshness(now, "APIError") == "degraded")
service._started_at = now
check("宽限期内无心跳 -> unknown",
      service._fast_cycle_freshness(None, None) == "unknown")
service._started_at = now - 600
check("超宽限期无心跳 -> missing",
      service._fast_cycle_freshness(None, None) == "missing")

print("\n[3] 停摆不得记为健康（覆盖率门禁只统计 healthy 分钟）")
service = build_service()
service._last_fast_cycle_at = time.time() - 600
service._record_release_observation()
row = service.db.rows[-1]
check("停摆分钟记为 degraded", row["health_state"] == "degraded", row["health_state"])
check("组件标记为 stale", "stale" in row["component_status"].values(),
      row["component_status"])

service = build_service()
service._record_release_observation()
check("正常心跳记为 healthy",
      service.db.rows[-1]["health_state"] == "healthy")

service = build_service()
service._last_fast_cycle_error = "APIError"
service._record_release_observation()
check("快循环出错记为 degraded", service.db.rows[-1]["health_state"] == "degraded")

service = build_service()
service._started_at = time.time() - 600
service._last_fast_cycle_at = None
service._record_release_observation()
check("从未完成快循环记为 degraded",
      service.db.rows[-1]["health_state"] == "degraded")

service = build_service()
service._closed = True
service._record_release_observation()
check("已关闭服务记为 degraded", service.db.rows[-1]["health_state"] == "degraded")

print("\n[4] 同分钟幂等：重复调用只写一行")
service = build_service()
service._record_release_observation()
service._record_release_observation()
service._record_release_observation()
check("同分钟只写一行", service.db.count() == 1, service.db.count())

print("\n[5] 采样与交易循环解耦：交易循环完全停摆仍能采样")
service = build_service()
service._next_evidence_boundary = lambda _now: 0.05  # type: ignore[method-assign]
started = threading.Event()


def stalled_loop() -> None:
    """模拟交易循环整轮卡死：本线程从不写证据，只标记已“进入停摆”。"""
    started.set()
    time.sleep(3.0)


stall = threading.Thread(target=stalled_loop, daemon=True)
stall.start()
started.wait(1.0)
sampler = threading.Thread(target=service._evidence_loop, daemon=True)
sampler.start()
deadline = time.time() + 1.5
while service.db.count() == 0 and time.time() < deadline:
    time.sleep(0.02)
check("交易循环阻塞期间仍有采样写入", service.db.count() >= 1, service.db.count())
check("采样线程持续存活", sampler.is_alive())
service._stop_event.set()
sampler.join(timeout=2.0)
check("停止信号能在预算内结束采样线程", not sampler.is_alive())

print("\n[6] start_background/stop_background 托管采样线程")
service = build_service()
service.intel_sidecar = SimpleNamespace(enabled=False)
service.db_backup = SimpleNamespace(maybe_backup=lambda: None)
service.request_run = lambda: False  # type: ignore[method-assign]
service.run_fast_once = lambda: None  # type: ignore[method-assign]
service._maybe_auto_review = lambda: None  # type: ignore[method-assign]
service.start_background(interval_minutes=10)
worker = getattr(service, "_evidence_thread", None)
check("start_background 启动采样线程", bool(worker and worker.is_alive()), worker)
check("采样线程标记为 release-evidence", getattr(worker, "name", None) == "release-evidence")
quiet = service.stop_background(wait_seconds=3.0)
check("stop_background 回收采样线程并静默", quiet and not worker.is_alive(), quiet)
check("停止后不再新增采样", service.db.count() <= 1, service.db.count())

print("\n[7] 采样线程异常不得终止循环")
service = build_service()
calls = {"n": 0}


def flaky() -> None:
    calls["n"] += 1
    raise RuntimeError("simulated evidence failure")


service._record_release_observation = flaky  # type: ignore[method-assign]
service._next_evidence_boundary = lambda _now: 0.05  # type: ignore[method-assign]
sampler = threading.Thread(target=service._evidence_loop, daemon=True)
sampler.start()
deadline = time.time() + 1.0
while calls["n"] < 3 and time.time() < deadline:
    time.sleep(0.02)
check("单次失败后继续采样", calls["n"] >= 3, calls["n"])
check("异常后线程仍存活", sampler.is_alive())
service._stop_event.set()
sampler.join(timeout=2.0)
check("停止后线程退出", not sampler.is_alive())

print("\n结果：全部通过")
