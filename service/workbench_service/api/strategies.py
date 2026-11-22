"""Authenticated strategy editing, candidate evaluation, preview & attribution APIs."""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

from fastapi import APIRouter, Body as FastAPIBody, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..errors import ServiceError
from ..market.config import WHITELIST
from ..market.transport import MarketUpstreamError
from ..security import require_auth
from ..strategies import StrategyStore
from ..strategies import scan as rule_scan
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
#: v3.1 P2：确定性规则扫描 / 派发回写路由（docs/18 §5.1）。
#:
#: 独立于 strategies router 的 prefix，由 ``app.py`` 显式装配：
#: ``app.include_router(strategies_api.rules_router)``。这样 /api/rules 的鉴权、
#: tags 与 OpenAPI 路径都由它自己决定，不依赖 strategies 路由的登记方式。
rules_router = APIRouter(
    prefix="/api/rules", tags=["rules"], dependencies=[Depends(require_auth)]
)
_WHITELIST_SET = frozenset(WHITELIST)
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")


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


class ScanBody(BaseModel):
    """``POST /api/rules/scan`` 的请求体；字段全为可选（Host 每 tick 只传变化项）。

    ``extra="ignore"`` 是刻意的：Host 会随时间追加诊断字段（例如 dispatches 的
    ``reason_code``），服务端不应该因为一个未知键就 422。
    """

    model_config = ConfigDict(extra="ignore")

    now_ms: StrictInt | None = Field(default=None, ge=0)
    scope: str = Field(default=rule_scan.DEFAULT_SCOPE, max_length=16)
    scan_interval_ms: StrictInt | None = Field(default=None, ge=0)


class DispatchBody(BaseModel):
    """``POST /api/rules/dispatched`` 的请求体（R5② 派发回写）。"""

    model_config = ConfigDict(extra="ignore")

    rule_key: str = Field(min_length=1, max_length=200)
    symbol: str = Field(min_length=2, max_length=20)
    outcome: str = Field(min_length=1, max_length=32)
    at_ms: StrictInt | None = Field(default=None, ge=0)
    scan_interval_ms: StrictInt | None = Field(default=None, ge=0)


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


def _strategy_priority(rules: Mapping[str, Any]) -> float:
    """确定性排序用的 priority（docs/18 §6.3）；非法值按 0 处理。"""

    try:
        value = float(rules.get("priority", 0))
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _scan_facts(rules: Mapping[str, Any]) -> tuple[str, bool, int]:
    """(timeframe, uses_klines, needed_bars) —— K 线类规则沿用现有 bars 加载逻辑。"""

    frames = rules.get("timeframes") or ["15m"]
    timeframe = str(frames[0]) if frames else "15m"
    entry = rules.get("entry")
    uses_klines = entry_uses_klines(entry)
    needed_bars = recommended_fetch_bars(entry, timeframe) if uses_klines else 0
    return timeframe, uses_klines, needed_bars


def _run_scan(request: Request, body: ScanBody) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """执行一次确定性边沿扫描，返回 (响应体, 逐条求值明细)。

    ``/api/rules/scan`` 与兼容别名 ``/api/strategies/evaluate-candidates`` 共用这一份实现：
    先在事务外取行情 / K 线，再在一个写事务里读 ``rule_eval_state``、查周期信号是否已
    消费、计划本轮触发并写回状态。扫描没有下单 / 派发副作用；也不做熔断巡检——那是
    界面查询与 P1/P2 的职责，15s 一次的扫描不应该反复算归因。
    """

    s = store(request)
    ms = _resolve_market(request)
    now_ms = int(body.now_ms) if body.now_ms is not None else int(ms.now_ms())
    scope = rule_scan.normalize_scope(body.scope)
    if not scope:
        raise ServiceError(
            422,
            "E_VALIDATION",
            "scope 必须为 " + "/".join(rule_scan.SCAN_SCOPES) + " 之一",
            details={"scope": str(body.scope)},
        )
    interval = rule_scan.clamp_interval_ms(body.scan_interval_ms)
    config = s.config()

    # 1) 规则筛选：只有已启用、已就绪、标的白名单内的用户策略参与扫描。
    skipped: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for row in s.authorized_rules():
        sid, name = row["strategy_id"], row["name"]
        confirmed = row.get("confirmed_version")
        if not confirmed:
            skipped.append(
                {
                    "strategy_id": sid,
                    "name": name,
                    "reason_code": "NOT_MOUNTED",
                    "detail": "策略尚未确认挂载版本",
                }
            )
            continue
        validation = confirmed.get("validation") or {}
        if not validation.get("execution_ready"):
            reasons = list(validation.get("issues") or []) + list(validation.get("unsupported") or [])
            skipped.append(
                {
                    "strategy_id": sid,
                    "name": name,
                    "reason_code": "NOT_READY",
                    "detail": "；".join(str(item) for item in reasons) or "策略规则尚不具备自动执行条件",
                }
            )
            continue
        rules = confirmed.get("rules") or {}
        entry = rules.get("entry")
        if not rule_scan.scope_allows(scope, entry):
            skipped.append(
                {
                    "strategy_id": sid,
                    "name": name,
                    "reason_code": "SCOPE_FILTERED",
                    "detail": f"本轮 scope={scope} 不覆盖 {rule_scan.rule_scope(entry)} 类规则",
                    "scope": rule_scan.rule_scope(entry),
                }
            )
            continue
        symbols: list[str] = []
        for raw_symbol in rules.get("symbols") or []:
            if not isinstance(raw_symbol, str):
                continue
            sym = raw_symbol.strip().upper()
            if sym not in _WHITELIST_SET:
                skipped.append(
                    {
                        "strategy_id": sid,
                        "name": name,
                        "symbol": sym,
                        "reason_code": "SYMBOL_NOT_ALLOWED",
                        "detail": "标的不在 20 币白名单内",
                    }
                )
                continue
            if sym not in symbols:
                symbols.append(sym)
        if not symbols:
            skipped.append(
                {
                    "strategy_id": sid,
                    "name": name,
                    "reason_code": "NO_SYMBOL",
                    "detail": "没有可扫描的白名单标的",
                }
            )
            continue
        targets.append(
            {
                "row": row,
                "confirmed": confirmed,
                "rules": rules,
                "symbols": symbols,
                "stale_after_ms": rule_scan.stale_after_ms(interval, rules),
            }
        )

    # 2) 行情 / K 线：事务外取好；取不到就按"数据不可用"处理（不推进边沿状态）。
    prices_map: dict[str, dict[str, Any]] = {}
    if targets:
        wanted_symbols = sorted({sym for target in targets for sym in target["symbols"]})
        prices_map, _ = _fetch_marks(ms, wanted_symbols)
        need_funding = sorted(
            {
                sym
                for target in targets
                if entry_uses_funding(target["rules"].get("entry"))
                for sym in target["symbols"]
                if sym in prices_map and prices_map[sym].get("funding_rate") is None
            }
        )
        if need_funding:
            try:
                snap = ms.price_snapshot(need_funding[:10])
                for sym, row in (snap or {}).items():
                    if sym in prices_map and row.get("funding_rate") is not None:
                        prices_map[sym]["funding_rate"] = row["funding_rate"]
            except Exception:  # noqa: BLE001
                pass

    bars_cache: dict[tuple[str, str], tuple[Any, ...]] = {}
    evaluations: list[dict[str, Any]] = []
    for target in targets:
        rules = target["rules"]
        timeframe, uses_klines, needed_bars = _scan_facts(rules)
        for sym in target["symbols"]:
            quote = prices_map.get(sym) or {"mark": None, "stale": True}
            bars = (
                _load_bars_cached_or_live(ms, sym, timeframe, needed_bars, now_ms, bars_cache)
                if uses_klines
                else None
            )
            evaluations.append(
                {
                    "target": target,
                    "symbol": sym,
                    "result": evaluate_entry(rules, sym, quote, bars=bars, now_ms=now_ms),
                }
            )

    # 3) 一个写事务：读状态 → 查已消费信号 → 计划触发 → 写回状态（幂等）。
    with s.transaction(write=True) as conn:
        states = rule_scan.load_states(conn)
        inputs: list[rule_scan.ScanInput] = []
        for item in evaluations:
            target, sym, result = item["target"], item["symbol"], item["result"]
            row, confirmed, rules = target["row"], target["confirmed"], target["rules"]
            version = int(confirmed.get("version") or row["current_version"])
            entry = rules.get("entry") if isinstance(rules.get("entry"), dict) else {}
            side = str(entry.get("side") or "long").lower()
            consumed = s.is_signal_consumed(
                conn, row["strategy_id"], version, sym, side, result["signal_key"]
            )
            item["version"] = version
            item["side"] = side
            item["consumed"] = consumed
            inputs.append(
                rule_scan.ScanInput(
                    rule_key=rule_scan.rule_key_for(row["strategy_id"], version),
                    strategy_id=row["strategy_id"],
                    name=row["name"],
                    version=version,
                    generation=int(row["generation"]),
                    mode_revision=int(config["revision"]),
                    execution_mode=str(rules.get("execution_mode") or "ai_assisted"),
                    symbol=sym,
                    side=side,
                    priority=_strategy_priority(rules),
                    created_at=str(row.get("created_at") or ""),
                    matched=bool(result["matched"]),
                    reason_code=str(result["reason_code"]),
                    detail=str(result["detail"]),
                    signal_key=str(result["signal_key"]),
                    mark=result["mark"],
                    distance_pct=result["distance_pct"],
                    facts=result.get("facts") or {},
                    consumed=consumed,
                    stale_after_ms=int(target["stale_after_ms"]),
                )
            )
        plan = rule_scan.plan_scan(inputs, states, now_ms=now_ms)
        rule_scan.write_states(conn, plan.states)

    payload = {
        "mode": config["mode"],
        "mode_revision": int(config["revision"]),
        "scope": scope,
        "scan_interval_ms": interval,
        "evaluated_at_ms": now_ms,
        "triggered": list(plan.triggered),
        "waiting": list(plan.waiting),
        "transitioned": list(plan.transitioned),
        "skipped": skipped,
        "prices": prices_map,
    }
    return payload, evaluations


def _legacy_projection(
    evaluations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """旧 ``/evaluate-candidates`` 的 candidates/skipped 形状（水平视图，兼容旧 Host）。

    ``/api/rules/scan`` 是边沿触发；旧链与界面仍按"当前是否满足 + 是否已消费"看
    候选，所以兼容别名把逐条求值结果再投影成旧字段，语义与 v0.2.1 保持一致。
    """

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in evaluations:
        target, sym, result = item["target"], item["symbol"], item["result"]
        row, confirmed, rules = target["row"], target["confirmed"], target["rules"]
        base = {
            "strategy_id": row["strategy_id"],
            "name": row["name"],
            "version": item["version"],
            "current_version": item["version"],
            "generation": int(row["generation"]),
            "enabled": True,
            "execution_mode": str(rules.get("execution_mode") or "ai_assisted"),
            "symbol": sym,
            "side": item["side"],
            "signal_key": result["signal_key"],
            "mark": result["mark"],
            "distance_pct": result["distance_pct"],
            "indicator_facts": result.get("facts") or {},
        }
        if result["matched"] and item["consumed"]:
            skipped.append(
                {
                    **base,
                    "reason_code": "ALREADY_SIGNALED",
                    "detail": "该周期信号已触发过",
                }
            )
            continue
        if not result["matched"]:
            skipped.append(
                {
                    **base,
                    "reason_code": result["reason_code"],
                    "detail": result["detail"],
                }
            )
            continue
        candidates.append(
            {
                **base,
                "rules": rules,
                "confirmed_version": confirmed,
            }
        )
    return candidates, skipped


@rules_router.post("/scan", summary="确定性规则扫描（边沿触发 + 冷却 + 停机保护）")
def rules_scan(
    request: Request,
    body: ScanBody | None = FastAPIBody(default=None),
):
    payload, _evaluations = _run_scan(request, body or ScanBody())
    return payload


@rules_router.post("/dispatched", summary="回写派发结果（幂等，R5② 重新武装 / rejected 冷却）")
def rules_dispatched(request: Request, body: DispatchBody):
    s = store(request)
    outcome = str(body.outcome or "").strip().lower()
    if outcome not in rule_scan.DISPATCH_OUTCOMES:
        raise ServiceError(
            422,
            "E_VALIDATION",
            "outcome 必须为 dispatched 或 rejected",
            details={"outcome": str(body.outcome)},
        )
    symbol = str(body.symbol or "").strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise ServiceError(422, "E_VALIDATION", "symbol 必须为 USDT 标的代码")
    at_ms = int(body.at_ms) if body.at_ms is not None else int(_resolve_market(request).now_ms())
    return s.apply_rule_dispatch(
        body.rule_key,
        symbol,
        outcome,
        at_ms=at_ms,
        scan_interval_ms=body.scan_interval_ms,
    )


@router.post("/evaluate-candidates", summary="兼容别名：与 /api/rules/scan 同一实现")
def evaluate_candidates(
    request: Request,
    body: ScanBody | None = FastAPIBody(default=None),
):
    payload, evaluations = _run_scan(request, body or ScanBody())
    candidates, legacy_skipped = _legacy_projection(evaluations)
    # 旧字段保持原语义（candidates/skipped 是逐条求值视图）；规则级跳过清单改名
    # rule_skipped，避免覆盖旧 Host 读的 skipped。
    payload["rule_skipped"] = payload.pop("skipped")
    payload["skipped"] = legacy_skipped
    payload["candidates"] = candidates
    return payload


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
