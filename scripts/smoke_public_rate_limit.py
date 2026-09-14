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
client_429 = Client(Response(429, retry_after="12"))
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
assert state_file.is_file()
rate_limit._reset_for_test()
rate_limit.configure_rate_limit_state(state_file)
restored = rate_limit.rate_limit_status()
assert restored["active"] is True and restored["last_status"] == 429
print("  PASS  只请求一次，未把429重试升级为418")
print("  PASS  重启后仍保留未到期冷却")

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

rate_limit._reset_for_test()
binance_public._ALL_TICKERS_CACHE = (0.0, [])
print("\n结果：PASS 5 / FAIL 0")
