"""Binance 签名传输层并发、重签与连接池重建回归；不访问网络。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402
from exchange.protocol import normalize_algo_orders_payload, parse_iso, parse_iso_ms  # noqa: E402
from data import rate_limit  # noqa: E402
from data.rate_limit import ExecutionRateLimited  # noqa: E402


class Response:
    def __init__(self, payload: dict[str, Any], status: int = 200,
                 headers: dict[str, Any] | None = None):
        self.payload = payload
        self.status_code = status
        self.headers = dict(headers or {})

    def json(self) -> dict[str, Any]:
        return self.payload


class SequenceClient:
    def __init__(self, payloads: list[dict[str, Any]]):
        self.payloads = list(payloads)
        self.closed = False

    def request(self, _method: str, _url: str, **_kwargs: Any) -> Response:
        return Response(self.payloads.pop(0))

    def close(self) -> None:
        self.closed = True


def broker_with(client: Any) -> BinanceFuturesBroker:
    broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker.api_key = "test"
    broker.api_secret = "test"
    broker.base_url = "https://example.invalid"
    broker._time_offset_ms = 0
    broker._client_lock = threading.RLock()
    broker._client = client
    broker._client_timeout = httpx.Timeout(1.0)
    broker._trust_env = False
    return broker


print("\n[1] -1022 后重签不会把旧 signature 再次参与 HMAC")
sequence = SequenceClient([{"code": -1022, "msg": "bad signature"}, {"ok": True}])
broker = broker_with(sequence)
sign_inputs: list[dict[str, Any]] = []
broker._sign = lambda params: sign_inputs.append(dict(params)) or "signed"
broker._calibrate_time = lambda: None
assert broker._signed("GET", "/signed", {}, attempts=2) == {"ok": True}
assert len(sign_inputs) == 2 and all("signature" not in row for row in sign_inputs)
print("  PASS  每次重试都从无旧签名的参数重新计算")


print("\n[2] 共享客户端请求严格串行，不会被另一线程并发关闭")


class ConcurrentClient:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def request(self, _method: str, _url: str, **_kwargs: Any) -> Response:
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.03)
        with self.guard:
            self.active -= 1
        return Response({"ok": True})

    def close(self) -> None:
        return None


concurrent = ConcurrentClient()
broker = broker_with(concurrent)
threads = [threading.Thread(target=lambda: broker._signed("GET", "/signed")) for _ in range(4)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
assert concurrent.max_active == 1
print("  PASS  四个并发调用的最大在途请求数为 1")


print("\n[3] 传输错误后在锁内替换连接池，读请求安全重试")


class BrokenClient(SequenceClient):
    def request(self, _method: str, _url: str, **_kwargs: Any) -> Response:
        raise httpx.ConnectError("network reset")


broken = BrokenClient([])
healthy = SequenceClient([{"ok": True}])
broker = broker_with(broken)
broker._new_client = lambda: healthy
assert broker._signed("GET", "/signed", attempts=2) == {"ok": True}
assert broken.closed is True and broker._client is healthy
print("  PASS  旧连接池关闭且第二次请求使用新连接池")

print("\n[4] 生产签名请求遇到418只记录一次且不重试")
rate_limit._reset_for_test()


class LimitedClient:
    calls = 0

    def request(self, _method: str, _url: str, **_kwargs: Any) -> Response:
        self.calls += 1
        return Response({}, status=418, headers={"Retry-After": "900"})

    def close(self) -> None:
        return None


limited_client = LimitedClient()
limited_broker = broker_with(limited_client)
limited_broker.base_url = "https://fapi.binance.com"
try:
    limited_broker._signed("GET", "/signed", {}, attempts=3)
    raise AssertionError("418 应抛出")
except ExecutionRateLimited:
    pass
assert limited_client.calls == 1
assert rate_limit.rate_limit_status()["last_status"] == 418
assert rate_limit.rate_limit_status()["event_count"] == 1
print("  PASS  生产私有请求的418不重试且只计一次")
rate_limit._reset_for_test()

print("\n[5] Algo列表响应包装归一化且未知结构fail-closed")
row = {"algoId": 1, "symbol": "BTCUSDT"}
assert BinanceFuturesBroker._normalize_algo_orders_payload([row]) == [row]
assert BinanceFuturesBroker._normalize_algo_orders_payload(
    {"code": "200", "msg": "success", "data": [row]}) == [row]
assert BinanceFuturesBroker._normalize_algo_orders_payload(
    {"data": {"orders": [row]}}) == [row]
try:
    BinanceFuturesBroker._normalize_algo_orders_payload({"code": "200", "msg": "success"})
    unknown_blocked = False
except RuntimeError:
    unknown_blocked = True
assert unknown_blocked
print("  PASS  裸列表和data包装均可读取，未知结构不会伪装为空列表")

print("\n[6] 协议纯解析函数与Broker兼容入口一致")
assert normalize_algo_orders_payload({"data": {"rows": [row]}}) == [row]
assert BinanceFuturesBroker._normalize_algo_orders_payload([row]) == \
    normalize_algo_orders_payload([row])
assert parse_iso("2026-09-12T01:02:03Z") is not None
assert parse_iso("invalid") is None
assert parse_iso_ms("2026-09-12 01:02:03") == \
    BinanceFuturesBroker._parse_iso_ms("2026-09-12 01:02:03")
assert parse_iso_ms("invalid") is None
print("  PASS  时间和Algo响应解析保持兼容且无效输入fail-closed")

print("\n结果: 6 通过 / 0 失败")
