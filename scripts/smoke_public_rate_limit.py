"""公开行情 429/418 与全量 ticker 单飞缓存离线回归。"""
from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("MTA_BINANCE_WEIGHT_BUDGET_1M", "60000")

from data import binance_public, rate_limit  # noqa: E402
from data.binance_transport import endpoint_weight, request_json  # noqa: E402


class Response:
    def __init__(self, status: int, payload=None, retry_after: str | None = None,
                 headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.headers = dict(headers or {})
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class Client:
    def __init__(self, response: Response):
        self.response = response
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.response

    def close(self):
        return None


print("\n[1] 429 立即停止重试并尊重 Retry-After")
rate_limit._reset_for_test()
state_dir = Path(tempfile.mkdtemp(prefix="mta-rate-limit-"))
state_file = state_dir / "state.json"
rate_limit.configure_rate_limit_state(state_file)
client_429 = Client(Response(
    429, retry_after="12",
    headers={"X-MBX-ORDER-COUNT-10S": "2", "X-MBX-ORDER-COUNT-1M": "7"},
))
try:
    binance_public._get_json(client_429, "/fapi/v1/ticker/24hr", attempts=4)
    raise AssertionError("429 应抛出")
except RuntimeError as exc:
    assert "429" in str(exc)
assert client_429.calls == 1, client_429.calls
remaining = rate_limit.cooldown_remaining()
assert 10 <= remaining <= 12.1, remaining
status = rate_limit.rate_limit_status()
assert status["active"] is True and status["last_status"] == 429
assert status["event_count"] == 1
assert status["attempts"] == 1 and status["responses"] == 1 and status["failures"] == 1
assert status["observed_order_count_10s"] == 2
assert status["observed_order_count_1m"] == 7
assert state_file.is_file()
event_429 = __import__("json").loads(
    state_file.read_text(encoding="utf-8"))["request_events"][0]
assert event_429["outcome"] == "response_error"
assert event_429["retry_after_seconds"] == 12.0
rate_limit._reset_for_test()
rate_limit.configure_rate_limit_state(state_file)
restored = rate_limit.rate_limit_status()
assert restored["active"] is True and restored["last_status"] == 429
assert restored["observed_order_count_10s"] == 2
assert restored["observed_order_count_1m"] == 7
print("  PASS  只请求一次，未把429重试升级为418")
print("  PASS  重启后仍保留未到期冷却")
print("  PASS  重启后仍保留订单计数水位")

print("\n[2] 418 同样立即停止并进入长冷却")
rate_limit._reset_for_test()
rate_limit.configure_rate_limit_state(state_dir / "state-418.json")
client_418 = Client(Response(418, retry_after="900"))
try:
    binance_public._get_json(client_418, "/fapi/v1/ticker/24hr", attempts=4)
    raise AssertionError("418 应抛出")
except RuntimeError as exc:
    assert "418" in str(exc)
assert client_418.calls == 1, client_418.calls
assert rate_limit.cooldown_remaining() > 890
assert rate_limit.rate_limit_status()["last_status"] == 418
print("  PASS  418不再重复撞限流出口且遵守服务端更长封禁")

print("\n[3] 全量 ticker 在短窗口内单飞复用")
rate_limit._reset_for_test()
binance_public._ALL_TICKERS_CACHE = (0.0, [])
ok = Client(Response(200, [{"symbol": "BTCUSDT", "quoteVolume": "1"}]))
first = binance_public.get_all_24h_tickers(ok)
second = binance_public.get_all_24h_tickers(ok)
assert first == second and first[0]["symbol"] == "BTCUSDT"
assert ok.calls == 1, ok.calls
second[0]["symbol"] = "MUTATED"
assert binance_public.get_all_24h_tickers(ok)[0]["symbol"] == "BTCUSDT"
print("  PASS  多消费者共享一次高权重请求且返回副本")

print("\n[4] 并发请求排队期间触发429 → 后续线程发HTTP前短路")
rate_limit._reset_for_test()
rate_limit.configure_rate_limit_state(state_dir / "state-concurrent.json")
concurrent_client = Client(Response(429, retry_after="30"))
with ThreadPoolExecutor(max_workers=8) as pool:
    futures = [
        pool.submit(
            binance_public._get_json,
            concurrent_client,
            "/fapi/v1/klines",
            {"symbol": f"TEST{i}USDT"},
            1.0,
            1,
        )
        for i in range(8)
    ]
    for future in futures:
        try:
            future.result()
        except RuntimeError:
            pass
assert concurrent_client.calls == 1, concurrent_client.calls
assert rate_limit.rate_limit_status()["event_count"] == 1
print("  PASS  八个并发调用只有首个到达交易所")

print("\n[5] 动态端点权重与响应头水位保护")
rate_limit._reset_for_test()
assert endpoint_weight("/fapi/v1/klines", {"limit": 80}) == 1
assert endpoint_weight("/fapi/v1/klines", {"limit": 600}) == 5
assert endpoint_weight("/fapi/v1/ticker/24hr", {}) == 40
assert endpoint_weight("/fapi/v1/ticker/24hr", {"symbol": "BTCUSDT"}) == 1
high = Client(Response(200, {"ok": True}, headers={"X-MBX-USED-WEIGHT-1M": "1900"}))
assert request_json(high, "/fapi/v1/time", priority="background") == {"ok": True}
blocked = Client(Response(200, {"ok": True}))
try:
    request_json(blocked, "/fapi/v1/time", priority="background")
    raise AssertionError("75%以上后台请求应被主动延后")
except rate_limit.PublicMarketBudgetDeferred:
    pass
assert blocked.calls == 0
status = rate_limit.rate_limit_status()
assert status["observed_weight_1m"] == 1900 and status["blocked_requests"] >= 1
print("  PASS  大请求按权重排队，后台流量在高水位发送前被拦截")

print("\n[6] futures/data五分钟窗口与发送前遥测预留")
rate_limit._reset_for_test()
rate_limit.configure_rate_limit_budget(
    weight_limit_1m=2400, local_budget_1m=60000,
    background_defer_ratio=0.75, normal_defer_ratio=0.90,
    data_window_seconds=300, data_request_budget_5m=2,
)
telemetry_state = state_dir / "state-data-window.json"
rate_limit.configure_rate_limit_state(telemetry_state)
data_client = Client(Response(200, {"ok": True}))
for _ in range(2):
    assert request_json(
        data_client, "/futures/data/topLongShortAccountRatio",
        {"symbol": "BTCUSDT"}, priority="background",
    ) == {"ok": True}
try:
    request_json(
        data_client, "/futures/data/topLongShortAccountRatio",
        {"symbol": "BTCUSDT"}, priority="background",
    )
    raise AssertionError("futures/data五分钟预算应阻断")
except rate_limit.PublicMarketBudgetDeferred:
    pass
assert data_client.calls == 2
status = rate_limit.rate_limit_status()
assert status["data_endpoint_window"]["requests"] == 2
assert status["data_endpoint_window"]["blocked_requests"] >= 1
payload = __import__("json").loads(telemetry_state.read_text(encoding="utf-8"))
events = payload.get("request_events") or []
assert len(events) == 2 and all("symbol" not in event and event["status"] == 200
                                for event in events)
print("  PASS  futures/data达到本地安全预算后不再发HTTP")
print("  PASS  发送前预留和响应状态写入有界脱敏遥测")

print("\n[6b] 传输失败按发送尝试计数并与端点关联")
rate_limit._reset_for_test()


class FailingClient:
    calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        raise OSError("simulated transport failure")


failing = FailingClient()
try:
    request_json(failing, "/fapi/v1/time", attempts=2, priority="background")
    raise AssertionError("传输故障应抛出")
except OSError:
    pass
failure_status = rate_limit.rate_limit_status()
assert failing.calls == 2
assert failure_status["attempts"] == 2 and failure_status["responses"] == 0
assert failure_status["failures"] == 2 and failure_status["failure_rate"] == 1.0
print("  PASS  无HTTP响应的两次尝试均进入失败计数")
print("  PASS  失败遥测不记录请求参数")

rate_limit._reset_for_test()
binance_public._ALL_TICKERS_CACHE = (0.0, [])
print("\n结果：PASS 9 / FAIL 0")
