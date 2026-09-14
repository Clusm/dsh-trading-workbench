"""LLM供应商共享准入、Retry-After和明确4xx回归（全离线）。"""
from __future__ import annotations

import sys
import threading
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import llm.openai_compatible_client as client_module  # noqa: E402
from llm.openai_compatible_client import (  # noqa: E402
    LLMTimeBudgetExceeded,
    OpenAICompatibleLLMClient,
)
from llm.request_coordinator import (  # noqa: E402
    LLMAdmissionTimeout,
    LLMRequestCoordinator,
    parse_retry_after,
    provider_scope,
)


def check(name: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  PASS  {name}")


class FakeHttpClient:
    responses: list[httpx.Response] = []
    calls = 0

    def __init__(self, *args, **kwargs):
        del args, kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        del args, kwargs
        type(self).calls += 1
        if not type(self).responses:
            raise AssertionError("fake response exhausted")
        return type(self).responses.pop(0)


def response(status: int, *, headers: dict[str, str] | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    payload = {"choices": [{"message": {"content": "ok"}}], "model": "fake"}
    return httpx.Response(status, headers=headers, json=payload, request=request)


def main() -> int:
    print("\n[1] 同一供应商凭据只允许一个在途请求")
    coord = LLMRequestCoordinator(
        max_concurrency=1, queue_poll_seconds=0.01,
        default_queue_deadline_seconds=0.2,
    )
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with coord.admit("provider#key", purpose="decision", priority="critical",
                         deadline=time.monotonic() + 1.0):
            entered.set()
            release.wait(1.0)

    thread = threading.Thread(target=holder)
    thread.start()
    check("第一个请求获得准入", entered.wait(1.0))
    try:
        with coord.admit("provider#key", purpose="review", priority="background",
                         deadline=time.monotonic() + 0.05):
            raise AssertionError("第二个请求不应越过并发上限")
    except LLMAdmissionTimeout:
        pass
    finally:
        release.set()
        thread.join(1.0)
    status = coord.status("provider#key")
    check("第二个请求在deadline前失败且有统计", status["providers"][0]["rejected"] == 1, status)

    print("\n[2] Retry-After=120秒传播到同凭据的所有Agent")
    old_client = client_module.httpx.Client
    try:
        client_module.httpx.Client = FakeHttpClient
        FakeHttpClient.calls = 0
        FakeHttpClient.responses = [response(429, headers={"Retry-After": "120"})]
        llm = OpenAICompatibleLLMClient(
            api_key="secret-for-hash-only", base_url="https://provider.invalid/v1",
            model="deepseek-v4-flash", max_retries=0, coordinator=coord,
            purpose="decision",
        )
        try:
            llm.chat([])
        except httpx.HTTPStatusError:
            pass
        else:
            raise AssertionError("429应向调用方传播")
        provider = coord.status(llm.provider_scope)["providers"][0]
        check("120秒冷却已登记", provider["cooldown_remaining_seconds"] > 119, provider)
        FakeHttpClient.responses = [response(200)]
        try:
            llm.chat([], deadline=time.monotonic() + 0.05)
        except LLMTimeBudgetExceeded:
            pass
        else:
            raise AssertionError("冷却超出剩余预算时必须失败")
        check("冷却期间没有第二次发送", FakeHttpClient.calls == 1, FakeHttpClient.calls)
    finally:
        client_module.httpx.Client = old_client

    print("\n[3] HTTP日期、缺Retry-After和明确鉴权错误")
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    parsed = parse_retry_after({"Retry-After": format_datetime(future, usegmt=True)}, maximum=60)
    check("HTTP日期格式被识别", 20 <= parsed <= 35, parsed)
    missing = LLMRequestCoordinator(max_concurrency=1)
    old_client = client_module.httpx.Client
    try:
        client_module.httpx.Client = FakeHttpClient
        FakeHttpClient.calls = 0
        FakeHttpClient.responses = [response(429)]
        retry_client = OpenAICompatibleLLMClient(
            api_key="another-secret", base_url="https://provider.invalid/v1",
            max_retries=0, coordinator=missing, default_429_cooldown_seconds=5,
        )
        try:
            retry_client.complete("", "")
        except httpx.HTTPStatusError:
            pass
        retry_status = missing.status(retry_client.provider_scope)["providers"][0]
        check("缺Retry-After使用短默认冷却且不忙等", 4 <= retry_status["cooldown_remaining_seconds"] <= 5.1)

        FakeHttpClient.calls = 0
        FakeHttpClient.responses = [response(401)]
        auth_client = OpenAICompatibleLLMClient(
            api_key="auth-secret", base_url="https://provider.invalid/v1",
            max_retries=3, coordinator=missing,
        )
        try:
            auth_client.complete("", "")
        except httpx.HTTPStatusError:
            pass
        check("401不进入网络重试", FakeHttpClient.calls == 1, FakeHttpClient.calls)
    finally:
        client_module.httpx.Client = old_client

    print("\n[4] 凭据只以匿名hash进入可观测标识")
    scope = provider_scope("https://provider.invalid/v1", "top-secret")
    check("provider scope不含密钥原文", "top-secret" not in scope and "key:" in scope, scope)
    print("\n结果：PASS 8 / FAIL 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
