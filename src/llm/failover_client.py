"""有截止时间约束的 LLM 主备切换。

备用供应商只承接主供应商的容量类/网络类故障。鉴权、请求格式和业务校验错误
必须原样失败，避免配置错误被备用链路掩盖。LLM 请求本身不执行交易副作用；只有
返回的工具调用通过 orchestrator 校验后才会执行，因此在 HTTP 失败后重放同一组
messages 不会重复下单。
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, Callable, TypeVar

import httpx

from .base import LLMClient, LLMResponse
from .openai_compatible_client import LLMTimeBudgetExceeded
from .request_coordinator import LLMAdmissionTimeout


_T = TypeVar("_T")
_RETRYABLE_STATUS = {408, 429}


def is_retryable_provider_error(exc: BaseException) -> bool:
    """只把供应商容量、瞬时服务和网络故障交给备用链路。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return (exc.response.status_code in _RETRYABLE_STATUS
                or 500 <= exc.response.status_code <= 599)
    if isinstance(exc, LLMTimeBudgetExceeded):
        return isinstance(exc.__cause__, LLMAdmissionTimeout)
    return isinstance(exc, (httpx.TransportError, LLMAdmissionTimeout))


class FailoverLLMClient(LLMClient):
    """在同一绝对 deadline 内从 primary 有界切换到 fallback。"""

    name = "failover"

    def __init__(self, primary: LLMClient, fallback: LLMClient, *,
                 primary_name: str, fallback_name: str,
                 min_remaining_seconds: float = 15.0,
                 declared_models: Sequence[str] = ()) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_name = str(primary_name)
        self.fallback_name = str(fallback_name)
        self.min_remaining_seconds = max(0.0, float(min_remaining_seconds))
        self.last_provider = self.primary_name
        self.fallback_count = 0
        #: 本链路**显式声明**的模型名集合（主 + 备）。调用方据此区分"合法的备用
        #: 路由"与"静默参数继承"（U05/S02 PARAMETER_INHERITANCE）。
        self.declared_fallbacks = tuple(
            str(item) for item in declared_models if str(item or "").strip()
        )
        #: 可选尝试观察器 ``observer(provider, purpose, deadline) -> None``：
        #: 每发起一次尝试调用一次，供执行侧落 K05 span 与路由风险判定（DL06/S02）。
        self.attempt_observer: Callable[[str, str | None, float | None], None] | None = None

    def _observe(self, provider: str, *, purpose: str | None,
                 deadline: float | None) -> None:
        """观测失败不得阻塞决策（可观测性是旁路，不是控制流）。"""
        observer = self.attempt_observer
        if observer is None:
            return
        try:
            observer(provider, purpose, deadline)
        except Exception:  # noqa: BLE001 - 观测器故障不得影响模型调用
            return

    @staticmethod
    def _cooldown_remaining(client: LLMClient) -> float:
        coordinator = getattr(client, "coordinator", None)
        scope = getattr(client, "provider_scope", None)
        if coordinator is None or not scope:
            return 0.0
        try:
            providers = coordinator.status(scope).get("providers") or []
            if not providers:
                return 0.0
            return float(providers[0].get("cooldown_remaining_seconds") or 0.0)
        except Exception:
            return 0.0

    def _can_fallback(self, deadline: float | None) -> bool:
        if deadline is None:
            return True
        remaining = deadline - time.monotonic()
        return remaining > 0 and remaining >= self.min_remaining_seconds

    def _call(self, primary_call: Callable[[], _T], fallback_call: Callable[[], _T],
              *, deadline: float | None, purpose: str | None = None) -> _T:
        if deadline is not None and time.monotonic() >= deadline:
            raise LLMTimeBudgetExceeded("LLM total time budget exceeded")
        # 已知主供应商正在 Retry-After 冷却时直接使用独立备用额度，避免在主协调器
        # 一直排队到本轮 deadline 后才发现没有剩余时间可切换。
        if self._cooldown_remaining(self.primary) > 0 and self._can_fallback(deadline):
            self.last_provider = self.fallback_name
            self.fallback_count += 1
            self._observe(self.fallback_name, purpose=purpose, deadline=deadline)
            return fallback_call()
        self._observe(self.primary_name, purpose=purpose, deadline=deadline)
        try:
            result = primary_call()
            self.last_provider = self.primary_name
            return result
        except Exception as exc:
            if (not is_retryable_provider_error(exc)
                    or not self._can_fallback(deadline)):
                raise
            self.last_provider = self.fallback_name
            self.fallback_count += 1
            self._observe(self.fallback_name, purpose=purpose, deadline=deadline)
            return fallback_call()

    @staticmethod
    def _annotate(raw: dict[str, Any], provider: str, fallback_used: bool,
                  *, deadline: float | None = None) -> dict[str, Any]:
        # 只添加本系统命名空间的可观测字段，不改供应商 choices/usage 协议。
        raw.setdefault("_mta", {})
        if isinstance(raw["_mta"], dict):
            raw["_mta"].update({
                "provider": provider,
                "fallback_used": fallback_used,
                # 期限落在本系统命名空间：绝对时刻用**单调时钟**（不可与墙钟混用），
                # 同时回带剩余毫秒便于判定"这次尝试是否借了本轮的预算"（U05/DL07）。
                "deadline_monotonic": deadline,
                "deadline_remaining_ms": (
                    None if deadline is None else int((deadline - time.monotonic()) * 1000)
                ),
            })
        return raw

    def complete(self, system: str, user: str, *, deadline: float | None = None,
                 purpose: str | None = None) -> LLMResponse:
        if deadline is None:
            deadline = time.monotonic() + float(getattr(self.primary, "timeout_seconds", 60))
        before = self.fallback_count
        response = self._call(
            lambda: self.primary.complete(system, user, deadline=deadline, purpose=purpose),
            lambda: self.fallback.complete(system, user, deadline=deadline, purpose=purpose),
            deadline=deadline, purpose=purpose,
        )
        response.raw = self._annotate(
            response.raw, self.last_provider, self.fallback_count > before,
            deadline=deadline,
        )
        return response

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             tool_choice: str = "auto", *, deadline: float | None = None,
             purpose: str | None = None) -> dict[str, Any]:
        if deadline is None:
            deadline = time.monotonic() + float(getattr(self.primary, "timeout_seconds", 60))
        before = self.fallback_count
        raw = self._call(
            lambda: self.primary.chat(
                messages, tools=tools, tool_choice=tool_choice,
                deadline=deadline, purpose=purpose,
            ),
            lambda: self.fallback.chat(
                messages, tools=tools, tool_choice=tool_choice,
                deadline=deadline, purpose=purpose,
            ),
            deadline=deadline, purpose=purpose,
        )
        return self._annotate(raw, self.last_provider, self.fallback_count > before,
                              deadline=deadline)
