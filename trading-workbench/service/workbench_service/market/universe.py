"""币种池：20 币白名单 + deep（≤8，含持仓）+ index。"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from . import config as C


def normalize_tickers(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 ``/fapi/v1/ticker/24hr`` 行规整成内部结构（缺字段保持 None）。"""

    normalized: list[dict[str, Any]] = []
    for row in rows or []:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        try:
            last = float(row.get("lastPrice") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        try:
            pct = float(row.get("priceChangePercent") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        try:
            quote_volume = float(row.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            quote_volume = 0.0
        normalized.append(
            {
                "symbol": symbol,
                "last": last,
                "pct_24h": pct,
                "quote_volume": quote_volume,
                "high_24h": _optional_float(row.get("highPrice")),
                "low_24h": _optional_float(row.get("lowPrice")),
                "close_time_ms": _optional_int(row.get("closeTime")),
            }
        )
    return normalized


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def select_universe(
    tickers: Sequence[dict[str, Any]],
    *,
    whitelist: Sequence[str] = C.WHITELIST,
    deep_max: int = C.DEEP_MAX,
    held: Sequence[str] = (),
    prev_pick: str | None = None,
    min_quote_volume_usd: float = C.MIN_QUOTE_VOLUME_USD,
    anomaly_pct: float = C.ANOMALY_PCT,
) -> dict[str, Any]:
    """确定性选池：held → prev_pick → top_volume×3 → anomaly×2 → fill。"""

    ticker_map = {row["symbol"]: row for row in tickers}
    whitelist = [symbol.upper() for symbol in whitelist]
    held_set = {symbol.upper() for symbol in held}
    deep: list[dict[str, Any]] = []
    chosen: set[str] = set()

    def add(symbol: str, reason: str) -> None:
        if symbol in chosen or symbol not in whitelist or len(deep) >= deep_max:
            return
        if symbol not in ticker_map:
            return
        row = ticker_map[symbol]
        if reason not in {"held", "prev_pick"} and float(row["quote_volume"]) < min_quote_volume_usd:
            return
        deep.append({"symbol": symbol, "reason": [reason]})
        chosen.add(symbol)

    for symbol in whitelist:
        if symbol in held_set:
            add(symbol, "held")

    if prev_pick:
        add(prev_pick.upper(), "prev_pick")

    by_volume = sorted(
        (row for row in tickers if row["symbol"] in whitelist),
        key=lambda row: float(row["quote_volume"]),
        reverse=True,
    )
    for row in by_volume:
        if len(deep) >= deep_max:
            break
        if len([item for item in deep if "top_volume" in item["reason"]]) >= 3:
            break
        add(row["symbol"], "top_volume")

    anomalies = sorted(
        (
            row
            for row in tickers
            if row["symbol"] in whitelist
            and row["symbol"] not in chosen
            and abs(float(row["pct_24h"])) >= anomaly_pct
        ),
        key=lambda row: abs(float(row["pct_24h"])),
        reverse=True,
    )
    for row in anomalies[:2]:
        add(row["symbol"], "anomaly")

    for row in by_volume:
        if len(deep) >= deep_max:
            break
        add(row["symbol"], "fill")

    omitted = [
        {"symbol": symbol, "reason": "no_ticker"}
        for symbol in whitelist
        if symbol not in ticker_map
    ]
    omitted_symbols = {item["symbol"] for item in omitted}
    index = [
        symbol
        for symbol in whitelist
        if symbol not in chosen and symbol not in omitted_symbols
    ]
    return {
        "whitelist": whitelist,
        "deep": deep,
        "index": index,
        "omitted": omitted,
    }


def _scan_row(row: dict[str, Any], whitelist: set[str]) -> dict[str, Any]:
    return {
        "symbol": row["symbol"],
        "pct_24h": round(float(row["pct_24h"]), 2),
        "quote_volume_usd": round(float(row["quote_volume"]), 0),
        "in_whitelist": row["symbol"] in whitelist,
    }


def scan_market(
    tickers: Sequence[dict[str, Any]],
    *,
    whitelist: Sequence[str] = C.WHITELIST,
    top_n: int = C.MARKET_SCAN_TOP_N,
    anomaly_n: int = C.MARKET_SCAN_ANOMALY_N,
    anomaly_pct: float = C.ANOMALY_PCT,
    min_quote_volume_usd: float = C.MIN_QUOTE_VOLUME_USD,
) -> dict[str, Any]:
    """全市场异动扫描：白名单外的异动必须可见，但绝不自动进入可交易池。

    数据源就是 ``select_universe`` 已经在用的 24h ticker 全量快照（无 symbol 参数），
    所以这里不产生任何新请求——原项目的"每小时全扫"缺的不是数据，而是这段排序。

    边界（docs/04 §5.3）：
      - 只做知情与发现：``in_whitelist=False`` 的行不会进入 ``deep``/``index``，
        可交易集合仍由 ``deep_symbols``（白名单内）唯一决定；
      - 用与选池相同的流动性下限过滤，避免低成交量的假异动占据视野；
      - 缺字段保持 None 语义：``quote_volume`` 缺失的行直接不参与排序，不折算成 0。
    """

    whitelist_set = {symbol.upper() for symbol in whitelist}
    floor = float(min_quote_volume_usd)
    eligible: list[dict[str, Any]] = []
    for row in tickers or []:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        if row.get("quote_volume") is None or row.get("pct_24h") is None:
            continue
        if float(row["quote_volume"]) < floor:
            continue
        eligible.append({**row, "symbol": symbol})

    gainers = sorted(eligible, key=lambda row: float(row["pct_24h"]), reverse=True)
    losers = sorted(eligible, key=lambda row: float(row["pct_24h"]))
    anomalies = sorted(
        (row for row in eligible if abs(float(row["pct_24h"])) >= anomaly_pct),
        key=lambda row: abs(float(row["pct_24h"])),
        reverse=True,
    )
    outside = [row for row in anomalies if row["symbol"] not in whitelist_set]

    return {
        "scanned_at_ms": 0,
        "total_symbols": len(tickers or []),
        "qualified_symbols": len(eligible),
        "below_volume_floor": len(tickers or []) - len(eligible),
        "threshold_pct": float(anomaly_pct),
        "anomaly_count": len(anomalies),
        "top_gainers": [_scan_row(row, whitelist_set) for row in gainers[:top_n]],
        "top_losers": [_scan_row(row, whitelist_set) for row in losers[:top_n]],
        "anomalies": [_scan_row(row, whitelist_set) for row in anomalies[:anomaly_n]],
        "outside_whitelist_anomalies": [
            _scan_row(row, whitelist_set) for row in outside[:anomaly_n]
        ],
    }


def deep_symbols(universe: dict[str, Any]) -> list[str]:
    return [item["symbol"] for item in universe.get("deep", [])]
