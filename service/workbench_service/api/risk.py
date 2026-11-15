"""风控路由：``GET /api/risk/envelope`` 与 ``POST /api/risk/evaluate``（P1 计划闸）。

- ``evaluate`` 返回 ``approval_token``（HMAC + TTL + 单次 + plan hash 绑定）；
- 被拒时返回 422 + ``error.code``（``E_RISK_*``），``details`` 带完整规则明细；
- 请求体里的市场事实（mark/新鲜度/异常行情）由调用方提供，服务端事实优先，
  客户端不能借此放宽账户级限制。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request

from ..execution.deps import get_services
from ..security import require_auth

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/envelope", summary="risk limits, rule switches, control state, blockers")
def envelope(request: Request, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    return get_services(request.app).envelope()


@router.post("/evaluate", summary="P1 gate: evaluate plan and issue one-time approval_token")
def evaluate(
    request: Request,
    payload: dict[str, Any] = Body(...),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    trace_id = getattr(request.state, "trace_id", None)
    return get_services(request.app).evaluate(payload, trace_id=trace_id)
