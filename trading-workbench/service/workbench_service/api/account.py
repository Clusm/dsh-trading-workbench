"""只读账户/仓位/订单/成交/权益曲线路由（``/api/status`` 等）。

约束：
- 全部 GET，``Depends(require_auth)``（M1.1 默认 Bearer）；
- 交易所优先、账本兜底，并在响应里显式标注 ``source``/``stale``；
- 永不下单；缺少 Demo 凭据时返回 ``credentials_configured=false`` 而不是报错。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..execution.deps import get_services
from ..security import require_auth

router = APIRouter(prefix="/api", tags=["account"])

_RANGE_SECONDS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "1d": 24 * 3600,
    "7d": 7 * 24 * 3600,
}
_BUCKET_SECONDS = {"30s": 30, "1m": 60, "5m": 300, "15m": 900, "1h": 3600}


def _range_seconds(value: str | None) -> int:
    return _RANGE_SECONDS.get((value or "1d").lower(), 24 * 3600)


def _bucket_seconds(value: str | None) -> int:
    return _BUCKET_SECONDS.get((value or "1m").lower(), 60)


@router.get("/status", summary="account/equity/positions/protection/outbox/control (demo)")
def status(request: Request, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    return get_services(request.app).status()


@router.get("/positions", summary="current positions with protection status")
def positions(request: Request, _auth: None = Depends(require_auth)) -> dict[str, Any]:
    return get_services(request.app).positions_payload()


@router.get("/orders", summary="order audit mirror")
def orders(
    request: Request,
    status: str | None = Query(default=None, description="filter by order status"),
    limit: int = Query(default=50, ge=1, le=500),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).orders_payload(status=status, limit=limit)


@router.get("/operations", summary="durable operator command history")
def operations(request: Request, limit: int = Query(default=100, ge=1, le=500),
               _auth: None = Depends(require_auth)) -> dict[str, Any]:
    service = get_services(request.app)
    return {"demo_only": True, "operations": service.ledger.operations(limit), "source": "local_ledger"}


@router.get("/fills", summary="fills from userTrades")
def fills(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).fills_payload(limit=limit)


@router.post("/sync-income", summary="拉取交易所 income 流水（资金费/划转）落库")
def sync_income(
    request: Request,
    range: str = Query(default="7d", description="1d|7d"),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    days = 7 if (range or "7d").lower() == "7d" else 1
    return get_services(request.app).sync_capital_flows(range_s=days * 24 * 3600)


@router.get("/pnl", summary="资金口径：四个权益口径 + 策略四本账 + 对账 (read-only)")
def pnl(
    request: Request,
    range: str = Query(default="1d", description="1h|6h|24h|1d|7d"),  # noqa: A002 - API 名称
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).pnl_payload(range_s=_range_seconds(range))


@router.get("/equity-curve", summary="equity snapshots (30s ledger, downsampled)")
def equity_curve(
    request: Request,
    range: str = Query(default="1d", description="1h|6h|24h|1d|7d"),  # noqa: A002 - API 名称
    bucket: str = Query(default="1m", description="30s|1m|5m|15m|1h"),
    _auth: None = Depends(require_auth),
) -> dict[str, Any]:
    return get_services(request.app).equity_curve_payload(
        range_s=_range_seconds(range), bucket_s=_bucket_seconds(bucket)
    )
