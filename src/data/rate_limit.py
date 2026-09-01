"""公共行情请求节流 + 418 全局冷却：避免 Binance HTTP 418 IP 限流。

两个层面：
1. throttle(min_interval)：单进程内相邻请求至少间隔 min_interval 秒；
2. 418 全局冷却：任何公开行情请求一旦命中 HTTP 418，触发进程级冷却，
   冷却期内所有公开行情请求立即短路失败（不再发请求），由上层走缓存/降级。
   这比"逐请求退避后继续撞墙"更能保护 IP 额度。
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0
_COOLDOWN_UNTIL = 0.0  # 单调时钟（time.monotonic）下的冷却截止时刻


def throttle(min_interval: float = 0.20) -> None:
    """每次请求前调用，保证相邻请求至少间隔 min_interval 秒。"""
    global _LAST_REQUEST_TS
    with _LOCK:
        now = time.monotonic()
        wait = _LAST_REQUEST_TS + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_TS = time.monotonic()


def note_418(cooldown_seconds: float = 480.0) -> None:
    """记录一次 HTTP 418，触发全局冷却（默认 8 分钟）。"""
    global _COOLDOWN_UNTIL
    with _LOCK:
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + cooldown_seconds)


def cooldown_remaining() -> float:
    """返回剩余冷却秒数；0 表示当前不在冷却期。"""
    global _COOLDOWN_UNTIL
    with _LOCK:
        return max(0.0, _COOLDOWN_UNTIL - time.monotonic())


def backoff_seconds(attempt: int, status: int | None = None) -> float:
    if status == 418:
        return min(30.0, 5.0 * (attempt + 1))
    return min(4.0, 0.8 * (attempt + 1))
