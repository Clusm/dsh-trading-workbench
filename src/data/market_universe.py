"""动态市场扫描池：短周期异动雷达 + 每小时全量扫描。

节奏：
- 每轮（默认5分钟）：核心小池 + 全市场价格快照异动检测；
- 每满1小时：全市场24h ticker 全量扫描，更新核心池/异动池；
- 返回给 SignalScanner 的 scan_pool。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from data.binance_public import get_all_24h_tickers
from data.rate_limit import cooldown_remaining, throttle

PUBLIC_BASE = "https://fapi.binance.com"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


class MarketUniverse:
    def __init__(self, state_file: Path,
                 core_symbols: list[str] | None = None,
                 quick_core_top: int = 3,
                 quick_anomaly_top: int = 2,
                 full_top_n: int = 20,
                 full_anomaly_n: int = 5,
                 short_move_pct: float = 3.0,
                 full_scan_interval_minutes: int = 60,
                 db: Any = None):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.core_symbols = [str(s).upper() for s in (core_symbols or [])]
        self.quick_core_top = int(quick_core_top)
        self.quick_anomaly_top = int(quick_anomaly_top)
        self.full_top_n = int(full_top_n)
        self.full_anomaly_n = int(full_anomaly_n)
        self.short_move_pct = float(short_move_pct)
        self.full_scan_interval_minutes = float(full_scan_interval_minutes)
        self.db = db
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                data.setdefault("last_full_scan", None)
                data.setdefault("prev_prices", {})
                data.setdefault("prev_ranks", {})
                data.setdefault("full_pool", [])
                return data
            except Exception:
                pass
        return {"last_full_scan": None, "prev_prices": {}, "prev_ranks": {}, "full_pool": []}

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _fetch_all_prices(self) -> dict[str, float]:
        if cooldown_remaining() > 0:
            raise RuntimeError("Binance 公开行情冷却中")
        throttle(0.20)
        r = httpx.get(f"{PUBLIC_BASE}/fapi/v1/ticker/price", timeout=10.0)
        r.raise_for_status()
        out: dict[str, float] = {}
        for item in r.json() or []:
            symbol = str(item.get("symbol") or "")
            if symbol.endswith("USDT"):
                try:
                    out[symbol] = float(item.get("price") or 0.0)
                except (TypeError, ValueError):
                    pass
        return out

    def detect_short_anomalies(self, now: datetime | None = None) -> list[str]:
        now = now or _utcnow()
        try:
            prices = self._fetch_all_prices()
        except Exception as e:
            if self.db:
                self.db.log_risk_event(event_type="market_universe_price_fail", severity="warn",
                                       details=f"{type(e).__name__}: {e}", action_taken="skip_short_anomaly")
            return []
        # 动态异动只能在配置的加密合约白名单内产生。公开价格接口还会返回
        # 黄金、白银、美股等代币化合约，若不先过滤，它们会绕过核心池进入策略扫描。
        allowed = set(self.core_symbols)
        if allowed:
            prices = {symbol: price for symbol, price in prices.items() if symbol in allowed}
        prev = self.state.get("prev_prices") or {}
        resolver = getattr(self.db, "resolve_risk_incidents_by_type", None)
        if callable(resolver):
            resolver("market_universe_price_fail")
        scored: list[tuple[float, str]] = []
        for symbol, price in prices.items():
            if symbol in prev and prev.get(symbol, 0.0) > 0:
                pct = (price - prev[symbol]) / prev[symbol] * 100.0
                if abs(pct) >= self.short_move_pct:
                    scored.append((abs(pct), symbol))
        # 只保留上一轮也存在的币，避免开机第一轮误报；保留价格快照以便下次对比
        self.state["prev_prices"] = prices
        self._save()
        scored.sort(reverse=True)
        return [s for _, s in scored[:self.quick_anomaly_top]]

    def _full_scan(self, now: datetime) -> list[str]:
        try:
            tickers = get_all_24h_tickers()
        except Exception as e:
            if self.db:
                self.db.log_risk_event(event_type="market_universe_full_scan_fail", severity="warn",
                                       details=f"{type(e).__name__}: {e}", action_taken="keep_old_pool")
            return self.state.get("full_pool") or []
        resolver = getattr(self.db, "resolve_risk_incidents_by_type", None)
        if callable(resolver):
            resolver("market_universe_full_scan_fail")
        allowed = set(self.core_symbols)
        sorted_by_vol = sorted(
            [
                t for t in tickers
                if str(t.get("symbol", "")).upper() in allowed
            ],
            key=lambda x: float(x.get("quoteVolume") or 0.0),
            reverse=True,
        )
        core = [str(t["symbol"]).upper() for t in sorted_by_vol[:self.full_top_n]]

        prev_ranks = self.state.get("prev_ranks") or {}
        core_set = set(core)
        scored: list[tuple[float, str]] = []
        for idx, t in enumerate(sorted_by_vol[: max(self.full_top_n * 2, 50)]):
            symbol = str(t["symbol"]).upper()
            try:
                pct = abs(float(t.get("priceChangePercent") or 0.0))
                vol = float(t.get("quoteVolume") or 0.0)
            except (TypeError, ValueError):
                continue
            old_rank = prev_ranks.get(symbol)
            rank_jump = 0.0
            if old_rank is not None:
                rank_jump = max(0.0, float(old_rank) - float(idx + 1))
            if symbol in core_set:
                continue
            # 异动分 = 涨跌幅 + 排名上升幅度 + 成交额占比
            score = pct * 1.0 + rank_jump * 0.5 + (vol / 1e6) * 0.001
            scored.append((score, symbol))
        scored.sort(reverse=True)
        anomalies = [s for _, s in scored[:self.full_anomaly_n] if s not in core_set]

        self.state["prev_ranks"] = {str(t["symbol"]).upper(): i + 1 for i, t in enumerate(sorted_by_vol[:200])}
        self.state["last_full_scan"] = now.isoformat()
        full_pool = core + anomalies
        self.state["full_pool"] = full_pool
        self._save()
        return full_pool

    def quick_pool(self, now: datetime | None = None) -> list[str]:
        now = now or _utcnow()
        core = [s for s in self.core_symbols[:self.quick_core_top] if s]
        anomalies = self.detect_short_anomalies(now)
        # 前部保留高优先级核心币和短时异动，后部补齐最近全扫结果及完整白名单。
        # SignalScanner 自己按游标取窗口；这里若只返回前 3~5 个币，轮转永远看不到其余币。
        candidates = core + anomalies + list(self.state.get("full_pool") or []) + self.core_symbols
        pool: list[str] = []
        allowed = set(self.core_symbols)
        for symbol in candidates:
            symbol = str(symbol).upper()
            if symbol and symbol in allowed and symbol not in pool:
                pool.append(symbol)
        return pool

    def get_scan_pool(self, now: datetime | None = None) -> list[str]:
        now = now or _utcnow()
        last = _parse_ts(self.state.get("last_full_scan"))
        if last is None or (now - last) >= timedelta(minutes=self.full_scan_interval_minutes):
            pool = self._full_scan(now)
            if pool:
                return pool
        return self.quick_pool(now)
