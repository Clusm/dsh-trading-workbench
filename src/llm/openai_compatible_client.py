"""OpenAI 兼容接口客户端：OpenCode Zen / DeepSeek 等。"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

from .base import LLMClient, LLMResponse

# 瞬时错误（可重试）：网关超时(504)、限流(429)、5xx 与网络层异常
_TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


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
                 max_retries: int = 1, retry_backoff_seconds: float = 2.0):
        self.api_key = (api_key or os.getenv("OPENCODE_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1")).rstrip("/")
        self.model = model or os.getenv("OPENCODE_MODEL", "deepseek-v4-flash")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """单次 POST，带 max_retries 次瞬时错误重试（指数退避）。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    r = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                r.raise_for_status()
                return r
            except Exception as e:
                last_error = e
                if attempt >= self.max_retries or not _is_transient_error(e):
                    raise
                time.sleep(self.retry_backoff_seconds * (attempt + 1))
        assert last_error is not None
        raise last_error

    def complete(self, system: str, user: str) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("OPENCODE_API_KEY 未设置")
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
        r = self._post(payload)
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
             tool_choice: str = "auto") -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OPENCODE_API_KEY 未设置")
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
        r = self._post(payload)
        return r.json()
