"""Binance USD-M 原生离线事件回放与成本模型。

本模块只消费显式传入的不可变 JSON 夹具：不联网、不读环境变量、不接触 Broker
或真实状态目录。历史盘口缺失时会降低 coverage，而不是假定零滑点。
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def load_fixture(path: Path) -> tuple[dict[str, Any], str]:
    """读取并校验夹具，返回副本和内容哈希，防止回放过程改写输入。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(raw.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("unsupported replay fixture schema")
    if str(raw.get("market") or "") != "BINANCE_USDM":
        raise ValueError("replay only accepts BINANCE_USDM fixtures")
    digest = hashlib.sha256(_canonical(raw)).hexdigest()
    return deepcopy(raw), digest


def _floor(value: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step/tick must be positive")
    left, right = Decimal(str(value)), Decimal(str(step))
    return float((left / right).to_integral_value(rounding=ROUND_DOWN) * right)


def _ordered_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous = -1
    for event in events:
        timestamp = int(event.get("timestamp_ms") or 0)
        if timestamp <= previous:
            raise ValueError("events must be strictly ordered by timestamp_ms")
        observed = int(event.get("observed_at_ms") or timestamp)
        source_at = int(event.get("source_at_ms") or timestamp)
        if observed > timestamp or source_at > timestamp:
            raise ValueError("future_data_leakage")
        submitted = int(event.get("submitted_at_ms") or timestamp)
        if submitted > timestamp:
            raise ValueError("order_submission_after_fill")
        previous = timestamp
    return events


def _fill_price(reference: float, side: str, action: str, order_type: str,
                slippage_bps: float, tick_size: float) -> float:
    if order_type == "MAKER":
        return _floor(reference, tick_size)
    buy = (action == "open" and side == "LONG") or (action == "close" and side == "SHORT")
    multiplier = 1.0 + (slippage_bps / 10_000.0) * (1 if buy else -1)
    return _floor(reference * multiplier, tick_size)


def replay_fixture(fixture: dict[str, Any], *, fees_enabled: bool = True,
                   funding_enabled: bool = True, slippage_enabled: bool = True) -> dict[str, Any]:
    """按事件时序回放成交；返回逐笔结果、成本拆分、覆盖缺口和稳定摘要。"""
    data = deepcopy(fixture)
    events = _ordered_events(list(data.get("events") or []))
    instruments = data.get("instruments") or {}
    costs = data.get("costs") or {}
    maker_bps = float(costs.get("maker_fee_bps") or 0.0)
    taker_bps = float(costs.get("taker_fee_bps") or 0.0)
    default_slippage = float(costs.get("slippage_bps") or 0.0) if slippage_enabled else 0.0
    open_positions: dict[tuple[str, str, str], dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    coverage_gaps: set[str] = set()

    for event in events:
        kind = str(event.get("type") or "")
        if kind == "funding" and funding_enabled:
            symbol = str(event.get("symbol") or "")
            rate = float(event.get("rate") or 0.0)
            for position in open_positions.values():
                if position["symbol"] != symbol:
                    continue
                direction = 1.0 if position["side"] == "LONG" else -1.0
                position["funding"] += -(position["entry_price"] * position["qty"] * rate * direction)
            continue
        if kind != "order":
            continue
        symbol = str(event.get("symbol") or "")
        side = str(event.get("side") or "").upper()
        variant = str(event.get("variant") or "baseline")
        action = str(event.get("action") or "")
        spec = instruments.get(symbol) or {}
        if not spec:
            raise ValueError(f"missing instrument filters for {symbol}")
        qty = _floor(abs(float(event.get("qty") or 0.0)), float(spec.get("step_size") or 0.0))
        if qty < float(spec.get("min_qty") or 0.0):
            continue
        reference = float(event.get("reference_price") or 0.0)
        order_type = str(event.get("order_type") or "TAKER").upper()
        depth_qty = event.get("available_qty")
        if depth_qty is None:
            coverage_gaps.add("historical_order_book")
            fill_ratio = float(event.get("declared_fill_ratio") or 1.0)
        else:
            fill_ratio = min(1.0, max(0.0, float(depth_qty) / qty)) if qty else 0.0
        qty = _floor(qty * fill_ratio, float(spec["step_size"]))
        if qty < float(spec.get("min_qty") or 0.0):
            continue
        price = _fill_price(reference, side, action, order_type, default_slippage,
                            float(spec.get("tick_size") or 0.0))
        fee_bps = maker_bps if order_type == "MAKER" else taker_bps
        fee = price * qty * fee_bps / 10_000.0 if fees_enabled else 0.0
        signal_price = float(event.get("signal_price") or reference)
        latency_ms = max(0, int(event["timestamp_ms"]) - int(
            event.get("submitted_at_ms") or event["timestamp_ms"]))
        buy = (action == "open" and side == "LONG") or (action == "close" and side == "SHORT")
        latency_cost = max(0.0, (reference - signal_price) * qty * (1 if buy else -1))
        key = (variant, symbol, side)
        if action == "open":
            existing = open_positions.get(key)
            if existing:
                # 同方向重复开仓按真实持仓语义合并，避免静默覆盖首笔成本与数量。
                combined_qty = float(existing["qty"]) + qty
                existing["entry_price"] = (
                    float(existing["entry_price"]) * float(existing["qty"]) + price * qty
                ) / combined_qty
                existing["qty"] = combined_qty
                existing["entry_fee"] += fee
                existing["entry_latency_cost"] += latency_cost
                existing["max_entry_latency_ms"] = max(
                    int(existing["max_entry_latency_ms"]), latency_ms)
                existing["partial_fill_open"] = bool(
                    existing.get("partial_fill_open")) or fill_ratio < 1.0
            else:
                open_positions[key] = {
                    "variant": variant, "symbol": symbol, "side": side, "qty": qty,
                    "entry_price": price, "opened_at_ms": int(event["timestamp_ms"]),
                    "entry_fee": fee, "funding": 0.0,
                    "entry_latency_cost": latency_cost,
                    "max_entry_latency_ms": latency_ms,
                    "strategy_id": event.get("strategy_id"),
                    "incident": event.get("incident"),
                    "partial_fill_open": fill_ratio < 1.0,
                }
            continue
        if action != "close" or key not in open_positions:
            continue
        position = open_positions[key]
        close_qty = min(qty, float(position["qty"]))
        gross = ((price - position["entry_price"]) * close_qty
                 if side == "LONG" else (position["entry_price"] - price) * close_qty)
        allocated_entry_fee = position["entry_fee"] * close_qty / position["qty"]
        allocated_funding = position["funding"] * close_qty / position["qty"]
        allocated_entry_latency = position["entry_latency_cost"] * close_qty / position["qty"]
        net = gross - allocated_entry_fee - fee + allocated_funding
        trades.append({
            **{k: position.get(k) for k in ("variant", "symbol", "side", "strategy_id", "incident")},
            "qty": close_qty, "entry_price": position["entry_price"], "exit_price": price,
            "opened_at_ms": position["opened_at_ms"], "closed_at_ms": int(event["timestamp_ms"]),
            "gross_pnl": round(gross, 12), "fees": round(allocated_entry_fee + fee, 12),
            "funding_pnl": round(allocated_funding, 12), "net_pnl": round(net, 12),
            "latency_cost": round(allocated_entry_latency + latency_cost, 12),
            "entry_latency_ms": int(position["max_entry_latency_ms"]),
            "exit_latency_ms": latency_ms,
            "partial_fill": bool(position.get("partial_fill_open")) or fill_ratio < 1.0,
        })
        remaining = float(position["qty"]) - close_qty
        if remaining < float(spec.get("min_qty") or 0.0):
            open_positions.pop(key)
        else:
            # 部分平仓后只扣除按比例分摊的入场费用、资金费和延迟成本。
            position["qty"] = remaining
            position["entry_fee"] -= allocated_entry_fee
            position["funding"] -= allocated_funding
            position["entry_latency_cost"] -= allocated_entry_latency

    return {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": data.get("fixture_id"),
        "trades": trades,
        "open_positions": list(open_positions.values()),
        "coverage_gaps": sorted(coverage_gaps),
        "cost_assumptions": {
            "fees_enabled": fees_enabled, "funding_enabled": funding_enabled,
            "slippage_enabled": slippage_enabled, "maker_fee_bps": maker_bps,
            "taker_fee_bps": taker_bps, "slippage_bps": default_slippage,
        },
    }
