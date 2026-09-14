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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

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
_RESPONSE_STATUS_COUNTS: Counter[str] = Counter()
_REQUEST_EVENTS: list[dict[str, Any]] = []
_STATE_LOCK_FILE: Path | None = None
_TELEMETRY_EVENT_LIMIT = 2000
_DATA_WINDOW_SECONDS = 300
_DATA_REQUEST_BUDGET_5M = 800
_DATA_BLOCKED_COUNT = 0


class PublicMarketCooldown(RuntimeError):
    """公共行情处于交易所冷却窗口，调用方必须使用缓存或安全降级。"""


class PublicMarketBudgetDeferred(RuntimeError):
    """当前分钟预算过高，低优先级调用被主动延迟。"""


class ExecutionRateLimited(RuntimeError):
    """签名执行请求收到交易所限流/封禁；调用方不得盲目重试。"""


def configure_rate_limit_budget(*, weight_limit_1m: int = 2400,
                                local_budget_1m: int = 600,
                                background_defer_ratio: float = 0.75,
                                normal_defer_ratio: float = 0.90,
                                data_window_seconds: int = 300,
                                data_request_budget_5m: int = 800) -> None:
    """从项目配置载入预算；不会清除正在生效的冷却或观测值。"""
    global _WEIGHT_LIMIT_1M, _LOCAL_WEIGHT_BUDGET_1M
    global _BACKGROUND_DEFER_RATIO, _NORMAL_DEFER_RATIO
    global _DATA_WINDOW_SECONDS, _DATA_REQUEST_BUDGET_5M
    with _LOCK:
        _WEIGHT_LIMIT_1M = max(1, int(weight_limit_1m))
        _LOCAL_WEIGHT_BUDGET_1M = max(60, int(local_budget_1m))
        _BACKGROUND_DEFER_RATIO = min(0.95, max(0.10, float(background_defer_ratio)))
        _NORMAL_DEFER_RATIO = min(0.99, max(_BACKGROUND_DEFER_RATIO,
                                           float(normal_defer_ratio)))
        _DATA_WINDOW_SECONDS = max(60, int(data_window_seconds))
        _DATA_REQUEST_BUDGET_5M = max(1, int(data_request_budget_5m))


@contextmanager
def _state_file_lock(path: Path | None):
    """跨 Python 进程保护限流状态文件；锁文件自身不会进入恢复包。"""
    if path is None:
        yield None
        return
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        yield handle
    finally:
        if locked and os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _read_state_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with _state_file_lock(path):
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            return payload if isinstance(payload, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def _event_time(event: Mapping[str, Any]) -> float:
    try:
        return float(event.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _prune_events(events: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    current = float(now if now is not None else time.time())
    cutoff = current - max(60, _DATA_WINDOW_SECONDS)
    valid = [dict(event) for event in events if _event_time(event) >= cutoff]
    return valid[-_TELEMETRY_EVENT_LIMIT:]


def _write_payload_locked(payload: dict[str, Any], path: Path) -> None:
    """调用方已持有同名锁时原子替换状态文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f"{path.stem}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _state_snapshot() -> tuple[Path | None, dict[str, Any]]:
    """取出不含敏感信息的内存状态，避免在文件锁内持有业务锁。"""
    with _LOCK:
        remaining = max(0.0, _COOLDOWN_UNTIL - time.monotonic())
        payload = {
            "cooldown_until": time.time() + remaining,
            "last_status": _LAST_LIMIT_STATUS,
            "last_limited_at": _LAST_LIMITED_AT,
            "event_count": _LIMIT_EVENT_COUNT,
            "request_events": _prune_events(list(_REQUEST_EVENTS)),
        }
        return _STATE_FILE, payload


def _persist_state() -> None:
    """跨进程合并并保存冷却与有界请求遥测。"""
    path, local = _state_snapshot()
    if path is None:
        return
    try:
        with _state_file_lock(path):
            existing: dict[str, Any] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8-sig"))
                    existing = loaded if isinstance(loaded, dict) else {}
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    existing = {}
            existing_events = [event for event in existing.get("request_events", [])
                               if isinstance(event, dict)]
            by_id = {str(event.get("id")): event for event in existing_events
                     if event.get("id")}
            for event in local.get("request_events", []):
                if event.get("id"):
                    by_id[str(event["id"])] = event
            merged = dict(existing)
            merged.update({key: local[key] for key in (
                "cooldown_until", "last_status", "last_limited_at", "event_count")
                if local.get(key) is not None})
            existing_cooldown = float(existing.get("cooldown_until") or 0.0)
            merged["cooldown_until"] = max(existing_cooldown,
                                            float(local.get("cooldown_until") or 0.0))
            merged["event_count"] = max(int(existing.get("event_count") or 0),
                                         int(local.get("event_count") or 0))
            merged["request_events"] = _prune_events(list(by_id.values()))
            _write_payload_locked(merged, path)
    except OSError:
        # 状态文件只是跨重启保护；磁盘故障不能改写已经做出的内存决策。
        return


def _refresh_external_state() -> None:
    """每次发送前读取其它进程刚写入的冷却与预算事件。"""
    path = _STATE_FILE
    if path is None:
        return
    payload = _read_state_file(path)
    events = [event for event in payload.get("request_events", [])
              if isinstance(event, dict)]
    with _LOCK:
        global _COOLDOWN_UNTIL, _LAST_LIMIT_STATUS, _LAST_LIMITED_AT
        global _LIMIT_EVENT_COUNT, _REQUEST_EVENTS
        try:
            persisted_until = float(payload.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            persisted_until = 0.0
        remaining = max(0.0, persisted_until - time.time())
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + remaining)
        status = payload.get("last_status")
        if status is not None:
            try:
                _LAST_LIMIT_STATUS = int(status)
            except (TypeError, ValueError):
                pass
        limited_at = payload.get("last_limited_at")
        if limited_at is not None:
            try:
                _LAST_LIMITED_AT = max(float(_LAST_LIMITED_AT or 0.0), float(limited_at))
            except (TypeError, ValueError):
                pass
        try:
            persisted_events = int(payload.get("event_count") or 0)
        except (TypeError, ValueError):
            persisted_events = 0
        _LIMIT_EVENT_COUNT = max(_LIMIT_EVENT_COUNT, persisted_events)
        _REQUEST_EVENTS = _prune_events(events)


def configure_rate_limit_state(path: Path) -> None:
    """绑定项目运行状态文件，并恢复尚未到期的公共行情冷却。"""
    global _STATE_FILE, _STATE_LOCK_FILE, _COOLDOWN_UNTIL
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT
    global _REQUEST_EVENTS
    state_path = Path(path)
    with _LOCK:
        _STATE_FILE = state_path
        _STATE_LOCK_FILE = state_path.with_name(f"{state_path.name}.lock")
        payload = _read_state_file(state_path)
        try:
            remaining = max(0.0, float(payload.get("cooldown_until") or 0.0) - time.time())
            _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + remaining)
            status = payload.get("last_status")
            _LAST_LIMIT_STATUS = int(status) if status is not None else None
            limited_at = payload.get("last_limited_at")
            _LAST_LIMITED_AT = float(limited_at) if limited_at is not None else None
            _LIMIT_EVENT_COUNT = max(_LIMIT_EVENT_COUNT, int(payload.get("event_count") or 0))
            _REQUEST_EVENTS = _prune_events([
                event for event in payload.get("request_events", [])
                if isinstance(event, dict)
            ])
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


def _data_group_count(events: list[dict[str, Any]], group: str) -> int:
    return sum(1 for event in events if str(event.get("group") or "") == group)


def check_available(priority: str = "normal", *, endpoint_group: str | None = None) -> None:
    """发送前检查硬冷却、分钟水位和 5 分钟数据端点预算，不执行等待。"""
    global _BLOCKED_COUNT, _DATA_BLOCKED_COUNT
    _refresh_external_state()
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
        if (endpoint_group == "futures_data_5m"
                and normalized != "critical"
                and _data_group_count(_REQUEST_EVENTS, endpoint_group)
                >= _DATA_REQUEST_BUDGET_5M):
            _BLOCKED_COUNT += 1
            _DATA_BLOCKED_COUNT += 1
            raise PublicMarketBudgetDeferred(
                f"Binance futures/data 五分钟预算已用尽（{_DATA_REQUEST_BUDGET_5M}），请求延后"
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


def reserve_request(path: str, *, weight: int = 1, priority: str = "normal",
                    endpoint_group: str = "general", source: str = "public_rest",
                    environment: str = "public") -> str:
    """在真正发 HTTP 前原子预留一次请求并写入脱敏遥测。

    预留发生在发送前，网络超时也会计入交易所额度；这避免“失败请求不计数”造成
    多进程一起穿透 1000/5m 数据端点窗口。critical 只绕过本地软预算，不绕过 418/429
    硬冷却，避免把已被封禁的出口继续打穿。
    """
    global _REQUEST_COUNT, _REQUEST_WEIGHT, _BLOCKED_COUNT
    global _DATA_BLOCKED_COUNT, _COOLDOWN_UNTIL
    global _REQUEST_EVENTS
    normalized_path = str(path or "unknown").split("?", 1)[0]
    normalized_priority = str(priority or "normal").lower()
    event = {
        "id": uuid4().hex,
        "at": time.time(),
        "path": normalized_path,
        "group": str(endpoint_group or "general"),
        "weight": max(1, int(weight)),
        "priority": normalized_priority,
        "source": str(source or "unknown")[:32],
        "environment": str(environment or "unknown")[:16],
        "status": None,
    }
    _refresh_external_state()
    path_state = _STATE_FILE
    if path_state is not None:
        try:
            with _state_file_lock(path_state):
                payload: dict[str, Any] = {}
                if path_state.exists():
                    try:
                        payload = json.loads(path_state.read_text(encoding="utf-8-sig"))
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        payload = {}
                now = time.time()
                remaining = max(0.0, float(payload.get("cooldown_until") or 0.0) - now)
                if remaining > 0:
                    with _LOCK:
                        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL,
                                              time.monotonic() + remaining)
                        _BLOCKED_COUNT += 1
                    raise PublicMarketCooldown(
                        f"Binance 公开行情冷却中（剩余 {remaining:.0f}s），本轮跳过请求"
                    )
                events = _prune_events([
                    item for item in payload.get("request_events", [])
                    if isinstance(item, dict)
                ], now)
                group = str(endpoint_group or "general")
                if (group == "futures_data_5m" and normalized_priority != "critical"
                        and _data_group_count(events, group) >= _DATA_REQUEST_BUDGET_5M):
                    with _LOCK:
                        _BLOCKED_COUNT += 1
                        _DATA_BLOCKED_COUNT += 1
                    raise PublicMarketBudgetDeferred(
                        f"Binance futures/data 五分钟预算已用尽（{_DATA_REQUEST_BUDGET_5M}），请求延后"
                    )
                events.append(event)
                payload["request_events"] = _prune_events(events, now)
                _write_payload_locked(payload, path_state)
        except (PublicMarketCooldown, PublicMarketBudgetDeferred):
            raise
        except OSError:
            # 不能因为遥测文件不可写而丢掉一次已通过检查的业务请求。
            pass
    with _LOCK:
        _REQUEST_EVENTS = _prune_events(_REQUEST_EVENTS + [event])
        _REQUEST_COUNT += 1
        _REQUEST_WEIGHT += event["weight"]
        _REQUESTS_BY_PATH[normalized_path] += 1
    return str(event["id"])


def observe_response(headers: Mapping[str, Any] | None, *, path: str,
                     weight: int, request_id: str | None = None,
                     status: int | None = None) -> None:
    """记录响应头、状态码和 Binance 返回的 IP 分钟权重。

    没有 request_id 时兼容旧调用方，将其视为一次未预留的请求；统一传输入口会
    传入 request_id，避免一次请求被遥测计数两次。
    """
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
        if request_id is None:
            event = {
                "id": uuid4().hex, "at": time.time(), "path": normalized_path,
                "group": "general", "weight": max(1, int(weight)),
                "priority": "unknown", "source": "unclassified",
                "environment": "unknown", "status": status,
            }
            _REQUEST_EVENTS[:] = _prune_events(_REQUEST_EVENTS + [event])
            _REQUEST_COUNT += 1
            _REQUEST_WEIGHT += max(1, int(weight))
            _REQUESTS_BY_PATH[normalized_path] += 1
        if status is not None:
            _RESPONSE_STATUS_COUNTS[str(int(status))] += 1
        if used is not None:
            _OBSERVED_WEIGHT_1M = used
            _OBSERVED_WEIGHT_AT = time.time()
            _OBSERVED_MINUTE = int(time.time() // 60)
    if request_id is not None:
        _update_request_event(request_id, status)


def _update_request_event(request_id: str, status: int | None) -> None:
    """把响应状态写回对应请求事件，且不暴露查询参数或凭据。"""
    with _LOCK:
        for event in _REQUEST_EVENTS:
            if str(event.get("id")) == str(request_id):
                event["status"] = int(status) if status is not None else None
                break
    path = _STATE_FILE
    if path is None:
        return
    try:
        with _state_file_lock(path):
            payload = {}
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
            events = [event for event in payload.get("request_events", [])
                      if isinstance(event, dict)]
            for event in events:
                if str(event.get("id")) == str(request_id):
                    event["status"] = int(status) if status is not None else None
                    break
            payload["request_events"] = _prune_events(events)
            _write_payload_locked(payload, path)
    except OSError:
        return


def note_rate_limit(cooldown_seconds: float, *, status: int | None = None) -> None:
    """按服务端建议记录公开行情全局冷却，保护共享出口 IP。"""
    global _COOLDOWN_UNTIL, _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT
    with _LOCK:
        _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + max(0.0, float(cooldown_seconds)))
        _LAST_LIMIT_STATUS = int(status) if status is not None else None
        _LAST_LIMITED_AT = time.time()
        _LIMIT_EVENT_COUNT += 1
    _persist_state()


def note_418(cooldown_seconds: float = 480.0) -> None:
    """记录一次 HTTP 418，触发全局冷却（默认 8 分钟）。"""
    note_rate_limit(cooldown_seconds, status=418)


def cooldown_remaining() -> float:
    """返回剩余冷却秒数；0 表示当前不在冷却期。"""
    global _COOLDOWN_UNTIL
    _refresh_external_state()
    with _LOCK:
        return max(0.0, _COOLDOWN_UNTIL - time.monotonic())


def rate_limit_status() -> dict[str, Any]:
    """返回脱敏的进程内限流状态，供健康页解释扫描降级。"""
    _refresh_external_state()
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
            "data_endpoint_window": {
                "window_seconds": _DATA_WINDOW_SECONDS,
                "group": "futures_data_5m",
                "budget": _DATA_REQUEST_BUDGET_5M,
                "requests": _data_group_count(_REQUEST_EVENTS, "futures_data_5m"),
                "blocked_requests": _DATA_BLOCKED_COUNT,
            },
            "requests_by_path": dict(_REQUESTS_BY_PATH.most_common(12)),
            "responses_by_status": dict(_RESPONSE_STATUS_COUNTS),
            "telemetry": {
                "retained_events": len(_REQUEST_EVENTS),
                "max_events": _TELEMETRY_EVENT_LIMIT,
            },
            "observed_weight_at": _OBSERVED_WEIGHT_AT,
        }


def _reset_for_test() -> None:
    """仅供隔离回归清空进程内时钟；生产代码不得调用。"""
    global _LAST_REQUEST_TS, _COOLDOWN_UNTIL, _NEXT_REQUEST_AT
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT, _STATE_FILE
    global _OBSERVED_WEIGHT_1M, _OBSERVED_WEIGHT_AT, _OBSERVED_MINUTE
    global _REQUEST_COUNT, _REQUEST_WEIGHT, _BLOCKED_COUNT
    global _RESPONSE_STATUS_COUNTS, _REQUEST_EVENTS, _STATE_LOCK_FILE
    global _DATA_BLOCKED_COUNT
    with _LOCK:
        _LAST_REQUEST_TS = 0.0
        _NEXT_REQUEST_AT = 0.0
        _COOLDOWN_UNTIL = 0.0
        _LAST_LIMIT_STATUS = None
        _LAST_LIMITED_AT = None
        _LIMIT_EVENT_COUNT = 0
        _STATE_FILE = None
        _STATE_LOCK_FILE = None
        _OBSERVED_WEIGHT_1M = None
        _OBSERVED_WEIGHT_AT = None
        _OBSERVED_MINUTE = None
        _REQUEST_COUNT = 0
        _REQUEST_WEIGHT = 0
        _BLOCKED_COUNT = 0
        _REQUESTS_BY_PATH.clear()
        _RESPONSE_STATUS_COUNTS.clear()
        _REQUEST_EVENTS.clear()
        _DATA_BLOCKED_COUNT = 0


def backoff_seconds(attempt: int, status: int | None = None) -> float:
    if status == 418:
        return min(30.0, 5.0 * (attempt + 1))
    return min(4.0, 0.8 * (attempt + 1))
