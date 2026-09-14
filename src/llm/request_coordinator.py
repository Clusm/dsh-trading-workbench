"""同一供应商凭据下的 LLM 请求准入、冷却和排队协调。

多个 Agent 可能共享同一个 SenseNova/OpenAI 兼容凭据。这个模块只做本地调度：
它不猜供应商的 RPM/TPM 上限，而是把并发、Retry-After 冷却和本轮 deadline
统一到一个可观测的临界区，避免主决策、情报摘要和复盘互相把额度打穿。
"""
from __future__ import annotations

import hashlib
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Mapping


class LLMAdmissionError(RuntimeError):
    """请求无法在本地供应商协调器中获得准入。"""


class LLMAdmissionTimeout(LLMAdmissionError):
    """排队或供应商冷却超出本次请求剩余预算。"""


def parse_retry_after(headers: Mapping[str, Any] | None, *, default: float = 0.0,
                     maximum: float = 86400.0) -> float:
    """解析秒数或HTTP日期格式的Retry-After，结果限制在安全范围内。"""
    raw = (headers or {}).get("Retry-After")
    try:
        if raw is not None and str(raw).strip():
            value = float(str(raw).strip())
            return max(0.0, min(float(maximum), value))
    except (TypeError, ValueError):
        pass
    if raw is not None:
        try:
            date = parsedate_to_datetime(str(raw))
            if date.tzinfo is None:
                date = date.astimezone()
            return max(0.0, min(float(maximum), date.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError, IndexError):
            pass
    return max(0.0, min(float(maximum), float(default)))


def provider_scope(base_url: str, api_key: str | None = None) -> str:
    """构造不含密钥原文的共享配额标识。"""
    digest = hashlib.sha256(str(api_key or "").encode("utf-8")).hexdigest()[:16]
    return f"{str(base_url or '').rstrip('/')}#key:{digest}"


@dataclass(frozen=True)
class RequestPermit:
    provider_scope: str
    purpose: str
    priority: str
    admitted_at: float
    queue_wait_ms: int


@dataclass
class _ProviderState:
    active: int = 0
    cooldown_until: float = 0.0
    cooldown_reason: str | None = None
    admitted: int = 0
    rejected: int = 0
    completed: int = 0
    total_wait_ms: int = 0
    max_wait_ms: int = 0
    purposes: Counter[str] | None = None

    def __post_init__(self) -> None:
        if self.purposes is None:
            self.purposes = Counter()


class LLMRequestCoordinator:
    """进程内按供应商凭据共享的有界请求调度器。"""

    def __init__(self, *, max_concurrency: int = 1,
                 queue_poll_seconds: float = 0.25,
                 default_queue_deadline_seconds: float = 30.0) -> None:
        self._condition = threading.Condition(threading.RLock())
        self.max_concurrency = max(1, int(max_concurrency))
        self.queue_poll_seconds = max(0.05, float(queue_poll_seconds))
        self.default_queue_deadline_seconds = max(0.1, float(default_queue_deadline_seconds))
        self._states: dict[str, _ProviderState] = {}

    def reconfigure(self, *, max_concurrency: int | None = None,
                    queue_poll_seconds: float | None = None,
                    default_queue_deadline_seconds: float | None = None) -> None:
        with self._condition:
            if max_concurrency is not None:
                self.max_concurrency = max(1, int(max_concurrency))
            if queue_poll_seconds is not None:
                self.queue_poll_seconds = max(0.05, float(queue_poll_seconds))
            if default_queue_deadline_seconds is not None:
                self.default_queue_deadline_seconds = max(0.1, float(default_queue_deadline_seconds))
            self._condition.notify_all()

    def _state(self, scope: str) -> _ProviderState:
        return self._states.setdefault(str(scope), _ProviderState())

    @contextmanager
    def admit(self, scope: str, *, purpose: str = "normal",
              priority: str = "normal", deadline: float | None = None
              ) -> Iterator[RequestPermit]:
        """排队直到获得一个请求槽；冷却期间任何优先级都不能绕过。"""
        started = time.monotonic()
        effective_deadline = deadline
        if effective_deadline is None:
            effective_deadline = started + self.default_queue_deadline_seconds
        scope = str(scope)
        purpose = str(purpose or "normal")
        priority = str(priority or "normal")
        with self._condition:
            state = self._state(scope)
            while True:
                now = time.monotonic()
                remaining = effective_deadline - now
                if remaining <= 0:
                    state.rejected += 1
                    raise LLMAdmissionTimeout(
                        f"LLM request admission deadline exceeded: purpose={purpose}"
                    )
                cooldown = max(0.0, state.cooldown_until - now)
                if cooldown <= 0 and state.active < self.max_concurrency:
                    state.active += 1
                    state.admitted += 1
                    state.purposes[purpose] += 1
                    waited_ms = int((now - started) * 1000)
                    state.total_wait_ms += waited_ms
                    state.max_wait_ms = max(state.max_wait_ms, waited_ms)
                    permit = RequestPermit(
                        provider_scope=scope, purpose=purpose, priority=priority,
                        admitted_at=now, queue_wait_ms=waited_ms,
                    )
                    break
                wait_for = min(self.queue_poll_seconds, remaining)
                if cooldown > 0:
                    wait_for = min(wait_for, cooldown)
                self._condition.wait(timeout=max(0.01, wait_for))
        try:
            yield permit
        finally:
            with self._condition:
                state = self._state(scope)
                state.active = max(0, state.active - 1)
                state.completed += 1
                self._condition.notify_all()

    def mark_cooldown(self, scope: str, seconds: float, *, reason: str = "") -> None:
        seconds = max(0.0, float(seconds))
        with self._condition:
            state = self._state(scope)
            state.cooldown_until = max(state.cooldown_until, time.monotonic() + seconds)
            state.cooldown_reason = str(reason or "retry_after")[:120] if seconds else None
            self._condition.notify_all()

    def status(self, scope: str | None = None) -> dict[str, Any]:
        now = time.monotonic()
        with self._condition:
            selected = ({str(scope): self._states.get(str(scope))}
                        if scope is not None else self._states)
            providers = []
            for key, state in selected.items():
                if state is None:
                    continue
                providers.append({
                    "scope": key,
                    "active": state.active,
                    "max_concurrency": self.max_concurrency,
                    "cooldown_remaining_seconds": round(
                        max(0.0, state.cooldown_until - now), 3,
                    ),
                    "cooldown_reason": state.cooldown_reason,
                    "admitted": state.admitted,
                    "rejected": state.rejected,
                    "completed": state.completed,
                    "queue_wait_avg_ms": round(
                        state.total_wait_ms / state.admitted, 1
                    ) if state.admitted else 0.0,
                    "queue_wait_max_ms": state.max_wait_ms,
                    "purposes": dict(state.purposes or {}),
                })
            return {
                "configured": True,
                "max_concurrency": self.max_concurrency,
                "provider_count": len(providers),
                "providers": providers,
            }


_DEFAULT_COORDINATOR = LLMRequestCoordinator()


def configure_default_coordinator(*, max_concurrency: int = 1,
                                  queue_poll_seconds: float = 0.25,
                                  default_queue_deadline_seconds: float = 30.0
                                  ) -> LLMRequestCoordinator:
    _DEFAULT_COORDINATOR.reconfigure(
        max_concurrency=max_concurrency,
        queue_poll_seconds=queue_poll_seconds,
        default_queue_deadline_seconds=default_queue_deadline_seconds,
    )
    return _DEFAULT_COORDINATOR


def get_default_coordinator() -> LLMRequestCoordinator:
    return _DEFAULT_COORDINATOR


def coordinator_status() -> dict[str, Any]:
    return _DEFAULT_COORDINATOR.status()
