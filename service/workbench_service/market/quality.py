"""数据质量：四态 verdict + 固定 issue codes（docs/04 §9）。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import config as C

VERDICTS = ("GOOD", "DEGRADED", "STALE", "BLOCKED")

#: 源级 issue：同一轮里重复出现只计一次（避免 8 个币各报一次 DERIVATIVES_PARTIAL 把整轮压成 DEGRADED）。
SYSTEMIC_CODES = frozenset(
    {
        "DERIVATIVES_PARTIAL",
        "UNIVERSE_DEGRADED",
        "WS_DOWN",
        "STREAM_LAG",
        "RATE_LIMITED_429",
        "IP_BANNED_418",
        "CLOCK_SKEW",
        "DATA_GAP",
        "PERSIST_FAILED",
        "SNAPSHOT_TRUNCATED",
        "BOOTSTRAP_INCOMPLETE",
    }
)


def _effective(issues: Sequence[Mapping[str, str]]) -> list[Mapping[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[Mapping[str, str]] = []
    for item in issues:
        code = str(item.get("code"))
        if code in SYSTEMIC_CODES:
            key = (code, str(item.get("severity")))
            if key in seen:
                continue
            seen.add(key)
        out.append(item)
    return out


def issue(code: str, severity: str, scope: str, detail: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "scope": scope[:60],
        "detail": detail[:180],
    }


def dedupe(issues: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, str]] = []
    for item in issues:
        key = (item["code"], item["severity"], item["scope"], item["detail"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def ticker_issues(symbol: str, age_s: float | None, *, deep: bool) -> list[dict[str, str]]:
    if age_s is None:
        return [issue("TICKER_STALE", "error", symbol, "ticker age unknown")]
    if age_s > C.TICKER_STALE_S:
        return [issue("TICKER_STALE", "error", symbol, f"ticker age {age_s:.0f}s > {C.TICKER_STALE_S:.0f}s")]
    if age_s > C.TICKER_FRESH_S:
        return [issue("TICKER_STALE", "warn", symbol, f"ticker age {age_s:.1f}s > {C.TICKER_FRESH_S:.0f}s")]
    return []


def bar_issues(
    scope: str,
    interval: str,
    meta: Mapping[str, Any],
    *,
    held: bool,
) -> list[dict[str, str]]:
    fresh_s, unusable_s = C.FRESHNESS_S[interval]
    age_s = meta.get("age_s")
    issues: list[dict[str, str]] = []
    count = int(meta.get("count") or 0)
    needed = int(meta.get("needed") or 0)
    if count < needed:
        issues.append(
            issue("KLINE_INSUFFICIENT", "error", scope, f"{interval} bars {count} < needed {needed}")
        )
    if meta.get("gap"):
        issues.append(issue("KLINE_GAP", "error", scope, f"{interval} gap in recent bars"))
    if meta.get("non_monotonic"):
        issues.append(issue("KLINE_STRUCTURE_INVALID", "error", scope, f"{interval} non-monotonic"))
    if age_s is None:
        issues.append(issue("KLINE_NOT_FINAL", "error", scope, f"{interval} last closed bar unknown"))
    elif age_s > unusable_s:
        issues.append(
            issue("KLINE_STALE", "error", scope, f"{interval} age {age_s:.0f}s > {unusable_s}s")
        )
    elif age_s > fresh_s:
        issues.append(
            issue("KLINE_STALE", "warn", scope, f"{interval} age {age_s:.0f}s > {fresh_s}s")
        )
    return issues


def clock_issues(offset_ms: int | None) -> list[dict[str, str]]:
    if offset_ms is None:
        return [issue("CLOCK_SKEW", "error", "clock", "server time unavailable")]
    absolute = abs(int(offset_ms))
    if absolute > C.CLOCK_BLOCK_MS:
        return [issue("CLOCK_SKEW", "error", "clock", f"offset {absolute}ms > {C.CLOCK_BLOCK_MS}ms")]
    if absolute > C.CLOCK_WARN_MS:
        return [issue("CLOCK_SKEW", "warn", "clock", f"offset {absolute}ms > {C.CLOCK_WARN_MS}ms")]
    return []


def transport_issues(status: Mapping[str, Any]) -> list[dict[str, str]]:
    state = str(status.get("state") or "ok")
    cooldown = float(status.get("cooldown_s") or 0.0)
    if state == "banned":
        return [issue("IP_BANNED_418", "error", "rest", f"IP banned cooldown {cooldown:.0f}s")]
    if state == "cooldown":
        return [issue("RATE_LIMITED_429", "error", "rest", f"rate-limit cooldown {cooldown:.0f}s")]
    if state == "deferred":
        return [issue("RATE_LIMITED_429", "warn", "rest", "local weight budget deferred")]
    return []


def derivatives_issues(symbol: str, missing: Sequence[str]) -> list[dict[str, str]]:
    if not missing:
        return []
    return [
        issue(
            "DERIVATIVES_PARTIAL",
            "warn",
            symbol,
            "missing: " + ",".join(sorted(missing))[:120],
        )
    ]


def score_of(issues: Sequence[Mapping[str, str]]) -> float:
    effective = _effective(issues)
    errors = sum(1 for item in effective if item.get("severity") == "error")
    warns = sum(1 for item in effective if item.get("severity") == "warn")
    return round(max(0.0, 1.0 - 0.25 * errors - 0.05 * warns), 4)


def verdict_of(
    issues: Sequence[Mapping[str, str]],
    *,
    held_15m_unusable: bool = False,
    snapshot_age_s: float | None = None,
    blocked: bool = False,
) -> str:
    effective = _effective(issues)
    codes = {str(item.get("code")) for item in effective if item.get("severity") == "error"}
    if blocked or "IP_BANNED_418" in codes or "PERSIST_FAILED" in codes or "CLOCK_SKEW" in codes:
        return "BLOCKED"
    if held_15m_unusable:
        return "STALE"
    if snapshot_age_s is not None and snapshot_age_s > C.SNAPSHOT_STALE_S:
        return "STALE"
    score = score_of(issues)
    errors = sum(1 for item in effective if item.get("severity") == "error")
    warns = sum(1 for item in effective if item.get("severity") == "warn")
    if errors or warns >= 4 or score < 0.85:
        return "DEGRADED"
    return "GOOD"


def summarize_health(
    issues: Sequence[Mapping[str, str]],
    *,
    held_15m_unusable: bool = False,
    snapshot_age_s: float | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    """返回对外一致的 issues/score/verdict。

    对外列表使用 ``_effective``：源级 issue（如 8 个 deep 各一条
    ``DERIVATIVES_PARTIAL``）只保留一条，避免把同一上游缺陷放大成 8 条告警；
    每个币的细节仍保留在 evidence 的 ``symbols.<symbol>.quality.issues``。
    score/verdict 也基于同一去重集合，保证三者语义一致。
    """

    deduped = dedupe(issues)
    effective = _effective(deduped)
    return {
        "verdict": verdict_of(
            effective,
            held_15m_unusable=held_15m_unusable,
            snapshot_age_s=snapshot_age_s,
            blocked=blocked,
        ),
        "score": score_of(effective),
        "issues": effective,
    }
