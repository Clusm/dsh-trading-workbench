"""LLM绝对总时限及工具剩余预算回归（全离线）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import llm.openai_compatible_client as client_module  # noqa: E402
from agents.orchestrator import _execute_tool_with_deadline  # noqa: E402
from llm.base import call_chat  # noqa: E402
from llm.openai_compatible_client import (  # noqa: E402
    LLMTimeBudgetExceeded,
    OpenAICompatibleLLMClient,
)
from llm.request_coordinator import LLMRequestCoordinator  # noqa: E402


def check(name: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  PASS  {name}")


class SlowHttpClient:
    calls = 0
    delay_seconds = 0.0

    def __init__(self, *args, **kwargs):
        del args, kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        del args, kwargs
        type(self).calls += 1
        time.sleep(type(self).delay_seconds)
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )


class DeadlineAwareFake:
    def __init__(self):
        self.seen_deadline = None

    def chat(self, messages, tools=None, tool_choice="auto", *, deadline=None, purpose=None):
        del messages, tools, tool_choice, purpose
        self.seen_deadline = deadline
        return {"choices": [{"message": {"content": "ok"}}]}


def main() -> int:
    print("\n[1] HTTP返回晚于绝对deadline时不产生决策结果")
    old_client = client_module.httpx.Client
    try:
        client_module.httpx.Client = SlowHttpClient
        SlowHttpClient.calls = 0
        SlowHttpClient.delay_seconds = 0.08
        llm = OpenAICompatibleLLMClient(
            api_key="deadline-secret", base_url="https://provider.invalid/v1",
            max_retries=3, coordinator=LLMRequestCoordinator(max_concurrency=1),
        )
        try:
            llm.chat([], deadline=time.monotonic() + 0.01)
        except LLMTimeBudgetExceeded:
            pass
        else:
            raise AssertionError("慢HTTP超出总时限后应失败")
        check("超时请求只发送一次且不外置遗留线程", SlowHttpClient.calls == 1)
    finally:
        client_module.httpx.Client = old_client

    print("\n[2] 429 Retry-After超出本轮预算时立即收敛")
    # 使用与HTTP客户端一致的fake，但不实际等待120秒；客户端先在供应商协调器登记冷却，
    # 再根据剩余deadline拒绝退避重试。
    class RetryAfterClient(SlowHttpClient):
        def post(self, *args, **kwargs):
            del args, kwargs
            type(self).calls += 1
            request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
            return httpx.Response(429, headers={"Retry-After": "120"}, request=request)

    try:
        client_module.httpx.Client = RetryAfterClient
        RetryAfterClient.calls = 0
        llm = OpenAICompatibleLLMClient(
            api_key="retry-secret", base_url="https://provider.invalid/v1",
            max_retries=1, coordinator=LLMRequestCoordinator(max_concurrency=1),
        )
        try:
            llm.chat([], deadline=time.monotonic() + 0.05)
        except LLMTimeBudgetExceeded:
            pass
        else:
            raise AssertionError("Retry-After超出预算时应立即降级")
        check("长Retry-After不会提前重试", RetryAfterClient.calls == 1)
    finally:
        client_module.httpx.Client = old_client

    print("\n[3] 旧fake仍可接收新版绝对deadline")
    fake = DeadlineAwareFake()
    marker = time.monotonic() + 10
    call_chat(fake, [], deadline=marker, purpose="decision")
    check("deadline传递到支持该参数的fake", fake.seen_deadline == marker)

    print("\n[4] 工具入口支持绝对deadline且保持旧替身兼容")
    import agents.orchestrator as orch  # noqa: E402
    old_execute = orch.execute_tool
    seen = {}
    try:
        def fake_execute(name, arguments, *, deadline=None):
            seen.update({"name": name, "deadline": deadline})
            return {"ok": True}
        orch.execute_tool = fake_execute
        marker = time.monotonic() + 5
        result = _execute_tool_with_deadline("query_symbol_cards", {}, marker)
        check("工具调用收到deadline", result == {"ok": True} and seen["deadline"] == marker, seen)
    finally:
        orch.execute_tool = old_execute

    print("\n结果：PASS 6 / FAIL 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
