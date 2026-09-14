"""Binance 公共 REST 的共享权重预算与 429/418 全局熔断。

这里保存的是同一出口 IP 的公共预算状态，而不是某个业务模块的状态。策略扫描、
市场简报、市场池和只读情报必须共享本模块，禁止各自实现重试或固定 sleep。

本模块只负责预算和状态，不负责发送 HTTP。真正的请求临界区位于
``data.binance_transport``，保证收到 429/418 与唤醒下一个排队请求之间不会穿插
新的 HTTP 请求。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0
_COOLDOWN_UNTIL = 0.0  # 单调时钟（time.monotonic）下的冷却截止时刻
_LAST_LIMIT_STATUS: int | None = None
_LAST_LIMITED_AT: float | None = None
_LIMIT_EVENT_COUNT = 0
_STATE_FILE: Path | None = None

# USD-M 当前公开额度由 exchangeInfo/响应头校正；2400/min 只是没有响应头时的保守基线。
_WEIGHT_LIMIT_1M = max(1, int(os.getenv("MTA_BINANCE_WEIGHT_LIMIT_1M", "2400")))
# 只使用官方额度的 25% 做本项目本地排队预算，为账户对账、保护单及共享出口流量留余量。
_LOCAL_WEIGHT_BUDGET_1M = max(60, int(os.getenv("MTA_BINANCE_WEIGHT_BUDGET_1M", "600")))
_BACKGROUND_DEFER_RATIO = 0.75
_NORMAL_DEFER_RATIO = 0.90
_NEXT_REQUEST_AT = 0.0
_OBSERVED_WEIGHT_1M: int | None = None
_OBSERVED_WEIGHT_AT: float | None = None
_OBSERVED_MINUTE: int | None = None
_REQUEST_COUNT = 0
_REQUEST_WEIGHT = 0
_BLOCKED_COUNT = 0
_REQUESTS_BY_PATH: Counter[str] = Counter()


class PublicMarketCooldown(RuntimeError):
    """公共行情处于交易所冷却窗口，调用方必须使用缓存或安全降级。"""


class PublicMarketBudgetDeferred(RuntimeError):
    """当前分钟预算过高，低优先级调用被主动延迟。"""


def configure_rate_limit_budget(*, weight_limit_1m: int = 2400,
                                local_budget_1m: int = 600,
                                background_defer_ratio: float = 0.75,
                                normal_defer_ratio: float = 0.90) -> None:
    """从项目配置载入预算；不会清除正在生效的冷却或观测值。"""
    global _WEIGHT_LIMIT_1M, _LOCAL_WEIGHT_BUDGET_1M
    global _BACKGROUND_DEFER_RATIO, _NORMAL_DEFER_RATIO
    with _LOCK:
        _WEIGHT_LIMIT_1M = max(1, int(weight_limit_1m))
        _LOCAL_WEIGHT_BUDGET_1M = max(60, int(local_budget_1m))
        _BACKGROUND_DEFER_RATIO = min(0.95, max(0.10, float(background_defer_ratio)))
        _NORMAL_DEFER_RATIO = min(0.99, max(_BACKGROUND_DEFER_RATIO,
                                           float(normal_defer_ratio)))


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


def _refresh_observed_window_locked() -> None:
    """Binance 分钟窗口按自然分钟重置；过期响应头不能阻塞下一分钟。"""
    global _OBSERVED_WEIGHT_1M, _OBSERVED_MINUTE
    minute = int(time.time() // 60)
    if _OBSERVED_MINUTE is not None and minute != _OBSERVED_MINUTE:
        _OBSERVED_WEIGHT_1M = None
        _OBSERVED_MINUTE = minute


def check_available(priority: str = "normal") -> None:
    """发送前检查硬冷却和响应头水位，不执行等待。"""
    global _BLOCKED_COUNT
    with _LOCK:
        remaining = max(0.0, _COOLDOWN_UNTIL - time.monotonic())
        if remaining > 0:
            _BLOCKED_COUNT += 1
            raise PublicMarketCooldown(
                f"Binance 公开行情冷却中（剩余 {remaining:.0f}s），本轮跳过请求"
            )
        _refresh_observed_window_locked()
        if _OBSERVED_WEIGHT_1M is None:
            return
        utilization = _OBSERVED_WEIGHT_1M / max(1, _WEIGHT_LIMIT_1M)
        normalized = str(priority or "normal").lower()
        # 情报和全市场扫描可以丢弃本轮；普通决策数据在 90% 前仍可排队。
        if utilization >= _BACKGROUND_DEFER_RATIO and normalized == "background":
            _BLOCKED_COUNT += 1
            raise PublicMarketBudgetDeferred(
                f"Binance 公开行情权重已用 {utilization:.0%}，后台采集延后"
            )
        if utilization >= _NORMAL_DEFER_RATIO and normalized != "critical":
            _BLOCKED_COUNT += 1
            raise PublicMarketBudgetDeferred(
                f"Binance 公开行情权重已用 {utilization:.0%}，非关键请求延后"
            )


def throttle(min_interval: float = 0.20, *, weight: int = 1,
             priority: str = "normal") -> None:
    """按端点权重平滑排队，并在等待前后检查全局熔断。

    ``min_interval`` 保留给旧调用方；新调用应传入端点 ``weight``。按本地 600
    weight/min 预算计算时，权重 5 的大 K 线请求至少占用约 0.5 秒的发送间隔。
    """
    global _LAST_REQUEST_TS, _NEXT_REQUEST_AT
    check_available(priority)
    normalized_weight = max(1, int(weight))
    with _LOCK:
        now = time.monotonic()
        weighted_interval = 60.0 * normalized_weight / _LOCAL_WEIGHT_BUDGET_1M
        interval = max(float(min_interval), weighted_interval)
        send_at = max(now, _NEXT_REQUEST_AT)
        _NEXT_REQUEST_AT = send_at + interval
        _LAST_REQUEST_TS = send_at
    wait = send_at - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    # 等待期间其他请求可能已经收到 429/418。
    check_available(priority)


def observe_response(headers: Mapping[str, Any] | None, *, path: str,
                     weight: int) -> None:
    """记录实际请求和 Binance 返回的 IP 分钟权重。"""
    global _OBSERVED_WEIGHT_1M, _OBSERVED_WEIGHT_AT, _OBSERVED_MINUTE
    global _REQUEST_COUNT, _REQUEST_WEIGHT
    normalized_path = str(path or "unknown").split("?", 1)[0]
    used: int | None = None
    for key, value in dict(headers or {}).items():
        if str(key).upper() == "X-MBX-USED-WEIGHT-1M":
            try:
                used = max(0, int(float(value)))
            except (TypeError, ValueError):
                used = None
            break
    with _LOCK:
        _REQUEST_COUNT += 1
        _REQUEST_WEIGHT += max(1, int(weight))
        _REQUESTS_BY_PATH[normalized_path] += 1
        if used is not None:
            _OBSERVED_WEIGHT_1M = used
            _OBSERVED_WEIGHT_AT = time.time()
            _OBSERVED_MINUTE = int(time.time() // 60)


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


def rate_limit_status() -> dict[str, Any]:
    """返回脱敏的进程内限流状态，供健康页解释扫描降级。"""
    with _LOCK:
        _refresh_observed_window_locked()
        remaining = max(0.0, _COOLDOWN_UNTIL - time.monotonic())
        utilization = ((_OBSERVED_WEIGHT_1M / max(1, _WEIGHT_LIMIT_1M))
                       if _OBSERVED_WEIGHT_1M is not None else None)
        return {
            "active": remaining > 0,
            "remaining_seconds": round(remaining, 3),
            "last_status": _LAST_LIMIT_STATUS,
            "last_limited_at": _LAST_LIMITED_AT,
            "event_count": _LIMIT_EVENT_COUNT,
            "observed_weight_1m": _OBSERVED_WEIGHT_1M,
            "weight_limit_1m": _WEIGHT_LIMIT_1M,
            "weight_utilization": round(utilization, 4) if utilization is not None else None,
            "local_budget_1m": _LOCAL_WEIGHT_BUDGET_1M,
            "requests": _REQUEST_COUNT,
            "estimated_weight": _REQUEST_WEIGHT,
            "blocked_requests": _BLOCKED_COUNT,
            "requests_by_path": dict(_REQUESTS_BY_PATH.most_common(12)),
            "observed_weight_at": _OBSERVED_WEIGHT_AT,
        }


def _reset_for_test() -> None:
    """仅供隔离回归清空进程内时钟；生产代码不得调用。"""
    global _LAST_REQUEST_TS, _COOLDOWN_UNTIL, _NEXT_REQUEST_AT
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT, _STATE_FILE
    global _OBSERVED_WEIGHT_1M, _OBSERVED_WEIGHT_AT, _OBSERVED_MINUTE
    global _REQUEST_COUNT, _REQUEST_WEIGHT, _BLOCKED_COUNT
    with _LOCK:
        _LAST_REQUEST_TS = 0.0
        _NEXT_REQUEST_AT = 0.0
        _COOLDOWN_UNTIL = 0.0
        _LAST_LIMIT_STATUS = None
        _LAST_LIMITED_AT = None
        _LIMIT_EVENT_COUNT = 0
        _STATE_FILE = None
        _OBSERVED_WEIGHT_1M = None
        _OBSERVED_WEIGHT_AT = None
        _OBSERVED_MINUTE = None
        _REQUEST_COUNT = 0
        _REQUEST_WEIGHT = 0
        _BLOCKED_COUNT = 0
        _REQUESTS_BY_PATH.clear()


def backoff_seconds(attempt: int, status: int | None = None) -> float:
    if status == 418:
        return min(30.0, 5.0 * (attempt + 1))
    return min(4.0, 0.8 * (attempt + 1))
