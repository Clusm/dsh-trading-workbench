"""OpenAI 兼容接口客户端：SenseNova、OpenCode Zen 等。"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .base import LLMClient, LLMResponse
from .request_coordinator import (
    LLMAdmissionTimeout,
    LLMRequestCoordinator,
    get_default_coordinator,
    parse_retry_after,
    provider_scope,
)

# 瞬时错误（可重试）：网关超时(504)、限流(429)、5xx 与网络层异常
_TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


class LLMTimeBudgetExceeded(RuntimeError):
    """本轮 LLM 总时间预算耗尽，调用方必须安全降级。"""


def _is_transient_error(exc: Exception) -> bool:
    """504/429/5xx 或网络层异常视为瞬时错误，可重试；401/400 等凭据/参数错误不重试。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_HTTP_STATUS
    return isinstance(exc, httpx.TransportError)


class OpenAICompatibleLLMClient(LLMClient):
    name = "openai_compatible"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = "deepseek-v4-flash", temperature: float = 0.2,
                 max_tokens: int = 2000, timeout_seconds: int = 60,
                 reasoning_effort: str | None = None,
                 max_retries: int = 1, retry_backoff_seconds: float = 2.0,
                 purpose: str = "normal",
                 coordinator: LLMRequestCoordinator | None = None,
                 default_429_cooldown_seconds: float = 5.0):
        self.api_key = (
            api_key
            or os.getenv("SENSENOVA_API_KEY", "")
            or os.getenv("OPENCODE_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url
            or os.getenv("SENSENOVA_BASE_URL", "")
            or os.getenv("OPENCODE_BASE_URL", "https://token.sensenova.cn/v1")
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("SENSENOVA_MODEL", "")
            or os.getenv("OPENCODE_MODEL", "deepseek-v4-flash")
        )
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.purpose = str(purpose or "normal")
        self.coordinator = coordinator or get_default_coordinator()
        self.provider_scope = provider_scope(self.base_url, self.api_key)
        self.default_429_cooldown_seconds = max(0.0, float(default_429_cooldown_seconds))
        # 与 Binance Broker 使用同一网络语义：MTA_NO_PROXY=1 时不仅依赖调用方
        # 清理环境变量，还关闭 httpx 的系统代理/PAC 自动发现，避免请求误入失效代理。
        self._trust_env = os.getenv("MTA_NO_PROXY", "").strip().lower() not in {
            "1", "true", "yes", "on",
        }

    def _post(self, payload: dict[str, Any], *, deadline: float | None = None,
              timeout_seconds: float | None = None,
              purpose: str | None = None) -> httpx.Response:
        """单次 POST，带有界重试，并遵守本轮总时限。"""
        last_error: Exception | None = None
        request_purpose = str(purpose or self.purpose)
        priority = ("critical" if request_purpose in {"decision", "protect", "exit"}
                    else "background" if request_purpose in {"intel", "review"}
                    else "normal")
        for attempt in range(self.max_retries + 1):
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise LLMTimeBudgetExceeded("LLM total time budget exceeded")
            try:
                request_timeout = float(timeout_seconds or self.timeout_seconds)
                if remaining is not None:
                    request_timeout = min(request_timeout, max(0.1, remaining))
                with self.coordinator.admit(
                    self.provider_scope, purpose=request_purpose,
                    priority=priority, deadline=deadline,
                ):
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise LLMTimeBudgetExceeded("LLM total time budget exceeded during admission")
                        request_timeout = min(request_timeout, remaining)
                    with httpx.Client(
                        timeout=request_timeout,
                        trust_env=self._trust_env,
                    ) as client:
                        r = client.post(
                            f"{self.base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {self.api_key}"},
                            json=payload,
                        )
                    r.raise_for_status()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LLMTimeBudgetExceeded(
                            "LLM total time budget exceeded during HTTP request"
                        )
                    return r
            except LLMAdmissionTimeout as e:
                raise LLMTimeBudgetExceeded(str(e)) from e
            except Exception as e:
                last_error = e
                if (isinstance(e, httpx.HTTPStatusError)
                        and e.response.status_code == 429):
                    retry_after = parse_retry_after(
                        e.response.headers,
                        default=self.default_429_cooldown_seconds,
                        maximum=86400.0,
                    )
                    self.coordinator.mark_cooldown(
                        self.provider_scope, retry_after, reason="HTTP 429",
                    )
                if attempt >= self.max_retries or not _is_transient_error(e):
                    raise
                delay = self.retry_backoff_seconds * (attempt + 1)
                # Retry-After是供应商给出的最早重试时间，不能再被60秒的人为上限截断。
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
                    delay = max(delay, parse_retry_after(
                        e.response.headers,
                        default=self.default_429_cooldown_seconds,
                        maximum=86400.0,
                    ))
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LLMTimeBudgetExceeded(
                            "LLM total time budget exceeded during retry backoff"
                        ) from e
                    delay = min(delay, remaining)
                time.sleep(delay)
        assert last_error is not None
        raise last_error

    def complete(self, system: str, user: str,
                 *, deadline: float | None = None,
                 purpose: str | None = None) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("LLM API key 未设置")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
            payload["thinking"] = {"type": "enabled"}
        started = time.monotonic()
        r = self._post(payload, deadline=deadline, purpose=purpose)
        data = r.json()
        latency_ms = int((time.monotonic() - started) * 1000)
        choice = data["choices"][0]
        usage = data.get("usage", {})
        return LLMResponse(
            content=choice["message"]["content"],
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_estimate=0.0,
            latency_ms=latency_ms,
            raw=data,
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             tool_choice: str = "auto", *, deadline: float | None = None,
             purpose: str | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("LLM API key 未设置")
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
            payload["thinking"] = {"type": "enabled"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        r = self._post(payload, deadline=deadline, purpose=purpose)
        return r.json()
