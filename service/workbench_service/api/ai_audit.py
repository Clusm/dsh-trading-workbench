"""Minimal AI audit routes (M1.3c): runs, per-attempt calls, events, artifacts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from ..cycles.service import CycleService
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["ai-audit"])


def _service(request: Request) -> CycleService:
    service = getattr(request.app.state, "cycles", None)
    if service is None:
        service = CycleService(request.app.state.settings)
        request.app.state.cycles = service
    return service


@router.post("/cycles/{cycle_id}/ai-runs", summary="create/open an AI run for a cycle")
def create_ai_run(
    request: Request,
    cycle_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).create_ai_run(cycle_id, payload or {})


@router.put("/ai-runs/{ai_run_id}/calls/{call_id}", summary="idempotent per-attempt AI call audit row")
def put_ai_call(
    request: Request,
    ai_run_id: str,
    call_id: str,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).put_call(ai_run_id, call_id, payload)


@router.post("/ai-runs/{ai_run_id}/events", summary="append an AI state transition event")
def post_ai_event(
    request: Request,
    ai_run_id: str,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).append_event(ai_run_id, payload)


@router.put("/cycles/{cycle_id}/artifacts/{agent}", summary="idempotent agent output + projection")
def put_artifact(
    request: Request,
    cycle_id: str,
    agent: str,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return _service(request).put_artifact(cycle_id, agent, payload)
