"""M1.1 行情/指标/证据层。"""

from __future__ import annotations

from .config import MarketSettings, MarketPaths
from .evidence import EvidenceBuilder, EvidenceError, EvidenceValidationError, build_cycle_evidence
from .klines import Bar, KlineResult, KlineStore, aggregate_closed
from .service import MarketService, UnknownSymbol
from .transport import (
    MarketBanned,
    MarketBudgetDeferred,
    MarketCooldown,
    MarketTimeout,
    MarketTransport,
    MarketUpstreamError,
)

__all__ = [
    "Bar",
    "EvidenceBuilder",
    "EvidenceError",
    "EvidenceValidationError",
    "KlineResult",
    "KlineStore",
    "MarketBanned",
    "MarketBudgetDeferred",
    "MarketCooldown",
    "MarketPaths",
    "MarketService",
    "MarketSettings",
    "MarketTimeout",
    "MarketTransport",
    "MarketUpstreamError",
    "UnknownSymbol",
    "aggregate_closed",
    "build_cycle_evidence",
]
