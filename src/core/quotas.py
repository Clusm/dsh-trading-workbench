"""交易统计（仅做参考，不设上限）：持续鼓励 Agent 每轮积极寻找机会。"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from audit.database import AuditDatabase


class TradeQuotaManager:
    # 统计结果短缓存：页面 5~15 秒轮询一次 status，不必每次都扫 1000 行订单
    COUNTS_CACHE_TTL = 30.0

    def __init__(self, db: AuditDatabase, config: dict[str, Any]):
        self.db = db
        self.enabled = bool(config.get("enabled", True))
        self.hourly_short_target = int(config.get("hourly_short_target", 5))
        self.daily_hourly_target = int(config.get("daily_hourly_target", 3))
        self._counts_cache: tuple[float, dict[str, int]] = (0.0, {})
        self._cache_lock = threading.Lock()

    def _open_orders(self) -> list[dict[str, Any]]:
        rows = self.db.fetch_all(
            "SELECT created_at, status, raw_json FROM orders ORDER BY id DESC LIMIT 1000"
        )
        out = []
        for r in rows:
            try:
                raw = json.loads(r["raw_json"] or "{}")
            except Exception:
                raw = {}
            if raw.get("action") != "open" or r["status"] not in ("FILLED", "NEW"):
                continue
            out.append({"created_at": r["created_at"], "trade_level": raw.get("trade_level", "short")})
        return out

    def counts(self) -> dict[str, int]:
        now_mono = time.monotonic()
        with self._cache_lock:
            ts, cached = self._counts_cache
            if cached and now_mono - ts < self.COUNTS_CACHE_TTL:
                return dict(cached)
        now = datetime.now(timezone.utc)
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        short_hour = 0
        short_day = 0
        hourly_day = 0
        for o in self._open_orders():
            try:
                ts = datetime.fromisoformat(o["created_at"].replace("Z", "+00:00"))
            except Exception:
                continue
            if o["trade_level"] == "short" and ts >= day_start:
                short_day += 1
                if ts >= hour_start:
                    short_hour += 1
            if o["trade_level"] == "hourly" and ts >= day_start:
                hourly_day += 1
        result = {
            "short_done_today": short_day,
            "short_done_this_hour": short_hour,
            "hourly_done_today": hourly_day,
            # 统一计数：不区分短线/小时级，今日开仓合计
            "total_done_today": short_day + hourly_day,
        }
        with self._cache_lock:
            self._counts_cache = (now_mono, result)
        return dict(result)

    def status(self) -> dict[str, Any]:
        c = self.counts()
        c["enabled"] = self.enabled
        c["hourly_short_target"] = self.hourly_short_target
        c["daily_hourly_target"] = self.daily_hourly_target
        c["policy"] = "no_cap_active_search"
        return c
