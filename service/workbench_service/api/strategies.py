"""Authenticated strategy editing, candidate evaluation, preview & attribution APIs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body as FastAPIBody, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..errors import ServiceError
from ..market.config import WHITELIST
from ..market.transport import MarketUpstreamError
from ..security import require_auth
from ..strategies import StrategyStore
from ..strategies.attribution import check_and_trip_circuits, list_attribution
from ..strategies.evaluator import (
    describe_rules,
    entry_uses_funding,
    entry_uses_klines,
    evaluate_entry,
    recommended_fetch_bars,
)
from .market import _market_service

router = APIRouter(prefix="/api/strategies", tags=["strategies"], dependencies=[Depends(require_auth)])
_WHITELIST_SET = frozenset(WHITELIST)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateBody(Body):
    name: str = Field(min_length=1, max_length=100)
    source_text: str = Field(default="", max_length=20_000)
    rules: dict[str, Any] = Field(default_factory=dict)


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
    reset_circuit: StrictBool | None = None


class ValidateBody(Body):
    strategy_id: str = Field(min_length=1, max_length=128)
    version: StrictInt = Field(ge=1)
    generation: StrictInt = Field(ge=0)
    purpose: str = "entry"


class ConfigBody(Body):
    mode: str
    revision: StrictInt = Field(ge=0)


class EvaluateCandidatesBody(Body):
    now_ms: StrictInt | None = Field(default=None, ge=0)


class DecisionBody(Body):
    cycle_id: str = Field(min_length=1, max_length=128)
    version: StrictInt = Field(ge=1)
    symbol: str = Field(min_length=2, max_length=20)
    side: str = Field(min_length=1, max_length=16)
    signal_key: str = Field(min_length=1, max_length=128)
    execution_mode: str = Field(default="ai_assisted", max_length=32)
    outcome: str = Field(min_length=1, max_length=64)
    reason_code: str | None = Field(default=None, max_length=64)
    entry_mark: float | int | str | None = None
    sl_price: float | int | str | None = None
    tp_price: float | int | str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def store(request: Request) -> StrategyStore:
    s = getattr(request.app.state, "strategy_store", None)
    if s is None:
        raise ServiceError(503, "E_DB_UNAVAILABLE", "strategy store unavailable")
    return s


def _resolve_market(request: Request):
    explicit = getattr(request.app.state, "market", None)
    if explicit is not None:
        return explicit
    services = getattr(request.app.state, "services", None)
    svc_market = getattr(services, "market_service", None) if services is not None else None
    if svc_market is not None:
        return svc_market
    return _market_service(request)


def _fetch_marks(ms: Any, symbols: list[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    if not symbols:
        return {}, True
    prices: dict[str, dict[str, Any]] = {}
    any_ok = False
    for index in range(0, len(symbols), 10):
        chunk = symbols[index : index + 10]
        try:
            snap = ms.mark_snapshot(chunk)
            chunk_prices = snap.get("prices") or {}
            for sym, quote in chunk_prices.items():
                prices[sym] = dict(quote)
                if quote.get("mark") is not None and not quote.get("stale"):
                    any_ok = True
        except Exception:  # noqa: BLE001
            for sym in chunk:
                prices[sym] = {
                    "mark": None,
                    "age_ms": None,
                    "stale": True,
                    "source": "unavailable",
                }
    return prices, any_ok


def _load_bars_cached_or_live(
    ms: Any,
    symbol: str,
    timeframe: str,
    needed_limit: int,
    now_ms: int,
    cache: dict[tuple[str, str], tuple[Any, ...]],
) -> tuple[Any, ...]:
    key = (symbol, timeframe)
    if key in cache:
        return cache[key]
    klines_store = getattr(ms, "klines", None)
    if klines_store is None:
        cache[key] = ()
        return ()
    try:
        res = klines_store.get_with_fallback(symbol, timeframe, needed_limit, now_ms=now_ms)
        bars = tuple(res.bars)
    except (MarketUpstreamError, Exception):  # noqa: BLE001
        bars = tuple(klines_store.cached_bars(symbol, timeframe))
    cache[key] = bars
    return bars


@router.get("")
def list_strategies(request: Request):
    s = store(request)
    check_and_trip_circuits(s)
    return {"strategies": s.list()}


@router.post("")
def create_strategy(body: CreateBody, request: Request):
    return {"strategy": store(request).create(body.name, body.source_text, rules=body.rules)}


@router.get("/authorized")
def authorized(request: Request):
    s = store(request)
    check_and_trip_circuits(s)
    return {"strategies": s.list(authorized=True)}


@router.get("/config")
def read_config(request: Request):
    return {"config": store(request).config(), "supported_symbols": list(WHITELIST)}


@router.put("/config")
def write_config(body: ConfigBody, request: Request):
    return {"config": store(request).set_config(body.mode, body.revision)}


@router.post("/validate-authorization")
def validate_authorization(body: ValidateBody, request: Request):
    from ..strategies.validation import fail

    if body.purpose != "entry":
        fail("E_STRATEGY_POSITION_OWNERSHIP", "仓位管理授权需由执行服务核对真实归属", 409)
    s = store(request)
    check_and_trip_circuits(s)
    return s.validate_authorization(body.strategy_id, body.version, body.generation)


@router.post("/evaluate-candidates")
def evaluate_candidates(
    request: Request,
    body: EvaluateCandidatesBody | None = FastAPIBody(default=None),
):
    s = store(request)
    check_and_trip_circuits(s)
    config = s.config()
    ms = _resolve_market(request)
    now_ms = int(body.now_ms) if (body is not None and body.now_ms is not None) else int(ms.now_ms())

    enabled = s.list(authorized=True)
    if not enabled:
        return {
            "mode": config["mode"],
            "mode_revision": config["revision"],
            "evaluated_at_ms": now_ms,
            "candidates": [],
            "skipped": [],
            "prices": {},
        }

    wanted_symbols = sorted(
        {
            sym
            for strat in enabled
            for sym in ((strat.get("confirmed_version") or {}).get("rules") or {}).get("symbols", [])
            if isinstance(sym, str) and sym in _WHITELIST_SET
        }
    )
    prices_map, _ = _fetch_marks(ms, wanted_symbols)

    need_funding = [
        sym
        for strat in enabled
        if entry_uses_funding(((strat.get("confirmed_version") or {}).get("rules") or {}).get("entry"))
        for sym in ((strat.get("confirmed_version") or {}).get("rules") or {}).get("symbols", [])
        if sym in prices_map and prices_map[sym].get("funding_rate") is None
    ]
    if need_funding:
        try:
            snap = ms.price_snapshot(sorted(set(need_funding))[:10])
            for sym, row in (snap or {}).items():
                if sym in prices_map and row.get("funding_rate") is not None:
                    prices_map[sym]["funding_rate"] = row["funding_rate"]
        except Exception:  # noqa: BLE001
            pass

    bars_cache: dict[tuple[str, str], tuple[Any, ...]] = {}
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with s.transaction() as conn:
        for strat in enabled:
            confirmed = strat.get("confirmed_version")
            if not confirmed or not confirmed.get("validation", {}).get("execution_ready"):
                continue
            rules = confirmed["rules"]
            entry = rules["entry"]
            ver = int(strat["current_version"])
            side_lower = str(entry["side"]).lower()
            timeframe = rules["timeframes"][0]
            uses_klines = entry_uses_klines(entry)
            needed_bars = recommended_fetch_bars(entry, timeframe) if uses_klines else 0

            for sym in rules["symbols"]:
                quote = prices_map.get(sym) or {"mark": None, "stale": True}
                bars = (
                    _load_bars_cached_or_live(ms, sym, timeframe, needed_bars, now_ms, bars_cache)
                    if uses_klines
                    else None
                )
                res = evaluate_entry(rules, sym, quote, bars=bars, now_ms=now_ms)
                if not res["matched"]:
                    skipped.append(
                        {
                            "strategy_id": strat["strategy_id"],
                            "name": strat["name"],
                            "version": ver,
                            "symbol": sym,
                            "side": side_lower,
                            "signal_key": res["signal_key"],
                            "reason_code": res["reason_code"],
                            "detail": res["detail"],
                            "mark": res["mark"],
                            "distance_pct": res["distance_pct"],
                            "indicator_facts": res["facts"],
                        }
                    )
                    continue

                if s.is_signal_consumed(
                    conn, strat["strategy_id"], ver, sym, side_lower, res["signal_key"]
                ):
                    skipped.append(
                        {
                            "strategy_id": strat["strategy_id"],
                            "name": strat["name"],
                            "version": ver,
                            "symbol": sym,
                            "side": side_lower,
                            "signal_key": res["signal_key"],
                            "reason_code": "ALREADY_SIGNALED",
                            "detail": "该周期信号已触发过",
                            "mark": res["mark"],
                            "distance_pct": res["distance_pct"],
                            "indicator_facts": res["facts"],
                        }
                    )
                    continue

                candidates.append(
                    {
                        "strategy_id": strat["strategy_id"],
                        "name": strat["name"],
                        "current_version": ver,
                        "version": ver,
                        "generation": strat["generation"],
                        "execution_mode": rules.get("execution_mode", "ai_assisted"),
                        "symbol": sym,
                        "side": side_lower,
                        "signal_key": res["signal_key"],
                        "mark": res["mark"],
                        "rules": rules,
                        "confirmed_version": confirmed,
                        "indicator_facts": res["facts"],
                    }
                )

    return {
        "mode": config["mode"],
        "mode_revision": config["revision"],
        "evaluated_at_ms": now_ms,
        "candidates": candidates,
        "skipped": skipped,
        "prices": prices_map,
    }


@router.get("/attribution")
def strategy_attribution(request: Request):
    s = store(request)
    ms = getattr(request.app.state, "market", None)
    if ms is None:
        try:
            ms = _resolve_market(request)
        except Exception:  # noqa: BLE001
            ms = None
    return {"strategies": list_attribution(s, market_service=ms)}


@router.get("/{strategy_id}")
def read_strategy(strategy_id: str, request: Request):
    s = store(request)
    check_and_trip_circuits(s)
    return {"strategy": s.get(strategy_id)}


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
    return {
        "strategy": store(request).authorize(
            strategy_id, enabled=body.enabled, generation=body.generation
        )
    }


@router.get("/{strategy_id}/versions/{version}")
def version(strategy_id: str, version: int, request: Request):
    return {"version": store(request).version(strategy_id, version)}


@router.get("/{strategy_id}/preview")
def preview_strategy(strategy_id: str, request: Request):
    s = store(request)
    strat = s.get(strategy_id)
    draft = strat.get("draft")
    confirmed = strat.get("confirmed_version")

    if draft and draft.get("validation", {}).get("execution_ready"):
        rules = draft.get("rules") or {}
        execution_ready = True
        version_num = strat.get("current_version")
    elif confirmed and confirmed.get("validation", {}).get("execution_ready"):
        rules = confirmed.get("rules") or {}
        execution_ready = True
        version_num = confirmed.get("version")
    elif draft and draft.get("rules"):
        rules = draft.get("rules") or {}
        execution_ready = False
        version_num = strat.get("current_version")
    elif confirmed:
        rules = confirmed.get("rules") or {}
        execution_ready = False
        version_num = confirmed.get("version")
    else:
        rules = {}
        execution_ready = False
        version_num = None

    summary = describe_rules(rules)
    raw_symbols = rules.get("symbols") if isinstance(rules.get("symbols"), list) else []
    valid_symbols = [sym for sym in raw_symbols if isinstance(sym, str) and sym in _WHITELIST_SET]
    if not valid_symbols:
        return {
            "strategy_id": strategy_id,
            "summary": summary,
            "execution_ready": execution_ready,
            "triggered_now": False,
            "market_available": False,
            "symbols": [],
        }

    try:
        ms = _resolve_market(request)
        now_ms = int(ms.now_ms())
        prices_map, market_available = _fetch_marks(ms, valid_symbols)
    except Exception:  # noqa: BLE001
        ms = None
        now_ms = 0
        prices_map = {}
        market_available = False

    if market_available and execution_ready and entry_uses_funding(rules.get("entry")):
        missing_f = [sym for sym in valid_symbols if prices_map.get(sym, {}).get("funding_rate") is None]
        if missing_f and ms is not None:
            try:
                snap = ms.price_snapshot(missing_f[:10])
                for sym, row in (snap or {}).items():
                    if sym in prices_map and row.get("funding_rate") is not None:
                        prices_map[sym]["funding_rate"] = row["funding_rate"]
            except Exception:  # noqa: BLE001
                pass

    bars_cache: dict[tuple[str, str], tuple[Any, ...]] = {}
    uses_klines = execution_ready and entry_uses_klines(rules.get("entry"))
    frames = rules.get("timeframes") or ["15m"]
    timeframe = str(frames[0]) if isinstance(frames, list) and frames else "15m"
    needed_bars = recommended_fetch_bars(rules.get("entry"), timeframe) if uses_klines else 0

    symbol_rows: list[dict[str, Any]] = []
    with s.transaction() as conn:
        for sym in valid_symbols:
            quote = prices_map.get(sym) or {}
            mark_val = quote.get("mark")
            if not market_available:
                symbol_rows.append(
                    {
                        "symbol": sym,
                        "mark": mark_val,
                        "distance_pct": None,
                        "matched": False,
                        "already_signaled": False,
                        "detail": "行情暂不可用",
                        "facts": {},
                    }
                )
                continue
            if not execution_ready:
                symbol_rows.append(
                    {
                        "symbol": sym,
                        "mark": mark_val,
                        "distance_pct": None,
                        "matched": False,
                        "already_signaled": False,
                        "detail": "策略规则尚未具备自动执行条件",
                        "facts": {"mark": mark_val} if mark_val is not None else {},
                    }
                )
                continue

            bars = (
                _load_bars_cached_or_live(ms, sym, timeframe, needed_bars, now_ms, bars_cache)
                if (uses_klines and ms is not None)
                else None
            )
            res = evaluate_entry(rules, sym, quote, bars=bars, now_ms=now_ms)
            if res["reason_code"] in ("KLINE_STALE", "INSUFFICIENT_BARS", "FUNDING_UNAVAILABLE") and not bars:
                # Klines/funding offline while mark succeeded: keep graceful degraded response
                pass
            already = False
            if version_num is not None and isinstance(rules.get("entry"), dict):
                already = s.is_signal_consumed(
                    conn,
                    strategy_id,
                    int(version_num),
                    sym,
                    str(rules["entry"].get("side") or "long"),
                    res["signal_key"],
                )
            symbol_rows.append(
                {
                    "symbol": sym,
                    "mark": res["mark"],
                    "distance_pct": res["distance_pct"],
                    "matched": bool(res["matched"]),
                    "already_signaled": already,
                    "detail": res["detail"],
                    "facts": res["facts"],
                }
            )

    triggered_now = any(row["matched"] for row in symbol_rows)
    return {
        "strategy_id": strategy_id,
        "summary": summary,
        "execution_ready": execution_ready,
        "triggered_now": triggered_now,
        "market_available": market_available,
        "symbols": symbol_rows,
    }


@router.post("/{strategy_id}/decisions")
def record_strategy_decision(strategy_id: str, body: DecisionBody, request: Request):
    return {"decision": store(request).record_decision(strategy_id, body.model_dump())}
