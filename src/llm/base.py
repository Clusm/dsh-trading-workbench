"""LLM 客户端抽象。"""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
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

    def complete(self, system: str, user: str, *, deadline: float | None = None,
                 purpose: str | None = None) -> LLMResponse:
        raise NotImplementedError

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             tool_choice: str = "auto", *, deadline: float | None = None,
             purpose: str | None = None) -> dict[str, Any]:
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


def _deadline_kwargs(method: Any, deadline: float | None, purpose: str | None) -> dict[str, Any]:
    """只向声明支持的客户端传递deadline，兼容旧的离线fake LLM。"""
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    if deadline is not None and ("deadline" in parameters or accepts_kwargs):
        kwargs["deadline"] = deadline
    if purpose is not None and ("purpose" in parameters or accepts_kwargs):
        kwargs["purpose"] = purpose
    return kwargs


def call_chat(client: LLMClient, messages: list[dict[str, Any]],
              tools: list[dict[str, Any]] | None = None,
              tool_choice: str = "auto", *, deadline: float | None = None,
              purpose: str | None = None) -> dict[str, Any]:
    """调用LLM chat并尽可能传递本轮绝对deadline。"""
    kwargs = _deadline_kwargs(client.chat, deadline, purpose)
    return client.chat(messages, tools=tools, tool_choice=tool_choice, **kwargs)


def call_complete(client: LLMClient, system: str, user: str, *,
                  deadline: float | None = None,
                  purpose: str | None = None) -> LLMResponse:
    """调用LLM complete并尽可能传递本轮绝对deadline。"""
    kwargs = _deadline_kwargs(client.complete, deadline, purpose)
    return client.complete(system, user, **kwargs)
