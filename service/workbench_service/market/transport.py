"""公共行情传输层：连接池、超时、权重预算、429/418 冷却。

约束（docs/04 §8）：
- 只发 GET，无 key、无签名；响应 429/418 **不重试**，记录冷却后直接抛错；
- 本地预算 600 weight/min（官方 2400 的 25%）；后台 75% 让路、普通 90% 让路；
- ``/futures/data/*`` 独立 800 次 / 300s 窗口；
- 冷却状态落盘 ``data/state/market_rate_limit.json``，进程重启后仍生效。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import httpx

from . import config as C

logger = logging.getLogger("workbench.market.transport")

USER_AGENT = "trading-workbench-service/0.1.0-m1.1 (read-only public market)"
FORBIDDEN_HEADERS = frozenset({"authorization", "x-mbx-apikey", "x-mbx-signature"})


class MarketUpstreamError(RuntimeError):
    """上游失败（非冷却类），调用方按 502 降级。"""

    def __init__(self, status_code: int | None, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


class MarketTimeout(MarketUpstreamError):
    def __init__(self, detail: str) -> None:
        super().__init__(None, "MARKET_TIMEOUT", detail)


class MarketCooldown(MarketUpstreamError):
    """429 冷却中；``retry_after_s`` 供 Retry-After 使用。"""

    def __init__(self, retry_after_s: float, detail: str, *, status_code: int = 429) -> None:
        super().__init__(status_code, "MARKET_COOLDOWN_429", detail)
        self.retry_after_s = float(retry_after_s)


class MarketBanned(MarketUpstreamError):
    """418 IP 封禁；``retry_after_s`` 供 Retry-After 使用。"""

    def __init__(self, retry_after_s: float, detail: str) -> None:
        super().__init__(418, "MARKET_BANNED_418", detail)
        self.retry_after_s = float(retry_after_s)


class MarketBudgetDeferred(MarketUpstreamError):
    """本地权重预算不足，低优先级请求主动延后。"""

    def __init__(self, detail: str) -> None:
        super().__init__(None, "MARKET_BUDGET_DEFERRED", detail)


@dataclass(slots=True)
class _Record:
    at: float
    weight: int
    group: str


def _parse_retry_after(headers: Mapping[str, str] | None, default: float, maximum: float) -> float:
    raw = None
    for key, value in dict(headers or {}).items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    try:
        seconds = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        seconds = default
    return min(maximum, max(1.0, seconds))


class MarketTransport:
    """同步只读 HTTP 客户端 + 本地权重预算。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        local_budget_1m: int = C.LOCAL_WEIGHT_BUDGET_1M,
        weight_limit_1m: int = C.WEIGHT_LIMIT_1M,
        data_budget_5m: int = C.DATA_BUDGET_5M,
        state_path: Path | None = None,
        client: httpx.Client | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.local_budget_1m = max(1, int(local_budget_1m))
        self.weight_limit_1m = max(1, int(weight_limit_1m))
        self.data_budget_5m = max(1, int(data_budget_5m))
        self.state_path = Path(state_path) if state_path else None
        self._clock: Callable[[], float] = now_fn or time.time
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0)),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        self._lock = threading.Lock()
        self._records: deque[_Record] = deque()
        self._data_records: deque[_Record] = deque()
        self._cooldown_until = 0.0
        self._cooldown_status: int | None = None
        self._last_deferred_at = 0.0
        self._requests = 0
        self._weight_total = 0
        self._status_counts: dict[str, int] = {}
        self._cache_hits = 0
        self._assert_read_only_client()
        self._load_state()

    # ------------------------------------------------------------------ guards
    def _assert_read_only_client(self) -> None:
        present = {str(key).lower() for key in self._client.headers}
        leaked = present.intersection(FORBIDDEN_HEADERS)
        if leaked:
            raise RuntimeError(f"market transport must not send credential headers: {sorted(leaked)}")

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            until = float(payload.get("cooldown_until", 0))
            if until > self._clock():
                self._cooldown_until = until
                self._cooldown_status = int(payload.get("status", 429))
        except (OSError, ValueError, TypeError):
            logger.warning("market rate-limit state unreadable; ignoring")

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "cooldown_until": self._cooldown_until,
                "status": self._cooldown_status,
                "saved_at": self._clock(),
            }
            self.state_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            logger.warning("market rate-limit state not persisted")

    # ------------------------------------------------------------------ state
    def cooldown_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def _used_weight_1m(self, now: float) -> int:
        cutoff = now - 60.0
        while self._records and self._records[0].at < cutoff:
            self._records.popleft()
        return sum(record.weight for record in self._records)

    def _used_data_5m(self, now: float) -> int:
        """``/futures/data`` 独立 300s 窗口；与 60s 全局权重记录完全分离。"""

        cutoff = now - C.DATA_WINDOW_S
        while self._data_records and self._data_records[0].at < cutoff:
            self._data_records.popleft()
        return len(self._data_records)

    def _check_budget(self, weight: int, priority: str, group: str) -> None:
        now = self._clock()
        used = self._used_weight_1m(now)
        utilization = used / max(1, self.local_budget_1m)
        normalized = str(priority or "normal").lower()
        # 1) 429/418 冷却在 get_json 入口已优先判断；2) 全局 600/min 预算其次。
        if normalized == "background" and utilization >= C.BACKGROUND_DEFER_RATIO:
            self._last_deferred_at = now
            raise MarketBudgetDeferred(
                f"weight budget {utilization:.0%} used; background request deferred"
            )
        if normalized != "critical" and utilization >= C.NORMAL_DEFER_RATIO:
            self._last_deferred_at = now
            raise MarketBudgetDeferred(
                f"weight budget {utilization:.0%} used; non-critical request deferred"
            )
        if group == "futures_data":
            data_used = self._used_data_5m(now)
            if data_used >= self.data_budget_5m:
                self._last_deferred_at = now
                raise MarketBudgetDeferred(
                    f"futures/data 5m budget exhausted ({data_used}/{self.data_budget_5m})"
                )

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            remaining = max(0.0, self._cooldown_until - now)
            deferred_recently = (now - self._last_deferred_at) <= 60.0
            if remaining > 0:
                state = "banned" if self._cooldown_status == 418 else "cooldown"
            elif deferred_recently:
                state = "deferred"
            else:
                state = "ok"
            return {
                "state": state,
                "cooldown_s": round(remaining, 3),
                "weight_used_1m": self._used_weight_1m(now),
                "weight_limit_1m": self.weight_limit_1m,
                "local_budget_1m": self.local_budget_1m,
                "data_weight_used_5m": self._used_data_5m(now),
                "data_budget_5m": self.data_budget_5m,
                "data_window_s": C.DATA_WINDOW_S,
                "requests": self._requests,
                "weight_total": self._weight_total,
                "status_counts": dict(self._status_counts),
            }

    def note_cache_hit(self) -> None:
        with self._lock:
            self._cache_hits += 1

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "requests": self._requests,
                "weight_total": self._weight_total,
                "cache_hits": self._cache_hits,
                "status_counts": dict(self._status_counts),
            }

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ request
    def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        weight: int = 1,
        priority: str = "normal",
        purpose: str = "",
        group: str = "general",
        timeout_s: float | None = None,
    ) -> Any:
        normalized_weight = max(1, int(weight))
        with self._lock:
            remaining = max(0.0, self._cooldown_until - self._clock())
            if remaining > 0:
                if self._cooldown_status == 418:
                    raise MarketBanned(
                        remaining, f"Binance IP ban cooldown active ({remaining:.0f}s)"
                    )
                raise MarketCooldown(
                    remaining, f"Binance rate-limit cooldown active ({remaining:.0f}s)"
                )
            self._check_budget(normalized_weight, priority, group)
            now = self._clock()
            self._requests += 1
            self._weight_total += normalized_weight
            record = _Record(at=now, weight=normalized_weight, group=group)
            self._records.append(record)
            if group == "futures_data":
                self._data_records.append(record)

        response: httpx.Response | None = None
        try:
            options = {"timeout": max(0.1, float(timeout_s))} if timeout_s is not None else {}
            response = self._client.get(path, params=dict(params or {}), **options)
        except httpx.TimeoutException as exc:
            self._note_status("timeout")
            raise MarketTimeout(f"upstream timeout on {path}: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            self._note_status("transport_error")
            raise MarketUpstreamError(None, "MARKET_UNREACHABLE", str(exc)) from exc

        status_code = int(response.status_code)
        self._note_status(str(status_code))
        if status_code == 429:
            retry_after = _parse_retry_after(
                response.headers, C.COOLDOWN_429_DEFAULT_S, C.COOLDOWN_429_MAX_S
            )
            self._set_cooldown(retry_after, 429)
            raise MarketCooldown(retry_after, "Binance HTTP 429 rate limited; no retry")
        if status_code == 418:
            retry_after = _parse_retry_after(
                response.headers, C.COOLDOWN_418_DEFAULT_S, C.COOLDOWN_418_MAX_S
            )
            self._set_cooldown(retry_after, 418)
            raise MarketBanned(retry_after, "Binance HTTP 418 temporary IP ban; REST stops")
        if status_code >= 400:
            raise MarketUpstreamError(status_code, "MARKET_HTTP_ERROR", f"HTTP {status_code} on {path}")

        try:
            return response.json()
        except ValueError as exc:
            raise MarketUpstreamError(status_code, "MARKET_BAD_JSON", f"bad json on {path}") from exc

    def _note_status(self, key: str) -> None:
        with self._lock:
            self._status_counts[key] = self._status_counts.get(key, 0) + 1

    def _set_cooldown(self, retry_after_s: float, status_code: int) -> None:
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + float(retry_after_s))
            self._cooldown_status = int(status_code)
        self._save_state()
        logger.error(
            "market cooldown armed status=%s retry_after_s=%.0f", status_code, retry_after_s
        )


def cooldown_retry_after(exc: MarketUpstreamError) -> float | None:
    """从异常里提取 Retry-After（供 API 层）。"""

    if isinstance(exc, (MarketCooldown, MarketBanned)):
        return exc.retry_after_s
    return None


def status_or_disabled() -> dict[str, Any]:
    """证据 schema 的 ``sources.rest`` 需要完整字段；无 transport 时给保守值。"""

    return {
        "state": "ok",
        "cooldown_s": 0.0,
        "weight_used_1m": 0,
        "weight_limit_1m": C.WEIGHT_LIMIT_1M,
        "local_budget_1m": C.LOCAL_WEIGHT_BUDGET_1M,
    }


def summarize_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """只保留证据 schema 允许的字段（避免把内部 telemetry 写进快照）。"""

    allowed = ("state", "cooldown_s", "weight_used_1m", "weight_limit_1m", "local_budget_1m")
    payload = {key: status.get(key) for key in allowed}
    if payload["state"] not in {"ok", "deferred", "cooldown", "banned"}:
        payload["state"] = "ok"
    payload["cooldown_s"] = float(payload.get("cooldown_s") or 0.0)
    payload["weight_used_1m"] = int(payload.get("weight_used_1m") or 0)
    payload["weight_limit_1m"] = int(payload.get("weight_limit_1m") or C.WEIGHT_LIMIT_1M)
    payload["local_budget_1m"] = int(payload.get("local_budget_1m") or C.LOCAL_WEIGHT_BUDGET_1M)
    return payload


def iter_statuses(counts: Mapping[str, int]) -> Iterable[tuple[str, int]]:
    return sorted(counts.items())
