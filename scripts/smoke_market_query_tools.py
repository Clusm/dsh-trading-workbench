"""验证主 Agent 的按需行情工具可用（不联网：mock 掉 httpx.Client）。

起因：market_query.py 里曾有一个本地 `def _get_json(url, ...)`，把顶部从
data.binance_public 导入的同名 `_get_json(client, path, ...)` 整个遮蔽了。
结果 query_symbol_detail 把 httpx.Client 当成 url 传进去，每次都抛
`TypeError: Invalid type for url`，100% 失败且没有任何告警。
主 Agent 因此连续 5 轮重试该工具却始终拿不到 K 线，轮次耗尽后整体降级为
no_trade（reasoning 里那句"主Agent连续多轮未输出最终JSON"）。

覆盖点：
1. 结构性：不允许再出现遮蔽 import 的同名 def（这是本次事故的直接原因）
2. 结构性：market_query._get_json 必须就是 binance_public 的那个对象
3. 功能性：query_symbol_detail 能返回完整的指标 + K 线包
4. 功能性：execute_tool 入口不返回 error
5. 功能性：query_symbol_cards / query_top_symbols 不受影响
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import httpx  # noqa: E402

from data import binance_public, market_query  # noqa: E402
from data.market_query import (  # noqa: E402
    TOOL_HANDLERS,
    execute_tool,
    query_symbol_cards,
    query_symbol_detail,
    query_top_symbols,
)
from data.binance_public import prepare_closed_klines  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def safe_call(fn, *args, **kwargs) -> dict:
    """调用工具并吞掉异常：让测试输出干净的 [FAIL] 而不是整段崩溃。

    生产路径上 execute_tool 也会捕获异常转成 {"error": ...}，这里保持一致。
    遮蔽 bug 复现时 query_symbol_detail 会直接抛 TypeError，正是要靠它兜住。
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- 合成行情数据
def _candles(n: int = 30, base: float = 60000.0,
             observed_at_ms: int | None = None, interval: str = "15m") -> list[list]:
    """缓慢上涨 + 固定振幅的合成 K 线，保证 ATR / 量比 / 趋势都算得出来。"""
    out = []
    interval_ms = binance_public.INTERVAL_SECONDS[interval] * 1000
    observed = observed_at_ms or int(time.time() * 1000)
    current_open = observed // interval_ms * interval_ms
    first_open = current_open - n * interval_ms
    for i in range(n):
        o = base + i * 10.0
        open_time = first_open + i * interval_ms
        out.append([
            open_time,                         # 0 open_time
            f"{o:.2f}",                        # 1 open
            f"{o + 120:.2f}",                  # 2 high
            f"{o - 120:.2f}",                  # 3 low
            f"{o + 60:.2f}",                   # 4 close
            "100.0",                           # 5 volume
            open_time + interval_ms - 1,        # 6 close_time
            f"{o * 100:.2f}",                  # 7 quote_volume
            500,                               # 8 trades
        ])
    return out


_TICKER_24H = {"symbol": "BTCUSDT", "priceChangePercent": "1.23", "quoteVolume": "123456789.0"}
_PREMIUM = {"symbol": "BTCUSDT", "markPrice": "60100.5", "lastFundingRate": "0.0001"}
_OI = {"openInterest": "12345.6"}
_PRICE_ONE = {"symbol": "BTCUSDT", "price": "60100.5"}
_ALL_TICKERS = [
    {"symbol": "BTCUSDT", "lastPrice": "60100", "quoteVolume": "9e9", "priceChangePercent": "1.2"},
    {"symbol": "ETHUSDT", "lastPrice": "3000", "quoteVolume": "5e9", "priceChangePercent": "-0.4"},
    {"symbol": "SOLUSDT", "lastPrice": "150", "quoteVolume": "1e9", "priceChangePercent": "2.1"},
]

SEEN: list[str] = []


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """替换 httpx.Client：按 URL 返回合成数据，并记录被请求的路径。"""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self) -> None:
        return None

    @property
    def is_closed(self) -> bool:
        return False

    def get(self, url: str, params=None, timeout=None, **kwargs):
        url = str(url)
        SEEN.append(url)
        if url.endswith("/fapi/v1/klines"):
            return _FakeResponse(_candles(interval=str((params or {}).get("interval") or "15m")))
        if url.endswith("/fapi/v1/ticker/24hr"):
            if params and params.get("symbol"):
                return _FakeResponse(dict(_TICKER_24H, symbol=params["symbol"]))
            return _FakeResponse(_ALL_TICKERS)
        if url.endswith("/fapi/v1/premiumIndex"):
            return _FakeResponse(_PREMIUM)
        if url.endswith("/fapi/v1/openInterest"):
            return _FakeResponse(_OI)
        if url.endswith("/fapi/v1/ticker/price"):
            if params and params.get("symbol"):
                return _FakeResponse(dict(_PRICE_ONE, symbol=params["symbol"]))
            return _FakeResponse([{"symbol": "BTCUSDT", "price": "60100.5"}])
        if url.endswith("/fapi/v1/exchangeInfo"):
            return _FakeResponse({"symbols": [{"symbol": s["symbol"], "status": "TRADING"}
                                              for s in _ALL_TICKERS]})
        return _FakeResponse({})


def install_fake_http() -> None:
    httpx.Client = _FakeClient  # type: ignore[misc, assignment]
    market_query.httpx.Client = _FakeClient  # type: ignore[misc]
    binance_public.httpx.Client = _FakeClient  # type: ignore[misc]
    # 冷却/节流是进程内状态，mock 环境下直接短路，避免测试被限速逻辑干扰
    market_query.cooldown_remaining = lambda: 0  # type: ignore[misc]
    binance_public.cooldown_remaining = lambda: 0  # type: ignore[misc]
    binance_public.throttle = lambda *_a, **_k: None  # type: ignore[misc]
    market_query._TOOL_CACHE.clear()


def main() -> int:
    global PASS, FAIL
    print(f"\n{'=' * 68}\n行情工具回归测试（不联网）\n{'=' * 68}")

    # ---------------- 1. 结构性：不允许遮蔽 import 的同名 def ----------------
    print("\n[1] 名字遮蔽防护（本次事故的直接原因）")
    src = Path(market_query.__file__).read_text(encoding="utf-8")
    check("market_query.py 内不存在本地 def _get_json", "def _get_json(" not in src)
    check("market_query._get_json 就是 binance_public._get_json",
          market_query._get_json is binance_public._get_json)
    check("本地共享连接版改名为 _shared_get", hasattr(market_query, "_shared_get"))

    # ---------------- 2. 功能性 ----------------
    install_fake_http()

    print("\n[2] query_symbol_detail 返回完整指标与 K 线")
    r = safe_call(query_symbol_detail, "BTCUSDT", interval="15m", limit=30,
                  include_candles=True)
    check("返回 dict 且无 error 字段", isinstance(r, dict) and "error" not in r, str(r)[:200])
    for key in ("symbol", "last_price", "atr", "atr_pct", "volume_ratio", "trend",
                "funding_rate", "mark_price", "high_window", "low_window", "candles"):
        check(f"  含字段 {key}", key in r)
    check("symbol 已规范为 BTCUSDT", r.get("symbol") == "BTCUSDT", str(r.get("symbol")))
    check("last_price > 0", float(r.get("last_price") or 0) > 0, str(r.get("last_price")))
    check("K 线包有 20 根", (r.get("candles") or {}).get("n") == 20,
          str((r.get("candles") or {}).get("n")))
    check("确实请求过 /fapi/v1/klines",
          any(u.endswith("/fapi/v1/klines") for u in SEEN))

    print("\n[3] include_candles=False 时不返回 K 线（省 token）")
    market_query._TOOL_CACHE.clear()
    r2 = safe_call(query_symbol_detail, "ETHUSDT", interval="1h", include_candles=False)
    check("无 error", "error" not in r2, str(r2)[:200])
    check("candles.n == 0", (r2.get("candles") or {}).get("n") == 0)

    print("\n[4] execute_tool 入口（主 Agent 实际调用路径）")
    market_query._TOOL_CACHE.clear()
    out = execute_tool("query_symbol_detail",
                       {"symbol": "SOLUSDT", "interval": "15m", "limit": 30,
                        "include_candles": True})
    check("不返回 error", "error" not in out, str(out)[:200])
    check("不含 'Invalid type for url'",
          "Invalid type for url" not in str(out))
    check("未知工具返回明确错误", "unknown tool" in str(execute_tool("no_such_tool", {})))

    print("\n[5] 其他工具未受影响")
    market_query._TOOL_CACHE.clear()
    cards = safe_call(query_symbol_cards, ["BTCUSDT", "ETHUSDT"], top_n=5)
    check("query_symbol_cards 无 error", "error" not in cards, str(cards)[:200])
    market_query._TOOL_CACHE.clear()
    top = safe_call(query_top_symbols, top_n=5)
    check("query_top_symbols 无 error", "error" not in top, str(top)[:200])
    check("TOOL_HANDLERS 覆盖全部声明的工具",
          set(TOOL_HANDLERS) == {"query_symbol_detail", "query_symbol_cards", "query_top_symbols"},
          str(set(TOOL_HANDLERS)))

    print("\n[6] 未完成 K 线绝不参与指标")
    interval_ms = 15 * 60 * 1000
    observed_at_ms = 1_700_000_000_000 + 30 * interval_ms
    rows = _candles(30, observed_at_ms=observed_at_ms)
    live = list(rows[-1])
    live[0] = observed_at_ms - 60_000
    live[4] = "999999.0"
    live[7] = "999999999999.0"
    live[6] = observed_at_ms + interval_ms - 60_001
    closed, live_bar, quality = prepare_closed_klines(
        rows[:-1] + [live], "15m", observed_at_ms=observed_at_ms, min_closed=20,
    )
    check("当前 bar 被单独识别", live_bar is not None and quality["live_bar_present"])
    check("当前 bar 未进入闭合集合", len(closed) == 29 and float(closed[-1][4]) != 999999.0)
    check("闭合数据质量完整", quality["complete"] and "insufficient_closed_klines" not in quality["issues"],
          str(quality))

    stale_rows = _candles(30, observed_at_ms=observed_at_ms)
    _, _, stale_quality = prepare_closed_klines(
        stale_rows, "15m", observed_at_ms=observed_at_ms + 10 * interval_ms,
        min_closed=20,
    )
    check("陈旧闭合 K 线有稳定原因码",
          not stale_quality["fresh"] and "stale_last_closed_kline" in stale_quality["issues"],
          str(stale_quality))

    unordered = _candles(30, observed_at_ms=observed_at_ms)
    unordered[-1], unordered[-2] = unordered[-2], unordered[-1]
    _, _, unordered_quality = prepare_closed_klines(
        unordered, "15m", observed_at_ms=observed_at_ms, min_closed=20,
    )
    check("乱序 K 线不能通过质量检查",
          not unordered_quality["ok"] and "non_monotonic_klines" in unordered_quality["issues"],
          str(unordered_quality))

    print("\n[7] 全市场简报链同样使用闭合 K 线口径")
    analyzed = safe_call(
        binance_public.analyze_symbol, _FakeClient(), "BTCUSDT", dict(_TICKER_24H, lastPrice="60100.5"),
    )
    check("analyze_symbol 无 error", "error" not in analyzed, str(analyzed)[:200])
    check("各周期数据质量均通过", bool((analyzed.get("data_quality") or {}).get("ok")),
          str(analyzed.get("data_quality"))[:300])
    check("简报最新价与实时 ticker 一致", analyzed.get("last_price") == 60100.5,
          str(analyzed.get("last_price")))

    print(f"\n{'=' * 68}")
    print(f"结果：PASS {PASS} / FAIL {FAIL}")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
