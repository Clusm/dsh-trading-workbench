"""M1.1 行情层常量与默认配置。

与 ``docs/04-行情与指标.md`` 对齐；本模块不读取 ``service/config.yaml``（M1.1 尚未接入），
所有阈值集中在常量里，便于测试与评审逐条对照。仅只读公共行情，无 key/签名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SERVICE_VERSION = "0.2.0"
PROVENANCE_SERVICE = "market-gateway"
SCHEMA_VERSION = "md-evidence/1"
RENDER_VERSION = "md-evidence-prompt/1"

#: 20 币白名单（docs/04 §5.1）；启动时按 demo exchangeInfo 校验由后续里程碑接入。
WHITELIST: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "ZECUSDT",
    "XRPUSDT",
    "ARBUSDT",
    "UNIUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "NEARUSDT",
    "SUIUSDT",
    "FILUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "OPUSDT",
    "TRXUSDT",
    "AAVEUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "DOTUSDT",
)

INTERVALS: tuple[str, ...] = ("15m", "1h", "4h", "1d")
INTERVAL_MS: dict[str, int] = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
#: 启动/证据构建所需 warmup 根数（docs/04 §6.1）。
WARMUP_BARS: dict[str, int] = {"15m": 500, "1h": 300, "4h": 300, "1d": 200}
MAX_BARS_PER_SERIES = 1200

DEEP_MAX = 8
MIN_QUOTE_VOLUME_USD = 200_000_000.0
ANOMALY_PCT = 5.0

#: 全市场异动扫描（docs/04 §5.3）。数据来自已有的 /fapi/v1/ticker/24hr 全量快照，
#: 不新增请求；扫描结果只用于知情与发现，绝不自动把白名单外的币纳入可交易池。
MARKET_SCAN_TOP_N = 10
MARKET_SCAN_ANOMALY_N = 10
MARKET_SCAN_REFRESH_S = 3600.0

#: 新鲜度阈值（docs/04 §9.1）：fresh_s 内为正常，超过 unusable_s 为不可用。
FRESHNESS_S: dict[str, tuple[int, int]] = {
    "15m": (960, 1800),
    "1h": (3900, 7200),
    "4h": (15600, 28800),
    "1d": (93600, 172800),
}
TICKER_FRESH_S = 5.0
TICKER_STALE_S = 60.0
MARK_FRESH_S = 15.0
MARK_STALE_S = 300.0
OI_FRESH_S = 120.0
OI_STALE_S = 600.0
SNAPSHOT_STALE_S = 600.0
CLOCK_WARN_MS = 1000
CLOCK_BLOCK_MS = 5000

#: 限流预算（docs/04 §8.2）。
WEIGHT_LIMIT_1M = 2400
LOCAL_WEIGHT_BUDGET_1M = 600
BACKGROUND_DEFER_RATIO = 0.75
NORMAL_DEFER_RATIO = 0.90
DATA_WINDOW_S = 300
DATA_BUDGET_5M = 800
COOLDOWN_429_DEFAULT_S = 60.0
COOLDOWN_429_MAX_S = 3600.0
COOLDOWN_418_DEFAULT_S = 480.0
COOLDOWN_418_MAX_S = 604800.0

#: 证据预算（docs/04 §10.5）。
PROMPT_TOKEN_BUDGET = 6000
ORDER_GATE_TTL_S = 180
PRICE_TTL_S = 15
EVIDENCE_AUDIENCE: tuple[str, ...] = (
    "analysis",
    "red",
    "blue",
    "decision",
    "risk",
    "operator",
)
CANDLE_BARS = 20
TOKEN_CHARS_PER_TOKEN = 3.6

#: 端点权重（docs/04 §4.1）。
WEIGHT_TIME = 1
WEIGHT_EXCHANGE_INFO = 1
WEIGHT_TICKER_24H_ALL = 40
WEIGHT_TICKER_24H_ONE = 1
WEIGHT_TICKER_PRICE_ALL = 2
WEIGHT_PREMIUM = 1
WEIGHT_OPEN_INTEREST = 1
WEIGHT_DERIVATIVES_DATA = 1
WEIGHT_DEPTH_LIMIT_5 = 2

#: 缓存 TTL（秒）。
CLOCK_TTL_S = 30.0
TICKER_TTL_S = 30.0
PRICE_TTL_S_CACHE = 1.0
HEALTH_TTL_S = 10.0
UNIVERSE_TTL_S = 60.0
KLINE_REFRESH_S = 10.0

#: 派生 flags 阈值（docs/04 §6.4）。
FLAG_VOLUME_SPIKE = 2.0
FLAG_ATR_EXPANSION_PCT = 1.2
FLAG_BB_COMPRESSION = 0.2
FLAG_NEAR_24H_PCT = 0.5
FLAG_FUNDING_EXTREME = 0.0005
FLAG_OI_SURGE_PCT = 5.0


def klines_weight(limit: int) -> int:
    """``/fapi/v1/klines`` 保守权重（与 docs/04 §4.1 一致）。"""

    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


def depth_weight(limit: int) -> int:
    if limit <= 50:
        return 2
    if limit <= 100:
        return 5
    if limit <= 500:
        return 10
    return 20


@dataclass(frozen=True, slots=True)
class MarketPaths:
    data_dir: Path

    @property
    def evidence_root(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def rate_limit_state(self) -> Path:
        return self.state_dir / "market_rate_limit.json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "workbench-v1.sqlite"


@dataclass(frozen=True, slots=True)
class MarketSettings:
    """M1.1 行情层配置（不来自 service/config.yaml，保持独立可测）。"""

    base_url: str
    data_dir: Path
    timeout_s: float = 10.0
    whitelist: tuple[str, ...] = WHITELIST
    deep_max: int = DEEP_MAX
    min_quote_volume_usd: float = MIN_QUOTE_VOLUME_USD
    anomaly_pct: float = ANOMALY_PCT
    local_budget_1m: int = LOCAL_WEIGHT_BUDGET_1M
    paths: MarketPaths = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", MarketPaths(Path(self.data_dir)))
