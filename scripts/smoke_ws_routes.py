"""USD-M WebSocket新路由和行情帧解析回归；全离线。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.price_stream import PriceStream  # noqa: E402
import data.price_stream as module  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


print("\n[1] ticker使用/market组合流")
stream = PriceStream(lambda: ["BTCUSDT", "ETHUSDT"])
names = stream._stream_names()
url = stream.connection_url(names)
check("symbol标准化且排序", names == ["btcusdt@ticker", "ethusdt@ticker"])
check("使用USD-M market路由", url.startswith("wss://fstream.binance.com/market/stream?streams="))
check("不再使用旧无路由地址", ".com/stream?" not in url)

print("\n[2] 组合流ticker帧写入价格缓存")
received: list[tuple[str, float]] = []
original = module.push_stream_price
module.push_stream_price = lambda symbol, price: received.append((symbol, price))
try:
    stream._handle_frame(json.dumps({
        "stream": "btcusdt@ticker",
        "data": {"e": "24hrTicker", "s": "BTCUSDT", "c": "62000.50"},
    }))
finally:
    module.push_stream_price = original
check("成功解析symbol与最新价", received == [("BTCUSDT", 62000.5)])
check("收到业务帧后状态为live", stream.state == "live")
rotation = stream._rotation_seconds()
check("公共流轮换窗口有界",
      module.PUBLIC_STREAM_ROTATION_SECONDS - module.PUBLIC_STREAM_ROTATION_JITTER_SECONDS
      <= rotation <=
      module.PUBLIC_STREAM_ROTATION_SECONDS + module.PUBLIC_STREAM_ROTATION_JITTER_SECONDS)
stream._last_frame_at = time.time() - module.STREAM_STALE_SECONDS - 1
check("无业务帧超过阈值标记stale", stream.status()["market_stale"] is True)

print("\n结果: 7 通过 / 0 失败")
