"""LLM 客户端抽象。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResponse:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_estimate: float = 0.0
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    name: str = "base"

    def complete(self, system: str, user: str) -> LLMResponse:
        raise NotImplementedError

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             tool_choice: str = "auto") -> dict[str, Any]:
        raise NotImplementedError

    def complete_json(self, system: str, user: str) -> LLMResponse:
        resp = self.complete(system, user)
        # 调用方负责校验；这里只做轻量清理，方便模型输出 ```json ... ``` 的情况
        text = resp.content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        resp.content = text.strip()
        return resp
