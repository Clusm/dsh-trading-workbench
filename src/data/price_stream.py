"""合约行情 WebSocket 订阅。

设计要点
--------
1. **纯增量**：推送只负责把最新价写进 `market_query` 的价格缓存，不碰任何业务逻辑。
   缓存命中时 `get_live_prices` 直接返回，REST 请求归零。
2. **自动降级**：推送断开后缓存自然过期，`get_live_prices` 自动回落到 REST 轮询。
   不需要任何"切换"代码，也永远不会因为推送挂掉而导致页面无数据。
3. **环境自适应**：部分出口会对 `fstream.binance.com`（永续合约推送域名）做**选择性放行**——
   TCP 建连和请求-响应式的交互都正常，但服务端主动推送的数据流会被静默丢弃。
   实测（同一代理、同一时刻）：

   | 域名                      | 业务     | REST | WebSocket            |
   |---------------------------|----------|------|----------------------|
   | api / stream.binance.com  | 现货     | 200  | ✓ 有推送（554帧/10s）|
   | fapi / fstream.binance.com| 永续合约 | 200  | ✗ 零帧（连错误也不回）|
   | dapi / dstream.binance.com| 交割合约 | —    | ✓ 有推送             |

   注意它既不是按业务类型拦（交割合约同样是衍生品却放行），也不是按协议拦（REST 全通），
   而是**精确到域名的黑名单**。这种拦截在连接阶段无法判定：握手 0.5s 内完成，
   主动发 SUBSCRIBE 还能收到 `{"result":null}` 回执，只有"首帧超时"能识别。
   因此首帧超时是唯一的可用判据；连续失败后拉长探测间隔，避免无效重连刷屏。

调用方只需 `await stream.serve()`，它是一个永不退出的后台协程。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Awaitable, Callable, Iterable

from data.kline_store import push_kline_event
from data.market_stream_cache import push_book, push_mark, push_ticker
from data.market_query import push_stream_price

FSTREAM_BASE = "wss://fstream.binance.com"
# Binance 2026-09-04 文档已将 ticker/markPrice/kline 等常规行情迁移到
# /market。旧的无路由地址只再推 /public 类数据，会出现“握手成功但零帧”。
MARKET_COMBINED_PATH = "/market/stream?streams={streams}"
PUBLIC_COMBINED_PATH = "/public/stream?streams={streams}"

# 握手成功但迟迟收不到首帧，基本可以判定该出口不支持合约推送
FIRST_FRAME_TIMEOUT = 15.0
# 连续失败达到这个次数后，把重连间隔从"快速重试"切换到"慢速探测"
FAILURES_BEFORE_BACKOFF = 3
FAST_RETRY_SECONDS = 5.0
SLOW_RETRY_SECONDS = 120.0
# 订阅列表多久核对一次（持仓变动后自动跟上）
RESUBSCRIBE_INTERVAL = 15.0
# 只订阅两个基础周期，避免20币×7周期形成数百帧/秒；高周期由kline_store聚合。
KLINE_INTERVALS = ("1m", "15m")
# 公共组合流按有界周期主动轮换，避免连接长期存活后服务端/中间代理状态漂移。
# 轮换窗口小幅随机化，多个实例不会在同一秒同时重连。
PUBLIC_STREAM_ROTATION_SECONDS = 20 * 60 * 60
PUBLIC_STREAM_ROTATION_JITTER_SECONDS = 5 * 60
PUBLIC_STREAM_RETRY_JITTER_SECONDS = (1.0, 5.0)
# 组合流通常包含ticker和K线；超过此时间没有任何有效业务帧就视为stale，主动重连。
STREAM_STALE_SECONDS = 45.0


def _proxy() -> str | None:
    if (os.environ.get("MTA_NO_PROXY", "").strip().lower() in
            {"1", "true", "yes", "on"}) or os.environ.get("NO_PROXY") == "*":
        return None
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = (os.environ.get(key) or "").strip()
        if val:
            return val
    return None


class PriceStream:
    """订阅合约 ticker 推送，写入共享价格缓存。"""

    def __init__(
        self,
        symbols_provider: Callable[[], Iterable[str]],
        on_status: Callable[[str, str], Awaitable[None]] | None = None,
        kline_intervals: Iterable[str] | None = None,
    ) -> None:
        self._symbols_provider = symbols_provider
        self._on_status = on_status
        requested_intervals = tuple(str(item) for item in (kline_intervals or KLINE_INTERVALS))
        self._kline_intervals = tuple(item for item in requested_intervals
                                      if item in KLINE_INTERVALS) or KLINE_INTERVALS
        self._subscribed: set[str] = set()
        self._failures = 0
        self._last_price_at = 0.0
        self._last_frame_at = 0.0
        self._messages = 0
        self._reconnects = 0
        self._rotations = 0
        self._book_subscribed: set[str] = set()
        self._book_active = False
        self._book_last_frame_at = 0.0
        self._book_failures = 0
        self._book_rotations = 0
        self._state = "init"  # init | live | blocked | disabled
        self._stop = False
        self._market_rotated = False
        self._book_rotated = False

    # ---- 对外状态 ----
    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        """推送是否在真正供数（用于前端展示数据链路）。"""
        return self._state == "live" and time.time() - self._last_price_at < 15.0

    def status_text(self) -> str:
        if self.active:
            return "WebSocket 推送中"
        if self._state == "blocked":
            return "推送不可用（REST 轮询中）"
        if self._state == "disabled":
            return "推送未启用（REST 轮询中）"
        if not self._wanted_symbols():
            return "无持仓，无需推送"
        return "正在连接推送…"

    def status(self) -> dict[str, object]:
        now = time.time()
        return {
            "enabled": True,
            "state": self._state,
            "active": self.active,
            "symbols": len(self._wanted_symbols()),
            "subscribed_streams": len(self._subscribed),
            "messages": self._messages,
            "reconnects": self._reconnects,
            "rotations": self._rotations,
            "failures": self._failures,
            "last_frame_at": self._last_frame_at or None,
            "last_frame_age_seconds": (round(now - self._last_frame_at, 3)
                                       if self._last_frame_at else None),
            "text": self.status_text(),
            "book_ticker_active": (self._book_active and
                                   now - self._book_last_frame_at < 15.0),
            "book_ticker_streams": len(self._book_subscribed),
            "book_ticker_failures": self._book_failures,
            "book_ticker_rotations": self._book_rotations,
            "market_stale": bool(
                self._last_frame_at and now - self._last_frame_at > STREAM_STALE_SECONDS
            ),
            "book_ticker_stale": bool(
                self._book_last_frame_at
                and now - self._book_last_frame_at > STREAM_STALE_SECONDS
            ),
        }

    def stop(self) -> None:
        self._stop = True

    # ---- 主循环 ----
    async def serve(self) -> None:
        """并行维护常规行情与高频最优买卖价，两条链路独立降级。"""
        market = asyncio.create_task(self._serve_market())
        book = asyncio.create_task(self._serve_books())
        try:
            await asyncio.gather(market, book)
        finally:
            market.cancel()
            book.cancel()
            await asyncio.gather(market, book, return_exceptions=True)

    async def _serve_market(self) -> None:
        """永不退出的常规行情协程：连接、收帧、断线退避。"""
        try:
            import websockets  # noqa: F401  依赖缺失是永久性的，直接降级不再重试
        except ImportError:
            self._state = "disabled"
            await self._emit()
            return

        while not self._stop:
            self._market_rotated = False
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._note_failure()
            if self._stop:
                return
            if self._market_rotated:
                delay = random.uniform(*PUBLIC_STREAM_RETRY_JITTER_SECONDS)
            else:
                delay = (SLOW_RETRY_SECONDS if self._failures >= FAILURES_BEFORE_BACKOFF
                         else FAST_RETRY_SECONDS)
            await asyncio.sleep(delay)

    async def _serve_books(self) -> None:
        """bookTicker使用/public路由；失败不拖垮/market上的价格和K线。"""
        try:
            import websockets  # noqa: F401
        except ImportError:
            return
        while not self._stop:
            self._book_rotated = False
            try:
                await self._book_session()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._book_failures += 1
                self._book_active = False
                self._book_subscribed.clear()
            if not self._stop:
                if self._book_rotated:
                    delay = random.uniform(*PUBLIC_STREAM_RETRY_JITTER_SECONDS)
                else:
                    delay = (SLOW_RETRY_SECONDS if self._book_failures >= FAILURES_BEFORE_BACKOFF
                             else FAST_RETRY_SECONDS)
                await asyncio.sleep(delay)

    async def _book_session(self) -> None:
        import websockets

        streams = self._book_stream_names()
        if not streams:
            await asyncio.sleep(FAST_RETRY_SECONDS)
            return
        url = FSTREAM_BASE + PUBLIC_COMBINED_PATH.format(streams="/".join(streams))
        kwargs: dict[str, object] = {
            "open_timeout": 15.0,
            "ping_interval": 20.0,
            "ping_timeout": 20.0,
            "proxy": _proxy(),
        }
        async with websockets.connect(url, **kwargs) as ws:  # type: ignore[arg-type]
            self._book_subscribed = set(streams)
            first = await asyncio.wait_for(ws.recv(), timeout=FIRST_FRAME_TIMEOUT)
            self._handle_book_frame(first)
            self._book_failures = 0
            self._book_active = True
            rotation_deadline = time.monotonic() + self._rotation_seconds()
            while not self._stop:
                remaining = rotation_deadline - time.monotonic()
                if remaining <= 0:
                    self._book_rotated = True
                    self._book_rotations += 1
                    return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(30.0, remaining))
                except asyncio.TimeoutError:
                    if time.monotonic() >= rotation_deadline:
                        self._book_rotated = True
                        self._book_rotations += 1
                        return
                    if (self._book_last_frame_at
                            and time.time() - self._book_last_frame_at > STREAM_STALE_SECONDS):
                        raise RuntimeError("public_book_stream_stale")
                    continue
                self._handle_book_frame(raw)

    async def _session(self) -> None:
        import websockets  # 延迟导入：不需要推送时不强制依赖

        streams = self._market_stream_names()
        if not streams:
            await asyncio.sleep(FAST_RETRY_SECONDS)
            return

        url = self.connection_url(streams)
        kwargs: dict[str, object] = {
            "open_timeout": 15.0,
            "ping_interval": 20.0,
            "ping_timeout": 20.0,
            # 币安要求 60 秒内回 pong，库自动处理
        }
        # websockets 15 会自动发现Windows系统代理；无显式项目代理时传None，
        # 避免残留注册表代理让连接表现为随机ConnectionRefusedError。
        kwargs["proxy"] = _proxy()

        async with websockets.connect(url, **kwargs) as ws:  # type: ignore[arg-type]
            self._subscribed = set(streams)
            # 首帧超时是识别"能握手但收不到数据"的唯一手段
            first_frame = await asyncio.wait_for(ws.recv(), timeout=FIRST_FRAME_TIMEOUT)
            self._handle_frame(first_frame)
            self._failures = 0
            self._state = "live"
            await self._emit()
            await self._consume(
                ws, rotation_deadline=time.monotonic() + self._rotation_seconds(),
            )

    @staticmethod
    def connection_url(streams: list[str]) -> str:
        """构造币安USD-M常规行情组合流地址。"""
        return FSTREAM_BASE + MARKET_COMBINED_PATH.format(streams="/".join(streams))

    @staticmethod
    def _rotation_seconds() -> float:
        return max(
            60.0,
            PUBLIC_STREAM_ROTATION_SECONDS
            + random.uniform(-PUBLIC_STREAM_ROTATION_JITTER_SECONDS,
                              PUBLIC_STREAM_ROTATION_JITTER_SECONDS),
        )

    async def _consume(self, ws, *, rotation_deadline: float | None = None) -> None:
        """收帧循环；订阅列表变化时增量调整。"""
        last_check = time.monotonic()
        while not self._stop:
            remaining = ((rotation_deadline - time.monotonic())
                         if rotation_deadline is not None else RESUBSCRIBE_INTERVAL)
            if remaining <= 0:
                self._market_rotated = True
                self._rotations += 1
                return
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=min(RESUBSCRIBE_INTERVAL, remaining),
                )
            except asyncio.TimeoutError:
                if rotation_deadline is not None and time.monotonic() >= rotation_deadline:
                    self._market_rotated = True
                    self._rotations += 1
                    return
                if (self._last_frame_at
                        and time.time() - self._last_frame_at > STREAM_STALE_SECONDS):
                    raise RuntimeError("public_market_stream_stale")
                # 没有帧不代表断了，正好利用这个间隙核对订阅列表
                await self._sync_subscriptions(ws)
                last_check = time.monotonic()
                continue

            self._handle_frame(raw)
            now = time.monotonic()
            if now - last_check >= RESUBSCRIBE_INTERVAL:
                await self._sync_subscriptions(ws)
                last_check = now

    def _handle_frame(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        data = payload.get("data") or {}
        self._last_frame_at = time.time()
        self._messages += 1
        event = str(data.get("e") or "")
        symbol = data.get("s")
        price = None
        if event == "24hrTicker" or ("c" in data and "k" not in data):
            push_ticker(data)
            price = data.get("c")
        elif event == "markPriceUpdate":
            push_mark(data)
            price = data.get("p")
        elif event == "kline" or isinstance(data.get("k"), dict):
            push_kline_event(data)
            body = data.get("k") or {}
            symbol = body.get("s") or symbol
            price = body.get("c")
        if not symbol or price is None:
            return
        try:
            push_stream_price(str(symbol), float(price))
        except (TypeError, ValueError):
            return
        self._last_price_at = time.time()
        if self._state != "live":
            self._state = "live"

    def _handle_book_frame(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        data = payload.get("data") or {}
        if not data.get("s") or data.get("b") is None or data.get("a") is None:
            return
        push_book(data)
        self._book_last_frame_at = time.time()
        self._book_active = True

    # ---- 订阅管理 ----
    def _wanted_symbols(self) -> set[str]:
        try:
            symbols = self._symbols_provider() or []
        except Exception:
            return set()
        return {str(s).upper() for s in symbols if s}

    def _stream_names(self) -> list[str]:
        """兼容旧调用：返回每个白名单标的的24h ticker流。"""
        return [f"{s.lower()}@ticker" for s in sorted(self._wanted_symbols())]

    def _market_stream_names(self) -> list[str]:
        """返回常规行情组合流；一次连接覆盖价格、标记价格和全部决策周期K线。"""
        streams: list[str] = []
        for symbol in sorted(self._wanted_symbols()):
            lower = symbol.lower()
            streams.extend((f"{lower}@ticker", f"{lower}@markPrice"))
            streams.extend(f"{lower}@kline_{interval}" for interval in self._kline_intervals)
        return streams

    def _book_stream_names(self) -> list[str]:
        return [f"{s.lower()}@bookTicker" for s in sorted(self._wanted_symbols())]

    async def _sync_subscriptions(self, ws) -> None:
        wanted = set(self._market_stream_names())
        if wanted == self._subscribed:
            return
        if not wanted:
            await asyncio.sleep(FAST_RETRY_SECONDS)
            return
        add = sorted(wanted - self._subscribed)
        remove = sorted(self._subscribed - wanted)
        try:
            if add:
                await ws.send(json.dumps({
                    "method": "SUBSCRIBE",
                    "params": add,
                    "id": int(time.time()),
                }))
            if remove:
                await ws.send(json.dumps({
                    "method": "UNSUBSCRIBE",
                    "params": remove,
                    "id": int(time.time()),
                }))
            self._subscribed = wanted
        except Exception:
            # 订阅调整失败不影响已建立的推送，下一轮再试
            pass

    # ---- 失败处理 ----
    async def _note_failure(self) -> None:
        self._failures += 1
        self._reconnects += 1
        self._subscribed = set()
        if self._failures >= FAILURES_BEFORE_BACKOFF:
            # 达到阈值才认定被挡住，避免偶发抖动就把链路判死
            self._state = "blocked"
            await self._emit()

    async def _emit(self) -> None:
        if self._on_status is None:
            return
        try:
            await self._on_status(self._state, self.status_text())
        except Exception:
            pass
