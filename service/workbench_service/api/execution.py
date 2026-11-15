"""执行路由：P2 下单、平仓、保护修复、控制态、手动对账。

- ``POST /api/execution/order``：P2 = 校验 approval_token（HMAC/TTL/单次/plan hash）
  → 用实时事实复检 → 市价开仓 → 强制 SL/TP；
- 无 Demo 凭据时统一返回 503 ``E_DEMO_CREDENTIALS_MISSING``（不会静默改纸面）；
- ``POST /api/control`` 的 ``emergency`` 会先落库控制态，再尝试全平（尽力而为）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response

from ..execution.deps import get_services
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["execution"])


@router.post("/execution/lease/acquire", summary="acquire the cross-process execution lease")
def acquire_lease(
    request: Request,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).acquire_lease(payload)


@router.post("/execution/lease/renew", summary="renew the cross-process execution lease")
def renew_lease(
    request: Request,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).renew_lease(payload)


@router.post("/execution/lease/release", summary="release the cross-process execution lease")
def release_lease(
    request: Request,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).release_lease(payload)


@router.post("/execution/lease/revoke_run", summary="revoke a stale run lease by run_id")
def revoke_lease_run(
    request: Request,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).revoke_lease_run(payload)


@router.post("/execution/order", summary="P2: submit approved market entry with forced SL/TP")
def submit_order(
    request: Request,
    response: Response,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    trace_id = getattr(request.state, "trace_id", None)
    result = get_services(request.app).submit(payload, trace_id=trace_id)
    response.status_code = int(result.pop("http_status", 201))
    return result


@router.post("/execution/close", summary="market close / reduce a position")
def close(
    request: Request,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    trace_id = getattr(request.state, "trace_id", None)
    return get_services(request.app).operator_command("close", payload, trace_id=trace_id)


@router.post("/protection/repair", summary="ensure SL/TP coverage (place missing, then dedupe)")
def repair(
    request: Request,
    payload: dict[str, Any] | None = Body(default=None),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).operator_command("repair", payload or {})


@router.post("/control", summary="change control state (requires confirm + reason)")
def control(
    request: Request,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).operator_command("control", payload)


@router.get(
    "/execution/order/{client_order_id}",
    summary="query-only convergence by the original clientOrderId (never reposts)",
)
def query_order(
    request: Request,
    client_order_id: str,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).query_order(client_order_id)


@router.post(
    "/execution/orders/recover",
    summary="converge SUBMITTING/UNKNOWN/stale working orders by exchange facts only",
)
def recover_orders(
    request: Request,
    payload: dict[str, Any] | None = Body(default=None),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).operator_command(
        "recover_orders", payload or {}
    )


@router.post("/execution/reconcile", summary="read-only reconciliation: outbox/positions/protection")
def reconcile(
    request: Request,
    payload: dict[str, Any] | None = Body(default=None),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).operator_command("reconcile", payload or {})
