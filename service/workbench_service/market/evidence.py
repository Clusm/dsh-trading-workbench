"""``md-evidence/1`` 生成、校验、落盘与 sha256（docs/04 §10）。

- 只使用闭合 bar；所有时间来自服务器校准时钟；
- 生成后先用冻结 schema 校验，失败即 fail-closed（不写文件）；
- 落盘 ``data/evidence/<UTC date>/<cycle_id>/evidence.json``，DB 只存引用；
- 无账户/密钥字段；``snapshot_hash`` 覆盖除 ``snapshot_id`` 与自身外的规范 JSON。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import sqlite3
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import config as C
from . import indicators as I
from . import quality as Q
from . import schema as S
from . import universe as U
from .klines import Bar, KlineResult
from .transport import MarketUpstreamError

logger = logging.getLogger("workbench.market.evidence")

CYCLE_PATTERN = re.compile(r"^cyc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
MIN_BARS_FOR_BLOCK = 60

_EMPTY_CANDLES: dict[str, Any] = {
    "n": 0,
    "t0_epoch_ms": 0,
    "t_sec": [],
    "o": [],
    "h": [],
    "l": [],
    "c": [],
    "v": [],
}


class EvidenceError(RuntimeError):
    pass


class EvidenceValidationError(EvidenceError):
    def __init__(self, errors: Sequence[str]) -> None:
        super().__init__("md-evidence/1 validation failed: " + "; ".join(errors[:5]))
        self.errors = list(errors)


class _SkipSymbol(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _iso_ms(ms: int | None) -> str | None:
    """Millisecond-precision ISO timestamp (T13: the four time points need ms)."""

    if ms is None:
        return None
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _stamp(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _round(value: float | None, digits: int = 8) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _pct(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _empty_candles() -> dict[str, Any]:
    return copy.deepcopy(_EMPTY_CANDLES)


def estimate_tokens(payload: Any) -> int:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return int(len(text) / C.TOKEN_CHARS_PER_TOKEN)


def canonical_hash(payload: Mapping[str, Any]) -> str:
    """``snapshot_hash`` 覆盖除 ``snapshot_id`` 与自身外的规范 JSON。"""

    clone = copy.deepcopy(dict(payload))
    clone.pop("snapshot_id", None)
    provenance = clone.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("snapshot_hash", None)
    canonical = json.dumps(clone, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_hash(payload: Mapping[str, Any]) -> bool:
    expected = str((payload.get("provenance") or {}).get("snapshot_hash") or "")
    if not expected.startswith("sha256:"):
        return False
    return expected == "sha256:" + canonical_hash(payload)


class EvidenceBuilder:
    def __init__(self, service: Any) -> None:
        self.service = service

    # ------------------------------------------------------------------ public
    def build(
        self,
        *,
        cycle_id: str | None = None,
        deep_symbols: Iterable[str] | None = None,
        held_symbols: Sequence[str] = (),
        prev_pick: str | None = None,
    ) -> dict[str, Any]:
        settings = self.service.settings
        started = time.perf_counter()
        clock = self.service.sync_clock(force=True)
        now = self.service.now_ms()
        tickers = self.service.tickers(force=True)
        ticker_map = {row["symbol"]: row for row in tickers}
        held = [symbol.upper() for symbol in held_symbols]
        selection = U.select_universe(
            tickers,
            whitelist=settings.whitelist,
            deep_max=settings.deep_max,
            held=held,
            prev_pick=prev_pick,
            min_quote_volume_usd=settings.min_quote_volume_usd,
            anomaly_pct=settings.anomaly_pct,
        )
        # 全市场异动扫描（docs/04 §5.3）：只做知情与发现。deep/index 仍只来自白名单，
        # 所以这个块再"炸"也不会把白名单外的币变成可交易标的。
        selection["market_scan"] = {
            key: value
            for key, value in self.service.market_scan().items()
            # cached 是"这次算的还是上次缓存的"，属于运行时状态，不进证据快照。
            if key != "cached"
        }

        selected_deep = [symbol.upper() for symbol in (deep_symbols or U.deep_symbols(selection))]
        selected_deep = [symbol for symbol in dict.fromkeys(selected_deep)][: settings.deep_max]
        if not selected_deep:
            selected_deep = ["BTCUSDT"]

        issues: list[dict[str, str]] = []
        issues.extend(Q.clock_issues(clock["offset_ms"]))
        ticker_age = self.service.tickers_snapshot_age_s()
        for row in tickers:
            if row["symbol"] not in settings.whitelist:
                continue
            issues.extend(
                Q.ticker_issues(row["symbol"], ticker_age, deep=row["symbol"] in selected_deep)
            )
        transport_status = self.service.transport.status()
        issues.extend(Q.transport_issues(transport_status))

        prices = self.service.price_snapshot(selected_deep)

        symbols: dict[str, Any] = {}
        deep_meta: dict[str, dict[str, Any]] = {}
        built_deep: list[str] = []
        omitted = list(selection["omitted"])
        held_unusable = False

        for symbol in selected_deep:
            price = prices.get(symbol) or {}
            if not price.get("last") or float(price["last"]) <= 0:
                omitted.append({"symbol": symbol, "reason": "no_price"})
                if symbol in held:
                    held_unusable = True
                continue
            try:
                block, block_issues, tf_meta = self._deep_block(
                    symbol, price, ticker_map.get(symbol) or {}, now, held=held
                )
            except _SkipSymbol as exc:
                omitted.append({"symbol": symbol, "reason": exc.reason[:60]})
                if symbol in held:
                    held_unusable = True
                continue
            except MarketUpstreamError as exc:
                logger.warning("deep symbol skipped symbol=%s error=%s", symbol, exc.code)
                omitted.append({"symbol": symbol, "reason": f"upstream:{exc.code}"[:60]})
                if symbol in held:
                    held_unusable = True
                continue
            symbols[symbol] = block
            deep_meta[symbol] = tf_meta
            built_deep.append(symbol)
            issues.extend(block_issues)
            if symbol in held and "KLINE_STALE" in block["quality"]["issues"]:
                held_unusable = True

        if not built_deep:
            raise EvidenceError("no deep symbol could be built from closed bars")

        selection["deep"] = [item for item in selection["deep"] if item["symbol"] in built_deep]
        built_set = set(built_deep)
        omitted_symbols = {item["symbol"] for item in omitted}
        selection["index"] = [
            symbol
            for symbol in settings.whitelist
            if symbol not in built_set and symbol not in omitted_symbols
        ]
        index_symbols: list[str] = []
        for symbol in selection["index"]:
            ticker = ticker_map.get(symbol)
            if ticker is None or float(ticker.get("last") or 0) <= 0:
                omitted.append({"symbol": symbol, "reason": "no_price"})
                omitted_symbols.add(symbol)
                continue
            symbols[symbol] = self._index_block(symbol, ticker, now, ticker_age)
            index_symbols.append(symbol)
        selection["index"] = [symbol for symbol in index_symbols if symbol not in omitted_symbols]
        selection["omitted"] = omitted

        primary = "BTCUSDT" if "BTCUSDT" in deep_meta else built_deep[0]
        primary_meta = deep_meta[primary]
        as_of = {
            "server_time_ms": int(clock["server_time_ms"]),
            "local_time_ms": int(clock["local_time_ms"]),
            "offset_ms": int(clock["offset_ms"]),
            "rtt_ms": int(clock["rtt_ms"]),
            "clock_trusted": bool(clock["clock_trusted"]),
            "last_closed_bar": {
                interval: _iso(primary_meta.get(interval, {}).get("last_closed_at_ms"))
                for interval in C.INTERVALS
            },
            "bar_age_s": {
                interval: (
                    int(primary_meta[interval]["age_s"])
                    if primary_meta.get(interval, {}).get("age_s") is not None
                    else None
                )
                for interval in C.INTERVALS
            },
        }

        market = self._market_block(tickers, built_set, symbols, settings.whitelist)
        summary = Q.summarize_health(
            issues,
            held_15m_unusable=held_unusable,
            snapshot_age_s=0.0,
        )
        stats = self.service.transport.stats()
        stamp = _stamp(now)
        resolved_cycle = cycle_id or f"cyc-{stamp}-{uuid.uuid4().hex[:6]}"
        if not CYCLE_PATTERN.match(resolved_cycle):
            raise EvidenceError(f"invalid cycle_id: {resolved_cycle!r}")

        payload: dict[str, Any] = {
            "schema_version": C.SCHEMA_VERSION,
            "snapshot_id": f"ev-{stamp}-000000",
            "cycle_id": resolved_cycle,
            "mode": "demo",
            "produced_at_ms": int(now),
            "produced_at": _iso(now),
            "as_of": as_of,
            "data_health": {
                "verdict": summary["verdict"],
                "score": summary["score"],
                "issues": summary["issues"],
                "sources": {
                    "rest": self._rest_source(transport_status),
                    "ws": {"state": "disabled", "last_frame_age_s": None},
                },
            },
            "market": market,
            "universe": selection,
            "symbols": symbols,
            "limits": {
                "prompt_token_budget": C.PROMPT_TOKEN_BUDGET,
                "prompt_token_estimate": 0,
                "truncated": False,
                "dropped_fields": [],
            },
            "provenance": {
                "service": C.PROVENANCE_SERVICE,
                "version": C.SERVICE_VERSION,
                "collector_pid": os.getpid(),
                "requests": int(stats.get("requests") or 0),
                "cache_hits": int(stats.get("cache_hits") or 0),
                "snapshot_hash": "sha256:" + "0" * 64,
                "git_commit": "unknown",
            },
            "delivery": {
                "order_gate_ttl_s": C.ORDER_GATE_TTL_S,
                "audience": list(C.EVIDENCE_AUDIENCE),
                "render": C.RENDER_VERSION,
                "news": None,
            },
        }

        self._apply_budget(payload, set(held))
        errors = S.validate(payload)
        if errors:
            raise EvidenceValidationError(errors)

        digest = canonical_hash(payload)
        payload["snapshot_id"] = f"ev-{stamp}-{digest[:6]}"
        payload["provenance"]["snapshot_hash"] = f"sha256:{digest}"
        final_errors = S.validate(payload)
        if final_errors:
            raise EvidenceValidationError(final_errors)

        written = self._persist(payload, settings.paths, build_started=started)
        # T13: millisecond build duration. The second-precision produced_at string
        # alone cannot serve D's four time points (0–999 ms of error erodes the 15 s
        # entry TTL); produced_at_ms is the build start and evidence_build_ms the
        # duration, so build end = produced_at_ms + evidence_build_ms.
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
        logger.info(
            "evidence_built cycle=%s verdict=%s deep=%s index=%s bytes=%s ms=%s",
            resolved_cycle,
            summary["verdict"],
            len(built_deep),
            len(index_symbols),
            written["bytes"],
            elapsed_ms,
        )
        return {
            "cycle_id": resolved_cycle,
            "snapshot_id": payload["snapshot_id"],
            "verdict": summary["verdict"],
            "score": summary["score"],
            "sha256": written["sha256"],
            "path": written["path"],
            "abs_path": written["abs_path"],
            "bytes": written["bytes"],
            "produced_at_ms": int(payload["produced_at_ms"]),
            "produced_at_iso_ms": _iso_ms(int(payload["produced_at_ms"])),
            "evidence_build_ms": int(round(elapsed_ms)),
            "prompt_token_estimate": payload["limits"]["prompt_token_estimate"],
            "deep": [entry["symbol"] for entry in payload["universe"]["deep"]],
            "index_count": len(payload["universe"]["index"]),
            "duration_ms": elapsed_ms,
            "evidence": payload,
        }

    # ------------------------------------------------------------------ symbols
    def _deep_block(
        self,
        symbol: str,
        price: Mapping[str, Any],
        ticker: Mapping[str, Any],
        now: int,
        *,
        held: Sequence[str],
    ) -> tuple[dict[str, Any], list[dict[str, str]], dict[str, dict[str, Any]]]:
        analyses: dict[str, dict[str, Any]] = {}
        bars_by_tf: dict[str, list[Bar]] = {}
        tf_meta: dict[str, dict[str, Any]] = {}
        sources: dict[str, str] = {}
        issues: list[dict[str, str]] = []
        is_held = symbol in held

        for interval in C.INTERVALS:
            result: KlineResult = self.service.klines.get_with_fallback(
                symbol, interval, C.WARMUP_BARS[interval], now_ms=now
            )
            if len(result.bars) < MIN_BARS_FOR_BLOCK:
                raise _SkipSymbol(f"{interval}_bars_{len(result.bars)}")
            bars = list(result.bars)
            bars_by_tf[interval] = bars
            analyses[interval] = self._analyse(bars)
            tf_meta[interval] = dict(result.meta)
            sources[interval] = str(result.meta.get("source") or "rest")
            issues.extend(Q.bar_issues(f"{symbol}:{interval}", interval, result.meta, held=is_held))

        detail = self._tf_detail(analyses["15m"], bars_by_tf["15m"], now)
        if detail["macd"] is None:
            raise _SkipSymbol("15m_macd_unavailable")

        timeframes = {"15m": detail}
        for interval in ("1h", "4h", "1d"):
            timeframes[interval] = self._tf_summary(analyses[interval], bars_by_tf[interval], now)

        derivatives, derivative_issues = self._derivatives(symbol, price, ticker, now)
        issues.extend(derivative_issues)
        trend = {
            "15m": analyses["15m"]["trend"],
            "1h": analyses["1h"]["trend"],
            "4h": analyses["4h"]["trend"],
            "1d": analyses["1d"]["trend"],
            "aligned": len({analyses[tf]["trend"] for tf in ("1h", "4h", "1d")}) == 1
            and analyses["1h"]["trend"] in {"up", "down"},
        }
        flags = self._flags(analyses, price, derivatives, issues)
        levels = self._levels(analyses, bars_by_tf, price)
        liquidity = self._liquidity(price, ticker)

        missing = sorted(
            {
                field
                for field, value in (
                    ("mark", price.get("mark")),
                    ("bid", price.get("bid")),
                    ("ask", price.get("ask")),
                    ("spread_bps", price.get("spread_bps")),
                    ("funding_rate", derivatives.get("funding_rate")),
                    ("oi_usd", derivatives.get("oi_usd")),
                    ("oi_change_15m_pct", derivatives.get("oi_change_15m_pct")),
                    ("oi_change_1h_pct", derivatives.get("oi_change_1h_pct")),
                    ("long_short_ratio_1h", derivatives.get("long_short_ratio_1h")),
                    ("taker_buy_sell_ratio_1h", derivatives.get("taker_buy_sell_ratio_1h")),
                )
                if value is None
            }
        )
        symbol_issues = [item["code"] for item in issues]
        block = {
            "depth": "deep",
            "price": {
                "last": _round(price.get("last")),
                "mark": _round(price.get("mark")),
                "bid": _round(price.get("bid")),
                "ask": _round(price.get("ask")),
                "spread_bps": _round(price.get("spread_bps"), 2),
                "pct_24h": _pct(ticker.get("pct_24h", 0.0)),
                "high_24h": _round(ticker.get("high_24h")),
                "low_24h": _round(ticker.get("low_24h")),
                "quote_vol_24h_usd": round(float(ticker.get("quote_volume") or 0.0), 2),
                "age_s": round(max(0.0, float(price.get("age_ms") or 0) / 1000.0), 1),
            },
            "trend": trend,
            "timeframes": timeframes,
            "derivatives": derivatives,
            "levels": levels,
            "liquidity": liquidity,
            "flags": flags[:8],
            "quality": {
                "ok": not any(item["severity"] == "error" for item in issues),
                "issues": sorted(set(symbol_issues)),
                "bar_sources": sources,
                "missing_fields": missing,
            },
        }
        return block, issues, tf_meta

    def _analyse(self, bars: Sequence[Bar]) -> dict[str, Any]:
        closes = [bar.close for bar in bars]
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        quotes = [bar.quote_volume for bar in bars]
        takers = [bar.taker_buy_quote for bar in bars]
        ema20 = I.ema(closes, 20)
        ema50 = I.ema(closes, 50)
        ema200 = I.ema(closes, 200)
        atr14 = I.atr(highs, lows, closes, 14)
        bb_mid, bb_upper, bb_lower, bb_width = I.bollinger(closes, 20, 2.0)
        macd_line, macd_signal, macd_hist = I.macd(closes)
        adx14, pdi, ndi = I.adx(highs, lows, closes, 14)
        donchian_upper, _, donchian_lower = I.donchian(highs, lows, 20)
        range_high, range_low, range_pos, _ = I.range_position(closes, highs, lows, 20)
        width_values = [value for value in bb_width if value is not None]
        return {
            "close": closes[-1],
            "high": highs[-1],
            "low": lows[-1],
            "ema20": I.last_value(ema20),
            "ema50": I.last_value(ema50),
            "ema200": I.last_value(ema200),
            "rsi14": I.last_value(I.rsi(closes, 14)),
            "atr14": I.last_value(atr14),
            "atr_pct": (
                (I.last_value(atr14) or 0.0) / closes[-1] * 100.0 if I.last_value(atr14) else None
            ),
            "adx14": I.last_value(adx14),
            "macd": {
                "line": I.last_value(macd_line),
                "signal": I.last_value(macd_signal),
                "hist": I.last_value(macd_hist),
            },
            "bb": {
                "mid": I.last_value(bb_mid),
                "upper": I.last_value(bb_upper),
                "lower": I.last_value(bb_lower),
                "width_pct": I.last_value(bb_width),
                "width_pctile_100": I.percentile_rank(width_values, 100),
            },
            "volume": {
                "quote_ratio_20": I.volume_ratio(quotes, 20),
                "taker_buy_ratio": I.taker_buy_ratios(quotes, takers, 20)[0],
                "taker_buy_ratio_avg20": I.taker_buy_ratios(quotes, takers, 20)[1],
            },
            "structure": {
                "range_high_20": range_high,
                "range_low_20": range_low,
                "range_pos": round(min(1.0, max(0.0, range_pos)), 4),
                "donchian_high_20": I.last_value(donchian_upper),
                "donchian_low_20": I.last_value(donchian_lower),
            },
            "trend": I.trend_from_ema(closes, ema20),
            "bars": len(bars),
            "last_close_time_ms": bars[-1].close_time_ms,
        }

    def _tf_detail(self, analysis: Mapping[str, Any], bars: Sequence[Bar], now: int) -> dict[str, Any]:
        macd = analysis["macd"]
        if macd["line"] is None or macd["signal"] is None or macd["hist"] is None:
            return {"macd": None}
        bb = analysis["bb"]
        if bb["mid"] is None or bb["upper"] is None or bb["lower"] is None:
            raise _SkipSymbol("15m_bb_unavailable")
        if analysis["atr_pct"] is None or analysis["ema20"] is None:
            raise _SkipSymbol("15m_indicator_unavailable")
        candles = self._candles(bars)
        return {
            "ready": True,
            "bars": analysis["bars"],
            "last_closed_at": _iso(int(analysis["last_close_time_ms"])),
            "age_s": int(max(0, (now - int(analysis["last_close_time_ms"])) / 1000.0)),
            "ema20": _round(analysis["ema20"]),
            "ema50": _round(analysis["ema50"]),
            "ema200": _round(analysis["ema200"]),
            "rsi14": _pct(analysis["rsi14"]),
            "macd": {
                "line": _round(macd["line"]),
                "signal": _round(macd["signal"]),
                "hist": _round(macd["hist"]),
            },
            "atr14": _round(analysis["atr14"]),
            "atr_pct": _pct(analysis["atr_pct"]),
            "adx14": _pct(analysis["adx14"]),
            "bb": {
                "mid": _round(bb["mid"]),
                "upper": _round(bb["upper"]),
                "lower": _round(bb["lower"]),
                "width_pct": _pct(bb["width_pct"]),
                "width_pctile_100": bb["width_pctile_100"],
            },
            "volume": {
                "quote_ratio_20": analysis["volume"]["quote_ratio_20"],
                "taker_buy_ratio": analysis["volume"]["taker_buy_ratio"],
                "taker_buy_ratio_avg20": analysis["volume"]["taker_buy_ratio_avg20"],
            },
            "structure": {
                "range_high_20": _round(analysis["structure"]["range_high_20"]),
                "range_low_20": _round(analysis["structure"]["range_low_20"]),
                "range_pos": analysis["structure"]["range_pos"],
            },
            "candles": candles,
        }

    def _tf_summary(self, analysis: Mapping[str, Any], bars: Sequence[Bar], now: int) -> dict[str, Any]:
        return {
            "ready": analysis["bars"] >= MIN_BARS_FOR_BLOCK,
            "bars": analysis["bars"],
            "last_closed_at": _iso(analysis["last_close_time_ms"]),
            "age_s": int(max(0, (now - int(analysis["last_close_time_ms"])) / 1000.0)),
            "ema20": _round(analysis["ema20"]),
            "ema50": _round(analysis["ema50"]),
            "ema200": _round(analysis["ema200"]),
            "rsi14": _pct(analysis["rsi14"]),
            "atr14": _round(analysis["atr14"]),
            "atr_pct": _pct(analysis["atr_pct"]),
            "adx14": _pct(analysis["adx14"]),
            "structure": {
                "range_high_20": _round(analysis["structure"]["range_high_20"]),
                "range_low_20": _round(analysis["structure"]["range_low_20"]),
                "range_pos": analysis["structure"]["range_pos"],
            },
        }

    def _candles(self, bars: Sequence[Bar]) -> dict[str, Any]:
        selected = list(bars)[-C.CANDLE_BARS :]
        if not selected:
            return _empty_candles()
        anchor = selected[0].open_time_ms
        return {
            "n": len(selected),
            "t0_epoch_ms": int(anchor),
            "t_sec": [round((bar.open_time_ms - anchor) / 1000.0) for bar in selected],
            "o": [_round(bar.open) for bar in selected],
            "h": [_round(bar.high) for bar in selected],
            "l": [_round(bar.low) for bar in selected],
            "c": [_round(bar.close) for bar in selected],
            "v": [round(bar.volume, 4) for bar in selected],
        }

    # ------------------------------------------------------------------ derivatives
    def _derivatives(
        self, symbol: str, price: Mapping[str, Any], ticker: Mapping[str, Any], now: int
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        missing: list[str] = []
        funding_rate = price.get("funding_rate")
        funding_bp = _pct(float(funding_rate) * 10_000.0, 2) if funding_rate is not None else None
        next_funding = _iso(price.get("next_funding_time_ms"))

        oi_contracts, oi_time = self._open_interest(symbol)
        mark = price.get("mark") or price.get("last")
        oi_usd = None
        oi_scale_rejected = False
        if oi_contracts is not None and mark:
            candidate = float(oi_contracts) * float(mark)
            quote_volume = float(ticker.get("quote_volume") or 0.0)
            # demo 的 openInterest 单位/量级不可信：OI notional 超过 24h 成交额 20 倍即拒收。
            if quote_volume > 0 and candidate > 20.0 * quote_volume:
                oi_scale_rejected = True
            else:
                oi_usd = round(candidate, 2)
        history, ratio_time = self._oi_history(symbol)
        change_15m = change_1h = None
        if len(history) >= 2 and history[-2]:
            change_15m = round((history[-1] / history[-2] - 1.0) * 100.0, 2)
        if len(history) >= 5 and history[0]:
            change_1h = round((history[-1] / history[0] - 1.0) * 100.0, 2)

        long_short, ls_time = self._ratio(
            "/futures/data/globalLongShortAccountRatio", symbol, "longShortRatio"
        )
        taker_ratio, taker_time = self._ratio(
            "/futures/data/takerlongshortRatio", symbol, "buySellRatio"
        )

        for name, value in (
            ("funding_rate", funding_rate),
            ("oi_usd", oi_usd),
            ("oi_change_15m_pct", change_15m),
            ("oi_change_1h_pct", change_1h),
            ("long_short_ratio_1h", long_short),
            ("taker_buy_sell_ratio_1h", taker_ratio),
        ):
            if value is None:
                missing.append(name)

        price_time = price.get("price_time_ms")
        ratios_time = max([value for value in (ls_time, taker_time) if value is not None], default=None)
        payload = {
            "funding_rate": funding_rate,
            "funding_bp": funding_bp,
            "funding_pctile_7d": None,
            "next_funding_at": next_funding,
            "oi_usd": oi_usd,
            "oi_change_15m_pct": change_15m,
            "oi_change_1h_pct": change_1h,
            "oi_change_24h_pct": None,
            "long_short_ratio_1h": long_short,
            "taker_buy_sell_ratio_1h": taker_ratio,
            "age_s": {
                "funding": self._age_s(price_time, now),
                "oi": self._age_s(oi_time or ratio_time, now),
                "ratios": self._age_s(ratios_time, now),
            },
        }
        issues = Q.derivatives_issues(symbol, missing)
        if oi_scale_rejected:
            for item in issues:
                if item["code"] == "DERIVATIVES_PARTIAL":
                    item["detail"] = (
                        item["detail"] + "; oi_usd rejected: implausible demo openInterest scale"
                    )[:180]
                    break
        return payload, issues

    def _open_interest(self, symbol: str) -> tuple[float | None, int | None]:
        try:
            payload = self.service.transport.get_json(
                "/fapi/v1/openInterest", {"symbol": symbol}, weight=C.WEIGHT_OPEN_INTEREST, purpose="derivatives"
            )
        except MarketUpstreamError:
            return None, None
        return _number(payload.get("openInterest")), _integer(payload.get("time"))

    def _oi_history(self, symbol: str) -> tuple[list[float], int | None]:
        try:
            payload = self.service.transport.get_json(
                "/futures/data/openInterestHist",
                {"symbol": symbol, "period": "15m", "limit": 5},
                weight=C.WEIGHT_DERIVATIVES_DATA,
                purpose="derivatives",
                group="futures_data",
            )
        except MarketUpstreamError:
            return [], None
        rows = [row for row in (payload or []) if isinstance(row, dict)]
        values: list[float] = []
        timestamps: list[int] = []
        for row in rows:
            value = _number(row.get("sumOpenInterest"))
            if value is not None:
                values.append(value)
                timestamps.append(_integer(row.get("timestamp")) or 0)
        return values, (max(timestamps) if timestamps else None)

    def _ratio(self, path: str, symbol: str, field: str) -> tuple[float | None, int | None]:
        try:
            payload = self.service.transport.get_json(
                path,
                {"symbol": symbol, "period": "1h", "limit": 1},
                weight=C.WEIGHT_DERIVATIVES_DATA,
                purpose="derivatives",
                group="futures_data",
            )
        except MarketUpstreamError:
            return None, None
        rows = [row for row in (payload or []) if isinstance(row, dict)]
        if not rows:
            return None, None
        row = rows[-1]
        return _number(row.get(field)), _integer(row.get("timestamp"))

    @staticmethod
    def _age_s(timestamp_ms: int | None, now: int) -> float | None:
        if timestamp_ms is None:
            return None
        return round(max(0.0, (now - int(timestamp_ms)) / 1000.0), 1)

    # ------------------------------------------------------------------ derived
    def _liquidity(self, price: Mapping[str, Any], ticker: Mapping[str, Any]) -> dict[str, Any]:
        spread = price.get("spread_bps")
        return {
            "spread_bps": spread,
            "top_imbalance": price.get("top_imbalance"),
            "quote_vol_24h_usd": round(float(ticker.get("quote_volume") or 0.0), 2),
            "est_slippage_bps_10k": round(float(spread) + 1.0, 2) if spread is not None else None,
            "book_age_s": round(max(0.0, float(price.get("age_ms") or 0) / 1000.0), 1),
        }

    def _levels(
        self,
        analyses: Mapping[str, Mapping[str, Any]],
        bars_by_tf: Mapping[str, Sequence[Bar]],
        price: Mapping[str, Any],
    ) -> dict[str, Any]:
        last = float(price.get("last") or 0.0)
        candidates: list[tuple[float, str]] = []

        def add(value: Any, kind: str) -> None:
            number = _number(value)
            if number is not None and number > 0:
                candidates.append((number, kind))

        add(price.get("high_24h"), "high_24h")
        add(price.get("low_24h"), "low_24h")
        daily = list(bars_by_tf.get("1d") or [])
        if daily:
            add(max(bar.high for bar in daily[-7:]), "high_7d")
            add(min(bar.low for bar in daily[-7:]), "low_7d")
        structure = analyses["15m"]["structure"]
        add(structure.get("range_high_20"), "range_high_20_15m")
        add(structure.get("range_low_20"), "range_low_20_15m")
        add(analyses["15m"].get("ema20"), "ema20_15m")
        add(analyses["1h"].get("ema50"), "ema50_1h")

        deduped: dict[float, str] = {}
        for level, kind in candidates:
            key = round(level, 8)
            deduped.setdefault(key, kind)
        resistances = [
            {"price": round(level, 8), "kind": kind, "dist_pct": round((level / last - 1.0) * 100.0, 2)}
            for level, kind in sorted(deduped.items())
            if level > last
        ][:6]
        supports = [
            {"price": round(level, 8), "kind": kind, "dist_pct": round((level / last - 1.0) * 100.0, 2)}
            for level, kind in sorted(deduped.items(), reverse=True)
            if level < last
        ][:6]
        anchors = {
            "ema20_15m": analyses["15m"].get("ema20"),
            "ema50_1h": analyses["1h"].get("ema50"),
            "range_high_20": structure.get("range_high_20"),
            "range_low_20": structure.get("range_low_20"),
            "high_24h": _number(price.get("high_24h")),
            "low_24h": _number(price.get("low_24h")),
            "prior_day_high": daily[-1].high if daily else None,
            "prior_day_low": daily[-1].low if daily else None,
        }
        return {
            "resistances": resistances,
            "supports": supports,
            "anchors": {
                key: _round(value) for key, value in anchors.items() if value is not None
            },
        }

    def _flags(
        self,
        analyses: Mapping[str, Mapping[str, Any]],
        price: Mapping[str, Any],
        derivatives: Mapping[str, Any],
        issues: Sequence[Mapping[str, str]],
    ) -> list[str]:
        flags: list[str] = []
        trend = {interval: analyses[interval]["trend"] for interval in C.INTERVALS}
        if trend["1h"] == trend["4h"] == trend["1d"] and trend["1h"] in {"up", "down"}:
            flags.append(f"trend_aligned_{trend['1h']}")
        if {trend["15m"], trend["1h"]} == {"up", "down"}:
            flags.append("trend_conflict")
        volume = analyses["15m"]["volume"]["quote_ratio_20"]
        if volume is not None and volume >= C.FLAG_VOLUME_SPIKE:
            flags.append("volume_spike")
        bb = analyses["15m"]["bb"]
        atr_pct = analyses["15m"]["atr_pct"] or 0.0
        if atr_pct >= C.FLAG_ATR_EXPANSION_PCT and (bb["width_pctile_100"] or 0) >= 0.8:
            flags.append("volatility_expansion")
        if bb["width_pctile_100"] is not None and bb["width_pctile_100"] <= C.FLAG_BB_COMPRESSION:
            flags.append("volatility_compression")
        last = float(price.get("last") or 0.0)
        high_24h = _number(price.get("high_24h"))
        low_24h = _number(price.get("low_24h"))
        if high_24h and last and abs(high_24h - last) / last * 100.0 <= C.FLAG_NEAR_24H_PCT:
            flags.append("near_24h_high")
        if low_24h and last and abs(last - low_24h) / last * 100.0 <= C.FLAG_NEAR_24H_PCT:
            flags.append("near_24h_low")
        ema20 = analyses["15m"].get("ema20")
        if ema20 and last and atr_pct and abs(last / ema20 - 1.0) * 100.0 >= 1.5 * atr_pct:
            flags.append("extended_from_ema20")
        funding = derivatives.get("funding_rate")
        if funding is not None and abs(float(funding)) >= C.FLAG_FUNDING_EXTREME:
            flags.append("funding_extreme")
        change_1h = derivatives.get("oi_change_1h_pct")
        if change_1h is not None and abs(float(change_1h)) >= C.FLAG_OI_SURGE_PCT:
            flags.append("oi_surge" if float(change_1h) > 0 else "oi_flush")
        taker = analyses["15m"]["volume"]["taker_buy_ratio"]
        if taker is not None and taker >= 0.60:
            flags.append("taker_skew_buy")
        elif taker is not None and taker <= 0.40:
            flags.append("taker_skew_sell")
        if issues:
            flags.append("stale_some_sources")
        return list(dict.fromkeys(flags))

    def _index_block(self, symbol: str, ticker: Mapping[str, Any], now: int, ticker_age_s: float | None = None) -> dict[str, Any]:
        trend_15m = "unknown"
        trend_1h = "unknown"
        for interval, target in (("15m", "trend_15m"), ("1h", "trend_1h")):
            bars = self.service.klines.cached_bars(symbol, interval)
            if len(bars) >= 30:
                closes = [bar.close for bar in bars]
                value = I.trend_from_ema(closes, I.ema(closes, 20))
                if target == "trend_15m":
                    trend_15m = value
                else:
                    trend_1h = value
        pct = float(ticker.get("pct_24h") or 0.0)
        flags: list[str] = []
        if abs(pct) >= 10.0:
            flags.append("move_10pct")
        elif abs(pct) >= 5.0:
            flags.append("move_5pct")
        if ticker_age_s is not None:
            age_s = round(max(0.0, float(ticker_age_s)), 1)
        else:
            close_time = ticker.get("close_time_ms")
            age_s = round(max(0.0, (now - int(close_time)) / 1000.0), 1) if close_time else 0.0
        return {
            "depth": "index",
            "last": round(float(ticker.get("last") or 0.0), 8),
            "pct_24h": _pct(pct, 2),
            "quote_vol_24h_usd": round(float(ticker.get("quote_volume") or 0.0), 2),
            "trend_15m": trend_15m,
            "trend_1h": trend_1h,
            "funding_bp": None,
            "oi_change_1h_pct": None,
            "flags": flags,
            "age_s": age_s,
        }

    # ------------------------------------------------------------------ market
    def _market_block(
        self,
        tickers: Sequence[Mapping[str, Any]],
        deep: set[str],
        symbols: Mapping[str, Mapping[str, Any]],
        whitelist: Sequence[str],
    ) -> dict[str, Any]:
        rows = [row for row in tickers if row["symbol"] in whitelist]
        pcts = [float(row.get("pct_24h") or 0.0) for row in rows]
        up = sum(1 for value in pcts if value > 0.5)
        down = sum(1 for value in pcts if value < -0.5)
        median = round(statistics.median(pcts), 2) if pcts else 0.0
        if not rows:
            regime = "unknown"
        elif up >= max(1, int(len(rows) * 0.6)):
            regime = "trend_up"
        elif down >= max(1, int(len(rows) * 0.6)):
            regime = "trend_down"
        elif median and abs(median) >= 3.0:
            regime = "volatile"
        else:
            regime = "range"

        movers = []
        for row in sorted(rows, key=lambda item: abs(float(item.get("pct_24h") or 0.0)), reverse=True)[:5]:
            pct = float(row.get("pct_24h") or 0.0)
            why: list[str] = []
            if abs(pct) >= 10.0:
                why.append("move_10pct")
            elif abs(pct) >= 5.0:
                why.append("move_5pct")
            if float(row.get("quote_volume") or 0.0) >= 1_000_000_000.0:
                why.append("high_volume")
            movers.append(
                {
                    "symbol": row["symbol"],
                    "pct_24h": round(pct, 2),
                    "quote_vol_24h_usd": round(float(row.get("quote_volume") or 0.0), 2),
                    "why": why[:4],
                }
            )

        anomalies: list[str] = []
        for symbol in sorted(deep):
            block = symbols.get(symbol) or {}
            funding = (block.get("derivatives") or {}).get("funding_rate")
            if funding is not None and abs(float(funding)) >= C.FLAG_FUNDING_EXTREME:
                anomalies.append(f"{symbol} funding {float(funding) * 100:.3f}%/8h (>0.05%)")
        for row in rows:
            pct = float(row.get("pct_24h") or 0.0)
            if abs(pct) >= 10.0:
                anomalies.append(f"{row['symbol']} 24h move {pct:+.1f}%")
        anomalies = [text[:120] for text in anomalies[:8]]

        notes = [
            f"{len(whitelist)} symbols scanned; {len(deep)} deep, {len(symbols) - len(deep)} index-only",
        ]
        return {
            "regime": regime,
            "regime_basis": {
                "breadth_up": up,
                "breadth_down": down,
                "median_pct_24h": median,
                "btc_pct_24h": next(
                    (round(float(row.get("pct_24h") or 0.0), 2) for row in rows if row["symbol"] == "BTCUSDT"),
                    0.0,
                ),
            },
            "movers": movers,
            "anomalies": anomalies,
            "notes": notes[:3],
        }

    # ------------------------------------------------------------------ budget
    def _apply_budget(self, payload: dict[str, Any], held: set[str]) -> None:
        budget = C.PROMPT_TOKEN_BUDGET
        dropped: list[str] = []
        estimate = estimate_tokens(payload)
        if estimate > budget:
            for symbol, block in payload["symbols"].items():
                if block.get("depth") != "deep" or symbol in held:
                    continue
                detail = block["timeframes"]["15m"]
                if detail.get("candles", {}).get("n", 0) > 0:
                    detail["candles"] = _empty_candles()
                    dropped.append(f"{symbol}.15m.candles")
                    estimate = estimate_tokens(payload)
                    if estimate <= budget:
                        break
        if estimate > budget:
            index = payload["universe"]["index"]
            if len(index) > 10:
                removed = index[10:]
                payload["universe"]["index"] = index[:10]
                for symbol in removed:
                    payload["symbols"].pop(symbol, None)
                    dropped.append(f"index.{symbol}")
                estimate = estimate_tokens(payload)
        if estimate > budget:
            for symbol, block in payload["symbols"].items():
                if block.get("depth") != "deep" or symbol in held:
                    continue
                timeframe = block["timeframes"]["1d"]
                if timeframe.get("ready") is not False:
                    block["timeframes"]["1d"] = {
                        "ready": False,
                        "bars": 0,
                        "last_closed_at": None,
                        "age_s": None,
                    }
                    dropped.append(f"{symbol}.1d")
                    estimate = estimate_tokens(payload)
                    if estimate <= budget:
                        break
        if estimate > budget:
            # 最后一档：把非持仓 deep 的 1h/4h 摘要收窄为 schema 必填四字段。
            for symbol, block in payload["symbols"].items():
                if block.get("depth") != "deep" or symbol in held:
                    continue
                for timeframe_name in ("1h", "4h"):
                    timeframe = block["timeframes"][timeframe_name]
                    if "ema20" not in timeframe:
                        continue
                    block["timeframes"][timeframe_name] = {
                        key: timeframe[key]
                        for key in ("ready", "bars", "last_closed_at", "age_s")
                    }
                    dropped.append(f"{symbol}.{timeframe_name}.indicators")
                    estimate = estimate_tokens(payload)
                    if estimate <= budget:
                        break
                if estimate <= budget:
                    break
        if estimate > budget:
            # 硬预算（docs/04 §10.5）：先收敛 index 到 4 行，再移除非持仓 deep；
            # 仍超则清空 index。全部登记 dropped_fields，保证 <= prompt_token_budget。
            while len(payload["universe"]["index"]) > 4 and estimate > budget:
                symbol = payload["universe"]["index"].pop()
                payload["symbols"].pop(symbol, None)
                dropped.append(f"index.{symbol}")
                estimate = estimate_tokens(payload)
        if estimate > budget:
            deep_entries = payload["universe"]["deep"]
            protected = set(held) | {"BTCUSDT"}
            for entry in reversed(list(deep_entries)):
                if estimate <= budget:
                    break
                symbol = str(entry.get("symbol"))
                if symbol in protected or len(deep_entries) <= 1:
                    continue
                payload["symbols"].pop(symbol, None)
                deep_entries.remove(entry)
                dropped.append(f"deep.{symbol}")
                estimate = estimate_tokens(payload)
        if estimate > budget:
            while payload["universe"]["index"] and estimate > budget:
                symbol = payload["universe"]["index"].pop()
                payload["symbols"].pop(symbol, None)
                dropped.append(f"index.{symbol}")
                estimate = estimate_tokens(payload)
        if estimate > budget:
            # held-only 快照仍超预算属于不可满足；保留最保守结果并显式记录。
            logger.error(
                "evidence token budget unresolved estimate=%s budget=%s", estimate, budget
            )
            dropped.append("budget_unresolved")
        if dropped:
            notes = payload.get("market", {}).get("notes")
            if notes:
                notes[0] = (
                    f"{len(payload['universe']['whitelist'])} symbols scanned; "
                    f"{len(payload['universe']['deep'])} deep, "
                    f"{len(payload['universe']['index'])} index-only (budget-trimmed)"
                )
        payload["limits"].update(
            prompt_token_estimate=int(estimate),
            truncated=bool(dropped),
            dropped_fields=dropped,
        )

    # ------------------------------------------------------------------ persist
    def _rest_source(self, status: Mapping[str, Any]) -> dict[str, Any]:
        from .transport import summarize_status

        return summarize_status(status)

    def _persist(
        self,
        payload: Mapping[str, Any],
        paths: C.MarketPaths,
        *,
        build_started: float | None = None,
    ) -> dict[str, Any]:
        produced_ms = int(payload["produced_at_ms"])
        day = datetime.fromtimestamp(produced_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        directory = paths.evidence_root / day / str(payload["cycle_id"])
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "evidence.json"
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        encoded = text.encode("utf-8")
        temp = target.with_name(target.name + ".tmp")
        try:
            temp.write_bytes(encoded)
        except OSError as exc:
            self._safe_unlink(temp)
            logger.error("evidence temp write failed cycle=%s error=%s", payload["cycle_id"], exc)
            raise EvidenceError(f"PERSIST_FAILED: temp write failed: {exc}") from exc

        relative = target.relative_to(paths.data_dir).as_posix()
        sha256 = str(payload["provenance"]["snapshot_hash"])
        try:
            # DB 引用先成功，再 rename 提交；DB 失败则清理临时文件并 fail-closed。
            self._record_reference(
                paths.db_path,
                payload,
                relative,
                sha256,
                len(encoded),
                build_started=build_started,
            )
        except (sqlite3.Error, OSError) as exc:
            self._safe_unlink(temp)
            logger.error(
                "evidence DB reference failed cycle=%s error=%s", payload["cycle_id"], exc
            )
            raise EvidenceError(f"PERSIST_FAILED: DB reference failed: {exc}") from exc

        try:
            os.replace(temp, target)
        except OSError as exc:
            # 补偿：撤销已写入的引用，避免出现指向缺失文件的悬空记录。
            self._safe_unlink(temp)
            try:
                self._delete_reference(paths.db_path, str(payload["cycle_id"]))
            except (sqlite3.Error, OSError):
                logger.exception("evidence DB compensation failed cycle=%s", payload["cycle_id"])
            logger.error(
                "evidence rename failed cycle=%s error=%s", payload["cycle_id"], exc
            )
            raise EvidenceError(f"PERSIST_FAILED: rename failed: {exc}") from exc
        return {
            "path": relative,
            "abs_path": str(target),
            "sha256": sha256,
            "bytes": len(encoded),
        }

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("evidence temp cleanup failed path=%s", path)

    @staticmethod
    def _delete_reference(db_path: Path, cycle_id: str) -> None:
        connection = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            connection.execute("DELETE FROM evidence_snapshots WHERE cycle_id=?", (cycle_id,))
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _record_reference(
        db_path: Path,
        payload: Mapping[str, Any],
        relative_path: str,
        sha256: str,
        size: int,
        *,
        build_started: float | None = None,
    ) -> None:
        """DB 只存引用；evidence_snapshots 表由本模块幂等创建（M1.1 本地表）。

        T13：同时写入毫秒级 produced_at_ms 与 evidence_build_ms。表可能先由
        cycles/schema.py 创建（旧库没有 evidence_build_ms 列），所以这里对缺失列做
        幂等 ALTER 再插入；不重写任何历史行。
        """

        build_ms = (
            None
            if build_started is None
            else int(round((time.perf_counter() - build_started) * 1000.0))
        )
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence_snapshots (
                    cycle_id          TEXT PRIMARY KEY,
                    schema_version    TEXT NOT NULL,
                    produced_at_ms    INTEGER NOT NULL,
                    verdict           TEXT NOT NULL,
                    score             REAL NOT NULL,
                    path              TEXT NOT NULL,
                    sha256            TEXT NOT NULL,
                    bytes             INTEGER NOT NULL,
                    prompt_tokens     INTEGER NOT NULL,
                    evidence_build_ms INTEGER,
                    created_at        TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(evidence_snapshots)"
                ).fetchall()
            }
            if "evidence_build_ms" not in columns:
                connection.execute(
                    "ALTER TABLE evidence_snapshots ADD COLUMN evidence_build_ms INTEGER"
                )
            connection.execute(
                """
                INSERT OR REPLACE INTO evidence_snapshots
                    (cycle_id, schema_version, produced_at_ms, verdict, score, path, sha256,
                     bytes, prompt_tokens, evidence_build_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload["cycle_id"]),
                    str(payload["schema_version"]),
                    int(payload["produced_at_ms"]),
                    str(payload["data_health"]["verdict"]),
                    float(payload["data_health"]["score"]),
                    relative_path,
                    sha256,
                    int(size),
                    int(payload["limits"]["prompt_token_estimate"]),
                    build_ms,
                    _iso(int(time.time() * 1000)),
                ),
            )
            connection.commit()
        finally:
            connection.close()


def build_cycle_evidence(service: Any, **kwargs: Any) -> dict[str, Any]:
    """内部证据构建入口（供后续 ``POST /api/cycles`` 调用；不对外暴露创建路径）。"""

    return service.build_evidence(**kwargs)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)
