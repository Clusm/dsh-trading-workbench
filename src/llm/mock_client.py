"""Mock LLM：没有 DeepSeek key 时使用，返回固定 JSON 文本。"""
from __future__ import annotations

from typing import Any

from .base import LLMClient, LLMResponse


class MockLLMClient(LLMClient):
    name = "mock"

    def __init__(self, fixed_output: str = '{"decision":"no_trade","intents":[],"reasoning":"Mock LLM模式：等待DeepSeek key后由主Agent自主决策"}'):
        self.fixed_output = fixed_output

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             tool_choice: str = "auto", *, deadline: float | None = None,
             purpose: str | None = None) -> dict[str, Any]:
        return {"choices": [{"message": {"role": "assistant", "content": self.fixed_output}}],
                "usage": {}, "model": "mock"}

    def complete(self, system: str, user: str, *, deadline: float | None = None,
                 purpose: str | None = None) -> LLMResponse:
        return LLMResponse(
            content=self.fixed_output,
            model="mock",
            prompt_tokens=0,
            completion_tokens=0,
            cost_estimate=0.0,
            latency_ms=0,
            raw={"mock": True, "system": system, "user": user},
        )
