"""币安 USD-M 公开情报侧车。

本模块不持有 Broker、交易 API key 或下单能力。它只读取币安公开行情，
并把标准化结果写入审计库。任何异常都降级成 unknown，不影响交易循环。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from intel.models import FuturesFeature, IntelEvent


PUBLIC_BASE_URL = "https://fapi.binance.com"
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
GOPLUS_BASE_URL = "https://api.gopluslabs.io/api/v1"
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
RATIO_PATHS = {
    "top_account_ratio": "/futures/data/topLongShortAccountRatio",
    "top_position_ratio": "/futures/data/topLongShortPositionRatio",
    "taker_buy_sell_ratio": "/futures/data/takerlongshortRatio",
}


def _iso_from_ms(value: Any) -> str:
    return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc).isoformat(timespec="milliseconds")


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except (TypeError, ValueError):
        return None


class ReadOnlyIntelService:
    """分层轮转采集公开情报，并生成 execution_weight=0 的影子信号。"""

    def __init__(self, *, db: Any, config: dict[str, Any], symbols: list[str]):
        self.db = db
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", True))
        self.shadow_only = True
        self.execution_weight = 0.0
        self.refresh_seconds = max(60.0, float(self.config.get("refresh_seconds", 300)))
        self.fresh_seconds = max(60, int(self.config.get("fresh_seconds", 600)))
        self.priority_count = max(1, int(self.config.get("priority_count", 3)))
        self.rotation_count = max(1, int(self.config.get("rotation_count", 4)))
        self.symbols = [s for s in dict.fromkeys(symbols) if self._valid_symbol(s)]
        self._symbol_set = set(self.symbols)
        self._priority = self.symbols[:self.priority_count]
        self._rotation = self.symbols[self.priority_count:]
        self._cursor = 0
        self._lock = threading.RLock()
        self._last_attempt = 0.0
        self._liquidations: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] = self._empty_snapshot("waiting" if self.enabled else "disabled")

    def _valid_symbol(self, symbol: str) -> bool:
        return bool(symbol and symbol.endswith("USDT") and symbol.isalnum() and symbol.upper() == symbol)

    def _empty_snapshot(self, status: str) -> dict[str, Any]:
        wallets = list(self.config.get("watched_wallets") or [])
        contracts = dict(self.config.get("token_contracts") or {})
        return {
            "enabled": self.enabled,
            "status": status,
            "shadow_only": True,
            "execution_weight": 0.0,
            "updated_at": None,
            "fresh_until": None,
            "source": "binance_usdm_public",
            "label": "币安 Top Trader（聪明钱代理，非链上钱包）",
            "symbols": [],
            "errors": [],
            "wallet_tracker": {
                "status": "unconfigured" if not wallets else "pending",
                "watched_count": len(wallets),
                "note": "需配置只读钱包地址与链上数据 API；当前不会推断或伪造钱包活动。",
            },
            "token_audit": {
                "status": "unconfigured" if not contracts else "pending",
                "mapped_count": len(contracts),
                "note": "只接受显式合约地址到 USD-M 白名单交易对的映射。",
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._snapshot)
            snapshot["liquidations"] = list(self._liquidations)
            return json.loads(json.dumps(snapshot, ensure_ascii=False))

    def record_internal_error(self, stage: str, exc: Exception) -> None:
        """记录侧车自身降级，不污染正式交易风险事件。"""
        with self._lock:
            current = dict(self._snapshot)
            errors = list(current.get("errors") or [])
            errors.append(f"{stage}:{type(exc).__name__}")
            current["errors"] = errors[-20:]
            current["status"] = "degraded"
            current["last_error_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self._snapshot = current

    def record_liquidation(self, payload: dict[str, Any]) -> None:
        """标准化公开强平事件；非白名单数据立即丢弃。"""
        order = payload.get("o") if isinstance(payload.get("o"), dict) else {}
        symbol = str(order.get("s") or "")
        if symbol not in self._symbol_set:
            return
        event_ms = int(payload.get("E") or order.get("T") or 0)
        if event_ms <= 0:
            return
        price = _safe_float(order.get("ap") or order.get("p"))
        qty = _safe_float(order.get("z") or order.get("q"))
        clean = {
            "symbol": symbol, "side": str(order.get("S") or ""),
            "price": price, "qty": qty,
            "notional_usdt": round(price * qty, 4) if price and qty else None,
            "observed_at": _iso_from_ms(event_ms),
        }
        canonical = json.dumps(clean, sort_keys=True, separators=(",", ":"))
        event = IntelEvent(
            source="binance_usdm_force_order_stream", kind="liquidation",
            candidate_symbol=symbol, observed_at=clean["observed_at"],
            expires_at=(datetime.fromtimestamp(event_ms / 1000, timezone.utc) +
                        timedelta(minutes=15)).isoformat(timespec="milliseconds"),
            confidence=1.0, quality="good", payload=clean,
            raw_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        )
        self.db.log_intel_event(event=event.model_dump())
        cutoff = time.time() - 900
        with self._lock:
            self._liquidations = [row for row in self._liquidations
                                  if datetime.fromisoformat(row["observed_at"]).timestamp() >= cutoff]
            self._liquidations.append(clean)
            self._liquidations = self._liquidations[-100:]

    def maybe_refresh(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if not self.enabled:
                return self.snapshot()
            if not force and now - self._last_attempt < self.refresh_seconds:
                return self.snapshot()
            self._last_attempt = now
        try:
            snapshot = self._refresh()
        except Exception as exc:
            previous = self.snapshot()
            previous["status"] = "degraded"
            previous["errors"] = [f"{type(exc).__name__}: {exc}"]
            previous["last_error_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            with self._lock:
                self._snapshot = previous
            return previous
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _selected_symbols(self) -> list[str]:
        selected = list(self._priority)
        if self._rotation:
            for offset in range(min(self.rotation_count, len(self._rotation))):
                selected.append(self._rotation[(self._cursor + offset) % len(self._rotation)])
            self._cursor = (self._cursor + self.rotation_count) % len(self._rotation)
        return list(dict.fromkeys(selected))

    def _get_json(self, client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
        response = client.get(f"{PUBLIC_BASE_URL}{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise RuntimeError(f"Binance {path}: {payload.get('msg') or payload.get('code')}")
        return payload

    def _fetch_ratios(self, symbol: str) -> tuple[dict[str, float | None], int | None, list[str]]:
        values: dict[str, float | None] = {}
        event_ms: int | None = None
        errors: list[str] = []
        with httpx.Client(timeout=httpx.Timeout(6.0, connect=4.0)) as client:
            for name, path in RATIO_PATHS.items():
                try:
                    rows = self._get_json(client, path, {"symbol": symbol, "period": "5m", "limit": 1})
                    row = rows[-1] if isinstance(rows, list) and rows else {}
                    values[name] = _safe_float(row.get("buySellRatio") if name == "taker_buy_sell_ratio"
                                               else row.get("longShortRatio"))
                    timestamp = int(row.get("timestamp") or 0)
                    if timestamp:
                        event_ms = max(event_ms or 0, timestamp)
                except Exception as exc:
                    values[name] = None
                    errors.append(f"{name}:{type(exc).__name__}")
            try:
                oi = self._get_json(client, "/fapi/v1/openInterest", {"symbol": symbol})
                values["open_interest"] = _safe_float(oi.get("openInterest"))
                oi_ms = int(oi.get("time") or 0)
                if oi_ms:
                    event_ms = max(event_ms or 0, oi_ms)
            except Exception as exc:
                values["open_interest"] = None
                errors.append(f"open_interest:{type(exc).__name__}")
            try:
                depth = self._get_json(client, "/fapi/v1/depth", {"symbol": symbol, "limit": 20})
                bids = depth.get("bids") or []
                asks = depth.get("asks") or []
                best_bid = _safe_float(bids[0][0]) if bids else None
                best_ask = _safe_float(asks[0][0]) if asks else None
                bid_qty = sum(float(row[1]) for row in bids)
                ask_qty = sum(float(row[1]) for row in asks)
                mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
                values["spread_bps"] = ((best_ask - best_bid) / mid * 10000) if mid else None
                values["depth_imbalance"] = ((bid_qty - ask_qty) / (bid_qty + ask_qty)) if bid_qty + ask_qty > 0 else None
                values["depth_update_id"] = int(depth.get("lastUpdateId") or 0) or None
            except Exception as exc:
                values["spread_bps"] = None
                values["depth_imbalance"] = None
                values["depth_update_id"] = None
                errors.append(f"depth:{type(exc).__name__}")
        return values, event_ms, errors

    def _refresh(self) -> dict[str, Any]:
        selected = self._selected_symbols()
        received = datetime.now(timezone.utc)
        fresh_until = received + timedelta(seconds=self.fresh_seconds)
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
            premium_rows = self._get_json(client, "/fapi/v1/premiumIndex")
        premium_by = {str(row.get("symbol")): row for row in premium_rows if isinstance(row, dict)}
        ratios: dict[str, tuple[dict[str, float | None], int | None, list[str]]] = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(selected)))) as pool:
            futures = {pool.submit(self._fetch_ratios, symbol): symbol for symbol in selected}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    ratios[symbol] = future.result()
                except Exception as exc:
                    ratios[symbol] = ({}, None, [f"collector:{type(exc).__name__}"])

        rows: list[dict[str, Any]] = []
        errors: list[str] = []
        for symbol in selected:
            premium = premium_by.get(symbol) or {}
            ratio_values, ratio_ms, symbol_errors = ratios.get(symbol, ({}, None, ["missing_ratio_result"]))
            mark = _safe_float(premium.get("markPrice"))
            index = _safe_float(premium.get("indexPrice"))
            funding = _safe_float(premium.get("lastFundingRate"))
            premium_ms = int(premium.get("time") or 0) or None
            event_ms = max(x for x in (premium_ms, ratio_ms) if x is not None) if (premium_ms or ratio_ms) else None
            quality = "good"
            if mark is None or index is None or mark <= 0 or index <= 0:
                quality = "invalid"
            elif symbol_errors or any(ratio_values.get(k) is None for k in RATIO_PATHS):
                quality = "degraded"
            basis_bps = ((mark / index) - 1.0) * 10000.0 if mark and index else None
            next_funding = premium.get("nextFundingTime")
            directional = {key: ratio_values.get(key) for key in RATIO_PATHS}
            signal = self._signal(directional)
            feature = FuturesFeature(
                symbol=symbol,
                event_time=_iso_from_ms(event_ms) if event_ms else received.isoformat(timespec="milliseconds"),
                received_at=received.isoformat(timespec="milliseconds"),
                fresh_until=fresh_until.isoformat(timespec="milliseconds"),
                quality=quality,
                mark_price=mark,
                index_price=index,
                basis_bps=round(basis_bps, 4) if basis_bps is not None else None,
                funding_rate=funding,
                next_funding_time=_iso_from_ms(next_funding) if next_funding else None,
                open_interest_usd=(ratio_values.get("open_interest") * mark
                                   if ratio_values.get("open_interest") is not None and mark else None),
                spread_bps=ratio_values.get("spread_bps"),
                depth_imbalance=ratio_values.get("depth_imbalance"),
                depth_update_id=(int(ratio_values["depth_update_id"])
                                 if ratio_values.get("depth_update_id") else None),
                top_account_ratio=ratio_values.get("top_account_ratio"),
                top_position_ratio=ratio_values.get("top_position_ratio"),
                taker_buy_sell_ratio=ratio_values.get("taker_buy_sell_ratio"),
                signal=signal,
            )
            dumped = feature.model_dump()
            rows.append(dumped)
            errors.extend(f"{symbol}:{item}" for item in symbol_errors)
            self.db.upsert_futures_feature(feature=dumped)
            self._persist_event(feature)
            self.db.log_shadow_decision(
                decision_id=str(uuid.uuid4()), symbol=symbol, signal=signal,
                reference_price=mark, confidence=self._confidence(directional),
                source="B_contract_market", execution_weight=0.0,
                feature_json=json.dumps(dumped, ensure_ascii=False),
            )
        # premiumIndex 是全市场快照，可让所有白名单影子决策按 5 分钟频率回看，
        # 不受分层轮转采集间隔影响。
        all_prices = {
            symbol: price for symbol, row in premium_by.items()
            if symbol in self._symbol_set and (price := _safe_float(row.get("markPrice")))
        }
        self.db.evaluate_shadow_decisions(prices=all_prices,
                                          grace_minutes=max(10, int(self.refresh_seconds / 30)))
        wallet_tracker = self._refresh_wallet_tracker(received, fresh_until)
        token_audit = self._refresh_token_audit(received, fresh_until)
        self._record_fused_shadows(rows, wallet_tracker, token_audit)
        return {
            **self._empty_snapshot("ok" if rows and not errors else "degraded"),
            "updated_at": received.isoformat(timespec="milliseconds"),
            "fresh_until": fresh_until.isoformat(timespec="milliseconds"),
            "symbols": rows,
            "errors": errors[:20],
            "coverage": {"collected": len(rows), "whitelist": len(self.symbols), "rotation_cursor": self._cursor},
            "wallet_tracker": wallet_tracker,
            "token_audit": token_audit,
            "shadow_evaluation": self.db.intel_observability(),
        }

    def record_baseline_shadow(self, *, run_id: str, decision: Any,
                               prices: dict[str, float]) -> int:
        """记录 A 组：当前系统开仓决策的零权重副本。"""
        if decision is None:
            return 0
        intents = list(getattr(decision, "intents", None) or [])
        inserted = 0
        for intent in intents:
            if str(getattr(intent, "action", "open")) != "open":
                continue
            symbol = str(getattr(intent, "symbol", ""))
            side = str(getattr(intent, "side", ""))
            reference = _safe_float(prices.get(symbol))
            if symbol not in self._symbol_set or side not in {"LONG", "SHORT"} or not reference:
                continue
            payload = (intent.model_dump() if hasattr(intent, "model_dump") else {
                "symbol": symbol, "side": side, "action": "open",
            })
            self.db.log_shadow_decision(
                decision_id=f"A:{run_id}:{symbol}:{side}", symbol=symbol,
                signal="bullish" if side == "LONG" else "bearish",
                reference_price=reference,
                confidence=float(getattr(intent, "confidence", 0.5) or 0.5),
                source="A_current_baseline", execution_weight=0.0,
                feature_json=json.dumps(payload, ensure_ascii=False),
            )
            inserted += 1
        return inserted

    def _record_fused_shadows(self, rows: list[dict[str, Any]],
                              wallet_tracker: dict[str, Any],
                              token_audit: dict[str, Any]) -> None:
        """记录 C 组；无明确 Web3 方向时必须为 unknown。"""
        wallet_by: dict[str, dict[str, Any]] = {}
        for event in list(wallet_tracker.get("events") or []):
            symbol = str(event.get("symbol") or "")
            if symbol in self._symbol_set and event.get("inferred_signal") in {"bullish", "bearish"}:
                wallet_by[symbol] = event
        flags_by = {
            str(item.get("symbol")): list(item.get("red_flags") or [])
            for item in list(token_audit.get("audits") or [])
        }
        for row in rows:
            symbol = str(row["symbol"])
            wallet = wallet_by.get(symbol)
            red_flags = flags_by.get(symbol, [])
            signal = str(wallet.get("inferred_signal")) if wallet and not red_flags else "unknown"
            confidence = min(float(row.get("signal") != "unknown") * 0.25 + 0.55, 0.9) if signal != "unknown" else 0.0
            payload = {"contract_market": row, "wallet_event": wallet,
                       "token_red_flags": red_flags, "fusion_available": signal != "unknown"}
            self.db.log_shadow_decision(
                decision_id=str(uuid.uuid4()), symbol=symbol, signal=signal,
                reference_price=_safe_float(row.get("mark_price")), confidence=confidence,
                source="C_contract_plus_web3", execution_weight=0.0,
                feature_json=json.dumps(payload, ensure_ascii=False),
            )

    def _contract_map(self) -> dict[tuple[str, str], str]:
        """返回 (chain_id, lowercase contract) -> 白名单 USD-M symbol。"""
        out: dict[tuple[str, str], str] = {}
        for symbol, item in dict(self.config.get("token_contracts") or {}).items():
            if symbol not in self._symbol_set or not isinstance(item, dict):
                continue
            address = str(item.get("address") or "")
            chain_id = str(item.get("chain_id") or "1")
            if EVM_ADDRESS_RE.fullmatch(address):
                out[(chain_id, address.lower())] = symbol
        return out

    def _refresh_wallet_tracker(self, received: datetime, fresh_until: datetime) -> dict[str, Any]:
        watched = list(self.config.get("watched_wallets") or [])
        if not watched:
            return self._empty_snapshot("ok")["wallet_tracker"]
        api_key_env = str(self.config.get("etherscan_api_key_env") or "ETHERSCAN_API_KEY")
        api_key = os.getenv(api_key_env, "").strip()
        if not api_key:
            return {"status": "unavailable", "watched_count": len(watched),
                    "events": [], "error": f"missing_env:{api_key_env}"}
        contract_map = self._contract_map()
        events: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in watched[:10]:
            cfg = item if isinstance(item, dict) else {"address": item}
            address = str(cfg.get("address") or "")
            chain_id = str(cfg.get("chain_id") or "1")
            label = str(cfg.get("label") or "watched_wallet")[:80]
            role = str(cfg.get("role") or "observe_only")
            if not EVM_ADDRESS_RE.fullmatch(address):
                errors.append(f"invalid_address:{label}")
                continue
            try:
                with httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
                    response = client.get(ETHERSCAN_V2_URL, params={
                        "chainid": chain_id, "module": "account", "action": "tokentx",
                        "address": address, "page": 1, "offset": 50, "sort": "desc",
                        "apikey": api_key,
                    })
                    response.raise_for_status()
                    payload = response.json()
                result = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(result, list):
                    raise RuntimeError(str(payload.get("message") if isinstance(payload, dict) else "invalid_schema"))
                for transfer in result:
                    contract = str(transfer.get("contractAddress") or "").lower()
                    symbol = contract_map.get((chain_id, contract))
                    if not symbol:
                        continue
                    ts = int(transfer.get("timeStamp") or 0)
                    decimals = min(30, max(0, int(transfer.get("tokenDecimal") or 0)))
                    amount = _safe_float(transfer.get("value"))
                    normalized = (amount / (10 ** decimals)) if amount is not None else None
                    direction = "out" if str(transfer.get("from") or "").lower() == address.lower() else "in"
                    inferred = "unknown"
                    if role == "smart_money":
                        inferred = "bullish" if direction == "in" else "bearish"
                    elif role == "exchange":
                        inferred = "bearish" if direction == "in" else "bullish"
                    clean = {"wallet_label": label, "wallet_role": role,
                             "direction": direction, "inferred_signal": inferred,
                             "amount": normalized,
                             "tx_hash": str(transfer.get("hash") or "")[:80]}
                    raw_hash = hashlib.sha256(
                        json.dumps({"chain": chain_id, "contract": contract, **clean},
                                   sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    event = IntelEvent(
                        source="etherscan_v2", kind="watched_wallet_transfer", chain=chain_id,
                        contract=contract, candidate_symbol=symbol,
                        observed_at=datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds"),
                        expires_at=fresh_until.isoformat(timespec="milliseconds"), confidence=0.8,
                        quality="good", payload=clean, raw_hash=raw_hash)
                    self.db.log_intel_event(event=event.model_dump())
                    events.append({"symbol": symbol, "observed_at": event.observed_at, **clean})
            except Exception as exc:
                errors.append(f"{label}:{type(exc).__name__}")
        return {"status": "ok" if not errors else "degraded", "watched_count": len(watched),
                "events": events[:50], "errors": errors[:10],
                "updated_at": received.isoformat(timespec="milliseconds")}

    def _refresh_token_audit(self, received: datetime, fresh_until: datetime) -> dict[str, Any]:
        contract_map = self._contract_map()
        if not contract_map:
            return self._empty_snapshot("ok")["token_audit"]
        grouped: dict[str, list[tuple[str, str]]] = {}
        for (chain_id, contract), symbol in contract_map.items():
            grouped.setdefault(chain_id, []).append((contract, symbol))
        audits: list[dict[str, Any]] = []
        errors: list[str] = []
        safe_fields = (
            "is_honeypot", "buy_tax", "sell_tax", "cannot_sell_all", "is_proxy",
            "owner_change_balance", "hidden_owner", "slippage_modifiable",
            "external_call", "trading_cooldown", "transfer_pausable",
        )
        for chain_id, mappings in grouped.items():
            addresses = ",".join(contract for contract, _ in mappings[:30])
            try:
                with httpx.Client(timeout=httpx.Timeout(8.0, connect=5.0)) as client:
                    response = client.get(f"{GOPLUS_BASE_URL}/token_security/{chain_id}",
                                          params={"contract_addresses": addresses})
                    response.raise_for_status()
                    payload = response.json()
                result = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(result, dict):
                    raise RuntimeError("invalid_schema")
                for contract, symbol in mappings:
                    raw = result.get(contract) or result.get(contract.lower()) or {}
                    clean = {key: raw.get(key) for key in safe_fields}
                    red_flags = [key for key in ("is_honeypot", "cannot_sell_all", "hidden_owner",
                                                  "slippage_modifiable", "trading_cooldown",
                                                  "transfer_pausable") if str(clean.get(key)) == "1"]
                    audit = {"symbol": symbol, "chain": chain_id, "contract": contract,
                             "quality": "good" if raw else "unknown", "red_flags": red_flags,
                             "details": clean}
                    canonical = json.dumps(audit, sort_keys=True, separators=(",", ":"))
                    event = IntelEvent(
                        source="goplus_token_security", kind="token_audit", chain=chain_id,
                        contract=contract, candidate_symbol=symbol,
                        observed_at=received.isoformat(timespec="milliseconds"),
                        expires_at=fresh_until.isoformat(timespec="milliseconds"),
                        confidence=0.9 if raw else 0.0, quality=audit["quality"], payload=audit,
                        raw_hash=hashlib.sha256(canonical.encode()).hexdigest())
                    self.db.log_intel_event(event=event.model_dump())
                    audits.append(audit)
            except Exception as exc:
                errors.append(f"chain_{chain_id}:{type(exc).__name__}")
        return {"status": "ok" if audits and not errors else ("degraded" if audits else "unavailable"),
                "mapped_count": len(contract_map), "audits": audits, "errors": errors[:10],
                "updated_at": received.isoformat(timespec="milliseconds")}

    @staticmethod
    def _signal(values: dict[str, float | None]) -> str:
        valid = [float(v) for v in values.values() if v is not None and v > 0]
        if len(valid) < 2:
            return "unknown"
        bullish = sum(v >= 1.05 for v in valid)
        bearish = sum(v <= 0.95 for v in valid)
        if bullish >= 2:
            return "bullish"
        if bearish >= 2:
            return "bearish"
        return "neutral"

    @staticmethod
    def _confidence(values: dict[str, float | None]) -> float:
        valid = [float(v) for v in values.values() if v is not None and v > 0]
        if not valid:
            return 0.0
        agreement = max(sum(v >= 1.05 for v in valid), sum(v <= 0.95 for v in valid)) / len(valid)
        magnitude = min(1.0, sum(abs(math.log(v)) for v in valid) / len(valid))
        return round(min(0.95, 0.35 + 0.4 * agreement + 0.2 * magnitude), 4)

    def _persist_event(self, feature: FuturesFeature) -> None:
        payload = feature.model_dump()
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event = IntelEvent(
            source="binance_top_trader_proxy",
            kind="smart_money_proxy",
            candidate_symbol=feature.symbol,
            observed_at=feature.event_time,
            expires_at=feature.fresh_until,
            confidence=self._confidence({
                "account": feature.top_account_ratio,
                "position": feature.top_position_ratio,
                "taker": feature.taker_buy_sell_ratio,
            }),
            quality=feature.quality if feature.quality in {"good", "degraded", "stale", "invalid"} else "unknown",
            payload=payload,
            raw_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        self.db.log_intel_event(event=event.model_dump())
