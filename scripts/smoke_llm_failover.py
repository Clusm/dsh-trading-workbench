"""Offline failover policy, deadline and factory regression checks."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm.base import LLMResponse
from llm.factory import build_llm_for_agent
from llm.failover_client import FailoverLLMClient
from llm.openai_compatible_client import LLMTimeBudgetExceeded, OpenAICompatibleLLMClient
from llm.request_coordinator import LLMAdmissionTimeout


class Fake:
    timeout_seconds = 60

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def chat(self, messages, tools=None, tool_choice="auto", **kwargs):
        self.calls.append((messages, tools, tool_choice, kwargs))
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": "ok"}}]}

    def complete(self, system, user, **kwargs):
        return LLMResponse("ok", "fake", raw=self.chat([system, user], **kwargs))


def status_error(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://test.invalid"))
    return httpx.HTTPStatusError("synthetic", request=response.request, response=response)


def pair(error=None, **kwargs):
    primary, fallback = Fake(error), Fake()
    return FailoverLLMClient(primary, fallback, primary_name="primary",
                             fallback_name="fallback", **kwargs), primary, fallback


def main():
    deadline = time.monotonic() + 60
    messages, tools = [{"role": "user", "content": "test"}], [{"type": "function"}]
    wrapped = LLMTimeBudgetExceeded("admission")
    wrapped.__cause__ = LLMAdmissionTimeout("capacity")
    for error in [*(status_error(code) for code in [408, 429, *range(500, 600)]),
                  httpx.ConnectError("offline"), httpx.ReadTimeout("timeout"),
                  LLMAdmissionTimeout("capacity"), wrapped]:
        client, primary, fallback = pair(error)
        result = client.chat(messages, tools, "required", deadline=deadline, purpose="decision")
        assert len(primary.calls) == len(fallback.calls) == 1
        assert primary.calls == fallback.calls
        assert fallback.calls[0][3]["deadline"] == deadline
        # U05/S02 之后 _mta 追加期限可观测字段（deadline_monotonic /
        # deadline_remaining_ms）。此处不再做整字典相等断言（否则任何新增
        # 可观测字段都会打断本冒烟），改为"关键事实 + 新增字段都存在"。
        assert result["_mta"]["provider"] == "fallback"
        assert result["_mta"]["fallback_used"] is True
        assert "deadline_monotonic" in result["_mta"]
        assert "deadline_remaining_ms" in result["_mta"]
    for error in [*(status_error(code) for code in [400, 401, 403, 404, 409, 422]),
                  ValueError("invalid"), LLMTimeBudgetExceeded("expired")]:
        client, primary, fallback = pair(error)
        try:
            client.chat([], deadline=deadline)
        except Exception as caught:
            assert caught is error
        else:
            raise AssertionError("unexpected fallback")
        assert not fallback.calls
    client, primary, fallback = pair()
    assert client.complete("s", "u", deadline=deadline).raw["_mta"]["provider"] == "primary"
    assert not fallback.calls
    client, primary, fallback = pair(status_error(503))
    assert client.complete("s", "u").raw["_mta"]["fallback_used"]
    assert primary.calls[0][3]["deadline"] == fallback.calls[0][3]["deadline"]
    for seconds in [-1, 1]:
        client, primary, fallback = pair(status_error(429))
        try:
            client.chat([], deadline=time.monotonic() + seconds)
        except (LLMTimeBudgetExceeded, httpx.HTTPStatusError):
            pass
        else:
            raise AssertionError("insufficient budget")
        assert not fallback.calls
    client, primary, fallback = pair()
    primary.provider_scope = "fake"
    primary.coordinator = SimpleNamespace(status=lambda _: {
        "providers": [{"cooldown_remaining_seconds": 100}]})
    client.chat([], deadline=deadline)
    assert not primary.calls and len(fallback.calls) == 1
    client, primary, fallback = pair(status_error(503))
    fallback.error = status_error(401)
    try:
        client.chat([], deadline=deadline)
    except httpx.HTTPStatusError as error:
        assert error is fallback.error
    assert len(fallback.calls) == 1

    settings = SimpleNamespace(llm_config={"max_retries": 2}, raw_agents={})
    with patch.dict(os.environ, {"SENSENOVA_API_KEY": "offline-placeholder",
                                "OPENCODE_API_KEY": "offline-placeholder"}, clear=True):
        assert isinstance(build_llm_for_agent(settings, "ai_trader")[0], OpenAICompatibleLLMClient)
        os.environ["MTA_AI_DECISION_FALLBACK_PROVIDER"] = "opencode"
        client, _ = build_llm_for_agent(settings, "ai_trader")
        assert isinstance(client, FailoverLLMClient)
        assert client.fallback.model == "union-alpha"
        assert client.primary.max_retries == client.fallback.max_retries == 0
        assert client.primary.provider_scope != client.fallback.provider_scope
        # 备用配置不完整必须显式失败，不能半启用或静默退回主链路。
        fallback_key = os.environ.pop("OPENCODE_API_KEY")
        try:
            build_llm_for_agent(settings, "ai_trader")
        except ValueError:
            pass
        else:
            raise AssertionError("missing fallback key must fail explicitly")
        os.environ["OPENCODE_API_KEY"] = fallback_key
        for broken in [{"OPENCODE_BASE_URL": "not-a-url"},
                       {"OPENCODE_BASE_URL": "https://user:secret@opencode.ai/zen/v1"},
                       {"OPENCODE_MODEL": "none"}]:
            with patch.dict(os.environ, broken):
                try:
                    build_llm_for_agent(settings, "ai_trader")
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"invalid fallback config must fail: {broken}")
        for agent in ["review_agent", "news_agent", "demo"]:
            assert isinstance(build_llm_for_agent(settings, agent)[0], OpenAICompatibleLLMClient)
        os.environ["MTA_AI_DECISION_PROVIDER"] = "workbuddy"
        assert not isinstance(build_llm_for_agent(settings, "ai_trader")[0], FailoverLLMClient)
    print("LLM_FAILOVER_OK")


if __name__ == "__main__":
    main()
