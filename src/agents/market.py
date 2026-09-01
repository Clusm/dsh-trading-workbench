"""加密市场情报 Agent：调用币安公开行情，生成结构化简报。"""
from __future__ import annotations

from data.binance_public import build_market_brief
from .models import MarketBrief


def run_market_agent(top_n: int = 40, analyze_top: int = 10) -> MarketBrief:
    return build_market_brief(top_n=top_n, analyze_top=analyze_top)
