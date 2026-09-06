"""情报侧车的稳定数据契约。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class IntelEvent(BaseModel):
    source: str
    kind: str
    chain: str | None = None
    contract: str | None = None
    candidate_symbol: str | None = None
    observed_at: str
    received_at: str = Field(default_factory=utc_now)
    expires_at: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    quality: Literal["good", "degraded", "stale", "unknown", "invalid"] = "unknown"
    payload: dict[str, Any] = Field(default_factory=dict)
    raw_hash: str


class FuturesFeature(BaseModel):
    symbol: str
    event_time: str
    received_at: str = Field(default_factory=utc_now)
    fresh_until: str
    quality: Literal["good", "degraded", "stale", "invalid"] = "good"
    source: str = "binance_usdm_public"
    mark_price: float | None = None
    index_price: float | None = None
    basis_bps: float | None = None
    funding_rate: float | None = None
    next_funding_time: str | None = None
    open_interest_usd: float | None = None
    spread_bps: float | None = None
    depth_imbalance: float | None = None
    depth_update_id: int | None = None
    top_account_ratio: float | None = None
    top_position_ratio: float | None = None
    taker_buy_sell_ratio: float | None = None
    signal: Literal["bullish", "bearish", "neutral", "unknown"] = "unknown"
