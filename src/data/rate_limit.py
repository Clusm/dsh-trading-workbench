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
import re
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
_COORDINATION_AVAILABLE = True
_COORDINATION_ERROR: str | None = None

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
_OBSERVED_ORDER_COUNT_10S: int | None = None
_OBSERVED_ORDER_COUNT_1M: int | None = None
_OBSERVED_ORDER_AT: float | None = None
_REQUEST_COUNT = 0
_REQUEST_WEIGHT = 0
_ATTEMPT_COUNT = 0
_RESPONSE_COUNT = 0
_FAILURE_COUNT = 0
_BLOCKED_COUNT = 0
_REQUESTS_BY_PATH: Counter[str] = Counter()
_RESPONSE_STATUS_COUNTS: Counter[str] = Counter()
_REQUEST_EVENTS: list[dict[str, Any]] = []
_STATE_LOCK_FILE: Path | None = None
_TELEMETRY_EVENT_LIMIT = 2000
_DATA_WINDOW_SECONDS = 300
_DATA_REQUEST_BUDGET_5M = 800
_DATA_BLOCKED_COUNT = 0


def _safe_telemetry_text(value: Any, limit: int = 180) -> str | None:
    """清理可进入审计遥测的错误摘要，绝不保存凭据或查询参数。"""
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    text = re.sub(
        r"(?i)(api[-_ ]?key|secret|signature|listen[-_ ]?key|token|password)"
        r"\s*[=:]\s*[^&\s,;]+",
        r"\1=<redacted>", text,
    )
    # URL 查询串可能没有常见字段名；只保留路径/错误摘要，避免把签名串落盘。
    if "?" in text:
        text = text.split("?", 1)[0]
    return text[:limit]


def _header_int(headers: Mapping[str, Any] | None, name: str) -> int | None:
    """读取指定的非敏感整数响应头；忽略缺失、异常和负值。"""
    expected = str(name).upper()
    for key, value in dict(headers or {}).items():
        if str(key).upper() != expected:
            continue
        raw = str(value or "").strip()
        if not raw.isdigit():
            return None
        return int(raw)
    return None


def _safe_int(value: Any, default: int = 0) -> int:
    """将状态文件中的计数安全转换为非负整数。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


def _merge_observed_order_payload(payload: dict[str, Any], latest: Mapping[str, Any]) -> None:
    """按观测时间合并订单计数，避免旧进程覆盖新窗口的水位。"""
    latest_at = latest.get("observed_order_at")
    if latest_at is None:
        return
    try:
        latest_at = float(latest_at)
    except (TypeError, ValueError):
        return
    try:
        existing_at = float(payload.get("observed_order_at") or 0.0)
    except (TypeError, ValueError):
        existing_at = 0.0
    if latest_at < existing_at:
        return
    payload["observed_order_count_10s"] = latest.get("observed_order_count_10s")
    payload["observed_order_count_1m"] = latest.get("observed_order_count_1m")
    payload["observed_order_at"] = latest_at


class PublicMarketCooldown(RuntimeError):
    """公共行情处于交易所冷却窗口，调用方必须使用缓存或安全降级。"""


class PublicMarketBudgetDeferred(RuntimeError):
    """当前分钟预算过高，低优先级调用被主动延迟。"""


class ExecutionRateLimited(RuntimeError):
    """签名执行请求收到交易所限流/封禁；调用方不得盲目重试。"""


class RateLimitCoordinationUnavailable(RuntimeError):
    """跨进程限流协调文件不可读写；普通请求必须暂停。"""


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


def _mark_coordination(available: bool, error: Any = None) -> None:
    """记录协调文件状态；错误摘要不保存路径中的敏感信息。"""
    global _COORDINATION_AVAILABLE, _COORDINATION_ERROR
    with _LOCK:
        _COORDINATION_AVAILABLE = bool(available)
        _COORDINATION_ERROR = None if available else _safe_telemetry_text(error, 120)


def coordination_available() -> bool:
    """返回跨进程限流协调是否可用。"""
    with _LOCK:
        return bool(_COORDINATION_AVAILABLE)


def _state_snapshot() -> tuple[Path | None, dict[str, Any]]:
    """取出不含敏感信息的内存状态，避免在文件锁内持有业务锁。"""
    with _LOCK:
        remaining = max(0.0, _COOLDOWN_UNTIL - time.monotonic())
        payload = {
            "cooldown_until": time.time() + remaining,
            "last_status": _LAST_LIMIT_STATUS,
            "last_limited_at": _LAST_LIMITED_AT,
            "event_count": _LIMIT_EVENT_COUNT,
            "attempt_count": _ATTEMPT_COUNT,
            "response_count": _RESPONSE_COUNT,
            "failure_count": _FAILURE_COUNT,
            "responses_by_status": dict(_RESPONSE_STATUS_COUNTS),
            "observed_order_count_10s": _OBSERVED_ORDER_COUNT_10S,
            "observed_order_count_1m": _OBSERVED_ORDER_COUNT_1M,
            "observed_order_at": _OBSERVED_ORDER_AT,
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
            merged["event_count"] = max(_safe_int(existing.get("event_count")),
                                         _safe_int(local.get("event_count")))
            for key in ("attempt_count", "response_count", "failure_count"):
                merged[key] = max(_safe_int(existing.get(key)),
                                  _safe_int(local.get(key)))
            existing_statuses = existing.get("responses_by_status")
            existing_statuses = existing_statuses if isinstance(existing_statuses, dict) else {}
            local_statuses = local.get("responses_by_status")
            local_statuses = local_statuses if isinstance(local_statuses, dict) else {}
            merged["responses_by_status"] = {
                str(status): max(_safe_int(existing_statuses.get(status)),
                                 _safe_int(local_statuses.get(status)))
                for status in set(existing_statuses) | set(local_statuses)
            }
            _merge_observed_order_payload(merged, local)
            merged["request_events"] = _prune_events(list(by_id.values()))
            _write_payload_locked(merged, path)
    except OSError as exc:
        # 状态文件只是跨重启保护；磁盘故障不能回滚已经发出的请求，
        # 但必须暴露协调不可用，让后续普通请求停止发送。
        _mark_coordination(False, f"state persist: {type(exc).__name__}: {exc}")
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
        global _ATTEMPT_COUNT, _RESPONSE_COUNT, _FAILURE_COUNT
        global _RESPONSE_STATUS_COUNTS
        global _OBSERVED_ORDER_COUNT_10S, _OBSERVED_ORDER_COUNT_1M, _OBSERVED_ORDER_AT
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
        for key, target in (("attempt_count", "_ATTEMPT_COUNT"),
                            ("response_count", "_RESPONSE_COUNT"),
                            ("failure_count", "_FAILURE_COUNT")):
            value = _safe_int(payload.get(key))
            if target == "_ATTEMPT_COUNT":
                _ATTEMPT_COUNT = max(_ATTEMPT_COUNT, value)
            elif target == "_RESPONSE_COUNT":
                _RESPONSE_COUNT = max(_RESPONSE_COUNT, value)
            else:
                _FAILURE_COUNT = max(_FAILURE_COUNT, value)
        persisted_statuses = payload.get("responses_by_status")
        if isinstance(persisted_statuses, dict):
            for status, count in persisted_statuses.items():
                try:
                    _RESPONSE_STATUS_COUNTS[str(status)] = max(
                        _RESPONSE_STATUS_COUNTS[str(status)], _safe_int(count))
                except (TypeError, ValueError):
                    continue
        try:
            persisted_order_at = float(payload.get("observed_order_at") or 0.0)
        except (TypeError, ValueError):
            persisted_order_at = 0.0
        if persisted_order_at >= float(_OBSERVED_ORDER_AT or 0.0) and persisted_order_at:
            _OBSERVED_ORDER_COUNT_10S = (
                _safe_int(payload.get("observed_order_count_10s"))
                if payload.get("observed_order_count_10s") is not None else None
            )
            _OBSERVED_ORDER_COUNT_1M = (
                _safe_int(payload.get("observed_order_count_1m"))
                if payload.get("observed_order_count_1m") is not None else None
            )
            _OBSERVED_ORDER_AT = persisted_order_at
        _REQUEST_EVENTS = _prune_events(events)


def configure_rate_limit_state(path: Path) -> None:
    """绑定项目运行状态文件，并恢复尚未到期的公共行情冷却。"""
    global _STATE_FILE, _STATE_LOCK_FILE, _COOLDOWN_UNTIL
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT
    global _ATTEMPT_COUNT, _RESPONSE_COUNT, _FAILURE_COUNT
    global _RESPONSE_STATUS_COUNTS
    global _OBSERVED_ORDER_COUNT_10S, _OBSERVED_ORDER_COUNT_1M, _OBSERVED_ORDER_AT
    global _REQUEST_EVENTS, _COORDINATION_AVAILABLE, _COORDINATION_ERROR
    state_path = Path(path)
    coordination_error: str | None = None
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if state_path.exists():
            with _state_file_lock(state_path):
                loaded = json.loads(state_path.read_text(encoding="utf-8-sig"))
                if not isinstance(loaded, dict):
                    raise ValueError("rate-limit state must be a JSON object")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        coordination_error = f"{type(exc).__name__}: {exc}"
    with _LOCK:
        _STATE_FILE = state_path
        _STATE_LOCK_FILE = state_path.with_name(f"{state_path.name}.lock")
        _COORDINATION_AVAILABLE = coordination_error is None
        _COORDINATION_ERROR = coordination_error
        payload = _read_state_file(state_path)
        try:
            remaining = max(0.0, float(payload.get("cooldown_until") or 0.0) - time.time())
            _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic() + remaining)
            status = payload.get("last_status")
            _LAST_LIMIT_STATUS = int(status) if status is not None else None
            limited_at = payload.get("last_limited_at")
            _LAST_LIMITED_AT = float(limited_at) if limited_at is not None else None
            _LIMIT_EVENT_COUNT = max(_LIMIT_EVENT_COUNT, _safe_int(payload.get("event_count")))
            _ATTEMPT_COUNT = max(_ATTEMPT_COUNT, _safe_int(payload.get("attempt_count")))
            _RESPONSE_COUNT = max(_RESPONSE_COUNT, _safe_int(payload.get("response_count")))
            _FAILURE_COUNT = max(_FAILURE_COUNT, _safe_int(payload.get("failure_count")))
            persisted_statuses = payload.get("responses_by_status")
            if isinstance(persisted_statuses, dict):
                for status, count in persisted_statuses.items():
                    try:
                        _RESPONSE_STATUS_COUNTS[str(status)] = max(
                            _RESPONSE_STATUS_COUNTS[str(status)], _safe_int(count))
                    except (TypeError, ValueError):
                        continue
            try:
                persisted_order_at = float(payload.get("observed_order_at") or 0.0)
            except (TypeError, ValueError):
                persisted_order_at = 0.0
            if persisted_order_at >= float(_OBSERVED_ORDER_AT or 0.0) and persisted_order_at:
                _OBSERVED_ORDER_COUNT_10S = (
                    _safe_int(payload.get("observed_order_count_10s"))
                    if payload.get("observed_order_count_10s") is not None else None
                )
                _OBSERVED_ORDER_COUNT_1M = (
                    _safe_int(payload.get("observed_order_count_1m"))
                    if payload.get("observed_order_count_1m") is not None else None
                )
                _OBSERVED_ORDER_AT = persisted_order_at
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
                    environment: str = "public", purpose: str = "unknown") -> str:
    """在真正发 HTTP 前原子预留一次请求并写入脱敏遥测。

    预留发生在发送前，网络超时也会计入交易所额度；这避免“失败请求不计数”造成
    多进程一起穿透 1000/5m 数据端点窗口。critical 只绕过本地软预算，不绕过 418/429
    硬冷却，避免把已被封禁的出口继续打穿。
    """
    global _REQUEST_COUNT, _REQUEST_WEIGHT, _ATTEMPT_COUNT, _BLOCKED_COUNT
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
        "purpose": str(purpose or "unknown")[:40],
        "source": str(source or "unknown")[:32],
        "environment": str(environment or "unknown")[:16],
        "status": None,
        "outcome": "pending",
        "retry_after_seconds": None,
        "error_type": None,
        "error_message": None,
    }
    _refresh_external_state()
    path_state = _STATE_FILE
    if (path_state is not None and not coordination_available()
            and normalized_priority != "critical"):
        raise RateLimitCoordinationUnavailable(
            "RATE_COORDINATION_UNAVAILABLE：普通请求暂停，等待限流协调文件恢复"
        )
    if path_state is not None:
        try:
            with _state_file_lock(path_state):
                payload: dict[str, Any] = {}
                if path_state.exists():
                    try:
                        payload = json.loads(path_state.read_text(encoding="utf-8-sig"))
                    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        _mark_coordination(False, f"state read: {type(exc).__name__}: {exc}")
                        # 让外层统一处理：普通请求立即暂停，critical 保护/恢复
                        # 继续使用进程内保守预算，但不覆盖损坏文件。
                        raise RateLimitCoordinationUnavailable(
                            "RATE_COORDINATION_UNAVAILABLE：限流状态文件损坏"
                        ) from exc
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
                # 同一出口的两个控制台必须原子共享分钟权重。
                # 在文件锁内累计发送前的预留，超时请求同样占额度；
                # critical 保留原有保护/恢复优先级，不受软预算阻断。
                minute_weight = sum(
                    _safe_int(item.get("weight")) for item in events
                    if _event_time(item) > now - 60
                )
                if (normalized_priority != "critical"
                        and minute_weight + event["weight"] > _LOCAL_WEIGHT_BUDGET_1M):
                    with _LOCK:
                        _BLOCKED_COUNT += 1
                    raise PublicMarketBudgetDeferred(
                        f"Binance 跨进程分钟权重预算已用尽（{_LOCAL_WEIGHT_BUDGET_1M}），请求延后"
                    )
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
                try:
                    payload["attempt_count"] = int(payload.get("attempt_count") or 0) + 1
                except (TypeError, ValueError):
                    payload["attempt_count"] = 1
                _write_payload_locked(payload, path_state)
        except (PublicMarketCooldown, PublicMarketBudgetDeferred):
            raise
        except RateLimitCoordinationUnavailable:
            if normalized_priority != "critical":
                raise
            # critical 保护/恢复请求不因协调文件损坏丢弃，但不进入下面的
            # “成功写回”分支，避免将损坏文件误标为已恢复。
        except OSError as exc:
            _mark_coordination(False, f"state write: {type(exc).__name__}: {exc}")
            if normalized_priority != "critical":
                raise RateLimitCoordinationUnavailable(
                    "RATE_COORDINATION_UNAVAILABLE：限流状态文件不可写"
                ) from exc
            # critical 保护/恢复请求不因遥测文件故障丢弃，但仍受进程内硬冷却。
        else:
            _mark_coordination(True)
    with _LOCK:
        _REQUEST_EVENTS = _prune_events(_REQUEST_EVENTS + [event])
        _REQUEST_COUNT += 1
        _REQUEST_WEIGHT += event["weight"]
        _ATTEMPT_COUNT += 1
        _REQUESTS_BY_PATH[normalized_path] += 1
    return str(event["id"])


def observe_response(headers: Mapping[str, Any] | None, *, path: str,
                     weight: int, request_id: str | None = None,
                     status: int | None = None,
                     retry_after_seconds: float | None = None,
                     error_code: Any = None,
                     error_message: Any = None) -> None:
    """记录响应头、状态码、错误摘要和 Binance 返回的 IP 分钟权重。

    没有 request_id 时兼容旧调用方，将其视为一次未预留的请求；统一传输入口会
    传入 request_id，避免一次请求被遥测计数两次。响应只保存脱敏错误摘要，绝不
    保存查询参数、签名、API Key 或 listenKey。
    """
    global _OBSERVED_WEIGHT_1M, _OBSERVED_WEIGHT_AT, _OBSERVED_MINUTE
    global _OBSERVED_ORDER_COUNT_10S, _OBSERVED_ORDER_COUNT_1M, _OBSERVED_ORDER_AT
    global _REQUEST_COUNT, _REQUEST_WEIGHT, _ATTEMPT_COUNT
    normalized_path = str(path or "unknown").split("?", 1)[0]
    used: int | None = None
    for key, value in dict(headers or {}).items():
        if str(key).upper() == "X-MBX-USED-WEIGHT-1M":
            try:
                used = max(0, int(float(value)))
            except (TypeError, ValueError):
                used = None
            break
    observed_order_10s = _header_int(headers, "X-MBX-ORDER-COUNT-10S")
    observed_order_1m = _header_int(headers, "X-MBX-ORDER-COUNT-1M")
    unreserved_request = request_id is None
    if unreserved_request:
        request_id = uuid4().hex
        with _LOCK:
            event = {
                "id": request_id, "at": time.time(), "path": normalized_path,
                "group": "general", "weight": max(1, int(weight)),
                "priority": "unknown", "source": "unclassified",
                "purpose": "unknown",
                "environment": "unknown", "status": None,
                "outcome": "pending", "retry_after_seconds": None,
                "error_type": None, "error_message": None,
            }
            _REQUEST_EVENTS[:] = _prune_events(_REQUEST_EVENTS + [event])
            _REQUEST_COUNT += 1
            _REQUEST_WEIGHT += max(1, int(weight))
            _ATTEMPT_COUNT += 1
            _REQUESTS_BY_PATH[normalized_path] += 1
    with _LOCK:
        if used is not None:
            _OBSERVED_WEIGHT_1M = used
            _OBSERVED_WEIGHT_AT = time.time()
            _OBSERVED_MINUTE = int(time.time() // 60)
        if observed_order_10s is not None or observed_order_1m is not None:
            _OBSERVED_ORDER_COUNT_10S = observed_order_10s
            _OBSERVED_ORDER_COUNT_1M = observed_order_1m
            _OBSERVED_ORDER_AT = time.time()
    _update_request_event(
        request_id,
        path=normalized_path,
        weight=weight,
        status=status,
        outcome="response_error" if status is not None and status >= 400 else "response",
        retry_after_seconds=retry_after_seconds,
        error_code=error_code,
        error_message=error_message,
        count_response=True,
        count_failure=status is not None and status >= 400,
        count_attempt=unreserved_request,
    )


def observe_failure(request_id: str | None, *, path: str, weight: int,
                    error_type: str, error_message: Any = None) -> None:
    """记录没有收到 HTTP 响应的传输失败，并与发送前预留关联。"""
    global _REQUEST_COUNT, _REQUEST_WEIGHT, _ATTEMPT_COUNT
    normalized_path = str(path or "unknown").split("?", 1)[0]
    if request_id is None:
        request_id = uuid4().hex
        with _LOCK:
            _REQUEST_EVENTS.append({
                "id": request_id, "at": time.time(), "path": normalized_path,
                "group": "general", "weight": max(1, int(weight)),
                "priority": "unknown", "source": "unclassified",
                "purpose": "unknown",
                "environment": "unknown", "status": None,
                "outcome": "pending", "retry_after_seconds": None,
                "error_type": None, "error_message": None,
            })
            _REQUEST_EVENTS[:] = _prune_events(_REQUEST_EVENTS)
            _REQUEST_COUNT += 1
            _REQUEST_WEIGHT += max(1, int(weight))
            _ATTEMPT_COUNT += 1
            _REQUESTS_BY_PATH[normalized_path] += 1
    _update_request_event(
        request_id,
        path=normalized_path,
        weight=weight,
        status=None,
        outcome="transport_error",
        error_code=None,
        error_message=error_message,
        error_type=error_type,
        count_failure=True,
        count_attempt=False,
    )


def _update_request_event(request_id: str, *, path: str, weight: int,
                          status: int | None, outcome: str,
                          retry_after_seconds: float | None = None,
                          error_code: Any = None, error_message: Any = None,
                          error_type: str | None = None,
                          count_response: bool = False,
                          count_failure: bool = False,
                          count_attempt: bool = False) -> None:
    """把请求结果写回内存和状态文件；更新采用同一跨进程文件锁。"""
    global _RESPONSE_COUNT, _FAILURE_COUNT, _ATTEMPT_COUNT
    normalized_path = str(path or "unknown").split("?", 1)[0]
    normalized_weight = max(1, int(weight))
    with _LOCK:
        target = next((event for event in _REQUEST_EVENTS
                       if str(event.get("id")) == str(request_id)), None)
        was_pending = target is None or target.get("outcome") in {None, "pending"}
        if target is None:
            target = {
                "id": str(request_id), "at": time.time(), "path": normalized_path,
                "group": "general", "weight": normalized_weight,
                "priority": "unknown", "source": "unclassified",
                "environment": "unknown",
            }
            _REQUEST_EVENTS.append(target)
        target["status"] = int(status) if status is not None else None
        target["outcome"] = outcome
        target["retry_after_seconds"] = (
            round(max(0.0, float(retry_after_seconds)), 3)
            if retry_after_seconds is not None else None)
        target["error_type"] = str(error_type)[:80] if error_type else (
            str(error_code)[:80] if error_code is not None and status and status >= 400 else None)
        target["error_message"] = _safe_telemetry_text(error_message)
        _REQUEST_EVENTS[:] = _prune_events(_REQUEST_EVENTS)
        if count_response and was_pending:
            _RESPONSE_COUNT += 1
            if status is not None:
                _RESPONSE_STATUS_COUNTS[str(int(status))] += 1
        if count_failure and was_pending:
            _FAILURE_COUNT += 1
        if count_attempt and was_pending:
            _ATTEMPT_COUNT += 1
    with _LOCK:
        observed_order = {
            "observed_order_count_10s": _OBSERVED_ORDER_COUNT_10S,
            "observed_order_count_1m": _OBSERVED_ORDER_COUNT_1M,
            "observed_order_at": _OBSERVED_ORDER_AT,
        }
    path_state = _STATE_FILE
    if path_state is None:
        return
    try:
        with _state_file_lock(path_state):
            payload: dict[str, Any] = {}
            if path_state.exists():
                try:
                    loaded = json.loads(path_state.read_text(encoding="utf-8-sig"))
                    payload = loaded if isinstance(loaded, dict) else {}
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
            events = [event for event in payload.get("request_events", [])
                      if isinstance(event, dict)]
            persisted = next((event for event in events
                              if str(event.get("id")) == str(request_id)), None)
            persisted_pending = persisted is None or persisted.get("outcome") in {None, "pending"}
            if persisted is None:
                persisted = {
                    "id": str(request_id), "at": time.time(), "path": normalized_path,
                    "group": "general", "weight": normalized_weight,
                    "priority": "unknown", "source": "unclassified",
                    "environment": "unknown",
                }
                events.append(persisted)
            persisted["status"] = int(status) if status is not None else None
            persisted["outcome"] = outcome
            persisted["retry_after_seconds"] = (
                round(max(0.0, float(retry_after_seconds)), 3)
                if retry_after_seconds is not None else None)
            persisted["error_type"] = str(error_type)[:80] if error_type else (
                str(error_code)[:80] if error_code is not None and status and status >= 400 else None)
            persisted["error_message"] = _safe_telemetry_text(error_message)
            payload["request_events"] = _prune_events(events)
            if count_attempt and persisted_pending:
                payload["attempt_count"] = _safe_int(payload.get("attempt_count")) + 1
            if count_response and persisted_pending:
                payload["response_count"] = _safe_int(payload.get("response_count")) + 1
                if status is not None:
                    statuses = payload.get("responses_by_status")
                    statuses = statuses if isinstance(statuses, dict) else {}
                    key = str(int(status))
                    statuses[key] = _safe_int(statuses.get(key)) + 1
                    payload["responses_by_status"] = statuses
            if count_failure and persisted_pending:
                payload["failure_count"] = _safe_int(payload.get("failure_count")) + 1
            _merge_observed_order_payload(payload, observed_order)
            _write_payload_locked(payload, path_state)
    except OSError as exc:
        _mark_coordination(False, f"event persist: {type(exc).__name__}: {exc}")
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
            "attempts": _ATTEMPT_COUNT,
            "responses": _RESPONSE_COUNT,
            "failures": _FAILURE_COUNT,
            "failure_rate": round(_FAILURE_COUNT / _ATTEMPT_COUNT, 6)
            if _ATTEMPT_COUNT else 0.0,
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
            "observed_order_count_10s": _OBSERVED_ORDER_COUNT_10S,
            "observed_order_count_1m": _OBSERVED_ORDER_COUNT_1M,
            "observed_order_at": _OBSERVED_ORDER_AT,
            "telemetry": {
                "retained_events": len(_REQUEST_EVENTS),
                "max_events": _TELEMETRY_EVENT_LIMIT,
            },
            "observed_weight_at": _OBSERVED_WEIGHT_AT,
            "coordination_available": _COORDINATION_AVAILABLE,
            "coordination_error": _COORDINATION_ERROR,
        }


def _reset_for_test() -> None:
    """仅供隔离回归清空进程内时钟；生产代码不得调用。"""
    global _LAST_REQUEST_TS, _COOLDOWN_UNTIL, _NEXT_REQUEST_AT
    global _LAST_LIMIT_STATUS, _LAST_LIMITED_AT, _LIMIT_EVENT_COUNT, _STATE_FILE
    global _OBSERVED_WEIGHT_1M, _OBSERVED_WEIGHT_AT, _OBSERVED_MINUTE
    global _OBSERVED_ORDER_COUNT_10S, _OBSERVED_ORDER_COUNT_1M, _OBSERVED_ORDER_AT
    global _REQUEST_COUNT, _REQUEST_WEIGHT, _ATTEMPT_COUNT, _RESPONSE_COUNT
    global _FAILURE_COUNT, _BLOCKED_COUNT
    global _RESPONSE_STATUS_COUNTS, _REQUEST_EVENTS, _STATE_LOCK_FILE
    global _DATA_BLOCKED_COUNT, _COORDINATION_AVAILABLE, _COORDINATION_ERROR
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
        _OBSERVED_ORDER_COUNT_10S = None
        _OBSERVED_ORDER_COUNT_1M = None
        _OBSERVED_ORDER_AT = None
        _REQUEST_COUNT = 0
        _REQUEST_WEIGHT = 0
        _ATTEMPT_COUNT = 0
        _RESPONSE_COUNT = 0
        _FAILURE_COUNT = 0
        _BLOCKED_COUNT = 0
        _REQUESTS_BY_PATH.clear()
        _RESPONSE_STATUS_COUNTS.clear()
        _REQUEST_EVENTS.clear()
        _DATA_BLOCKED_COUNT = 0
        _COORDINATION_AVAILABLE = True
        _COORDINATION_ERROR = None


def backoff_seconds(attempt: int, status: int | None = None) -> float:
    if status == 418:
        return min(30.0, 5.0 * (attempt + 1))
    return min(4.0, 0.8 * (attempt + 1))
