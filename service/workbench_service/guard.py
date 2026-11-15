"""Demo-only 守卫：行情基址必须指向币安 Demo 域名的公共只读接口。

设计依据：docs/01 §10.2 L1、docs/03 §3（demo-only 硬隔离）、docs/06 §6.2(b)。
M1.0 只做“公共只读行情”守卫：无 key、无签名、无写方法。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import DEMO_HOST, VENUE_HOSTS

REASON_OK = "DEMO_GUARD_OK"
REASON_OK_LIVE = "LIVE_GUARD_OK"
REASON_VENUE = "E_GUARD_VENUE"
REASON_HOST = "E_DEMO_GUARD_HOST"
REASON_SCHEME = "E_DEMO_GUARD_SCHEME"
REASON_CREDENTIALS = "E_DEMO_GUARD_CREDENTIALS"
REASON_PORT = "E_DEMO_GUARD_PORT"
REASON_PATH = "E_DEMO_GUARD_PATH"


@dataclass(frozen=True, slots=True)
class GuardResult:
    """守卫结论；``reason_code`` 直接进 `/api/ready.reasons[]`。"""

    ok: bool
    reason_code: str
    detail: str
    base_url: str
    venue: str = "demo"

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "base_url": self.base_url,
            "venue": self.venue,
            "signed_trading_enabled": False,
            "auth": "none",
        }


def evaluate_guard(base_url: str, venue: str = "demo") -> GuardResult:
    """§6 M2：按环境校验基址；失败时返回结构化原因码，绝不抛异常。

    守卫只做一件事：**该环境的基址必须是该环境受控映射里的 host**。
    Live 不是“放开 Demo 守卫”，而是换一张白名单——未知环境一律拒绝。
    """

    expected_host = VENUE_HOSTS.get(venue)
    if expected_host is None:
        return GuardResult(
            False, REASON_VENUE, f"unknown venue {venue!r}; allowed={sorted(VENUE_HOSTS)}",
            (base_url or "").strip(), venue or "unknown",
        )

    raw = (base_url or "").strip()
    parsed = urlsplit(raw)

    if parsed.scheme != "https":
        return GuardResult(False, REASON_SCHEME, f"https required, got scheme={parsed.scheme!r}", raw, venue)
    if parsed.username or parsed.password:
        return GuardResult(False, REASON_CREDENTIALS, "base URL must not embed username/password", raw, venue)
    if parsed.hostname != expected_host:
        return GuardResult(
            False, REASON_HOST,
            f"{venue} host must be {expected_host}, got {parsed.hostname!r}", raw, venue,
        )
    if parsed.port not in (None, 443):
        return GuardResult(False, REASON_PORT, f"only 443/default port allowed, got {parsed.port!r}", raw, venue)
    if parsed.path not in ("", "/"):
        return GuardResult(False, REASON_PATH, f"base URL must not contain a path, got {parsed.path!r}", raw, venue)
    if parsed.query or parsed.fragment:
        return GuardResult(False, REASON_PATH, "base URL must not contain query/fragment", raw, venue)

    ok_code = REASON_OK if venue == "demo" else REASON_OK_LIVE
    detail = (
        "demo-only public read-only market guard passed"
        if venue == "demo"
        else "live venue guard passed: host is the controlled live mapping"
    )
    return GuardResult(True, ok_code, detail, raw, venue)


def evaluate_demo_guard(base_url: str) -> GuardResult:
    """兼容入口：Demo 守卫（旧调用点保持不变）。"""
    return evaluate_guard(base_url, "demo")


def _legacy_evaluate_demo_guard_body(base_url: str) -> GuardResult:  # pragma: no cover
    raw = (base_url or "").strip()
    parsed = urlsplit(raw)

    if parsed.scheme != "https":
        return GuardResult(False, REASON_SCHEME, f"https required, got scheme={parsed.scheme!r}", raw)
    if parsed.username or parsed.password:
        return GuardResult(False, REASON_CREDENTIALS, "base URL must not embed username/password", raw)
    if parsed.hostname != DEMO_HOST:
        return GuardResult(False, REASON_HOST, f"host must be {DEMO_HOST}, got {parsed.hostname!r}", raw)
    if parsed.port not in (None, 443):
        return GuardResult(False, REASON_PORT, f"only 443/default port allowed, got {parsed.port!r}", raw)
    if parsed.path not in ("", "/"):
        return GuardResult(False, REASON_PATH, f"base URL must not contain a path, got {parsed.path!r}", raw)
    if parsed.query or parsed.fragment:
        return GuardResult(False, REASON_PATH, "base URL must not contain query/fragment", raw)

    return GuardResult(True, REASON_OK, "demo-only public read-only market guard passed", raw)
