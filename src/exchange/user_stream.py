"""Binance USD-M私有User Data Stream传输层。

本组件默认不启动，只把原始事件写入幂等收件箱，不更新账户镜像、不执行订单。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx

from exchange.event_inbox import ExchangeEventInbox


USER_EVENTS = "/".join([
    "ORDER_TRADE_UPDATE",
    "ACCOUNT_UPDATE",
    "MARGIN_CALL",
    "ACCOUNT_CONFIG_UPDATE",
    "listenKeyExpired",
])

STREAM_BASES = {
    "live": "wss://fstream.binance.com",
    "demo": "wss://fstream.binancefuture.com",
    "testnet": "wss://stream.binancefuture.com",
}


def _proxy() -> str | None:
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


class BinanceUserDataStream:
    def __init__(self, *, api_key: str, rest_base_url: str, environment: str,
                 inbox: ExchangeEventInbox, keepalive_minutes: float = 30,
                 planned_reconnect_hours: float = 23, reconnect_seconds: float = 5,
                 trust_env: bool = True) -> None:
        if environment not in STREAM_BASES:
            raise ValueError(f"私有流不支持执行环境: {environment!r}")
        self.api_key = str(api_key)
        self.rest_base_url = str(rest_base_url).rstrip("/")
        self.environment = environment
        self.inbox = inbox
        self.keepalive_seconds = max(60.0, float(keepalive_minutes) * 60)
        self.planned_reconnect_seconds = max(300.0, float(planned_reconnect_hours) * 3600)
        self.reconnect_seconds = max(1.0, float(reconnect_seconds))
        self._client = httpx.AsyncClient(timeout=15.0, trust_env=trust_env)
        self._stop = False
        self.state = "disabled"
        self.last_event_at: float | None = None
        self.last_error: str | None = None
        self.reconnects = 0

    def stop(self) -> None:
        self._stop = True

    def connection_url(self, listen_key: str) -> str:
        base = STREAM_BASES[self.environment]
        if self.environment == "live":
            return f"{base}/private/ws?listenKey={listen_key}&events={USER_EVENTS}"
        # 非生产环境当前仍使用各自流主机的listenKey原始流格式。
        return f"{base}/ws/{listen_key}"

    async def _listen_key(self, method: str) -> str | None:
        response = await self._client.request(
            method, f"{self.rest_base_url}/fapi/v1/listenKey",
            headers={"X-MBX-APIKEY": self.api_key},
        )
        response.raise_for_status()
        if method == "DELETE":
            return None
        data = response.json()
        key = str(data.get("listenKey") or "")
        if not key:
            raise RuntimeError("Binance未返回listenKey")
        return key

    async def _keepalive(self, listen_key: str) -> None:
        while not self._stop:
            await asyncio.sleep(self.keepalive_seconds)
            if self._stop:
                return
            refreshed = await self._listen_key("PUT")
            if refreshed and refreshed != listen_key:
                raise RuntimeError("续期返回了不同listenKey，需要重建连接")

    async def _session(self) -> None:
        import websockets

        listen_key = await self._listen_key("POST")
        if not listen_key:
            raise RuntimeError("无法创建listenKey")
        url = self.connection_url(listen_key)
        kwargs: dict[str, Any] = {
            "open_timeout": 15.0,
            "ping_interval": 20.0,
            "ping_timeout": 20.0,
        }
        proxy = _proxy()
        if proxy:
            kwargs["proxy"] = proxy
        keepalive = asyncio.create_task(self._keepalive(listen_key))
        started = time.monotonic()
        try:
            self.state = "connecting"
            async with websockets.connect(url, **kwargs) as ws:
                self.state = "live"
                self.last_error = None
                while not self._stop:
                    if time.monotonic() - started >= self.planned_reconnect_seconds:
                        return
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        continue
                    payload = json.loads(raw)
                    event, _ = self.inbox.receive(payload, source="user_stream")
                    self.last_event_at = time.time()
                    if event.event_type == "listenKeyExpired":
                        return
        finally:
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)
            try:
                await self._listen_key("DELETE")
            except Exception:
                pass

    async def serve(self) -> None:
        self._stop = False
        while not self._stop:
            try:
                await self._session()
                if not self._stop:
                    self.reconnects += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 异常文本可能带连接URL，绝不把listenKey写入状态或日志。
                self.last_error = type(exc).__name__
                self.state = "degraded"
                self.reconnects += 1
            if not self._stop:
                await asyncio.sleep(self.reconnect_seconds)
        self.state = "stopped"

    async def close(self) -> None:
        self.stop()
        await self._client.aclose()
