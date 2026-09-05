"""只读探测Binance USD-M WebSocket新路由；不使用API密钥，不提交订单。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.net_env import apply_proxy_env  # noqa: E402


def _proxy() -> str | None:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


async def probe(name: str, url: str, timeout: float) -> dict[str, object]:
    import websockets

    started = time.monotonic()
    kwargs: dict[str, object] = {
        "open_timeout": timeout,
        "ping_interval": 20.0,
        "ping_timeout": 20.0,
    }
    proxy = _proxy()
    if proxy:
        kwargs["proxy"] = proxy
    try:
        async with websockets.connect(url, **kwargs) as ws:  # type: ignore[arg-type]
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        event = (data or payload).get("e") if isinstance(data or payload, dict) else None
        return {
            "name": name,
            "ok": True,
            "event": event,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": round((time.monotonic() - started) * 1000),
        }


async def run(timeout: float) -> int:
    routes = [
        (
            "public_book_ticker",
            "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker",
        ),
        (
            "market_ticker",
            "wss://fstream.binance.com/market/stream?streams=btcusdt@ticker",
        ),
    ]
    results = [await probe(name, url, timeout) for name, url in routes]
    print(json.dumps({"proxy_configured": bool(_proxy()), "results": results},
                     ensure_ascii=False, indent=2))
    return 0 if all(bool(item["ok"]) for item in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=12.0)
    args = parser.parse_args()
    apply_proxy_env(ROOT / ".env")
    return asyncio.run(run(max(2.0, args.timeout)))


if __name__ == "__main__":
    raise SystemExit(main())
