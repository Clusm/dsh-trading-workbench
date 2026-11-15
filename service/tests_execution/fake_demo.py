"""本地 fake Demo 上游（httpx.MockTransport）：离线验证签名、下单、保护与失败路径。

覆盖（与 docs/03 §5/§9 对齐）：
- HMAC 签名校验（错误签名返回 ``-1022``，证明客户端签名正确）；
- 市价开仓成交/部分成交/明确拒绝/超时（结果未知）；
- Algo 保护单成功（``code="200"`` 字符串，D2）与失败（``-4120``，D1）；
- 账户/持仓/挂单/保护单/成交流水查询；
- 持仓与钱包随成交变化（平仓计入已实现盈亏）。

**只在测试中使用**；不包含任何真实域名或凭据。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from decimal import Decimal
from typing import Any

import httpx

STEP = Decimal("0.001")
TICK = Decimal("0.10")
MIN_NOTIONAL = Decimal("5")


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return Decimal(default)


def _text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"", "-0"} else text


class FakeDemoExchange:
    def __init__(
        self,
        *,
        api_key: str = "fake-key",
        api_secret: str = "fake-secret",
        mark_price: str = "100000.0",
        wallet: str = "5000.0",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.mark_price = Decimal(mark_price)
        self.wallet = Decimal(wallet)
        self.mode = "ok"
        self.partial_fill = False
        self.reject_orders = 0
        self.timeout_orders = 0
        self.timeout_after_create = 0
        self.protection_failures = 0
        self.algo_timeout_after_create = 0
        self.fail_algo_query = 0
        self.fail_account = 0
        self.fail_market_facts = 0
        self.fail_leverage_bracket = 0
        self.fail_margin_type = 0
        self.fail_position_query = 0
        self.fail_position_after_order = 0
        self.hide_position_once = 0
        self.cancel_no_fill = 0
        self.index_price = Decimal(mark_price)
        self.bid_price = Decimal(mark_price) - Decimal("5")
        self.ask_price = Decimal(mark_price) + Decimal("5")
        self.bracket_mmr: str | None = None
        self.available_override: Decimal | None = None
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[int, dict[str, Any]] = {}
        self.by_cid: dict[str, int] = {}
        self.algos: dict[int, dict[str, Any]] = {}
        self.trades: dict[int, list[dict[str, Any]]] = {}
        self._order_seq = 1000
        self._algo_seq = 2000
        self._trade_seq = 5000
        self.calls: list[dict[str, Any]] = []
        self.signed_calls: list[dict[str, Any]] = []
        self.bad_signature_calls = 0
        # T20：可配置延迟，用来模拟“上游慢/挂住”。按路径覆盖优先于全局值，
        # 前端/其他任务也能复用它验证“未知态”，不要写成某处的 hardcode sleep。
        self.delay_s: float = 0.0
        self.delay_paths: dict[str, float] = {}

    # ------------------------------------------------------------------ transport
    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        raw_query = request.url.query.decode("utf-8")
        params = _parse_query(raw_query)
        # 记录 httpx 为本次请求解析出的超时，便于断言“读用 3s、下单仍用 12s”。
        timeout = request.extensions.get("timeout")
        self.calls.append(
            {"method": method, "path": path, "query": raw_query, "timeout": timeout}
        )
        delay = self.delay_paths.get(path, self.delay_s)
        if delay and delay > 0:
            # 模拟“上游不回应”。MockTransport 里无法让 socket 真的超时，所以按
            # httpx 的语义等价模拟：最多等到本次请求的读超时，超过就抛 ReadTimeout。
            read_limit = None if not timeout else timeout.get("read")
            wait_s = float(delay)
            if read_limit is not None:
                wait_s = min(wait_s, float(read_limit))
            time.sleep(wait_s)
            if read_limit is not None and float(delay) >= float(read_limit):
                raise httpx.ReadTimeout(  # type: ignore[arg-type]
                    "fake upstream read timeout", request=request
                )

        if "signature" in params or request.headers.get("x-mbx-apikey"):
            if not self._verify_signature(request, raw_query, path):
                self.bad_signature_calls += 1
                return _json(request, 401, {"code": -1022, "msg": "Signature for this request is not valid."})
            self.signed_calls.append(
                {"method": method, "path": path, "params": params, "timeout": timeout}
            )

        if path == "/fapi/v1/time":
            return _json(request, 200, {"serverTime": int(time.time() * 1000)})
        if path == "/fapi/v1/exchangeInfo":
            return _json(request, 200, self._exchange_info())
        if path == "/fapi/v2/account":
            if self.fail_account > 0:
                self.fail_account -= 1
                return _json(request, 500, {"code": -1000, "msg": "fake account unavailable"})
            return _json(request, 200, self._account())
        if path == "/fapi/v1/income" and method == "GET":
            # §8·C2：账户资金流水（资金费/划转）。测试可预置 self.income_rows。
            start = int(params.get("startTime") or 0)
            end = int(params.get("endTime") or 0)
            rows = [
                row
                for row in getattr(self, "income_rows", [])
                if (not start or int(row.get("time") or 0) >= start)
                and (not end or int(row.get("time") or 0) < end)
            ]
            return _json(request, 200, rows)
        if path == "/fapi/v1/openOrders":
            return _json(request, 200, self._open_orders())
        if path == "/fapi/v1/premiumIndex" and method == "GET":
            if self.fail_market_facts > 0:
                self.fail_market_facts -= 1
                return _json(request, 500, {"code": -1000, "msg": "fake premiumIndex failure"})
            return _json(
                request,
                200,
                {
                    "symbol": params.get("symbol"),
                    "markPrice": _text(self.mark_price),
                    "indexPrice": _text(self.index_price),
                    "lastFundingRate": "0.0001",
                },
            )
        if path == "/fapi/v1/ticker/bookTicker" and method == "GET":
            return _json(
                request,
                200,
                {
                    "symbol": params.get("symbol"),
                    "bidPrice": _text(self.bid_price),
                    "askPrice": _text(self.ask_price),
                },
            )
        if path == "/fapi/v1/klines" and method == "GET":
            now_ms = int(time.time() * 1000)
            previous_close = self.mark_price
            return _json(
                request,
                200,
                [
                    [now_ms - 120_000, "0", "0", "0", _text(previous_close), "0"],
                    [now_ms - 60_000, "0", "0", "0", _text(self.mark_price), "0"],
                ],
            )
        if path == "/fapi/v1/leverageBracket" and method == "GET":
            if self.fail_leverage_bracket > 0:
                self.fail_leverage_bracket -= 1
                return _json(request, 500, {"code": -1000, "msg": "fake leverageBracket failure"})
            mmr = self.bracket_mmr or "0.01"
            return _json(
                request,
                200,
                [
                    {
                        "symbol": symbol,
                        "brackets": [
                            {"initialLeverage": 125, "maintMarginRatio": "0.004"},
                            {"initialLeverage": 10, "maintMarginRatio": mmr},
                        ],
                    }
                    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
                ],
            )
        if path == "/fapi/v2/positionRisk":
            if self.fail_position_query > 0:
                self.fail_position_query -= 1
                return _json(request, 500, {"code": -1000, "msg": "fake position query failure"})
            if self.fail_position_after_order > 0 and self.orders:
                self.fail_position_after_order -= 1
                return _json(request, 500, {"code": -1000, "msg": "fake post-order position failure"})
            if self.hide_position_once > 0:
                self.hide_position_once -= 1
                return _json(request, 200, [])
            return _json(request, 200, self._position_risk())
        if path == "/fapi/v1/openAlgoOrders":
            if self.fail_algo_query > 0:
                self.fail_algo_query -= 1
                return _json(request, 500, {"code": -1000, "msg": "fake algo query failure"})
            return _json(request, 200, self._open_algos())
        if path == "/fapi/v1/positionSide/dual":
            return _json(request, 200, {"dualSidePosition": False})
        if method == "POST" and path == "/fapi/v1/marginType":
            if self.fail_margin_type > 0:
                self.fail_margin_type -= 1
                return _json(request, 400, {"code": -4048, "msg": "Margin type cannot be changed."})
            return _json(request, 200, {"code": 200, "msg": "success"})
        if method == "POST" and path == "/fapi/v1/leverage":
            return _json(request, 200, {"leverage": int(params.get("leverage", "10")), "symbol": params.get("symbol")})
        if method == "POST" and path == "/fapi/v1/order":
            return self._submit_order(request, params)
        if method == "GET" and path == "/fapi/v1/order":
            return self._get_order(request, params)
        if method == "DELETE" and path == "/fapi/v1/order":
            return self._cancel_order(request, params)
        if method == "POST" and path == "/fapi/v1/algoOrder":
            return self._submit_algo(request, params)
        if method == "GET" and path == "/fapi/v1/algoOrder":
            return self._get_algo(request, params)
        if method == "DELETE" and path == "/fapi/v1/algoOrder":
            return self._cancel_algo(request, params)
        if method == "GET" and path == "/fapi/v1/userTrades":
            return self._user_trades(request, params)
        return _json(request, 404, {"code": -1121, "msg": f"unknown fake path {path}"})

    # ------------------------------------------------------------------ auth
    def _verify_signature(self, request: httpx.Request, raw_query: str, path: str) -> bool:
        del path
        if request.headers.get("x-mbx-apikey") != self.api_key:
            return False
        body, marker, signature = raw_query.rpartition("&signature=")
        if not marker:
            return False
        expected = hmac.new(
            self.api_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    # ------------------------------------------------------------------ market data
    def _exchange_info(self) -> dict[str, Any]:
        def row(symbol: str, step: str, tick: str, min_notional: str = "5") -> dict[str, Any]:
            return {
                "symbol": symbol,
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "quoteAsset": "USDT",
                "pricePrecision": 2,
                "quantityPrecision": 3,
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": tick},
                    {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step, "maxQty": "1000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": step, "maxQty": "100"},
                    {"filterType": "MIN_NOTIONAL", "notional": min_notional},
                ],
            }

        return {
            "timezone": "UTC",
            "symbols": [
                row("BTCUSDT", _text(STEP), _text(TICK)),
                row("ETHUSDT", _text(STEP), "0.01"),
                row("SOLUSDT", _text(STEP), "0.001"),
            ],
        }

    # ------------------------------------------------------------------ account
    def _upnl(self) -> Decimal:
        total = Decimal("0")
        for symbol, position in self.positions.items():
            del symbol
            qty = position["qty"]
            entry = position["entry"]
            diff = self.mark_price - entry
            total += diff * qty if position["side"] == "LONG" else -diff * qty
        return total

    def _margin(self) -> Decimal:
        total = Decimal("0")
        for position in self.positions.values():
            total += position["qty"] * position["entry"] / Decimal(max(1, position["leverage"]))
        return total

    def _account(self) -> dict[str, Any]:
        upnl = self._upnl()
        margin = self._margin()
        available = self.available_override if self.available_override is not None else (self.wallet - margin)
        return {
            "totalMarginBalance": _text(self.wallet + upnl),
            "totalWalletBalance": _text(self.wallet),
            "availableBalance": _text(available),
            "totalUnrealizedProfit": _text(upnl),
            "totalMaintMargin": "0",
            "totalInitialMargin": _text(margin),
        }

    def _position_risk(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for symbol, position in self.positions.items():
            amount = position["qty"] if position["side"] == "LONG" else -position["qty"]
            diff = self.mark_price - position["entry"]
            upnl = diff * position["qty"] if position["side"] == "LONG" else -diff * position["qty"]
            rows.append(
                {
                    "symbol": symbol,
                    "positionSide": "BOTH",
                    "positionAmt": _text(amount),
                    "entryPrice": _text(position["entry"]),
                    "markPrice": _text(self.mark_price),
                    "unRealizedProfit": _text(upnl),
                    "leverage": str(position["leverage"]),
                    "isolatedMargin": _text(
                        position["qty"] * position["entry"] / Decimal(max(1, position["leverage"]))
                    ),
                    "liquidationPrice": "0",
                    "marginType": "isolated",
                }
            )
        return rows

    # ------------------------------------------------------------------ orders
    def _submit_order(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        if self.reject_orders > 0:
            self.reject_orders -= 1
            return _json(request, 400, {"code": -2019, "msg": "Margin is insufficient."})
        if self.timeout_orders > 0:
            self.timeout_orders -= 1
            raise httpx.ReadTimeout("fake order timeout", request=request)

        symbol = params["symbol"]
        side = params["side"]
        qty = _d(params["quantity"])
        if self.cancel_no_fill > 0:
            self.cancel_no_fill -= 1
            order_id = self._next_order_id()
            cid = params.get("newClientOrderId") or f"fake-{order_id}"
            record = {
                "orderId": order_id,
                "clientOrderId": cid,
                "symbol": symbol,
                "side": side,
                "type": params.get("type", "MARKET"),
                "status": "CANCELED",
                "origQty": _text(qty),
                "executedQty": "0",
                "avgPrice": "0",
                "reduceOnly": params.get("reduceOnly", "false").lower() == "true",
                "updateTime": int(time.time() * 1000),
            }
            self.orders[order_id] = record
            self.by_cid[cid] = order_id
            return _json(request, 200, record)
        reduce_only = params.get("reduceOnly", "false").lower() == "true"
        cid = params.get("newClientOrderId") or f"fake-{self._order_seq}"
        order_id = self._next_order_id()
        fill_qty = qty
        status = "FILLED"
        if self.partial_fill:
            fill_qty = (qty / 2).quantize(STEP, rounding="ROUND_DOWN")
            status = "PARTIALLY_FILLED"
        if fill_qty <= 0:
            fill_qty = qty

        record = {
            "orderId": order_id,
            "clientOrderId": cid,
            "symbol": symbol,
            "side": side,
            "type": params.get("type", "MARKET"),
            "status": status,
            "origQty": _text(qty),
            "executedQty": _text(fill_qty),
            "avgPrice": _text(self.mark_price),
            "reduceOnly": reduce_only,
            "updateTime": int(time.time() * 1000),
        }
        self.orders[order_id] = record
        self.by_cid[cid] = order_id
        realized = self._apply_fill(symbol, side, fill_qty, self.mark_price, reduce_only)
        self._record_trade(order_id, symbol, side, fill_qty, self.mark_price, realized)

        if self.timeout_after_create > 0:
            self.timeout_after_create -= 1
            raise httpx.ReadTimeout("fake order timeout after create", request=request)
        return _json(request, 200, record)

    def _apply_fill(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        reduce_only: bool,
    ) -> Decimal:
        position = self.positions.get(symbol)
        if reduce_only:
            if position is None:
                return Decimal("0")
            close_qty = min(qty, position["qty"])
            realized = (
                (price - position["entry"]) * close_qty
                if position["side"] == "LONG"
                else (position["entry"] - price) * close_qty
            )
            position["qty"] -= close_qty
            self.wallet += realized
            if position["qty"] <= 0:
                self.positions.pop(symbol, None)
            return realized

        new_side = "LONG" if side == "BUY" else "SHORT"
        if position is None:
            self.positions[symbol] = {
                "side": new_side,
                "qty": qty,
                "entry": price,
                "leverage": 10,
            }
        elif position["side"] == new_side:
            total = position["qty"] + qty
            position["entry"] = (position["entry"] * position["qty"] + price * qty) / total
            position["qty"] = total
        return Decimal("0")

    def _record_trade(
        self,
        order_id: int,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        realized: Decimal,
    ) -> None:
        self._trade_seq += 1
        self.trades.setdefault(order_id, []).append(
            {
                "id": self._trade_seq,
                "orderId": order_id,
                "symbol": symbol,
                "side": side,
                "price": _text(price),
                "qty": _text(qty),
                "realizedPnl": _text(realized),
                "commission": "0.01",
                "time": int(time.time() * 1000),
            }
        )

    def _get_order(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        order = None
        if params.get("orderId"):
            order = self.orders.get(int(params["orderId"]))
        elif params.get("origClientOrderId"):
            order_id = self.by_cid.get(params["origClientOrderId"])
            order = self.orders.get(order_id) if order_id is not None else None
        if order is None:
            return _json(request, 400, {"code": -2013, "msg": "Order does not exist."})
        return _json(request, 200, order)

    def _cancel_order(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        order_id = self.by_cid.get(params.get("origClientOrderId", ""))
        if order_id is None or order_id not in self.orders:
            return _json(request, 400, {"code": -2011, "msg": "Unknown order sent."})
        self.orders[order_id]["status"] = "CANCELED"
        return _json(request, 200, self.orders[order_id])

    def _open_orders(self) -> list[dict[str, Any]]:
        return [
            order
            for order in self.orders.values()
            if order["status"] in {"NEW", "PARTIALLY_FILLED"}
        ]

    # ------------------------------------------------------------------ protection
    def _submit_algo(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        if self.protection_failures > 0:
            self.protection_failures -= 1
            return _json(
                request, 400, {"code": -4120, "msg": "Order type not supported for this endpoint."}
            )
        self._algo_seq += 1
        algo = {
            "algoId": self._algo_seq,
            "clientAlgoId": params.get("clientAlgoId"),
            "symbol": params.get("symbol"),
            "side": params.get("side"),
            "positionSide": params.get("positionSide"),
            "orderType": params.get("type"),
            "triggerPrice": params.get("triggerPrice"),
            "quantity": params.get("quantity", "0"),
            "closePosition": params.get("closePosition", "false"),
            "algoStatus": "NEW",
            "status": "NEW",
        }
        self.algos[self._algo_seq] = algo
        if self.algo_timeout_after_create > 0:
            # 交易所已收单但回执丢失：客户端只能按 clientAlgoId 查询收敛。
            self.algo_timeout_after_create -= 1
            raise httpx.ReadTimeout("fake algo timeout after create", request=request)
        return _json(request, 200, {**algo, "code": "200"})

    def _get_algo(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        if self.fail_algo_query > 0:
            self.fail_algo_query -= 1
            return _json(request, 500, {"code": -1000, "msg": "fake algo query failure"})
        client_algo_id = params.get("clientAlgoId")
        for algo in self.algos.values():
            if algo.get("clientAlgoId") == client_algo_id:
                return _json(request, 200, {**algo, "code": "200"})
        return _json(request, 400, {"code": -2013, "msg": "Order does not exist."})

    def _cancel_algo(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        algo_id = params.get("algoId")
        if algo_id is not None and int(algo_id) in self.algos:
            self.algos[int(algo_id)]["algoStatus"] = "CANCELED"
            self.algos[int(algo_id)]["status"] = "CANCELED"
            return _json(request, 200, {"code": "200", "msg": "success"})
        return _json(request, 400, {"code": -2011, "msg": "Unknown order sent."})

    def _open_algos(self) -> list[dict[str, Any]]:
        return [algo for algo in self.algos.values() if algo["algoStatus"] == "NEW"]

    def _user_trades(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        order_id = params.get("orderId")
        if order_id is not None:
            return _json(request, 200, self.trades.get(int(order_id), []))
        rows: list[dict[str, Any]] = []
        for trades in self.trades.values():
            rows.extend(trades)
        return _json(request, 200, rows)

    # ------------------------------------------------------------------ helpers
    def _next_order_id(self) -> int:
        self._order_seq += 1
        return self._order_seq

    def calls_for(self, path: str, method: str | None = None) -> list[dict[str, Any]]:
        return [
            call
            for call in self.calls
            if call["path"] == path and (method is None or call["method"] == method)
        ]

    def signed_calls_for(self, path: str, method: str | None = None) -> list[dict[str, Any]]:
        return [
            call
            for call in self.signed_calls
            if call["path"] == path and (method is None or call["method"] == method)
        ]

    def add_algo(
        self,
        symbol: str,
        order_type: str,
        trigger: str,
        *,
        position_side: str | None = None,
        side: str = "SELL",
    ) -> int:
        self._algo_seq += 1
        algo = {
            "algoId": self._algo_seq,
            "clientAlgoId": f"seed-{self._algo_seq}",
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "orderType": order_type,
            "triggerPrice": trigger,
            "quantity": "0",
            "closePosition": "true",
            "algoStatus": "NEW",
            "status": "NEW",
        }
        self.algos[self._algo_seq] = algo
        return self._algo_seq

    def add_position(
        self, symbol: str, side: str, qty: str, entry: str, leverage: int = 10
    ) -> None:
        self.positions[symbol] = {
            "side": side,
            "qty": Decimal(qty),
            "entry": Decimal(entry),
            "leverage": leverage,
        }


def _parse_query(raw_query: str) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(raw_query, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _json(request: httpx.Request, status: int, payload: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        request=request,
    )
