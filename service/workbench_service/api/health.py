"""存活/就绪端点：``/health``（免鉴权）与 ``/api/ready``。

- ``/health`` 不查询任何上游，只回答“进程在不在、库可读不可读、服务版本是否匹配”；
- ``/api/ready`` 回答“是否允许开新仓”，原因码进 ``reasons[]``（docs/01 §10.3）。
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, Request

from ..config import SERVICE_NAME, read_service_lock_status
from ..db import db_status, utc_now_iso
from ..execution.deps import get_services
from ..guard import GuardResult
from ..security import auth_status, require_auth

router = APIRouter(tags=["health"])


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def _readiness(request: Request) -> tuple[bool, list[str], dict[str, object]]:
    state = request.app.state
    settings = state.settings
    guard: GuardResult = state.guard
    db = db_status(settings.data_dir)

    reasons: list[str] = []
    if not guard.ok:
        reasons.append(guard.reason_code)
    if not db.ok:
        reasons.append("E_DB_UNAVAILABLE")
    elif db.schema_version != settings.schema_version:
        reasons.append("E_DB_SCHEMA_MISMATCH")
    if state.phase == "starting":
        reasons.append("E_SERVICE_STARTING")
    elif state.phase != "ready":
        reasons.append("E_SERVICE_DEGRADED")

    detail = {
        "phase": state.phase,
        "db": db.as_dict(),
        "guard": guard.as_dict(),
        "schema_expected": settings.schema_version,
    }
    return (not reasons), reasons, detail


def _execution_net(request: Request) -> dict[str, object]:
    """P0②（docs/19 §4）：出口代理 + 451 地区限制退避的只读状态。

    只读本地状态：不查上游、不改任何状态；没有 services（只读启动失败/未装配）时
    降级为 None，绝不让 /health 因为取不到执行层状态而失败。
    三个值都不含凭据：egress_proxy 只有 host:port 或 none。
    """

    settings = request.app.state.settings
    # egress_proxy 以启动配置为准（同一个值），只有配置对象缺这个属性时才回落到服务端。
    label = getattr(settings, "egress_proxy_label", None)
    net: dict[str, object] = {
        "venue_geo_blocked": None,
        "geo_retry_at_ms": None,
        "egress_proxy": label if label is not None else "none",
    }
    services = getattr(request.app.state, "services", None)
    reader = getattr(services, "health_flags", None)
    if not callable(reader):
        return net
    try:
        flags = reader()
    except Exception:  # noqa: BLE001 - /health 不因执行层异常而失败
        return net
    if isinstance(flags, dict):
        net["venue_geo_blocked"] = flags.get("venue_geo_blocked")
        net["geo_retry_at_ms"] = flags.get("geo_retry_at_ms")
        if label is None and flags.get("egress_proxy"):
            net["egress_proxy"] = flags["egress_proxy"]
    return net


def _identity(request: Request) -> dict[str, object]:
    """§4.2：把“这个进程是哪个环境、哪个账本、哪组凭据”一次说清。

    注意 `account_verified` 恒为 False 的诚实含义：key 指纹只能证明用的是同一把
    凭据，不能证明是同一个交易账户（§8·F 明确禁止编造绿色勾选）。
    """

    settings = request.app.state.settings
    services = getattr(request.app.state, "services", None)
    ledger = getattr(services, "ledger", None)
    identity: dict[str, object] = {
        "venue": settings.venue,
        "venue_host": settings.venue_host,
        "data_dir": str(settings.data_dir),
        "ledger_id": None,
        "account_fingerprint": None,
        "account_fingerprint_source": None,
        "account_verified": False,
        "account_verified_note": "key fingerprint identifies the credential, not the account",
    }
    if ledger is None:
        return identity
    try:
        identity["ledger_id"] = ledger.ledger_id
        identity["account_fingerprint"] = ledger.get_meta("account_fingerprint")
        identity["account_fingerprint_source"] = ledger.get_meta("account_fingerprint_source")
    except Exception:  # noqa: BLE001 - 身份读取失败不应让 /health 500
        return identity
    return identity


@router.get("/health", summary="liveness / version / db / ready")
def health(request: Request) -> dict[str, object]:
    state = request.app.state
    settings = state.settings
    db = db_status(settings.data_dir)
    ready, reasons, _ = _readiness(request)
    uptime = round(max(0.0, time.monotonic() - state.started_monotonic), 3)

    return {
        "service": SERVICE_NAME,
        "ok": True,
        "capabilities": {"strategies": getattr(state, "strategy_store", None) is not None},
        "service_version": settings.service_version,
        "schema_version": settings.schema_version,
        "phase": state.phase,
        "ready": ready,
        "reasons": reasons,
        "uptime_s": uptime,
        "started_at": state.started_at_iso,
        "now": utc_now_iso(),
        "pid": os.getpid(),
        "venue": settings.venue,
        "identity": _identity(request),
        "host": settings.host,
        "port": settings.port,
        "db": db.as_dict(),
        "guard": state.guard.as_dict(),
        "auth": auth_status(request),
        "lock": read_service_lock_status(settings.data_dir),
        "supervisor": getattr(state, "supervisor", None),
        # P0②：出口代理 / 地区限制退避（三个键，均不含凭据）。
        **_execution_net(request),
        "last_probe": state.last_probe,
        "trace_id": _trace_id(request),
    }


@router.get("/api/ready", summary="new-entry readiness + demo guard reason codes")
def ready(request: Request, _auth: None = Depends(require_auth)) -> dict[str, object]:
    state = request.app.state
    settings = state.settings
    service_ready, service_reasons, detail = _readiness(request)
    execution_reasons: list[str] = []
    execution_readiness: dict[str, object] = {}
    status: dict[str, object] = {}
    try:
        status = get_services(request.app).status()
        execution_readiness = status.get("readiness") or {}
        execution_reasons = [
            str(reason) for reason in (execution_readiness.get("reasons") or [])
        ]
    except Exception as exc:  # noqa: BLE001 - readiness must stay available
        execution_reasons = ["E_EXECUTION_UNAVAILABLE"]
        execution_readiness = {
            "new_entry_allowed": False,
            "reasons": execution_reasons,
            "error": type(exc).__name__,
        }
    new_entry_allowed = bool(service_ready and execution_readiness.get("new_entry_allowed"))
    reasons = list(dict.fromkeys([*service_reasons, *execution_reasons]))
    control = status.get("control")
    return {
        "ready": new_entry_allowed,
        "capabilities": {"strategies": getattr(state, "strategy_store", None) is not None},
        "service_ready": service_ready,
        "new_entry_allowed": new_entry_allowed,
        "reasons": reasons,
        "control_state": str(control.get("state") or "normal")
        if isinstance(control, dict)
        else "normal",
        "credentials_configured": bool(status.get("credentials_configured")),
        "execution_readiness": execution_readiness,
        "phase": detail["phase"],
        "guard": detail["guard"],
        "db": detail["db"],
        "schema_expected": settings.schema_version,
        "checked_at": utc_now_iso(),
        "trace_id": _trace_id(request),
    }
