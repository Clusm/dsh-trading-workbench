"""Binance USD-M私有User Data Stream传输层。

本组件默认不启动，只把原始事件写入幂等收件箱，不更新账户镜像、不执行订单。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Callable

import httpx

from data.binance_transport import endpoint_weight
from data.rate_limit import (
    ExecutionRateLimited,
    PublicMarketCooldown,
    cooldown_remaining,
    note_418,
    note_rate_limit,
    observe_failure,
    observe_response,
    reserve_request,
)
from exchange.event_inbox import ExchangeEventInbox
from exchange.event_models import ExchangeEvent


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


class EventQueueFull(RuntimeError):
    """内存收件队列已满；调用方应断线并触发REST对账。"""


def _proxy() -> str | None:
    if (os.environ.get("MTA_NO_PROXY", "").strip().lower() in
            {"1", "true", "yes", "on"}) or os.environ.get("NO_PROXY") == "*":
        return None
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


class BinanceUserDataStream:
    """管理 Binance USD-M listenKey、WebSocket 接收队列与事件持久化。

    网络线程只负责收包和排队，消费线程先写幂等收件箱再调用镜像；队列溢出或持久化
    失败会设置 ``reconcile_required``，要求 REST 对账恢复真值，不能静默丢事件。
    """
    def __init__(self, *, api_key: str, rest_base_url: str, environment: str,
                 inbox: ExchangeEventInbox, keepalive_minutes: float = 30,
                 planned_reconnect_hours: float = 23, reconnect_seconds: float = 5,
                 queue_size: int = 5000,
                 event_applier: Callable[[ExchangeEvent], bool] | None = None,
                 trust_env: bool = True) -> None:
        if environment not in STREAM_BASES:
            raise ValueError(f"私有流不支持执行环境: {environment!r}")
        self.api_key = str(api_key)
        self.rest_base_url = str(rest_base_url).rstrip("/")
        self.environment = environment
        self.inbox = inbox
        self.event_applier = event_applier
        self.keepalive_seconds = max(60.0, float(keepalive_minutes) * 60)
        self.planned_reconnect_seconds = max(300.0, float(planned_reconnect_hours) * 3600)
        self.reconnect_seconds = max(1.0, float(reconnect_seconds))
        self.queue_size = max(1, int(queue_size))
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.queue_size)
        self._client = httpx.AsyncClient(timeout=15.0, trust_env=trust_env)
        self._stop = False
        self._transport_connected = False
        self.state = "disabled"
        self.started_at: float | None = None
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self.last_keepalive_at: float | None = None
        self.last_disconnect_at: float | None = None
        self.last_error: str | None = None
        self.reconnects = 0
        self.events_received = 0
        self.events_inserted = 0
        self.events_applied = 0
        self.queue_overflows = 0
        self.persistence_errors = 0
        self.last_persisted_at: float | None = None
        self.last_db_write_latency_ms: float | None = None
        self.reconcile_required = False

    def stop(self) -> None:
        self._stop = True

    def status(self) -> dict[str, Any]:
        """返回可公开到健康接口的脱敏状态，绝不包含API Key/listenKey/URL。"""
        return {
            "enabled": True,
            "observe_only": True,
            "environment": self.environment,
            "state": self.state,
            "active": self.state == "live",
            "started_at": self.started_at,
            "connected_at": self.connected_at,
            "last_event_at": self.last_event_at,
            "last_keepalive_at": self.last_keepalive_at,
            "last_disconnect_at": self.last_disconnect_at,
            "last_error": self.last_error,
            "reconnects": self.reconnects,
            "events_received": self.events_received,
            "events_inserted": self.events_inserted,
            "events_applied": self.events_applied,
            "queue_depth": self._queue.qsize(),
            "queue_capacity": self.queue_size,
            "queue_overflows": self.queue_overflows,
            "persistence_errors": self.persistence_errors,
            "last_persisted_at": self.last_persisted_at,
            "last_db_write_latency_ms": self.last_db_write_latency_ms,
            "reconcile_required": self.reconcile_required,
        }

    def _enqueue_payload(self, payload: dict[str, Any]) -> None:
        self.events_received += 1
        self.last_event_at = time.time()
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull as exc:
            self.queue_overflows += 1
            self.reconcile_required = True
            self.state = "degraded"
            self.last_error = "EventQueueFull"
            raise EventQueueFull("私有事件队列已满") from exc

    async def _consume_inbox(self) -> None:
        """单消费者串行落库；短暂DB错误时保留当前事件并持续重试。"""
        while not self._stop or not self._queue.empty():
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                while True:
                    started = time.perf_counter()
                    try:
                        event, inserted = await asyncio.to_thread(
                            self.inbox.receive, payload, source="user_stream")
                        applied = event.event_type == "listenKeyExpired"
                        if (inserted and self.event_applier is not None and
                                event.event_type != "listenKeyExpired"):
                            applied = bool(await asyncio.to_thread(self.event_applier, event))
                        if applied:
                            await asyncio.to_thread(
                                self.inbox.mark, event.event_key, "APPLIED")
                            self.events_applied += 1
                        self.last_db_write_latency_ms = round(
                            (time.perf_counter() - started) * 1000, 3)
                        self.last_persisted_at = time.time()
                        if inserted:
                            self.events_inserted += 1
                        if self._transport_connected and self.last_error == "PersistenceError":
                            self.state = "live"
                            self.last_error = None
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.persistence_errors += 1
                        self.reconcile_required = True
                        self.state = "degraded"
                        self.last_error = "PersistenceError"
                        if self._stop:
                            return
                        await asyncio.sleep(self.reconnect_seconds)
            finally:
                self._queue.task_done()

    def connection_url(self, listen_key: str) -> str:
        base = STREAM_BASES[self.environment]
        if self.environment == "live":
            return f"{base}/private/ws?listenKey={listen_key}&events={USER_EVENTS}"
        # 非生产环境当前仍使用各自流主机的listenKey原始流格式。
        return f"{base}/ws/{listen_key}"

    def _observe_rest_response(self, response: httpx.Response,
                               request_id: str | None = None) -> None:
        """把生产 listenKey 的权重/限流反馈并入共享出口状态。

        私有流是账户真值通道，critical 请求只绕过软预算；遇到同一出口的硬冷却时
        不再盲目重试，交由 REST 对账恢复。Demo/Testnet 使用独立主机，严禁拿其
        响应头覆盖生产 IP 的权重水位。
        """
        if self.environment != "live":
            return
        path = "/fapi/v1/listenKey"
        retry_after = None
        if response.status_code in {418, 429}:
            default = 480.0 if response.status_code == 418 else 60.0
            maximum = 604800.0 if response.status_code == 418 else 3600.0
            try:
                retry_after = float(response.headers.get("Retry-After") or default)
            except (TypeError, ValueError):
                retry_after = default
            retry_after = min(maximum, max(5.0, retry_after))
        observe_response(
            response.headers, path=path, weight=endpoint_weight(path),
            request_id=request_id, status=response.status_code,
            retry_after_seconds=retry_after,
            error_code=response.status_code if response.status_code >= 400 else None,
            error_message=getattr(response, "text", None),
        )
        if response.status_code not in {418, 429}:
            return
        if response.status_code == 418:
            note_418(retry_after)
        else:
            note_rate_limit(retry_after, status=429)

    async def _listen_key(self, method: str) -> str | None:
        path = "/fapi/v1/listenKey"
        request_id: str | None = None
        if self.environment == "live":
            request_id = reserve_request(
                path, weight=endpoint_weight(path), priority="critical",
                endpoint_group="signed_execution", source="user_stream",
                environment=self.environment,
            )
        try:
            response = await self._client.request(
                method, f"{self.rest_base_url}{path}",
                headers={"X-MBX-APIKEY": self.api_key},
            )
        except httpx.TransportError as exc:
            if request_id is not None:
                observe_failure(
                    request_id, path=path, weight=endpoint_weight(path),
                    error_type=type(exc).__name__, error_message=exc,
                )
            raise
        self._observe_rest_response(response, request_id=request_id)
        if response.status_code in {418, 429}:
            raise ExecutionRateLimited(
                f"HTTP {response.status_code}: user stream request rate limited"
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
            self.last_keepalive_at = time.time()

    async def _session(self) -> None:
        import websockets

        self.state = "connecting"
        listen_key = await self._listen_key("POST")
        if not listen_key:
            raise RuntimeError("无法创建listenKey")
        url = self.connection_url(listen_key)
        kwargs: dict[str, Any] = {
            "open_timeout": 15.0,
            "ping_interval": 20.0,
            "ping_timeout": 20.0,
        }
        # websockets 15 默认会自动发现Windows系统代理；没有项目级代理时也要
        # 显式传None，否则可能命中注册表里已失效的本地端口。
        kwargs["proxy"] = _proxy()
        keepalive = asyncio.create_task(self._keepalive(listen_key))
        started = time.monotonic()
        try:
            async with websockets.connect(url, **kwargs) as ws:
                self._transport_connected = True
                self.state = "live"
                self.connected_at = time.time()
                self.last_error = None
                while not self._stop:
                    if time.monotonic() - started >= self.planned_reconnect_seconds:
                        return
                    if keepalive.done() and not keepalive.cancelled():
                        error = keepalive.exception()
                        if error is not None:
                            raise RuntimeError(
                                f"listenKey_keepalive_failed:{type(error).__name__}"
                            ) from error
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        if keepalive.done() and not keepalive.cancelled():
                            error = keepalive.exception()
                            if error is not None:
                                raise RuntimeError(
                                    f"listenKey_keepalive_failed:{type(error).__name__}"
                                ) from error
                        continue
                    payload = json.loads(raw)
                    self._enqueue_payload(payload)
                    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                    if body.get("e") == "listenKeyExpired":
                        return
        finally:
            self._transport_connected = False
            self.last_disconnect_at = time.time()
            keepalive.cancel()
            await asyncio.gather(keepalive, return_exceptions=True)
            try:
                await self._listen_key("DELETE")
            except Exception:
                pass

    async def serve(self) -> None:
        self._stop = False
        self.started_at = time.time()
        consumer = asyncio.create_task(self._consume_inbox())
        try:
            while not self._stop:
                try:
                    await self._session()
                    if not self._stop:
                        self.reconnects += 1
                except asyncio.CancelledError:
                    raise
                except PublicMarketCooldown:
                    # 同一出口硬冷却时不按固定5秒重建，避免只在本地循环制造噪声；
                    # 冷却到期后再做一次 listenKey/WS 恢复尝试。
                    self.last_error = "PublicMarketCooldown"
                    self.state = "degraded"
                    self.reconnects += 1
                    await asyncio.sleep(min(300.0, max(5.0, cooldown_remaining())))
                except Exception as exc:
                    # 异常文本可能带连接URL，绝不把listenKey写入状态或日志。
                    self.last_error = type(exc).__name__
                    self.state = "degraded"
                    self.reconnects += 1
                if not self._stop:
                    await asyncio.sleep(self.reconnect_seconds)
        finally:
            self._stop = True
            try:
                await asyncio.wait_for(self._queue.join(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)
            self.state = "stopped"

    async def close(self) -> None:
        self.stop()
        await self._client.aclose()
