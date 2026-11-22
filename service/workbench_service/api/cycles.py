"""Cycle/evidence routes (M1.3c): create, list, detail, evidence, finalize."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ..cycles.service import CycleService
from ..errors import ServiceError
from ..execution.deps import get_services
from ..security import require_auth
from . import health as health_api
from . import market as market_api

router = APIRouter(prefix="/api/cycles", tags=["cycles"])


def _ensure_ready(request: Request) -> None:
    """Creation is allowed only after demo guard + readiness pass."""

    guard = getattr(request.app.state, "guard", None)
    if guard is None or not guard.ok:
        raise ServiceError(403, "E_DEMO_GUARD", "demo guard is not OK")
    ready, reasons, _ = health_api._readiness(request)
    if not ready:
        raise ServiceError(
            503,
            "E_NOT_READY",
            "service is not ready for a new cycle",
            details={"reasons": reasons},
        )


def _service(request: Request) -> CycleService:
    service = getattr(request.app.state, "cycles", None)
    if service is None:
        service = CycleService(request.app.state.settings)
        request.app.state.cycles = service
    return service


@router.post("", summary="create one RUNNING cycle + md-evidence/1 + account/envelope snapshot")
def create_cycle(
    request: Request,
    payload: dict[str, Any] | None = Body(default=None),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    """创建一轮 RUNNING cycle。

    body 除 v0.2.1 的 {trigger, slot_id, symbol, prev_pick} 外，v3.1 P2 新增两个可选字段：

    * ``purpose``：``research`` / ``trigger`` / ``manual``（默认 manual）。同一个 purpose
      同时只允许 1 个 RUNNING，不同 purpose 可以并存（docs/19 R2）；
    * ``symbols``：证据生成范围（单币触发 cycle 传 ``[symbol]``，服务端只生成该标的
      的 deep 证据，满足 R14 ≤400s）。它不扩大可交易标的：标的仍必须在白名单内。
    """

    _ensure_ready(request)
    return _service(request).create(
        payload or {},
        market=market_api._market_service(request),
        execution=get_services(request.app),
    )


@router.get("", summary="list cycles (newest first, optional purpose filter)")
def list_cycles(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=64),
    purpose: str | None = Query(default=None, max_length=16),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).list_cycles(limit=limit, cursor=cursor, purpose=purpose)


@router.get("/{cycle_id}", summary="cycle detail with agents/ai runs/outputs")
def get_cycle(
    request: Request,
    cycle_id: str,
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).get_cycle(cycle_id)


@router.get("/{cycle_id}/evidence", summary="read persisted md-evidence/1 (hash verified)")
def get_cycle_evidence(
    request: Request,
    cycle_id: str,
    agent: str | None = Query(default=None, max_length=20),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).evidence(cycle_id, agent=agent)


@router.post("/{cycle_id}/finalize", summary="RUNNING -> terminal (idempotent)")
def finalize_cycle(
    request: Request,
    cycle_id: str,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).finalize(cycle_id, payload)
