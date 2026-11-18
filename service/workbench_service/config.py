"""服务配置：只读环境变量 + 内置默认值。

M1.1 约束：
- **不加载** `.env` / `.env.local`；只读进程环境变量。
- 鉴权默认 Bearer；token 优先取 ``WORKBENCH_API_TOKEN``，否则读 ``data/.api-token``，
  文件不存在时生成 32 字节随机 token 并写入（0600；Windows 尽力用 icacls 收紧 ACL）。
- 仅显式 ``WORKBENCH_AUTH_MODE=dev-open`` 才关闭 Bearer（离线测试用）。
- Demo-only：行情基址只允许 ``https://demo-fapi.binance.com``（由 ``guard`` 校验）。
- 只监听 loopback，端口默认 8791。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

SERVICE_NAME = "trading-workbench"
SERVICE_VERSION = "0.2.1"
SCHEMA_VERSION = 1

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791

AUTH_MODE_DEV_OPEN = "dev-open"
AUTH_MODE_BEARER = "bearer"
AUTH_MODES = frozenset({AUTH_MODE_DEV_OPEN, AUTH_MODE_BEARER})
AUTH_TOKEN_FILENAME = ".api-token"
AUTH_TOKEN_BYTES = 32
AUTH_TOKEN_MIN_LENGTH = 16

DEMO_BASE_URL = "https://demo-fapi.binance.com"
DEMO_HOST = "demo-fapi.binance.com"

# §6 M2：环境在进程启动时固定，域名来自受控映射（不从用户输入拼）。
# Live 缺失凭据时不得借用 Demo，也不得静默回退——由 credentials/guard 层负责拒绝。
LIVE_BASE_URL = "https://fapi.binance.com"
LIVE_HOST = "fapi.binance.com"
VENUES = ("demo", "live")
VENUE_BASE_URLS = {"demo": DEMO_BASE_URL, "live": LIVE_BASE_URL}
VENUE_HOSTS = {"demo": DEMO_HOST, "live": LIVE_HOST}
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

SUPPORTED_INTERVALS = ("15m", "1h", "4h", "1d")
DEFAULT_PROBE_SYMBOL = "BTCUSDT"
DEFAULT_PROBE_INTERVAL = "15m"
DEFAULT_PROBE_LIMIT = 30

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,15}USDT$")
_LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})


class AuthTokenError(RuntimeError):
    """token 文件不可读/不可写；启动应失败而不是静默降级为 dev-open。"""


def is_valid_symbol(symbol: str) -> bool:
    """交易对只允许 ``[A-Z0-9]{2,15}USDT``，避免把任意字符串拼进上游查询。"""

    return bool(_SYMBOL_RE.match(symbol))


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - 配置错误路径
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be within [{minimum}, {maximum}], got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:  # pragma: no cover - 配置错误路径
        raise ValueError(f"{name} must be numeric, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be within [{minimum}, {maximum}], got {value}")
    return value


def _restrict_token_file(path: Path) -> None:
    """尽力收紧 token 文件权限；失败不阻断启动，也不打印 token。"""

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def generate_auth_token() -> str:
    """生成 32 字节（256-bit）URL-safe token；足够 Bearer 使用。"""

    return secrets.token_urlsafe(AUTH_TOKEN_BYTES)


def _validate_auth_token(token: str, source: str) -> str:
    """Reject short/blank tokens without ever logging the value."""

    if len(token) < AUTH_TOKEN_MIN_LENGTH:
        raise AuthTokenError(
            f"{source} API token must be at least {AUTH_TOKEN_MIN_LENGTH} characters"
        )
    return token


def read_auth_token_file(path: Path) -> str | None:
    """读取 ``data/.api-token``；不存在/空白返回 None，不可读则抛错。"""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthTokenError(f"cannot read API token file {path}: {exc}") from exc
    token = raw.strip()
    return token or None


def create_auth_token_file(path: Path, token: str) -> bool:
    """O_EXCL 创建 token 文件；已存在返回 False（调用方读取赢家）。

    先创建独占文件再写入，不再使用 mkstemp + replace：两个进程同时首次启动
    时只有一个能创建，另一个走读取路径，不会覆盖已生成 token。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise AuthTokenError(f"cannot create API token file {path}: {exc}") from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_token_file(path)
        return True
    except OSError as exc:
        # Do not leave a half-written token file behind.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise AuthTokenError(f"cannot write API token file {path}: {exc}") from exc


def read_service_lock_status(data_dir: Path) -> dict[str, object]:
    """只读诊断 ``data/service.lock``；不判断 OS 锁是否仍被持有。

    ``state=held-or-stale`` 表示文件内容没有 ``released_at``——可能是正常运行，
    也可能是强制终止后残留；OS 锁已释放时新实例仍可接管。
    """

    path = Path(data_dir) / "service.lock"
    if not path.exists():
        return {"state": "absent", "path": str(path)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {"state": "invalid", "path": str(path)}
    except (OSError, ValueError):
        return {"state": "invalid", "path": str(path)}
    released = bool(raw.get("released_at"))
    return {
        "state": "released" if released else "held-or-stale",
        "path": str(path),
        "pid": raw.get("pid"),
        "started_at": raw.get("started_at"),
        "released_at": raw.get("released_at"),
    }


def resolve_runtime_auth(settings: "Settings") -> "Settings":
    """解析实际鉴权配置：env token > 文件 token > 首次生成。

    - 显式 ``dev-open`` 不读取/生成 token（仅离线测试）。
    - 默认 ``bearer`` 下，token 文件不可读/不可写会让启动失败，而不是降级为 dev-open。
    """

    if settings.auth_mode == AUTH_MODE_DEV_OPEN:
        return replace(settings, api_token=None, auth_token_source="none")

    env_token = _env("WORKBENCH_API_TOKEN")
    if env_token:
        env_token = _validate_auth_token(env_token, "environment")
        return replace(
            settings,
            api_token=env_token,
            auth_mode=AUTH_MODE_BEARER,
            auth_token_source="env",
        )

    if settings.api_token:
        _validate_auth_token(settings.api_token, "provided")
        source = settings.auth_token_source if settings.auth_token_source != "none" else "provided"
        return replace(settings, auth_mode=AUTH_MODE_BEARER, auth_token_source=source)

    path = settings.api_token_path
    token = read_auth_token_file(path)
    source = "file"
    if token is None:
        candidate = generate_auth_token()
        if create_auth_token_file(path, candidate):
            token = candidate
            source = "generated"
        else:
            # Another first-start won the O_EXCL race; read its token. Retry
            # briefly because the winner may still be flushing the file.
            for _ in range(50):
                token = read_auth_token_file(path)
                if token is not None:
                    break
                time.sleep(0.02)
            if token is None:
                raise AuthTokenError(
                    f"API token file {path} exists but is empty or unreadable"
                )
            token = _validate_auth_token(token, "file")
    else:
        token = _validate_auth_token(token, "file")
        # Existing files also get a best-effort permission tightening.
        _restrict_token_file(path)
    return replace(
        settings,
        api_token=token,
        auth_mode=AUTH_MODE_BEARER,
        auth_token_source=source,
    )


def default_data_dir(venue: str = "demo") -> Path:
    """默认数据目录（§4.2 每环境独立库）。

    双轨过渡（本期决定）：Demo 继续用 ``trading-workbench/data``（不动用户正在跑的
    账本）；Live 用 ``trading-workbench/data/live``。等 M3 稳定后再统一搬迁。
    """

    root = Path(__file__).resolve().parents[2] / "data"
    return root if venue == "demo" else root / venue


@dataclass(frozen=True, slots=True)
class Settings:
    """不可变运行配置；不含任何 token 值。"""

    host: str
    port: int
    data_dir: Path
    demo_base_url: str
    probe_timeout_s: float
    probe_symbol: str
    probe_interval: str
    probe_limit: int
    probe_cache_ttl_s: float
    log_level: str
    api_token: str | None = field(repr=False)
    auth_mode: str = AUTH_MODE_DEV_OPEN
    auth_token_source: str = "none"
    require_execution_lease: bool = False
    auto_time_stop_hours: float = 0.0
    fill_symbols: tuple[str, ...] = ()
    # §8·C3：资金对账容差。按计价币最小精度量级设定，不用大容差掩盖问题。
    # 0.01 USDT 约等于一个最小报价变动，差额超出即判 partial 并给原因。
    pnl_reconcile_tolerance: str = "0.01"
    # T18 §8·C2：income（资金费/划转）自动回填。资金费不再依赖操作员手动
    # POST /api/sync-income，而是由服务端监督循环按节奏自己收敛。
    # - interval：两次自动尝试之间的最小间隔（默认 15 分钟，一个资金费周期
    #   远长于此，没必要更密）；
    # - min_interval：两次**真正打交易所**之间的硬下限，手动/强制触发也受它约束；
    # - window：单次请求的时间跨度上限（交易所 income 接口本身最多 7 天）；
    # - overlap：与已覆盖区间的小幅重叠，防边界丢行（重叠不产生重复行）。
    # T20 §2.2：交易所不可达时只读端点不得挂住。读路径（只读 GET）用独立的有界
    # 超时；下单 POST 保持原有的 12s 语义——不得用缩短下单超时来换读超时。
    read_timeout_s: float = 3.0
    # 一次只读快照/对账的**总**预算：防止“3s × 多次串联”把端点拖成十几秒。
    exchange_read_budget_s: float = 4.0
    # 读取失败后的冷却：这段时间内不再打上游，端点直接按 stale/不可用返回。
    exchange_failure_cooldown_s: float = 15.0
    income_sync_enabled: bool = True
    income_sync_interval_s: float = 900.0
    income_sync_min_interval_s: float = 60.0
    income_sync_window_s: float = 604800.0
    income_sync_overlap_s: float = 60.0
    service_version: str = SERVICE_VERSION
    schema_version: int = SCHEMA_VERSION
    # §6 M2：环境在启动时固定；未知值拒绝启动（fail-closed），不做静默回退。
    # 放在默认值字段区，避免破坏位置参数兼容性。
    venue: str = "demo"

    def __post_init__(self) -> None:
        if self.venue not in VENUES:
            raise ValueError(f"venue must be one of {VENUES}, got {self.venue!r}")
        if self.auth_mode not in AUTH_MODES:
            raise ValueError(f"auth_mode must be one of {sorted(AUTH_MODES)}, got {self.auth_mode!r}")
        # Direct construction / dataclasses.replace(..., api_token=...) should
        # enable Bearer. Explicit dev-open paths clear api_token before this point.
        if self.api_token and self.auth_mode == AUTH_MODE_DEV_OPEN:
            object.__setattr__(self, "auth_mode", AUTH_MODE_BEARER)
        if self.api_token and self.auth_token_source == "none":
            object.__setattr__(self, "auth_token_source", "provided")

    # ---------------------------------------------------------------- venue
    @property
    def base_url(self) -> str:
        """当前环境的受控基址（Live 不读 WORKBENCH_DEMO_BASE_URL）。"""

        if self.venue == "live":
            return LIVE_BASE_URL
        return self.demo_base_url

    @property
    def venue_host(self) -> str:
        return VENUE_HOSTS[self.venue]

    # ---------------------------------------------------------------- paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "workbench-v1.sqlite"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def api_token_path(self) -> Path:
        return self.data_dir / AUTH_TOKEN_FILENAME

    # ---------------------------------------------------------------- derived
    @property
    def bearer_required(self) -> bool:
        return self.auth_mode == AUTH_MODE_BEARER

    @property
    def token_ready(self) -> bool:
        return bool(self.api_token)

    def describe(self) -> dict[str, object]:
        """可安全写日志/返回给 /health 的配置摘要（绝不含 token 值）。"""

        return {
            "service": SERVICE_NAME,
            "service_version": self.service_version,
            "schema_version": self.schema_version,
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "db_path": str(self.db_path),
            "demo_base_url": self.demo_base_url,
            "probe": {
                "symbol": self.probe_symbol,
                "interval": self.probe_interval,
                "limit": self.probe_limit,
                "timeout_s": self.probe_timeout_s,
                "cache_ttl_s": self.probe_cache_ttl_s,
            },
            "auth": {
                "mode": self.auth_mode,
                "bearer_required": self.bearer_required,
                "token_ready": self.token_ready,
                "token_source": self.auth_token_source,
                "token_path": str(self.api_token_path),
            },
            # T20：读路径的有界超时/冷却也进摘要，便于核对“端点为什么快速降级”。
            "exchange_read": {
                "read_timeout_s": self.read_timeout_s,
                "read_budget_s": self.exchange_read_budget_s,
                "failure_cooldown_s": self.exchange_failure_cooldown_s,
                "scope": "read-only GET; order POST/DELETE keeps its own timeout",
            },
            # T18：自动回填的调度参数也进摘要，便于运维核对“为什么资金费没收敛”，
            # 而不是让调用方去猜默认值。
            "income_sync": {
                "enabled": self.income_sync_enabled,
                "interval_s": self.income_sync_interval_s,
                "min_interval_s": self.income_sync_min_interval_s,
                "window_s": self.income_sync_window_s,
                "overlap_s": self.income_sync_overlap_s,
            },
            "log_level": self.log_level,
        }

    # ---------------------------------------------------------------- factory
    @classmethod
    def from_env(
        cls,
        *,
        host: str | None = None,
        port: int | None = None,
        data_dir: str | Path | None = None,
        log_level: str | None = None,
        default_auth_mode: str = AUTH_MODE_DEV_OPEN,
    ) -> "Settings":
        """命令行参数优先于环境变量，环境变量优先于内置默认值。

        ``default_auth_mode`` 由入口决定：``run_service`` 传 ``bearer``（默认安全），
        测试/库调用默认 ``dev-open``（保持旧测试无需 token）。显式
        ``WORKBENCH_AUTH_MODE`` 始终优先。
        """

        # §6 M2：环境先定，后面所有默认值都依赖它。
        resolved_venue = (_env("WORKBENCH_VENUE") or "demo").strip().lower()
        if resolved_venue not in VENUES:
            raise ValueError(
                f"WORKBENCH_VENUE must be one of {VENUES}, got {resolved_venue!r}"
            )

        resolved_host = (host or _env("WORKBENCH_HOST") or DEFAULT_HOST).strip()
        if resolved_host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"only loopback (127.0.0.1/::1) is allowed, got {resolved_host!r}"
            )

        resolved_port = (
            int(port)
            if port is not None
            else _env_int("WORKBENCH_PORT", DEFAULT_PORT, minimum=1, maximum=65535)
        )
        if not 1 <= resolved_port <= 65535:
            raise ValueError(f"port must be within [1, 65535], got {resolved_port}")

        if data_dir is not None:
            resolved_data_dir = Path(data_dir).expanduser()
        elif _env("WORKBENCH_DATA_DIR"):
            resolved_data_dir = Path(_env("WORKBENCH_DATA_DIR") or "").expanduser()
        else:
            # Live 默认落到 data/live/，与 Demo 账本物理隔离（§4.2）。
            resolved_data_dir = default_data_dir(resolved_venue)

        resolved_log_level = (
            log_level or _env("WORKBENCH_LOG_LEVEL") or "info"
        ).strip().lower()
        if resolved_log_level not in _LOG_LEVELS:
            raise ValueError(
                f"log level must be one of {sorted(_LOG_LEVELS)}, got {resolved_log_level!r}"
            )

        auth_mode = (_env("WORKBENCH_AUTH_MODE") or default_auth_mode).strip().lower()
        if auth_mode not in AUTH_MODES:
            raise ValueError(
                f"auth mode must be one of {sorted(AUTH_MODES)}, got {auth_mode!r}"
            )
        env_token = _env("WORKBENCH_API_TOKEN") if auth_mode == AUTH_MODE_BEARER else None
        token_source = "env" if env_token else "none"

        probe_symbol = (_env("WORKBENCH_PROBE_SYMBOL") or DEFAULT_PROBE_SYMBOL).upper()
        if not is_valid_symbol(probe_symbol):
            raise ValueError(f"invalid probe symbol: {probe_symbol!r}")

        probe_interval = (_env("WORKBENCH_PROBE_INTERVAL") or DEFAULT_PROBE_INTERVAL).lower()
        if probe_interval not in SUPPORTED_INTERVALS:
            raise ValueError(
                f"probe interval must be one of {SUPPORTED_INTERVALS}, got {probe_interval!r}"
            )

        return cls(
            host=resolved_host,
            port=resolved_port,
            data_dir=resolved_data_dir.resolve(),
            demo_base_url=(_env("WORKBENCH_DEMO_BASE_URL") or DEMO_BASE_URL).rstrip("/"),
            venue=resolved_venue,
            probe_timeout_s=_env_float(
                "WORKBENCH_PROBE_TIMEOUT_S", 10.0, minimum=1.0, maximum=30.0
            ),
            probe_symbol=probe_symbol,
            probe_interval=probe_interval,
            probe_limit=_env_int(
                "WORKBENCH_PROBE_LIMIT", DEFAULT_PROBE_LIMIT, minimum=1, maximum=100
            ),
            probe_cache_ttl_s=_env_float(
                "WORKBENCH_PROBE_CACHE_TTL_S", 5.0, minimum=0.0, maximum=60.0
            ),
            log_level=resolved_log_level,
            api_token=env_token,
            auth_mode=auth_mode,
            auth_token_source=token_source,
            require_execution_lease=(
                (_env("WORKBENCH_REQUIRE_EXECUTION_LEASE") or "").strip().lower()
                in {"1", "true", "yes", "on"}
                if (_env("WORKBENCH_REQUIRE_EXECUTION_LEASE") or "").strip()
                else auth_mode == AUTH_MODE_BEARER
            ),
            auto_time_stop_hours=_env_float(
                "WORKBENCH_TIME_STOP_HOURS", 0.0, minimum=0.0, maximum=168.0
            ),
            fill_symbols=tuple(
                symbol
                for symbol in (
                    part.strip().upper()
                    for part in (_env("WORKBENCH_FILL_SYMBOLS") or "").split(",")
                )
                if is_valid_symbol(symbol)
            ),
            read_timeout_s=_env_float(
                "WORKBENCH_READ_TIMEOUT_S", 3.0, minimum=0.2, maximum=12.0
            ),
            exchange_read_budget_s=_env_float(
                "WORKBENCH_EXCHANGE_READ_BUDGET_S", 4.0, minimum=0.2, maximum=30.0
            ),
            exchange_failure_cooldown_s=_env_float(
                "WORKBENCH_EXCHANGE_COOLDOWN_S", 15.0, minimum=0.0, maximum=300.0
            ),
            # T18 §8·C2：默认开启；显式 WORKBENCH_INCOME_SYNC=0 才关闭（关闭后
            # 只能手动 POST /api/sync-income，status.income_sync 会标注 disabled）。
            income_sync_enabled=(
                (_env("WORKBENCH_INCOME_SYNC") or "1").strip().lower()
                not in {"0", "off", "false", "no"}
            ),
            income_sync_interval_s=_env_float(
                "WORKBENCH_INCOME_SYNC_INTERVAL_S", 900.0, minimum=60.0, maximum=86400.0
            ),
            income_sync_min_interval_s=_env_float(
                "WORKBENCH_INCOME_SYNC_MIN_INTERVAL_S", 60.0, minimum=5.0, maximum=3600.0
            ),
            income_sync_window_s=_env_float(
                "WORKBENCH_INCOME_SYNC_WINDOW_S", 604800.0, minimum=3600.0, maximum=604800.0
            ),
            income_sync_overlap_s=_env_float(
                "WORKBENCH_INCOME_SYNC_OVERLAP_S", 60.0, minimum=0.0, maximum=600.0
            ),
        )
