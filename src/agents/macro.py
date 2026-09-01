"""宏观情报 Agent：Yahoo 公开行情 + Tavily 搜索美联储/宏观事件。"""
from __future__ import annotations

from data.macro_public import build_macro_brief
from .models import MacroBrief


def run_macro_agent(tavily_api_key: str | None = None) -> MacroBrief:
    return build_macro_brief(tavily_api_key=tavily_api_key)
