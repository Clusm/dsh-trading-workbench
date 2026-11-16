"""Authenticated strategy editing APIs; no execution or exchange side effects."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..security import require_auth
from ..strategies import StrategyStore

router = APIRouter(prefix="/api/strategies", tags=["strategies"], dependencies=[Depends(require_auth)])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(Body):
    name: str = Field(min_length=1, max_length=100)
    source_text: str = Field(default="", max_length=20_000)


class RenameBody(Body):
    name: str = Field(min_length=1, max_length=100)


class DraftBody(Body):
    revision: StrictInt = Field(ge=0)
    source_text: str = Field(min_length=1, max_length=20_000)
    rules: dict[str, Any] = Field(default_factory=dict)
    questions: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    answers: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[Any] = Field(default_factory=list, max_length=20)
    summary: str = Field(default="", max_length=5000)
    sources: dict[str, Any] = Field(default_factory=dict)


class MountBody(Body):
    revision: StrictInt = Field(ge=1)
    content_hash: str = Field(min_length=64, max_length=64)
    request_id: str = Field(min_length=1, max_length=128)
    confirmed: StrictBool


class AuthorizationBody(Body):
    enabled: StrictBool
    generation: StrictInt = Field(ge=0)


class ValidateBody(Body):
    strategy_id: str = Field(min_length=1, max_length=128)
    version: StrictInt = Field(ge=1)
    generation: StrictInt = Field(ge=0)
    purpose: str = "entry"


class ConfigBody(Body):
    mode: str
    revision: StrictInt = Field(ge=0)


def store(request: Request) -> StrategyStore:
    return request.app.state.strategy_store


@router.get("")
def list_strategies(request: Request):
    return {"strategies": store(request).list()}


@router.post("")
def create_strategy(body: CreateBody, request: Request):
    return {"strategy": store(request).create(body.name, body.source_text)}


@router.get("/authorized")
def authorized(request: Request):
    return {"strategies": store(request).list(authorized=True)}


@router.get("/config")
def read_config(request: Request):
    return {"config": store(request).config()}


@router.put("/config")
def write_config(body: ConfigBody, request: Request):
    return {"config": store(request).set_config(body.mode, body.revision)}


@router.post("/validate-authorization")
def validate_authorization(body: ValidateBody, request: Request):
    # Position ownership is trusted only when read by the execution service from
    # its ledger, never accepted as an arbitrary HTTP caller assertion.
    from ..strategies.validation import fail
    if body.purpose != "entry":
        fail("E_STRATEGY_POSITION_OWNERSHIP", "仓位管理授权需由执行服务核对真实归属", 409)
    return store(request).validate_authorization(body.strategy_id, body.version, body.generation)


@router.get("/{strategy_id}")
def read_strategy(strategy_id: str, request: Request):
    return {"strategy": store(request).get(strategy_id)}


@router.patch("/{strategy_id}")
def rename_strategy(strategy_id: str, body: RenameBody, request: Request):
    return {"strategy": store(request).rename(strategy_id, body.name)}


@router.put("/{strategy_id}/draft")
def save_draft(strategy_id: str, body: DraftBody, request: Request):
    return {"strategy": store(request).save_draft(strategy_id, body.model_dump())}


@router.post("/{strategy_id}/mount")
def mount(strategy_id: str, body: MountBody, request: Request):
    return store(request).mount(strategy_id, body.model_dump())


@router.post("/{strategy_id}/authorization")
def authorization(strategy_id: str, body: AuthorizationBody, request: Request):
    return {"strategy": store(request).authorize(strategy_id, enabled=body.enabled, generation=body.generation)}


@router.get("/{strategy_id}/versions/{version}")
def version(strategy_id: str, version: int, request: Request):
    return {"version": store(request).version(strategy_id, version)}
