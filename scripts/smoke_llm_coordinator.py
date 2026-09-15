"""LLM供应商共享准入、Retry-After和明确4xx回归（全离线）。"""
from __future__ import annotations

import sys
import multiprocessing
import os
import tempfile
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


def cross_process_worker(lock_dir: str, gate, output) -> None:
    os.environ["MTA_LLM_COORDINATION_DIR"] = lock_dir
    coordinator = LLMRequestCoordinator(
        max_concurrency=1, queue_poll_seconds=0.01,
        default_queue_deadline_seconds=2.0,
    )
    gate.wait(timeout=5)
    with coordinator.admit("same-provider#key:anonymous", deadline=time.monotonic() + 2):
        started = time.monotonic()
        time.sleep(0.15)
        output.put((started, time.monotonic()))


def contended_worker(lock_dir: str, gate, rounds: int, output) -> None:
    """在全新锁目录上并发初始化并反复准入。

    回传异常类型而不是让它变成子进程 traceback：父进程曾因拿不到队列结果而只看到
    一个 `_queue.Empty`，无法定位到真正的 `PermissionError`。
    """
    os.environ["MTA_LLM_COORDINATION_DIR"] = lock_dir
    coordinator = LLMRequestCoordinator(
        max_concurrency=1, queue_poll_seconds=0.005,
        default_queue_deadline_seconds=5.0,
    )
    gate.wait(timeout=15)
    try:
        for _ in range(rounds):
            with coordinator.admit("race-provider#key:anonymous",
                                   deadline=time.monotonic() + 5):
                time.sleep(0.01)
    except BaseException as exc:
        output.put(f"{type(exc).__name__}: {exc}")
        raise
    output.put("ok")


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
            api_key="secret" + "-for-hash-only", base_url="https://provider.invalid/v1",
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
            api_key="another" + "-secret", base_url="https://provider.invalid/v1",
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

    print("\n[5] 两个独立进程共享一个供应商并发槽")
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="mta-llm-coordination-") as lock_dir:
        gate = ctx.Barrier(2)
        output = ctx.Queue()
        workers = [ctx.Process(target=cross_process_worker, args=(lock_dir, gate, output))
                   for _ in range(2)]
        for worker in workers:
            worker.start()
        results = []
        for _ in workers:
            try:
                results.append(output.get(timeout=8))
            except Exception as exc:  # queue.Empty：子进程没回传，单独报出来
                results.append(None)
                print(f"        ! 子进程未在时限内回传: {type(exc).__name__}")
        for worker in workers:
            worker.join(timeout=8)
        check("两个进程均正常退出", all(worker.exitcode == 0 for worker in workers),
              f"exitcodes={[worker.exitcode for worker in workers]}")
        check("两个进程均回传结果", all(item is not None for item in results))
        intervals = sorted(item for item in results if item is not None)
        check("跨进程请求没有重叠",
              len(intervals) == 2 and intervals[1][0] >= intervals[0][1] - 0.005, intervals)

    print("\n[6] 全新锁文件下多进程首次并发初始化不因锁竞争失败")
    # 旧实现把「文件为空则写入 0」放在加锁之前，Windows 上后到进程会在 flush()
    # 拿到 EACCES 并让整次准入失败。多进程同时打一个全新的锁文件即可复现。
    with tempfile.TemporaryDirectory(prefix="mta-llm-race-") as lock_dir:
        racing = 6
        gate = ctx.Barrier(racing)
        output = ctx.Queue()
        workers = [ctx.Process(target=contended_worker, args=(lock_dir, gate, 3, output))
                   for _ in range(racing)]
        for worker in workers:
            worker.start()
        results = []
        for _ in workers:
            try:
                results.append(output.get(timeout=30))
            except Exception as exc:
                results.append(f"NO_RESULT:{type(exc).__name__}")
        for worker in workers:
            worker.join(timeout=10)
        failures = [item for item in results if item != "ok"]
        check("并发初始化未出现权限错误", not failures, f"failures={failures[:3]} results={results}")
        check("所有竞争进程正常退出",
              all(worker.exitcode == 0 for worker in workers),
              f"exitcodes={[worker.exitcode for worker in workers]}")

    print("\n结果：PASS 13 / FAIL 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
