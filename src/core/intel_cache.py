"""市场/宏观/链上情报缓存：默认 1 小时刷新一次，避免每个 5 分钟决策循环重复抓取公开数据。"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable


class IntelCache:
    """进程内 TTL 缓存。

    图形化 Agent 只保存最近一次完整情报，Service 持续运行时跨 cycle 复用。
    每次刷新会执行 refresher 并替换整个 value；缓存期间读取同一个 Pydantic 对象。
    """

    def __init__(self, ttl_seconds: float = 3600) -> None:
        self.default_ttl_seconds = float(ttl_seconds)
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get_or_refresh(self, key: str, refresher: Callable[[], Any],
                       ttl_seconds: float | None = None) -> Any:
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        with self._lock:
            item = self._items.get(key)
            now = time.time()
            if item is not None and now - item["ts"] < ttl:
                return item["value"]
            value = refresher()
            self._items[key] = {"ts": time.time(), "value": value}
            return value

    def age_seconds(self, key: str) -> float | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            return time.time() - item["ts"]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            now = time.time()
            return {
                key: {
                    "age_seconds": round(now - item["ts"], 1),
                    "ttl_seconds": self.default_ttl_seconds,
                }
                for key, item in self._items.items()
            }
