"""Web3 链上情报 Agent：DefiLlama / CoinGecko / 可选 Etherscan。"""
from __future__ import annotations

from pathlib import Path

from data.onchain_public import build_onchain_brief
from .models import OnchainBrief


def run_onchain_agent(etherscan_api_key: str | None = None,
                      cache_dir: Path | None = None) -> OnchainBrief:
    return build_onchain_brief(etherscan_api_key=etherscan_api_key, cache_dir=cache_dir)
