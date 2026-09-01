"""宏观公开数据：Yahoo 行情 + Tavily 新闻/日历搜索。FRED 在此环境不可达，暂不使用。"""
from __future__ import annotations

from typing import Any

import httpx
from urllib.parse import quote as url_quote

from agents.models import MacroBrief

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


def _yahoo_chart(symbol: str, range_: str = "5d", interval: str = "1d") -> dict[str, Any]:
    safe_symbol = url_quote(symbol, safe="")
    r = httpx.get(
        f"{YAHOO_BASE}/{safe_symbol}",
        params={"range": range_, "interval": interval},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15.0,
    )
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    meta = result.get("meta", {})
    quote_rows = result.get("indicators", {}).get("quote", [{}])[0]
    closes = [x for x in quote_rows.get("close", []) if x is not None]
    return {
        "symbol": symbol,
        "last": float(meta.get("regularMarketPrice") or (closes[-1] if closes else 0.0)),
        "prev": float(closes[-2]) if len(closes) >= 2 else float(closes[-1]) if closes else 0.0,
        "change_pct": float(meta.get("regularMarketChangePercent") or 0.0)
        if meta.get("regularMarketChangePercent") is not None else 0.0,
    }


def _tavily_search(api_key: str | None, query: str) -> dict[str, Any]:
    if not api_key:
        return {}
    r = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced",
            "max_results": 6,
            "include_answer": True,
        },
        timeout=25.0,
    )
    r.raise_for_status()
    return r.json()


def build_macro_brief(tavily_api_key: str | None = None) -> MacroBrief:
    errors: list[str] = []
    notes: list[str] = []
    risk_events: list[str] = []
    market_bias = "neutral"

    spx = vix = tnx = dxy = nasdaq = gold = oil = None
    try:
        spx = _yahoo_chart("^GSPC")
        notes.append(f"标普500 {spx['last']:.2f} ({spx['change_pct']:+.2f}%)")
    except Exception as e:
        errors.append(f"SPX: {type(e).__name__} {e}")
    try:
        vix = _yahoo_chart("^VIX")
        notes.append(f"VIX {vix['last']:.2f}")
    except Exception as e:
        errors.append(f"VIX: {type(e).__name__} {e}")
    try:
        tnx = _yahoo_chart("^TNX")
        notes.append(f"美国10年期收益率 {tnx['last']:.2f}%")
    except Exception as e:
        errors.append(f"US10Y: {type(e).__name__} {e}")
    try:
        nasdaq = _yahoo_chart("^IXIC")
        notes.append(f"纳斯达克 {nasdaq['last']:.2f} ({nasdaq['change_pct']:+.2f}%)")
    except Exception as e:
        errors.append(f"NASDAQ: {type(e).__name__} {e}")
    try:
        dxy = _yahoo_chart("DX-Y.NYB")
        notes.append(f"美元指数DXY {dxy['last']:.2f} ({dxy['change_pct']:+.2f}%)")
    except Exception as e:
        errors.append(f"DXY: {type(e).__name__} {e}")
    try:
        gold = _yahoo_chart("GC=F")
        notes.append(f"黄金 {gold['last']:.2f} ({gold['change_pct']:+.2f}%)")
    except Exception as e:
        errors.append(f"GOLD: {type(e).__name__} {e}")
    try:
        oil = _yahoo_chart("CL=F")
        notes.append(f"WTI原油 {oil['last']:.2f} ({oil['change_pct']:+.2f}%)")
    except Exception as e:
        errors.append(f"OIL: {type(e).__name__} {e}")

    if spx and vix:
        if spx["change_pct"] <= -0.5 and vix["last"] >= 25:
            market_bias = "risk_off"
        elif spx["change_pct"] >= 0.5 and vix["last"] <= 18:
            market_bias = "risk_on"
        elif vix["last"] >= 30:
            market_bias = "risk_off"
        else:
            market_bias = "neutral"

    try:
        result = _tavily_search(
            tavily_api_key,
            "美联储 FOMC CPI PCE 非农 重要经济数据 日历 未来一周 2026 美元 美股 黄金 原油",
        )
        answer = (result.get("answer") or "").strip()
        if answer:
            risk_events.append(answer[:500])
        for item in result.get("results", [])[:4]:
            title = (item.get("title") or "").strip()
            if title:
                risk_events.append(title[:180])
    except Exception as e:
        errors.append(f"Tavily: {type(e).__name__} {e}")

    summary = (
        f"宏观状态={market_bias}。"
        + ("；".join(notes) if notes else "无美股行情数据。")
        + (" 已获取宏观事件线索。" if risk_events else " 暂无宏观事件线索。")
    )
    return MacroBrief(
        risk_events_24h=risk_events,
        market_bias=market_bias,
        notes=notes,
        summary=summary,
        errors=errors,
    )
