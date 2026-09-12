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
_LAST_LIMIT_STATUS: int | None = None
_LAST_LIMITED_AT: float | None = None
_LIMIT_EVENT_COUNT = 0


def throttle(min_interval: float = 0.20) -> None:
    """每次请求前调用，保证相邻请求至少间隔 min_interval 秒。"""
    global _LAST_REQUEST_TS
    with _LOCK:
        now = time.monotonic()
        wait = _LAST_REQUEST_TS + min_interval - now
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_TS = time.monotonic()


def note_rate_limit(cooldown_seconds: float, *, status: int | None = None) -> None:
    """按服务端建议记录公开行情全局冷却，保护共享出口 IP。"""
    global _COOLDOWN_UNTIL, _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT
    with _LOCK:
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + max(0.0, float(cooldown_seconds)))
        _LAST_LIMIT_STATUS = int(status) if status is not None else None
        _LAST_LIMITED_AT = time.time()
        _LIMIT_EVENT_COUNT += 1


def note_418(cooldown_seconds: float = 480.0) -> None:
    """记录一次 HTTP 418，触发全局冷却（默认 8 分钟）。"""
    note_rate_limit(cooldown_seconds, status=418)


def cooldown_remaining() -> float:
    """返回剩余冷却秒数；0 表示当前不在冷却期。"""
    global _COOLDOWN_UNTIL
    with _LOCK:
        return max(0.0, _COOLDOWN_UNTIL - time.monotonic())


def rate_limit_status() -> dict[str, float | int | bool | None]:
    """返回脱敏的进程内限流状态，供健康页解释扫描降级。"""
    with _LOCK:
        remaining = max(0.0, _COOLDOWN_UNTIL - time.monotonic())
        return {
            "active": remaining > 0,
            "remaining_seconds": round(remaining, 3),
            "last_status": _LAST_LIMIT_STATUS,
            "last_limited_at": _LAST_LIMITED_AT,
            "event_count": _LIMIT_EVENT_COUNT,
        }


def _reset_for_test() -> None:
    """仅供隔离回归清空进程内时钟；生产代码不得调用。"""
    global _LAST_REQUEST_TS, _COOLDOWN_UNTIL
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT
    with _LOCK:
        _LAST_REQUEST_TS = 0.0
        _COOLDOWN_UNTIL = 0.0
        _LAST_LIMIT_STATUS = None
        _LAST_LIMITED_AT = None
        _LIMIT_EVENT_COUNT = 0


def backoff_seconds(attempt: int, status: int | None = None) -> float:
    if status == 418:
        return min(30.0, 5.0 * (attempt + 1))
    return min(4.0, 0.8 * (attempt + 1))
