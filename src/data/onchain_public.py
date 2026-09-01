"""Web3 链上免费数据：DefiLlama + CoinGecko + 可选 Etherscan。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx

from agents.models import OnchainBrief
from data.cache import SnapshotCache


def _pct(cur: float, prev: float | None) -> str:
    if prev is None or prev == 0:
        return "n/a"
    return f"{(cur / prev - 1) * 100:+.2f}%"


def build_onchain_brief(etherscan_api_key: str | None = None,
                        cache_dir: Path | None = None) -> OnchainBrief:
    errors: list[str] = []
    whale_activity: list[str] = []
    stable_change = "n/a"
    tvl_change = "n/a"
    stable_total = None
    tvl_total = None

    # 1) DefiLlama 稳定币
    try:
        r = httpx.get(
            "https://stablecoins.llama.fi/stablecoins",
            params={"includePrices": "false"},
            timeout=20.0,
        )
        r.raise_for_status()
        data = r.json()
        cur = 0.0
        prev = 0.0
        pegged_assets = data.get("peggedAssets", [])
        sorted_assets = []
        for asset in pegged_assets:
            try:
                cur += float(asset["circulating"]["peggedUSD"])
                prev += float(asset.get("circulatingPrevDay", {}).get("peggedUSD") or 0.0)
                sorted_assets.append(
                    (asset.get("name", "?"), float(asset["circulating"]["peggedUSD"]))
                )
            except (KeyError, TypeError, ValueError):
                continue
        stable_total = cur
        stable_change = _pct(cur, prev if prev > 0 else None)
        sorted_assets.sort(key=lambda x: x[1], reverse=True)
        if sorted_assets:
            top_stable = ", ".join(
                f"{name} ${val / 1e9:.2f}B" for name, val in sorted_assets[:4]
            )
            whale_activity.append("DefiLlama稳定币TOP: " + top_stable)
    except Exception as e:
        errors.append(f"DefiLlama stablecoins: {type(e).__name__} {e}")

    # 2) DefiLlama 全链 TVL
    try:
        r = httpx.get("https://api.llama.fi/v2/chains", timeout=20.0)
        r.raise_for_status()
        chains = r.json()
        tvl_total = float(sum(float(c.get("tvl") or 0.0) for c in chains))
        named_chains = [c for c in chains if c.get("name")]
        named_chains.sort(key=lambda c: float(c.get("tvl") or 0.0), reverse=True)
        top_parts = []
        for c in named_chains[:5]:
            try:
                tvl_b = float(c.get("tvl") or 0.0) / 1e9
                change = c.get("change_1d")
                ch = f"{change:+.2f}%" if isinstance(change, (int, float)) else ""
                top_parts.append(f"{c['name']} ${tvl_b:.2f}B{ch}")
            except (TypeError, ValueError):
                continue
        if top_parts:
            whale_activity.append("DefiLlama Top链TVL: " + ", ".join(top_parts))
    except Exception as e:
        errors.append(f"DefiLlama TVL: {type(e).__name__} {e}")

    # 3) 用本地快照计算 TVL 24h 变化（首次运行为 n/a）
    try:
        cache = SnapshotCache(cache_dir or Path("data/cache"))
        key = "onchain_totals"
        previous = cache.load(key)
        cache.save(key, {
            "ts": time.time(),
            "stable_total": stable_total,
            "tvl_total": tvl_total,
        })
        if previous and tvl_total is not None:
            prev_tvl = float(previous.get("tvl_total") or 0.0)
            tvl_change = _pct(tvl_total, prev_tvl if prev_tvl > 0 else None)
    except Exception as e:
        errors.append(f"onchain cache: {type(e).__name__} {e}")

    # 4) CoinGecko 趋势 + 全局市值
    try:
        r = httpx.get("https://api.coingecko.com/api/v3/search/trending", timeout=15.0)
        r.raise_for_status()
        coins = r.json().get("coins", [])[:6]
        names = [c.get("item", {}).get("name", "?") for c in coins]
        whale_activity.append("CoinGecko趋势: " + ", ".join(names))
    except Exception as e:
        errors.append(f"CoinGecko trending: {type(e).__name__} {e}")

    try:
        r = httpx.get("https://api.coingecko.com/api/v3/global", timeout=15.0)
        r.raise_for_status()
        gdata = (r.json() or {}).get("data") or {}
        total_mcap = float(gdata.get("total_market_cap", {}).get("usd") or 0.0)
        mcap_chg = gdata.get("market_cap_change_percentage_24h_usd")
        if total_mcap > 0:
            chg_str = f" ({mcap_chg:+.2f}% 24h)" if isinstance(mcap_chg, (int, float)) else ""
            whale_activity.append(
                f"CoinGecko全网加密市值 ${total_mcap / 1e12:.3f}T{chg_str}"
            )
    except Exception as e:
        errors.append(f"CoinGecko global: {type(e).__name__} {e}")

    # 5) Etherscan 可选
    if etherscan_api_key:
        try:
            r = httpx.get(
                "https://api.etherscan.io/api",
                params={"module": "proxy", "action": "eth_gasPrice", "apikey": etherscan_api_key},
                timeout=15.0,
            )
            r.raise_for_status()
            j = r.json()
            gas_wei = int(j.get("result", "0x0"), 16)
            whale_activity.append(f"ETH gas: {gas_wei / 1e9:.2f} Gwei")
        except Exception as e:
            errors.append(f"Etherscan gas: {type(e).__name__} {e}")
    else:
        whale_activity.append("Etherscan未配置key，跳过gas/大额转账")

    summary = (
        f"稳定币市值24h变化={stable_change}，DeFi TVL24h变化={tvl_change}。"
        + ("；".join(whale_activity) if whale_activity else "无链上活动线索。")
    )
    return OnchainBrief(
        stablecoin_change_24h=stable_change,
        tvl_change_24h=tvl_change,
        whale_activity=whale_activity,
        summary=summary,
        errors=errors,
    )
