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
import time
from typing import Awaitable, Callable, Iterable

from data.market_query import push_stream_price

FSTREAM_BASE = "wss://fstream.binance.com"
# 组合流支持在运行中增减订阅（SUBSCRIBE / UNSUBSCRIBE），单流做不到
COMBINED_PATH = "/stream?streams={streams}"

# 握手成功但迟迟收不到首帧，基本可以判定该出口不支持合约推送
FIRST_FRAME_TIMEOUT = 15.0
# 连续失败达到这个次数后，把重连间隔从"快速重试"切换到"慢速探测"
FAILURES_BEFORE_BACKOFF = 3
FAST_RETRY_SECONDS = 5.0
SLOW_RETRY_SECONDS = 120.0
# 订阅列表多久核对一次（持仓变动后自动跟上）
RESUBSCRIBE_INTERVAL = 15.0


def _proxy() -> str | None:
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
    ) -> None:
        self._symbols_provider = symbols_provider
        self._on_status = on_status
        self._subscribed: set[str] = set()
        self._failures = 0
        self._last_price_at = 0.0
        self._state = "init"  # init | live | blocked | disabled
        self._stop = False

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

    def stop(self) -> None:
        self._stop = True

    # ---- 主循环 ----
    async def serve(self) -> None:
        """永不退出的后台协程：连接 → 收帧 → 断线重连。"""
        try:
            import websockets  # noqa: F401  依赖缺失是永久性的，直接降级不再重试
        except ImportError:
            self._state = "disabled"
            await self._emit()
            return

        while not self._stop:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._note_failure()
            if self._stop:
                return
            delay = SLOW_RETRY_SECONDS if self._failures >= FAILURES_BEFORE_BACKOFF else FAST_RETRY_SECONDS
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        import websockets  # 延迟导入：不需要推送时不强制依赖

        streams = self._stream_names()
        if not streams:
            await asyncio.sleep(FAST_RETRY_SECONDS)
            return

        url = FSTREAM_BASE + COMBINED_PATH.format(streams="/".join(streams))
        kwargs: dict[str, object] = {
            "open_timeout": 15.0,
            "ping_interval": 20.0,
            "ping_timeout": 20.0,
            # 币安要求 60 秒内回 pong，库自动处理
        }
        proxy = _proxy()
        if proxy:
            kwargs["proxy"] = proxy

        async with websockets.connect(url, **kwargs) as ws:  # type: ignore[arg-type]
            self._subscribed = set(self._wanted_symbols())
            # 首帧超时是识别"能握手但收不到数据"的唯一手段
            await asyncio.wait_for(ws.recv(), timeout=FIRST_FRAME_TIMEOUT)
            self._failures = 0
            self._state = "live"
            await self._emit()
            await self._consume(ws)

    async def _consume(self, ws) -> None:
        """收帧循环；订阅列表变化时增量调整。"""
        last_check = time.monotonic()
        while not self._stop:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RESUBSCRIBE_INTERVAL)
            except asyncio.TimeoutError:
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
        symbol = data.get("s")
        # ticker 事件用 c（最新成交价）；24hTicker 也带 c，语义一致
        price = data.get("c")
        if not symbol or price is None:
            return
        try:
            push_stream_price(str(symbol), float(price))
        except (TypeError, ValueError):
            return
        self._last_price_at = time.time()
        if self._state != "live":
            self._state = "live"

    # ---- 订阅管理 ----
    def _wanted_symbols(self) -> set[str]:
        try:
            symbols = self._symbols_provider() or []
        except Exception:
            return set()
        return {str(s).upper() for s in symbols if s}

    def _stream_names(self) -> list[str]:
        return [f"{s.lower()}@ticker" for s in sorted(self._wanted_symbols())]

    async def _sync_subscriptions(self, ws) -> None:
        wanted = self._wanted_symbols()
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
                    "params": [f"{s.lower()}@ticker" for s in add],
                    "id": int(time.time()),
                }))
            if remove:
                await ws.send(json.dumps({
                    "method": "UNSUBSCRIBE",
                    "params": [f"{s.lower()}@ticker" for s in remove],
                    "id": int(time.time()),
                }))
            self._subscribed = wanted
        except Exception:
            # 订阅调整失败不影响已建立的推送，下一轮再试
            pass

    # ---- 失败处理 ----
    async def _note_failure(self) -> None:
        self._failures += 1
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
