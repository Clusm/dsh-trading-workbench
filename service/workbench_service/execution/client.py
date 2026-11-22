"""币安 Demo 签名 REST 客户端（HMAC-SHA256 + 时间同步 + demo-only 断言）。

只服务 USD-M Demo（``https://demo-fapi.binance.com``）：
- **每次请求前**校验 URL 的 scheme/host/port/path，任何非 demo 主机直接抛
  :class:`DemoGuardViolation`，不发出任何字节（docs/03 §3.4 L2/L3）；
- 写操作（POST/DELETE）**绝不重试**；读操作仅在传输错误或 ``-1021/-1022`` 时
  重新校准时间后重试一次；
- 签名串与发送串完全一致（按插入顺序 urlencode），避免 httpx 二次编码导致 ``-1022``。
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

import httpx

from ..config import (
    DEMO_HOST,
    EGRESS_PROXY_ENV,
    egress_proxy_label,
    httpx_proxy_kwargs,
    parse_egress_proxy,
)
from .credentials import DemoCredentials
from .errors import DemoApiError, DemoGuardViolation

__all__ = ["DemoBinanceClient", "DEMO_REQUIRED_HOST"]

logger = logging.getLogger("workbench.exchange")

DEMO_REQUIRED_HOST = DEMO_HOST
USER_AGENT = "trading-workbench-service/0.1.0-m1.2 (demo signed execution)"

_TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"})

#: T20：一次“只读读取”的总预算（只影响 GET）。ContextVar 让预算天然按线程隔离：
#: 监督循环与请求线程各自开预算，互不干扰，也不会把 3s 读超时叠加成十几秒。
_READ_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "twb_read_deadline", default=None
)



def _sanitize_proxy_env() -> list[str]:
    """清掉 NO_PROXY 里 httpx 无法解析的条目（例如 IPv6 方括号写法）。

    真实故障（2026-11-15）：环境里 NO_PROXY 含 [::1] 时，httpx.Client() 在解析
    代理白名单阶段抛 InvalidURL，于是任何客户端构造都失败——包括与代理无关的调用。
    表现为启动即崩、报错位置误导。这里在构造客户端前过滤非法条目并记日志。
    """

    removed: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        kept: list[str] = []
        for item in raw.split(","):
            entry = item.strip()
            if not entry:
                continue
            candidate = entry.lstrip(".")
            if candidate.startswith("[") or "]" in candidate:
                removed.append(entry)
                continue
            kept.append(entry)
        os.environ[key] = ",".join(kept)
    if removed:
        logger.warning("proxy_env_sanitized removed=%s", ",".join(sorted(set(removed))))
    return removed

def _utc_ms() -> int:
    return int(time.time() * 1000)


class DemoBinanceClient:
    """轻量签名客户端；``transport`` 可在测试中替换为 fake 上游。"""

    def __init__(
        self,
        base_url: str,
        credentials: DemoCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 12.0,
        # 默认 3s：直接构造客户端也不会再让只读 GET 顶满 12s 的写超时。
        read_timeout_s: float = 3.0,
        recv_window_ms: int = 5000,
        clock=time.time,
        allowed_host: str | None = None,
        egress_proxy: str | None = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        # §6 M2：守卫按环境放行，而不是永远只认 demo host。默认 demo 保持向后兼容；
        # live 由 Settings.venue_host 传入——否则实盘启动时客户端会直接拒绝自己。
        self._allowed_host = str(allowed_host or DEMO_REQUIRED_HOST).strip().lower()
        self._guard_url(self._base_url, base=True)
        self.credentials = credentials
        self._recv_window_ms = int(recv_window_ms)
        self._clock = clock
        self._offset_ms = 0
        # T20：读写分开计时。写（下单/撤单）保持 ``timeout_s``（默认 12s）语义不变；
        # 读（GET）用 ``read_timeout_s``（默认 3s）有界超时，交易所挂掉时端点不会
        # 被拖住十几秒。两者都不做“用缩短下单超时换读超时”的折中。
        self._write_timeout_s = max(0.05, float(timeout_s))
        self._read_timeout_s = max(
            0.05, float(timeout_s if read_timeout_s is None else read_timeout_s)
        )
        # 包入口已执行过一次；这里再兜底一次（直接 import 本模块的场景）。
        from ..proxyenv import sanitize_proxy_env as _sanitize
        _sanitize()
        # P0②（docs/19 §4 / R1）：出口代理只**显式**交给这个 httpx 客户端。
        # 不写进 os.environ（否则本地/无关请求也会被改道）；空值 = 维持现状。
        self._egress_proxy = parse_egress_proxy(
            egress_proxy if egress_proxy is not None else os.environ.get(EGRESS_PROXY_ENV)
        )
        self._client = httpx.Client(**self._client_options(transport=transport))
        # 启动日志只打 host:port：凭据/完整 URL 一律不落日志。
        logger.info(
            "egress_proxy=%s component=execution venue_host=%s",
            self.egress_proxy_label,
            self._allowed_host,
        )

    # ------------------------------------------------------------------ guard
    def _guard_url(self, url: str, *, base: bool = False) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise DemoGuardViolation(f"https required, got scheme={parsed.scheme!r}", url=url)
        if parsed.username or parsed.password:
            raise DemoGuardViolation("base URL must not embed credentials", url=url)
        if parsed.hostname != self._allowed_host:
            raise DemoGuardViolation(
                f"host must be {self._allowed_host}, got {parsed.hostname!r}", url=url
            )
        if parsed.port not in (None, 443):
            raise DemoGuardViolation(f"only default https port allowed, got {parsed.port!r}", url=url)
        if base and parsed.path not in ("", "/"):
            raise DemoGuardViolation(f"base URL must not contain a path: {parsed.path!r}", url=url)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def host(self) -> str:
        return self._allowed_host

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------- 出口代理
    @property
    def egress_proxy(self) -> str | None:
        """生效的出口代理（已校验，不含 userinfo）；None = 未显式配置。"""

        return self._egress_proxy

    @property
    def egress_proxy_label(self) -> str:
        """日志/健康端点用的脱敏标签：``host:port`` 或 ``none``。"""

        return egress_proxy_label(self._egress_proxy)

    def _client_options(self, *, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
        """httpx.Client 的构造参数（供测试断言“代理确实传给了客户端”）。

        未配置代理时**不加** proxy 键：保持 httpx 原有的进程环境行为不变。
        """

        options: dict[str, Any] = {
            "timeout": self._timeout_for("GET"),
            "transport": transport,
            "follow_redirects": False,
            "headers": {"User-Agent": USER_AGENT, "Accept": "application/json"},
        }
        options.update(httpx_proxy_kwargs(self._egress_proxy))
        return options

    # ------------------------------------------------------------- 超时预算
    @property
    def read_timeout_s(self) -> float:
        """只读 GET 的有界超时（T20）。"""

        return self._read_timeout_s

    @property
    def write_timeout_s(self) -> float:
        """签名写请求（下单/撤单）的超时；T20 不改这个语义。"""

        return self._write_timeout_s

    def _timeout_for(self, method: str, path: str = "") -> httpx.Timeout:
        """读/写分别取超时；读还会被当前读预算裁剪到“剩余额度”。"""

        if method.upper() != "GET":
            return httpx.Timeout(
                self._write_timeout_s, connect=min(self._write_timeout_s, 5.0)
            )
        budget = self._read_timeout_s
        deadline = _READ_DEADLINE.get()
        if deadline is not None:
            remaining = deadline - self._clock()
            if remaining <= 0.01:
                # 预算耗尽：不再发这一次上游请求，直接按“结果未知”失败。
                raise DemoApiError(
                    "read budget exhausted before upstream call",
                    path=path,
                    kind="timeout",
                    payload={"error": "ReadBudgetExhausted"},
                )
            budget = min(budget, remaining)
        return httpx.Timeout(budget, connect=min(budget, 5.0))

    @contextlib.contextmanager
    def read_budget(self, seconds: float | None) -> Iterator[None]:
        """一次只读操作的总预算（只影响 GET 超时，不影响写请求）。

        嵌套取**更早**的截止时间，内层不会把外层预算放大——否则“快照里再开预算”
        会让总时长重新计时，端点又变回十几秒。
        """

        if seconds is None or float(seconds) <= 0:
            yield
            return
        current = _READ_DEADLINE.get()
        proposed = self._clock() + float(seconds)
        deadline = proposed if current is None else min(current, proposed)
        token = _READ_DEADLINE.set(deadline)
        try:
            yield
        finally:
            _READ_DEADLINE.reset(token)

    # ------------------------------------------------------------------ time
    def sync_time(self) -> int | None:
        """读取服务器时间并计算偏移；失败静默返回 ``None``。"""

        url = f"{self._base_url}/fapi/v1/time"
        try:
            self._guard_url(url)
            # 校时也是只读 GET：同样用有界读超时，不能挂在 12s。
            response = self._client.get(url, timeout=self._timeout_for("GET", "/fapi/v1/time"))
            if response.status_code != 200:
                return None
            payload = response.json()
            server_ms = int(payload.get("serverTime") or 0)
            if server_ms <= 0:
                return None
            self._offset_ms = server_ms - int(self._clock() * 1000)
            return self._offset_ms
        except Exception:  # noqa: BLE001 - 校时失败不阻断启动
            return None

    @property
    def time_offset_ms(self) -> int:
        return self._offset_ms

    # ------------------------------------------------------------------ HTTP
    def _send(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        *,
        signed: bool,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if signed:
            query = self._signed_query(params or {})
            url = f"{url}?{query}"
            headers["X-MBX-APIKEY"] = self.credentials.api_key
        elif params:
            query = urllib.parse.urlencode(
                [(key, value) for key, value in params.items() if value is not None]
            )
            if query:
                url = f"{url}?{query}"
        self._guard_url(url)
        return self._client.request(
            method, url, headers=headers, timeout=self._timeout_for(method, path)
        )

    def _signed_query(self, params: Mapping[str, Any]) -> str:
        payload: dict[str, Any] = {key: value for key, value in params.items() if value is not None}
        payload.pop("signature", None)
        payload["timestamp"] = str(int(self._clock() * 1000) + self._offset_ms)
        payload["recvWindow"] = str(self._recv_window_ms)
        query = urllib.parse.urlencode(list(payload.items()))
        signature = hmac.new(
            self.credentials.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _parse(self, response: httpx.Response, path: str) -> Any:
        if response.status_code == 418:
            raise DemoApiError(
                "HTTP 418 IP ban", path=path, status_code=418, kind="banned",
                retry_after_s=_retry_after(response),
            )
        if response.status_code == 429:
            raise DemoApiError(
                "HTTP 429 rate limited", path=path, status_code=429, kind="rate_limited",
                retry_after_s=_retry_after(response),
            )
        # P0②（docs/19 R1）：451 = 币安地区访问限制（出口 IP 位于受限地区）。
        # 它既不是 5xx 也不是“非 JSON 的 4xx 拒绝”：重试没有意义，必须单独归类，
        # 由上层映射成 E_VENUE_GEO_BLOCKED 并退避——这正是每 30–36s 一条
        # snapshot_failed 的根因路径。
        if response.status_code == 451:
            raise DemoApiError(
                "HTTP 451 venue geo restriction",
                path=path,
                status_code=451,
                kind="geo_blocked",
                retry_after_s=_retry_after(response),
                payload={"error": "VenueGeoRestricted"},
            )
        if response.status_code >= 500:
            raise DemoApiError(
                f"HTTP {response.status_code}", path=path, status_code=response.status_code,
                kind="unknown",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise DemoApiError(
                "upstream returned non-JSON", path=path, status_code=response.status_code,
                kind="rejected" if response.status_code < 500 else "unknown",
            ) from exc
        if isinstance(payload, dict):
            code = payload.get("code")
            if code is not None:
                try:
                    ok = int(code) == 200
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    raise DemoApiError(
                        f"binance error {code}: {payload.get('msg')}",
                        path=path,
                        status_code=response.status_code,
                        code=int(code) if _is_int(code) else None,
                        kind="rejected" if response.status_code < 500 else "unknown",
                        payload=payload,
                    )
        if response.status_code >= 400:
            raise DemoApiError(
                f"HTTP {response.status_code}", path=path, status_code=response.status_code,
                kind="rejected",
            )
        return payload

    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        signed: bool = True,
        retry_read: bool = True,
    ) -> Any:
        """统一入口；写方法永不重试，读方法最多重试一次。"""

        method = method.upper()
        attempts = 2 if (method == "GET" and retry_read) else 1
        last_error: DemoApiError | None = None
        for attempt in range(attempts):
            try:
                response = self._send(method, path, params, signed=signed)
                return self._parse(response, path)
            except DemoApiError as exc:
                last_error = exc
                # 时钟漂移：GET 可重新校时后重试一次；POST/DELETE 永不重试
                # （结果不确定只能按 clientOrderId 查询，见 docs/03 §9 F03）。
                clock_skew = exc.code in (-1021, -1022)
                if clock_skew and method == "GET" and attempt + 1 < attempts:
                    self.sync_time()
                    continue
                # geo_blocked 与 banned/rate_limited 一样：重试只会再要一个 451，
                # 止住重试风暴（退避由 execution/service.py 负责）。
                if exc.definitely_rejected or exc.kind in {
                    "banned",
                    "rate_limited",
                    "timeout",
                    "geo_blocked",
                }:
                    raise
                if attempt + 1 < attempts:
                    self.sync_time()
                    continue
                raise
            except httpx.HTTPError as exc:
                # T20：超时不再重试。读超时重试一次就把 3s 变成 6s，端点又变慢；
                # 且“没读到”本身就是结果未知，重试不改变事实（只读端点随后降级）。
                timed_out = isinstance(exc, httpx.TimeoutException)
                last_error = DemoApiError(
                    f"transport error: {type(exc).__name__}",
                    path=path,
                    kind="timeout" if timed_out else "unknown",
                    payload={"error": type(exc).__name__},
                )
                if timed_out or attempt + 1 >= attempts:
                    raise last_error from exc
                self.sync_time()
                continue
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ 端点
    def exchange_info(self) -> dict[str, Any]:
        payload = self.request("GET", "/fapi/v1/exchangeInfo", signed=False)
        return payload if isinstance(payload, dict) else {}

    def premium_index(self, symbol: str) -> dict[str, Any]:
        """标记价/指数价/资金费（公共端点，用于服务端行情事实）。"""

        payload = self.request(
            "GET", "/fapi/v1/premiumIndex", {"symbol": symbol.upper()}, signed=False
        )
        return payload if isinstance(payload, dict) else {}

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        """最优买卖（公共端点，用于价差 bps）。"""

        payload = self.request(
            "GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol.upper()}, signed=False
        )
        return payload if isinstance(payload, dict) else {}

    def klines(self, symbol: str, *, interval: str = "1m", limit: int = 2) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            "/fapi/v1/klines",
            {"symbol": symbol.upper(), "interval": interval, "limit": int(limit)},
            signed=False,
        )
        if not isinstance(payload, list):
            return []
        rows: list[dict[str, Any]] = []
        for row in payload:
            if isinstance(row, list) and len(row) >= 5:
                rows.append({"open_time": row[0], "close": row[4]})
        return rows

    def leverage_bracket(self, symbol: str | None = None) -> Any:
        """杠杆档位（逐 symbol 最大杠杆与维持保证金率）。"""

        params = {"symbol": symbol.upper()} if symbol else None
        return self.request("GET", "/fapi/v1/leverageBracket", params)

    def account(self) -> dict[str, Any]:
        payload = self.request("GET", "/fapi/v2/account")
        return payload if isinstance(payload, dict) else {}

    def position_risk(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/fapi/v2/positionRisk")
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def open_orders(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/fapi/v1/openOrders")
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def open_algo_orders(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/fapi/v1/openAlgoOrders")
        return normalize_algo_orders(payload)

    def position_mode(self) -> str | None:
        payload = self.request("GET", "/fapi/v1/positionSide/dual")
        if not isinstance(payload, dict) or "dualSidePosition" not in payload:
            return None
        return "HEDGE" if _truthy(payload.get("dualSidePosition")) else "ONE_WAY"

    def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            self.request(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}
            )
        except DemoApiError as exc:
            # -4046: No need to change margin type
            if exc.code == -4046:
                return
            raise

    def set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self.request(
                "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}
            )
        except DemoApiError as exc:
            # -4046: No need to change leverage（部分环境返回同一码）
            if exc.code == -4046:
                return
            raise

    def submit_order(self, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.request("POST", "/fapi/v1/order", params)
        return payload if isinstance(payload, dict) else {}

    def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        payload = self.request(
            "GET",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        return payload if isinstance(payload, dict) else {}

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        payload = self.request(
            "DELETE",
            "/fapi/v1/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        return payload if isinstance(payload, dict) else {}

    def submit_algo_order(self, params: Mapping[str, Any]) -> dict[str, Any]:
        payload = self.request("POST", "/fapi/v1/algoOrder", params)
        return payload if isinstance(payload, dict) else {}

    def query_algo_order(self, client_algo_id: str) -> dict[str, Any]:
        payload = self.request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_algo_id})
        return payload if isinstance(payload, dict) else {}

    def cancel_algo_order(self, algo_id: str) -> dict[str, Any]:
        payload = self.request("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
        return payload if isinstance(payload, dict) else {}

    def income(
        self, *, start_ms: int, end_ms: int, limit: int = 1000, income_type: str | None = None
    ) -> list[dict[str, Any]]:
        """§8·C2：账户资金流水（资金费/划转/返佣）。只读 GET，可重试。

        这是“策略盈亏”与“账户权益变动”之间的桥：不取这条流水，
        资金费只能算未知，不能按 0 计入。
        """

        params: dict[str, Any] = {
            "startTime": int(start_ms),
            "endTime": int(end_ms),
            "limit": max(1, min(int(limit), 1000)),
        }
        if income_type:
            params["incomeType"] = income_type
        payload = self.request("GET", "/fapi/v1/income", params)
        return payload if isinstance(payload, list) else []

    def user_trades(
        self,
        symbol: str,
        order_id: int | str | None = None,
        *,
        from_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": symbol, "limit": max(1, min(int(limit), 1000))}
        if order_id not in (None, ""):
            params["orderId"] = order_id
        if from_id is not None:
            params["fromId"] = max(1, int(from_id))
        payload = self.request("GET", "/fapi/v1/userTrades", params)
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


# ---------------------------------------------------------------------- helpers
def normalize_algo_orders(payload: Any) -> list[dict[str, Any]]:
    """兼容裸数组 / ``{"orders": []}`` / ``{"data": []}`` / 单对象。"""

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("orders", "data", "rows"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            if payload.get("algoId") is not None:
                return [payload]
            if payload.get("code") is not None:
                return []
            raise DemoApiError(
                "openAlgoOrders returned unknown structure",
                path="/fapi/v1/openAlgoOrders",
                kind="unknown",
            )
    else:
        raise DemoApiError(
            "openAlgoOrders returned unknown structure",
            path="/fapi/v1/openAlgoOrders",
            kind="unknown",
        )
    return [row for row in rows if isinstance(row, dict)]


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_int(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False
