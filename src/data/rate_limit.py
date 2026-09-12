"""公共行情请求节流 + 418 全局冷却：避免 Binance HTTP 418 IP 限流。

两个层面：
1. throttle(min_interval)：单进程内相邻请求至少间隔 min_interval 秒；
2. 418 全局冷却：任何公开行情请求一旦命中 HTTP 418，触发进程级冷却，
   冷却期内所有公开行情请求立即短路失败（不再发请求），由上层走缓存/降级。
   这比"逐请求退避后继续撞墙"更能保护 IP 额度。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0
_COOLDOWN_UNTIL = 0.0  # 单调时钟（time.monotonic）下的冷却截止时刻
_LAST_LIMIT_STATUS: int | None = None
_LAST_LIMITED_AT: float | None = None
_LIMIT_EVENT_COUNT = 0
_STATE_FILE: Path | None = None


def _save_state_locked() -> None:
    """调用方持锁时原子保存跨重启冷却截止时间；未配置状态路径则只使用内存。"""
    if _STATE_FILE is None:
        return
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    remaining = max(0.0, _COOLDOWN_UNTIL - time.monotonic())
    payload = {
        "cooldown_until": time.time() + remaining,
        "last_status": _LAST_LIMIT_STATUS,
        "last_limited_at": _LAST_LIMITED_AT,
        "event_count": _LIMIT_EVENT_COUNT,
    }
    temp = _STATE_FILE.with_name(
        f"{_STATE_FILE.stem}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, _STATE_FILE)


def configure_rate_limit_state(path: Path) -> None:
    """绑定项目运行状态文件，并恢复尚未到期的公共行情冷却。"""
    global _STATE_FILE, _COOLDOWN_UNTIL
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT
    state_path = Path(path)
    with _LOCK:
        _STATE_FILE = state_path
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8-sig"))
            remaining = max(0.0, float(payload.get("cooldown_until") or 0.0) - time.time())
            _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + remaining)
            status = payload.get("last_status")
            _LAST_LIMIT_STATUS = int(status) if status is not None else None
            limited_at = payload.get("last_limited_at")
            _LAST_LIMITED_AT = float(limited_at) if limited_at is not None else None
            _LIMIT_EVENT_COUNT = max(_LIMIT_EVENT_COUNT, int(payload.get("event_count") or 0))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            # 公共行情状态损坏不能影响交易保护；下一次真实限流会覆盖为合法原子文件。
            return


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
        try:
            _save_state_locked()
        except OSError:
            # 状态文件只是跨重启保护；磁盘异常不能吞掉内存冷却或改变原HTTP错误语义。
            pass


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
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT, _STATE_FILE
    with _LOCK:
        _LAST_REQUEST_TS = 0.0
        _COOLDOWN_UNTIL = 0.0
        _LAST_LIMIT_STATUS = None
        _LAST_LIMITED_AT = None
        _LIMIT_EVENT_COUNT = 0
        _STATE_FILE = None


def backoff_seconds(attempt: int, status: int | None = None) -> float:
    if status == 418:
        return min(30.0, 5.0 * (attempt + 1))
    return min(4.0, 0.8 * (attempt + 1))
