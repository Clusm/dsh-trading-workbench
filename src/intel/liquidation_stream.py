"""币安 USD-M 全市场强平公开流；仅用于情报展示与审计。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable


class LiquidationStream:
    def __init__(self, on_event: Callable[[dict[str, Any]], None]):
        self.on_event = on_event
        self._stop = False
        self.active = False
        self.last_event_at: float | None = None
        self.last_error: str | None = None
        self.reconnects = 0

    def stop(self) -> None:
        self._stop = True

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True, "active": self.active, "last_event_at": self.last_event_at,
            "last_event_age_seconds": (round(time.time() - self.last_event_at, 2)
                                       if self.last_event_at else None),
            "last_error": self.last_error, "reconnects": self.reconnects,
        }

    async def serve(self) -> None:
        try:
            import websockets
        except ImportError:
            self.last_error = "websockets_dependency_missing"
            return
        while not self._stop:
            try:
                async with websockets.connect(
                    "wss://fstream.binance.com/ws/!forceOrder@arr",
                    open_timeout=10, close_timeout=5, ping_interval=20, ping_timeout=20,
                ) as ws:
                    self.active = True
                    self.last_error = None
                    async for raw in ws:
                        if self._stop:
                            break
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            # SQLite繁忙时可能等待 busy_timeout；移出事件循环，避免看板卡住。
                            await asyncio.to_thread(self.on_event, payload)
                            self.last_event_at = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.reconnects += 1
            finally:
                self.active = False
            if not self._stop:
                await asyncio.sleep(5)
