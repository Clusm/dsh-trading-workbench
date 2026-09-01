"""币安 U 本位 Futures Broker：支持 demo（主网真实行情+虚拟资金）/ testnet。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import httpx

from agents.models import RiskVerdict, TradeDecision, TradeIntent
from audit.database import AuditDatabase, utc_now
from concurrent.futures import ThreadPoolExecutor


def _parse_iso(value: Any) -> Any:
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class BinanceFuturesBroker:
    mode = "testnet"

    def __init__(self, api_key: str, api_secret: str,
                 base_url: str = "https://demo-fapi.binance.com",
                 db: AuditDatabase | None = None, environment: str = "demo",
                 ledger_file: Path | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.mode = environment
        self.environment = environment
        self.ledger_file = Path(ledger_file) if ledger_file is not None else (
            Path(__file__).resolve().parents[2] / "data" / "futures_source_ledger.json")
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        # 看板会周期性读取账户；30 秒超时叠加重试会把一次请求拖到 60 秒以上。
        # 读请求采用较短超时，失败后由看板保留最近有效快照。
        self._client_timeout = httpx.Timeout(12.0, connect=5.0)
        self._client = httpx.Client(timeout=self._client_timeout)
        self._symbol_filters: dict[str, dict[str, Any]] = {}
        self._load_exchange_info()
        self.last_error: str | None = None
        # 账户/持仓短缓存：页面和决策循环共用，减少 Binance 请求与限流
        self.cache_ttl_seconds = 2.0
        self._cache_lock = threading.RLock()
        self._account_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._positions_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])

    # ---------- 基础设施 ----------
    def _sign(self, params: dict[str, Any]) -> str:
        # 签名必须与最终发送的 query string 顺序/编码一致；这里按插入顺序签名
        qs = urllib.parse.urlencode(params)
        return hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()

    def _signed(self, method: str, path: str, params: dict[str, Any] | None = None,
                attempts: int = 2) -> Any:
        params = dict(params or {})
        headers = {"X-MBX-APIKEY": self.api_key}
        # 写操作（下单/撤单）不允许盲目重试，避免重复成交；只对读操作重试
        max_attempts = attempts if method == "GET" else 1
        last_err: Exception | None = None
        for i in range(max_attempts):
            # 每次尝试重新生成 timestamp/signature，避免重试必撞 recvWindow
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            params["signature"] = self._sign(params)
            try:
                if method == "GET":
                    r = self._client.get(f"{self.base_url}{path}", params=params, headers=headers)
                elif method == "POST":
                    r = self._client.post(f"{self.base_url}{path}", params=params, headers=headers)
                elif method == "DELETE":
                    r = self._client.delete(f"{self.base_url}{path}", params=params, headers=headers)
                else:
                    raise ValueError(f"unsupported method {method}")
                if r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                if isinstance(data, dict) and data.get("code") and data.get("code") != 200:
                    raise RuntimeError(f"Binance {data.get('code')}: {data.get('msg')}")
                return data
            except Exception as e:
                last_err = e
                # 10054/连接被重置时，旧 keep-alive 连接可能已失效，重建 client 再重试
                msg = str(e)
                if "10054" in msg or isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    self._client = httpx.Client(timeout=self._client_timeout)
                if i < max_attempts - 1:
                    time.sleep(1.0)
        raise last_err if last_err else RuntimeError("request failed")

    def _load_exchange_info(self) -> None:
        try:
            data = self._client.get(f"{self.base_url}/fapi/v1/exchangeInfo", timeout=20).json()
            for s in data.get("symbols", []):
                if s.get("status") != "TRADING" or not s.get("symbol", "").endswith("USDT"):
                    continue
                self._symbol_filters[s["symbol"]] = {f["filterType"]: f for f in s.get("filters", [])}
        except Exception as e:
            self.last_error = f"exchangeInfo: {type(e).__name__} {e}"

    def _load_ledger(self) -> dict[str, Any]:
        if self.ledger_file.exists():
            try:
                data = json.loads(self.ledger_file.read_text(encoding="utf-8"))
                data.setdefault("positions", {})
                data.setdefault("pending_limits", [])
                return data
            except Exception:
                pass
        return {"positions": {}, "pending_limits": []}

    def _save_ledger(self, ledger: dict[str, Any]) -> None:
        tmp = self.ledger_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.ledger_file)

    def _ledger_position(self, symbol: str) -> dict[str, Any] | None:
        ledger = self._load_ledger()
        return ledger.get("positions", {}).get(symbol)

    def _ledger_source(self, symbol: str) -> tuple[str, dict[str, Any]]:
        pos = self._ledger_position(symbol)
        if pos:
            return str(pos.get("source", "ai")), dict(pos)
        return "ai", {}

    def _record_ledger_position(self, symbol: str, source: str,
                                strategy_id: str | None, ticket_id: str | None,
                                margin_mode: str = "ISOLATED") -> dict[str, Any]:
        """把交易所真实仓位快照写入来源台账。"""
        exchange_pos = next((x for x in self.positions(force=True) if x["symbol"] == symbol), None)
        qty = abs(float(exchange_pos.get("positionAmt") or 0)) if exchange_pos else 0.0
        entry = float(exchange_pos.get("entryPrice") or 0.0) if exchange_pos else 0.0
        ledger = self._load_ledger()
        if qty <= 0:
            ledger.get("positions", {}).pop(symbol, None)
        else:
            old = ledger.get("positions", {}).get(symbol) or {}
            ledger.setdefault("positions", {})[symbol] = {
                "symbol": symbol,
                "source": source,
                "strategy_id": strategy_id or old.get("strategy_id"),
                "ticket_id": ticket_id or old.get("ticket_id"),
                "qty": qty,
                "entry_price": entry,
                "margin_mode": margin_mode,
                "updated_at": utc_now(),
            }
        self._save_ledger(ledger)
        return ledger.get("positions", {}).get(symbol) or {}

    def _remove_ledger_position(self, symbol: str) -> None:
        ledger = self._load_ledger()
        ledger.get("positions", {}).pop(symbol, None)
        self._save_ledger(ledger)

    def _format_qty(self, symbol: str, qty: float) -> str:
        filters = self._symbol_filters.get(symbol, {})
        step_str = str(filters.get("LOT_SIZE", {}).get("stepSize", "0.001"))
        try:
            step = Decimal(step_str)
            if step <= 0:
                step = Decimal("0.001")
        except Exception:
            step = Decimal("0.001")
        q = Decimal(str(qty))
        # 向下截断到 stepSize，避免 reduceOnly 数量被舍大导致 -2022
        snapped = (q / step).to_integral_value(rounding=ROUND_DOWN) * step
        # 根据 step 的小数位格式化，规避科学计数法
        exp = step.normalize().as_tuple().exponent
        decimals = max(0, -int(exp) if exp < 0 else 0)
        return f"{snapped:.{decimals}f}"

    def _tick_size(self, symbol: str) -> float:
        filters = self._symbol_filters.get(symbol, {})
        tick_str = str(filters.get("PRICE_FILTER", {}).get("tickSize", "0.01"))
        try:
            tick = float(Decimal(tick_str))
            if tick <= 0:
                tick = 0.01
        except Exception:
            tick = 0.01
        return tick

    def _format_price(self, symbol: str, price: float) -> str:
        tick = Decimal(str(self._tick_size(symbol)))
        if tick <= 0:
            tick = Decimal("0.01")
        snapped = (Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        exp = tick.normalize().as_tuple().exponent
        decimals = max(0, -int(exp) if exp < 0 else 0)
        return f"{snapped:.{decimals}f}"

    def _min_qty(self, symbol: str) -> Decimal:
        filters = self._symbol_filters.get(symbol, {})
        min_str = str(filters.get("LOT_SIZE", {}).get("minQty", "0"))
        try:
            return Decimal(min_str)
        except Exception:
            return Decimal("0")

    def _symbol_max_leverage(self, symbol: str) -> int | None:
        """查询该 symbol 在交易所的实际最大杠杆（所有风险档里的最高 initialLeverage）。"""
        try:
            rows = self._signed("GET", "/fapi/v1/leverageBracket", {"symbol": symbol})
            brackets: list[Any] = []
            if isinstance(rows, list) and rows:
                first = rows[0]
                if isinstance(first, dict):
                    brackets = list(first.get("brackets") or [])
            levels = [int(b.get("initialLeverage") or 0) for b in brackets if isinstance(b, dict)]
            return max(levels) if levels else None
        except Exception:
            return None

    @staticmethod
    def _fill_confirmed(data: dict[str, Any] | None) -> bool:
        """订单是否到达终态且成交价可用；超时未终态或 0 价一律视为未确认。"""
        d = data or {}
        if str(d.get("status") or "") not in ("FILLED", "PARTIALLY_FILLED"):
            return False
        try:
            return float(d.get("avgPrice") or 0.0) > 0
        except (TypeError, ValueError):
            return False

    def _log_order(self, *, symbol: str, side: str, order_type: str, qty: float,
                   price: float | None, status: str, exchange_id: str | None = None,
                   raw: dict[str, Any] | None = None, client_id: str | None = None,
                   order_source: str = "ai", strategy_id: str | None = None,
                   ticket_id: str | None = None, margin_mode: str = "ISOLATED") -> str:
        cid = client_id or f"mta-{uuid.uuid4().hex[:28]}"
        if self.db:
            self.db.log_order(client_order_id=cid, exchange_order_id=exchange_id,
                              symbol=symbol, side=side, order_type=order_type, qty=qty,
                              price=price, status=status, raw=raw or {"mode": self.environment},
                              order_source=order_source, strategy_id=strategy_id,
                              ticket_id=ticket_id, margin_mode=margin_mode)
        return cid

    def _wait_fill(self, symbol: str, order_id: Any) -> dict[str, Any]:
        terminal = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}
        data = {}
        for _ in range(12):
            try:
                data = self._signed("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})
            except Exception:
                break
            if str(data.get("status") or "") in terminal:
                break
            time.sleep(0.5)
        return data

    def _order_trade_fee(self, symbol: str, order_id: Any) -> tuple[float | None, float | None]:
        """从成交流水取该订单的 realizedPnl 和手续费，用于精确对账。"""
        if order_id is None:
            return None, None
        try:
            rows = self._signed("GET", "/fapi/v1/userTrades", {
                "symbol": symbol, "orderId": order_id, "limit": 200,
            })
            if not rows:
                return None, None
            pnl = 0.0
            fee = 0.0
            for r in rows:
                pnl += float(r.get("realizedPnl") or 0.0)
                # userTrades 的 commission 是正数金额，按成本计为负数
                fee += -abs(float(r.get("commission") or 0.0))
            return pnl, fee
        except Exception:
            return None, None

    def _position_margin(self, symbol: str) -> float:
        try:
            pos = next((p for p in self.positions() if p["symbol"] == symbol), None)
            if pos:
                return float(pos.get("isolatedMargin") or pos.get("isolatedWallet") or 0.0)
        except Exception:
            pass
        return 0.0

    # ---------- 账户 ----------
    def _invalidate_cache(self) -> None:
        with self._cache_lock:
            self._account_cache = (0.0, {})
            self._positions_cache = (0.0, [])

    def account(self, force: bool = False) -> dict[str, Any]:
        if not force:
            with self._cache_lock:
                ts, data = self._account_cache
                if data and time.time() - ts < self.cache_ttl_seconds:
                    return data
        data = self._signed("GET", "/fapi/v2/account")
        if isinstance(data, dict) and data:
            with self._cache_lock:
                self._account_cache = (time.time(), data)
        return data

    def positions(self, force: bool = False) -> list[dict[str, Any]]:
        if not force:
            with self._cache_lock:
                ts, data = self._positions_cache
                if time.time() - ts < self.cache_ttl_seconds:
                    return list(data)
        data = self._signed("GET", "/fapi/v2/positionRisk")
        rows = [p for p in data if abs(float(p.get("positionAmt") or 0)) > 0]
        with self._cache_lock:
            self._positions_cache = (time.time(), rows)
        return rows

    def margin_used(self, channel: str | None = None) -> float:
        """按来源台账统计已用保证金；channel 为 strategy/ai/None。"""
        try:
            pos = self.positions()
            ledger = self._load_ledger().get("positions", {})
            total = 0.0
            for p in pos:
                source = str(ledger.get(p["symbol"], {}).get("source", "ai") or "ai")
                if channel is not None and source != channel:
                    continue
                total += float(p.get("isolatedMargin") or p.get("isolatedWallet") or 0.0)
            return total
        except Exception:
            return 0.0

    def available_cash(self) -> float:
        try:
            acct = self.account()
            return float(acct.get("availableBalance") or 0.0)
        except Exception as e:
            self.last_error = str(e)
            return 0.0

    def equity(self, prices: dict[str, float] | None = None) -> float:
        try:
            acct = self.account()
            return float(acct.get("totalMarginBalance") or acct.get("totalWalletBalance") or 0.0)
        except Exception:
            return 0.0

    @property
    def state(self) -> dict[str, Any]:
        last_err: Exception | None = None
        for _ in range(3):
            try:
                # 账户与持仓是两个独立只读接口，并行请求把 state 延迟减半
                # （代理环境下各需 1-2 秒，串行是页面卡顿主因之一）
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fut_acct = pool.submit(self.account)
                    fut_pos = pool.submit(self.positions)
                    acct = fut_acct.result()
                    pos = fut_pos.result()
                ledger = self._load_ledger()
                # 一次性拉取全部未触发 Algo 单，按 symbol 归集当前真实止损/止盈价，
                # 供 execution_feedback 回传给 LLM。之前这里硬编码 None，导致模型
                # 每轮都「看不到」已有止损，于是每轮都用当前价重新算一个 SL/TP 塞进
                # adjust intent，价格一跳就微调一次，最终表现为反复重设相同止损。
                algo_by_symbol: dict[str, dict[str, float]] = {}
                try:
                    for algo in self._open_algos(None):
                        sym = str(algo.get("symbol") or "")
                        otype = str(algo.get("orderType") or "")
                        if sym and otype in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                            try:
                                algo_by_symbol.setdefault(sym, {})[otype] = float(algo.get("triggerPrice") or 0.0)
                            except (TypeError, ValueError):
                                pass
                except Exception:
                    algo_by_symbol = {}
                return {
                    "cash": float(acct.get("availableBalance") or 0.0),
                    "initial_equity": float(acct.get("totalMarginBalance") or acct.get("totalWalletBalance") or 0.0),
                    # 钱包余额（不含浮盈亏）：给页面计算"已实现收益"用，
                    # 已实现 = totalWalletBalance - baseline，不会随行情抖动
                    "wallet_balance": float(acct.get("totalWalletBalance") or 0.0),
                    "exchange_upnl": float(acct.get("totalUnrealizedProfit") or 0.0),
                    "positions": [
                        {
                            "symbol": p["symbol"],
                            "side": "LONG" if float(p.get("positionAmt") or 0) > 0 else "SHORT",
                            "qty": abs(float(p.get("positionAmt") or 0)),
                            "entry_price": float(p.get("entryPrice") or 0),
                            "mark_price": float(p.get("markPrice") or 0),
                            "upnl": float(p.get("unRealizedProfit") or 0),
                            "leverage": int(float(p.get("leverage") or 1)),
                            "margin": float(p.get("isolatedMargin") or p.get("isolatedWallet") or 0.0),
                            "stop_loss": algo_by_symbol.get(p["symbol"], {}).get("STOP_MARKET"),
                            "take_profit": algo_by_symbol.get(p["symbol"], {}).get("TAKE_PROFIT_MARKET"),
                            "source": str(ledger.get("positions", {}).get(p["symbol"], {}).get("source", "ai") or "ai"),
                            "strategy_id": ledger.get("positions", {}).get(p["symbol"], {}).get("strategy_id"),
                            "ticket_id": ledger.get("positions", {}).get(p["symbol"], {}).get("ticket_id"),
                            "margin_mode": str(ledger.get("positions", {}).get(p["symbol"], {}).get("margin_mode", "ISOLATED") or "ISOLATED"),
                        }
                        for p in pos
                    ],
                }
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        return {"cash": 0.0, "initial_equity": 0.0, "positions": [], "error": str(last_err) if last_err else "unknown"}

    # ---------- 执行 ----------
    def execute(self, decision: TradeDecision, verdict: RiskVerdict,
                prices: dict[str, float], risk_config: dict[str, Any],
                db: AuditDatabase) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        # 每轮先同步本地 LIMIT 台账：成交→记来源+补挂SL/TP，过期→撤单。
        try:
            receipts.extend(self._sync_pending_limits())
        except Exception as e:
            if self.db:
                self.db.log_risk_event(event_type="futures_limit_sync_error", severity="warn",
                                       details=f"{type(e).__name__}: {e}",
                                       action_taken="continue cycle")
        if decision.decision == "no_trade" or verdict.verdict not in ("approved", "reduced"):
            return receipts
        intents = verdict.adjusted_intents if verdict.adjusted_intents else decision.intents
        for intent in intents:
            try:
                if getattr(intent, "client_order_id", None) and self.db:
                    old_order = self.db.fetch_one(
                        "SELECT status FROM orders WHERE client_order_id=?",
                        (str(intent.client_order_id),),
                    )
                    if old_order:
                        receipts.append({"order_id": intent.client_order_id, "action": "duplicate_skipped",
                                         "symbol": intent.symbol,
                                         "source": str(getattr(intent, "order_source", "ai") or "ai"),
                                         "existing_status": old_order["status"]})
                        continue
                action = getattr(intent, "action", None) or decision.decision
                if action == "open":
                    receipt = self._open(intent)
                elif action in ("close", "reduce"):
                    receipt = self._close_or_reduce(intent)
                elif action == "adjust":
                    receipt = self._adjust(intent)
                else:
                    continue
                if receipt:
                    receipts.append(receipt)
            except Exception as e:
                if self.db:
                    self.db.log_risk_event(event_type="futures_order_error", severity="error",
                                           details=f"{intent.symbol}: {type(e).__name__} {e}",
                                           action_taken="skip intent")
        return receipts

    def _ensure_protection_from_order(self, order: dict[str, Any]) -> None:
        """待成交限价单成交后补挂 SL/TP；失败则立即平仓，遵守强制初始保护规则。"""
        ok = self._resync_protection(
            str(order["symbol"]),
            stop_loss=order.get("stop_loss"),
            take_profit=order.get("take_profit"),
        )
        if ok:
            return
        try:
            close_intent = TradeIntent(
                symbol=str(order["symbol"]), action="close",
                side=str(order.get("side") or "LONG"),
                qty=float(order.get("qty") or 0.0),
                leverage=int(order.get("leverage") or 1),
                order_type="MARKET",
                order_source=str(order.get("source", "ai") or "ai"),
                strategy_id=order.get("strategy_id"),
                ticket_id=order.get("ticket_id"),
                margin_mode=str(order.get("margin_mode", "ISOLATED") or "ISOLATED"),
            )
            self._close_or_reduce(close_intent)
            if self.db:
                self.db.log_risk_event(event_type="futures_protection_close", severity="critical",
                                       details=f"{order['symbol']}: 限价单成交后SL/TP挂单失败，已立即平仓",
                                       action_taken="emergency_close")
        except Exception as e:
            if self.db:
                self.db.log_risk_event(event_type="futures_protection_close_error", severity="critical",
                                       details=f"{order['symbol']}: 限价单成交后保护失败且紧急平仓失败: "
                                               f"{type(e).__name__} {e}",
                                       action_taken="manual attention required")

    def _sync_pending_limits(self) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        ledger = self._load_ledger()
        pending = list(ledger.get("pending_limits", []))
        if not pending:
            return receipts
        now_ts = _parse_iso(utc_now())
        keep: list[dict[str, Any]] = []
        for order in pending:
            symbol = str(order.get("symbol") or "")
            cid = str(order.get("client_order_id") or "")
            exchange_id = order.get("exchange_order_id")
            try:
                if exchange_id:
                    data = self._signed("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": exchange_id})
                else:
                    data = self._signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
            except Exception as e:
                msg = str(e)
                if "-2013" in msg or "Order does not exist" in msg:
                    keep.append(order)
                else:
                    keep.append(order)
                continue

            status = str(data.get("status") or "")
            original_qty = float(data.get("origQty") or order.get("qty") or 0.0)
            executed_qty = float(data.get("executedQty") or 0.0)
            avg_price = float(data.get("avgPrice") or data.get("price") or 0.0)

            if executed_qty > 0:
                # 部分或全部成交：先把真实仓位记入来源台账并补保护
                self._record_ledger_position(
                    symbol,
                    str(order.get("source", "ai") or "ai"),
                    order.get("strategy_id"),
                    order.get("ticket_id"),
                    str(order.get("margin_mode", "ISOLATED") or "ISOLATED"),
                )
                if order.get("ticket_id") and self.db:
                    self.db.update_signal_ticket(ticket_id=str(order["ticket_id"]), status="filled")
                self._ensure_protection_from_order(order)
                remaining = max(0.0, original_qty - executed_qty)
                order["qty"] = remaining
                receipts.append({"order_id": cid, "action": "limit_partial_or_filled",
                                 "symbol": symbol, "qty": executed_qty, "price": avg_price,
                                 "source": order.get("source", "ai")})

            terminal = status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED")
            if terminal:
                if self.db:
                    self.db.update_order(client_order_id=cid, status=status, price=avg_price or None)
                if status == "FILLED" and order.get("ticket_id") and self.db:
                    self.db.update_signal_ticket(ticket_id=str(order["ticket_id"]), status="filled")
                continue

            # 尚未结束：检查 AI 设置的过期时间，到期主动撤单
            expires = _parse_iso(order.get("expires_at"))
            if expires is not None and expires <= now_ts:
                try:
                    self._signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
                except Exception:
                    # 撤单失败必须留在台账里，下一轮继续处理；否则可能成交后无 SL/TP
                    keep.append(order)
                    continue
                if self.db:
                    self.db.update_order(client_order_id=cid, status="CANCELED")
                receipts.append({"order_id": cid, "action": "cancel_expired_limit",
                                 "symbol": symbol, "source": order.get("source", "ai")})
                continue

            order["qty"] = max(0.0, original_qty - executed_qty)
            if order["qty"] > 0:
                keep.append(order)
        ledger["pending_limits"] = keep
        self._save_ledger(ledger)
        return receipts

    def _open(self, intent: Any) -> dict[str, Any] | None:
        symbol = intent.symbol
        if self._symbol_filters and symbol not in self._symbol_filters:
            if self.db:
                self.db.log_risk_event(event_type="futures_unsupported_symbol", severity="warn",
                                       details=f"{symbol} 不在当前交易所支持列表中，跳过开仓",
                                       action_taken="skip")
            return None
        margin_mode = str(getattr(intent, "margin_mode", "ISOLATED") or "ISOLATED").upper()
        try:
            self._signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_mode})
        except Exception:
            pass
        try:
            self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(intent.leverage)})
        except Exception as e:
            # 逐币种杠杆上限不同（BTC 125x，山寨常为 20x/50x），AI 的 25x 强信心档会撞上限：
            # 降档到交易所实际允许的最大杠杆重试；仍失败则放弃本单，避免以错误杠杆静默成交。
            max_lev = self._symbol_max_leverage(symbol)
            target = int(intent.leverage)
            if max_lev and max_lev > 0:
                target = min(target, max_lev)
                try:
                    self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": target})
                    if self.db:
                        self.db.log_risk_event(
                            event_type="futures_leverage_downgraded", severity="warn",
                            details=f"{symbol}: 请求杠杆 {intent.leverage}x 超过交易所上限 {max_lev}x，已降档为 {target}x",
                            action_taken="downgrade_leverage")
                except Exception as e2:
                    if self.db:
                        self.db.log_risk_event(
                            event_type="futures_leverage_error", severity="critical",
                            details=f"{symbol}: 设置杠杆失败（请求 {intent.leverage}x，降档 {target}x 仍失败）: {type(e2).__name__} {e2}",
                            action_taken="skip order")
                    return None
            else:
                if self.db:
                    self.db.log_risk_event(
                        event_type="futures_leverage_error", severity="critical",
                        details=f"{symbol}: 设置杠杆失败且无法查询上限: {type(e).__name__} {e}",
                        action_taken="skip order")
                return None
        total_qty = float(intent.qty)
        side = "BUY" if intent.side == "LONG" else "SELL"

        # MARKET 单有单笔最大数量限制，超过时分多笔下单
        if intent.order_type == "MARKET":
            max_qty = self._market_max_qty(symbol)
        else:
            max_qty = float("inf")
        chunks: list[float] = []
        remaining = total_qty
        while remaining > 1e-12:
            chunk = min(remaining, max_qty)
            chunks.append(chunk)
            remaining -= chunk
        if not chunks:
            return None

        first_cid = None
        first_exchange_id = None
        total_margin = 0.0
        order_count = 0
        fill_unconfirmed = False
        base_cid = getattr(intent, "client_order_id", None) or f"mta-{uuid.uuid4().hex[:28]}"
        for chunk in chunks:
            qty = self._format_qty(symbol, chunk)
            qty_dec = Decimal(qty)
            min_qty = self._min_qty(symbol)
            if qty_dec <= 0 or (min_qty > 0 and qty_dec < min_qty):
                if self.db:
                    self.db.log_risk_event(
                        event_type="futures_min_qty_skip", severity="warn",
                        details=f"{symbol} 名义数量 {chunk} 格式化后为 {qty}，低于最小下单量 {min_qty}，跳过下单",
                        action_taken="skip")
                continue
            cid = base_cid if len(chunks) == 1 else f"{base_cid}-{order_count}"
            order_params: dict[str, Any] = {
                "symbol": symbol, "side": side, "type": intent.order_type, "quantity": qty,
                "newClientOrderId": cid, "reduceOnly": "false",
            }
            if intent.order_type == "LIMIT":
                limit_price = float(getattr(intent, "limit_price", 0.0) or 0.0)
                if limit_price <= 0:
                    raise ValueError(f"{symbol} LIMIT 单缺少有效 limit_price")
                order_params["price"] = self._format_price(symbol, limit_price)
                order_params["timeInForce"] = "GTC"
            data = self._signed("POST", "/fapi/v1/order", order_params)
            if intent.order_type == "MARKET":
                data = self._wait_fill(symbol, data.get("orderId")) or data
            chunk_confirmed = self._fill_confirmed(data)
            fill_unconfirmed = fill_unconfirmed or not chunk_confirmed
            price = float(data.get("avgPrice") or data.get("price") or 0.0)
            margin = self._position_margin(symbol)
            total_margin = margin
            order_count += 1
            if first_cid is None:
                first_cid = cid
                first_exchange_id = data.get("orderId")
            raw = dict(data or {})
            raw.update({"mode": self.environment, "action": "open",
                        "trade_level": getattr(intent, "trade_level", "short"), "margin": margin,
                        "order_source": getattr(intent, "order_source", "ai"),
                        "strategy_id": getattr(intent, "strategy_id", None),
                        "ticket_id": getattr(intent, "ticket_id", None),
                        "margin_mode": margin_mode,
                        "limit_price": getattr(intent, "limit_price", None),
                        "limit_expires_at": getattr(intent, "limit_expires_at", None),
                        "fill_confirmed": chunk_confirmed,
                        "chunk_index": order_count, "chunk_total": len(chunks)})
            self._log_order(symbol=symbol, side=intent.side, order_type=intent.order_type,
                            qty=float(qty), price=price,
                            status=str(data.get("status") or "NEW"),
                            exchange_id=str(data.get("orderId") or ""), raw=raw, client_id=cid,
                            order_source=str(getattr(intent, "order_source", "ai") or "ai"),
                            strategy_id=getattr(intent, "strategy_id", None),
                            ticket_id=getattr(intent, "ticket_id", None),
                            margin_mode=margin_mode)
        if first_cid is None:
            self._invalidate_cache()
            return None
        self._invalidate_cache()
        order_source = str(getattr(intent, "order_source", "ai") or "ai")
        if intent.order_type == "MARKET":
            self._record_ledger_position(
                symbol, order_source,
                getattr(intent, "strategy_id", None),
                getattr(intent, "ticket_id", None),
                str(getattr(intent, "margin_mode", "ISOLATED") or "ISOLATED"),
            )
            if self.db and getattr(intent, "ticket_id", None):
                self.db.update_signal_ticket(ticket_id=str(intent.ticket_id), status="filled")
        else:
            # LIMIT：挂到交易所后进入本地 pending 台账，由 _sync_pending_limits 监控成交/过期。
            ledger = self._load_ledger()
            ledger.setdefault("pending_limits", []).append({
                "client_order_id": first_cid,
                "exchange_order_id": str(first_exchange_id or ""),
                "symbol": symbol,
                "side": intent.side,
                "qty": float(total_qty),
                "limit_price": float(getattr(intent, "limit_price", 0.0) or 0.0),
                "leverage": int(intent.leverage),
                "stop_loss": intent.stop_loss,
                "take_profit": intent.take_profit,
                "source": order_source,
                "strategy_id": getattr(intent, "strategy_id", None),
                "ticket_id": getattr(intent, "ticket_id", None),
                "margin_mode": str(getattr(intent, "margin_mode", "ISOLATED") or "ISOLATED"),
                "trade_level": getattr(intent, "trade_level", "short"),
                "expires_at": getattr(intent, "limit_expires_at", None),
                "created_at": utc_now(),
            })
            self._save_ledger(ledger)
        protection_ok = self._place_protection(symbol, intent)
        if not protection_ok and intent.order_type == "MARKET"                 and (getattr(intent, "stop_loss", None) or getattr(intent, "take_profit", None)):
            # 硬规则：初始止盈/止损必须挂上，否则立即平掉刚开的仓位。
            try:
                close_intent = TradeIntent(
                    symbol=symbol, action="close", side=intent.side,
                    qty=float(total_qty), leverage=int(intent.leverage),
                    order_type="MARKET", order_source=str(getattr(intent, "order_source", "ai") or "ai"),
                    strategy_id=getattr(intent, "strategy_id", None),
                    ticket_id=getattr(intent, "ticket_id", None),
                    margin_mode=str(getattr(intent, "margin_mode", "ISOLATED") or "ISOLATED"),
                )
                self._close_or_reduce(close_intent)
                if self.db:
                    self.db.log_risk_event(event_type="futures_protection_close", severity="critical",
                                           details=f"{symbol}: 初始SL/TP挂单失败，已立即平仓",
                                           action_taken="emergency_close")
            except Exception as e:
                if self.db:
                    self.db.log_risk_event(event_type="futures_protection_close_error", severity="critical",
                                           details=f"{symbol}: 保护单失败且紧急平仓失败: {type(e).__name__} {e}",
                                           action_taken="manual attention required")
        if fill_unconfirmed and self.db:
            self.db.log_risk_event(
                event_type="futures_fill_unconfirmed", severity="warn",
                details=f"{symbol}: 开仓单在等待窗口内未确认完全成交，回执数量可能高估，待下轮对账",
                action_taken="flag for reconcile")
        return {"order_id": first_cid, "action": "open", "symbol": symbol, "side": intent.side,
                "qty": float(total_qty), "exchange_order_id": first_exchange_id,
                "order_count": order_count, "fill_unconfirmed": fill_unconfirmed}

    def _place_algo_protection(self, symbol: str, close_side: str, qty: str,
                              order_type: str, trigger_price: float,
                              tag: str, log_side: str) -> None:
        """通过 Algo Order API 挂止损/止盈，解决 -4120 问题。

        经实测，Demo 环境下 STOP_MARKET / TAKE_PROFIT_MARKET 不能走普通 /fapi/v1/order，
        必须走 POST /fapi/v1/algoOrder：
          algotype=CONDITIONAL
          type=STOP_MARKET / TAKE_PROFIT_MARKET
          triggerprice=触发价
        """
        cid = f"mta-{tag}-{uuid.uuid4().hex[:28]}"
        data = self._signed("POST", "/fapi/v1/algoOrder", {
            "symbol": symbol,
            "side": close_side,
            "type": order_type,
            "quantity": qty,
            "triggerprice": self._format_price(symbol, trigger_price),
            "algotype": "CONDITIONAL",
            "reduceonly": "true",
            "timeinforce": "GTC",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
        })
        algo_id = str(data.get("algoId") or "")
        self._log_order(symbol=symbol, side=log_side, order_type=order_type,
                        qty=float(qty), price=float(trigger_price), status="NEW",
                        exchange_id=algo_id,
                        raw={"mode": self.environment, "action": "algo_protection",
                             "algoType": "CONDITIONAL", "algoId": algo_id}, client_id=cid)

    def _open_algos(self, symbol: str | None = None) -> list[dict[str, Any]]:
        try:
            data = self._signed("GET", "/fapi/v1/openAlgoOrders", {})
        except Exception:
            return []
        if symbol is None:
            return data or []
        return [o for o in (data or []) if o.get("symbol") == symbol]

    def _cancel_symbol_algos(self, symbol: str) -> None:
        for algo in self._open_algos(symbol):
            algo_id = str(algo.get("algoId") or "")
            try:
                self._signed("DELETE", "/fapi/v1/algoOrder", {"algoId": algo.get("algoId")})
                # 撤单成功：把本地流水里对应的保护单收口成 CANCELED，
                # 避免「移动止损反复重设」在 orders 表堆积一堆永远 NEW 的幽灵单。
                if self.db and algo_id:
                    self.db.cancel_order_by_exchange_id(exchange_order_id=algo_id)
            except Exception as e:
                msg = str(e)
                # 已经触发/已不存在的旧单无需处理，也不算错误
                if "-2011" in msg or "Unknown order" in msg:
                    # 旧单已不在交易所（已触发或已被外部撤销），本地流水同样要收口。
                    if self.db and algo_id:
                        self.db.cancel_order_by_exchange_id(exchange_order_id=algo_id)
                    continue
                if self.db:
                    self.db.log_risk_event(event_type="futures_adjust_error", severity="warn",
                                           details=f"{symbol}: 取消Algo单失败 {type(e).__name__} {e}",
                                           action_taken="继续")

    def _algo_prices(self, symbol: str) -> dict[str, float]:
        prices: dict[str, float] = {}
        for algo in self._open_algos(symbol):
            otype = str(algo.get("orderType") or "")
            if otype in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                try:
                    prices[otype] = float(algo.get("triggerPrice") or 0.0)
                except (TypeError, ValueError):
                    pass
        return prices

    def _resync_protection(self, symbol: str, intent: Any = None,
                           stop_loss: float | None = None,
                           take_profit: float | None = None) -> bool:
        """统一同步止损/止盈：只在数量或价格变化时重挂，避免每轮刷大量重复 Algo 单。"""
        existing = self._open_algos(symbol)
        old_prices = self._algo_prices(symbol)

        pos = next((p for p in self.positions() if p["symbol"] == symbol), None)
        if not pos or abs(float(pos.get("positionAmt") or 0)) <= 0:
            if existing:
                self._cancel_symbol_algos(symbol)
            return True

        qty = self._format_qty(symbol, abs(float(pos["positionAmt"])))
        close_side = "SELL" if float(pos.get("positionAmt") or 0) > 0 else "BUY"

        sl = stop_loss
        if sl is None and intent is not None and getattr(intent, "stop_loss", None) is not None:
            sl = float(intent.stop_loss)
        if sl is None:
            sl = old_prices.get("STOP_MARKET")

        tp = take_profit
        if tp is None and intent is not None and getattr(intent, "take_profit", None) is not None:
            tp = float(intent.take_profit)
        if tp is None:
            tp = old_prices.get("TAKE_PROFIT_MARKET")

        # 价格/数量比较用容差而非精确等值：交易所返回的 triggerPrice 与本地 round(8)
        # 精度可能不同，qty 浮点尾数也会漂移。若用 1e-12 精确等值，价格每跳一个 tick
        # 或数量尾数变化就误判「不一致」，触发撤旧挂新，表现为反复重设相同止损止盈。
        tick = self._tick_size(symbol)
        price_tol = max(tick * 0.5, 1e-8)
        qty_eps = max(self._min_qty(symbol), Decimal("1e-8"))
        qty_f = float(qty)

        def _matches(otype: str, target: float | None) -> int:
            n = 0
            for a in existing:
                if a.get("orderType") != otype:
                    continue
                try:
                    qty_ok = abs(float(a.get("quantity") or 0) - qty_f) <= float(qty_eps)
                    price_ok = abs(float(a.get("triggerPrice") or 0) - float(target)) <= price_tol
                    if qty_ok and price_ok:
                        n += 1
                except (TypeError, ValueError):
                    continue
            return n

        stop_total = sum(1 for a in existing if a.get("orderType") == "STOP_MARKET")
        take_total = sum(1 for a in existing if a.get("orderType") == "TAKE_PROFIT_MARKET")
        stop_ok = (not sl and stop_total == 0) or (sl is not None and stop_total == 1 and _matches("STOP_MARKET", sl) == 1)
        take_ok = (not tp and take_total == 0) or (tp is not None and take_total == 1 and _matches("TAKE_PROFIT_MARKET", tp) == 1)

        if stop_ok and take_ok:
            return True

        # 有变化/重复单：全部取消后按当前仓位重挂
        self._cancel_symbol_algos(symbol)
        failed = False
        if sl:
            try:
                self._place_algo_protection(symbol, close_side, qty, "STOP_MARKET", sl, "sl", "STOP")
            except Exception as e:
                failed = True
                if self.db:
                    self.db.log_risk_event(event_type="futures_sl_failed", severity="critical",
                                           details=f"{symbol}: {type(e).__name__} {e}",
                                           action_taken="protection_failed")
        if tp:
            try:
                self._place_algo_protection(symbol, close_side, qty, "TAKE_PROFIT_MARKET", tp, "tp", "TAKE")
            except Exception as e:
                failed = True
                if self.db:
                    self.db.log_risk_event(event_type="futures_tp_failed", severity="critical",
                                           details=f"{symbol}: {type(e).__name__} {e}",
                                           action_taken="protection_failed")
        return not failed

    def _place_protection(self, symbol: str, intent: Any) -> bool:
        return self._resync_protection(symbol, intent=intent)

    def _market_max_qty(self, symbol: str) -> float:
        filters = self._symbol_filters.get(symbol, {})
        try:
            return float(
                filters.get("MARKET_LOT_SIZE", {}).get("maxQty")
                or filters.get("LOT_SIZE", {}).get("maxQty")
                or 5000
            )
        except (TypeError, ValueError):
            return 5000.0

    def _close_or_reduce(self, intent: Any) -> dict[str, Any] | None:
        symbol = intent.symbol
        pos = next((p for p in self.positions() if p["symbol"] == symbol), None)
        if not pos:
            if self.db:
                self.db.log_risk_event(event_type="futures_close_nopos", severity="warn",
                                       details=f"{symbol} 无仓位可平")
            return None
        current = abs(float(pos.get("positionAmt") or 0))
        close_qty = min(float(intent.qty), current)
        pos_side = "LONG" if float(pos.get("positionAmt") or 0) > 0 else "SHORT"
        side = "SELL" if pos_side == "LONG" else "BUY"
        entry = float(pos.get("entryPrice") or 0.0)
        ledger_source, ledger_meta = self._ledger_source(symbol)
        source = str(getattr(intent, "order_source", ledger_source) or ledger_source or "ai")

        # MARKET 单有单笔最大数量限制（例如 SKR 是 5000），超过时分多笔平仓
        max_qty = self._market_max_qty(symbol) if intent.order_type == "MARKET" else float("inf")
        chunks: list[float] = []
        remaining = close_qty
        while remaining > 1e-12:
            chunk = min(remaining, max_qty)
            chunks.append(chunk)
            remaining -= chunk
        if not chunks:
            return None

        first_cid = None
        total_pnl = 0.0
        total_filled = 0.0
        order_count = 0
        close_unconfirmed = False
        base_cid = getattr(intent, "client_order_id", None) or f"mta-{uuid.uuid4().hex[:28]}"
        for chunk in chunks:
            qty = self._format_qty(symbol, chunk)
            cid = base_cid if len(chunks) == 1 else f"{base_cid}-{order_count}"
            data = self._signed("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": side, "type": intent.order_type,
                "quantity": qty, "reduceOnly": "true", "newClientOrderId": cid,
            })
            if intent.order_type == "MARKET":
                data = self._wait_fill(symbol, data.get("orderId")) or data
            chunk_confirmed = self._fill_confirmed(data)
            close_unconfirmed = close_unconfirmed or not chunk_confirmed
            close_price = float(data.get("avgPrice") or data.get("price") or 0.0)
            if pos_side == "LONG":
                pnl = chunk * (close_price - entry)
            else:
                pnl = chunk * (entry - close_price)
            exact_pnl, commission = self._order_trade_fee(symbol, data.get("orderId"))
            if exact_pnl is not None:
                pnl = exact_pnl
            fee = commission if commission is not None else 0.0
            total_pnl += pnl
            total_filled += chunk
            order_count += 1
            if first_cid is None:
                first_cid = cid
            raw = dict(data or {})
            raw.update({"mode": self.environment, "action": "close",
                        "trade_level": getattr(intent, "trade_level", "short"),
                        "margin": self._position_margin(symbol), "pnl": pnl,
                        "commission": fee, "net_pnl": pnl + fee,
                        "fill_confirmed": chunk_confirmed,
                        "order_source": source,
                        "strategy_id": ledger_meta.get("strategy_id"),
                        "ticket_id": ledger_meta.get("ticket_id"),
                        "chunk_index": order_count, "chunk_total": len(chunks)})
            self._log_order(symbol=symbol, side=intent.side, order_type=intent.order_type,
                            qty=float(qty), price=close_price,
                            status=str(data.get("status") or "NEW"),
                            exchange_id=str(data.get("orderId") or ""), raw=raw, client_id=cid,
                            order_source=source,
                            strategy_id=ledger_meta.get("strategy_id"),
                            ticket_id=ledger_meta.get("ticket_id"),
                            margin_mode=str(ledger_meta.get("margin_mode", "ISOLATED") or "ISOLATED"))

        if close_unconfirmed and self.db:
            self.db.log_risk_event(
                event_type="futures_fill_unconfirmed", severity="warn",
                details=f"{symbol}: 平仓单在等待窗口内未确认完全成交，盈亏与数量可能失真，待下轮对账",
                action_taken="flag for reconcile")
        self._invalidate_cache()
        # 同步来源台账：剩余仓位继续保留来源；清仓则移除
        remaining_pos = next((x for x in self.positions(force=True) if x["symbol"] == symbol), None)
        remaining_qty = abs(float(remaining_pos.get("positionAmt") or 0)) if remaining_pos else 0.0
        if remaining_qty <= 0:
            self._remove_ledger_position(symbol)
        else:
            self._record_ledger_position(
                symbol, source,
                ledger_meta.get("strategy_id"),
                ledger_meta.get("ticket_id"),
                str(ledger_meta.get("margin_mode", "ISOLATED") or "ISOLATED"),
            )
        self._resync_protection(symbol, intent=intent)
        return {"order_id": first_cid, "action": "close", "symbol": symbol,
                "side": pos_side, "qty": float(close_qty), "pnl": total_pnl,
                "order_count": order_count, "fill_unconfirmed": close_unconfirmed}

    def _adjust(self, intent: Any) -> dict[str, Any] | None:
        symbol = intent.symbol
        pos = next((p for p in self.positions() if p["symbol"] == symbol), None)
        if not pos or abs(float(pos.get("positionAmt") or 0)) <= 0:
            if self.db:
                self.db.log_risk_event(event_type="futures_adjust_skip", severity="info",
                                       details=f"{symbol} 无仓位，无法调整止损/止盈", action_taken="skip")
            return {"action": "adjust", "symbol": symbol, "note": "no_position"}

        # 统一先取消该 symbol 所有旧 Algo 单，再按当前仓位和价格重新挂
        self._resync_protection(symbol, intent=intent)

        if self.db:
            self.db.log_risk_event(event_type="futures_adjust", severity="info",
                                   details=f"{symbol} 已自动调整止损/止盈："
                                           f"SL={intent.stop_loss}, TP={intent.take_profit}",
                                   action_taken="algo_order")
        return {"action": "adjust", "symbol": symbol,
                "stop_loss": intent.stop_loss, "take_profit": intent.take_profit}

    def close_all(self, prices: dict[str, float], db: AuditDatabase) -> list[dict[str, Any]]:
        receipts = []
        closed_symbols: list[str] = []
        ledger = self._load_ledger()
        for pos in self.positions():
            symbol = pos["symbol"]
            source = str(ledger.get("positions", {}).get(symbol, {}).get("source", "ai") or "ai")
            meta = ledger.get("positions", {}).get(symbol, {}) or {}
            amt = abs(float(pos.get("positionAmt") or 0))
            if amt <= 0:
                continue
            side = "SELL" if float(pos.get("positionAmt") or 0) > 0 else "BUY"
            entry = float(pos.get("entryPrice") or 0.0)
            try:
                max_qty = self._market_max_qty(symbol)
                chunks: list[float] = []
                remaining = amt
                while remaining > 1e-12:
                    chunk = min(remaining, max_qty)
                    chunks.append(chunk)
                    remaining -= chunk
                first_cid = None
                total_pnl = 0.0
                order_count = 0
                close_all_unconfirmed = False
                for chunk in chunks:
                    qty = self._format_qty(symbol, chunk)
                    cid = f"mta-{uuid.uuid4().hex[:28]}"
                    data = self._signed("POST", "/fapi/v1/order", {
                        "symbol": symbol, "side": side, "type": "MARKET",
                        "quantity": qty, "reduceOnly": "true", "newClientOrderId": cid,
                    })
                    data = self._wait_fill(symbol, data.get("orderId")) or data
                    chunk_confirmed = self._fill_confirmed(data)
                    close_all_unconfirmed = close_all_unconfirmed or not chunk_confirmed
                    close_price = float(data.get("avgPrice") or data.get("price") or 0.0)
                    if float(pos.get("positionAmt") or 0) > 0:
                        pnl = chunk * (close_price - entry)
                    else:
                        pnl = chunk * (entry - close_price)
                    exact_pnl, commission = self._order_trade_fee(symbol, data.get("orderId"))
                    if exact_pnl is not None:
                        pnl = exact_pnl
                    fee = commission if commission is not None else 0.0
                    total_pnl += pnl
                    order_count += 1
                    if first_cid is None:
                        first_cid = cid
                    raw = dict(data or {})
                    raw.update({"mode": self.environment, "action": "close_all", "margin": 0.0,
                                "pnl": pnl, "commission": fee, "net_pnl": pnl + fee,
                                "fill_confirmed": chunk_confirmed,
                                "chunk_index": order_count, "chunk_total": len(chunks)})
                    self._log_order(symbol=symbol, side="CLOSE", order_type="MARKET", qty=float(qty),
                                    price=close_price,
                                    status=str(data.get("status") or "NEW"),
                                    exchange_id=str(data.get("orderId") or ""), raw=raw, client_id=cid,
                                    order_source=source,
                                    strategy_id=meta.get("strategy_id"),
                                    ticket_id=meta.get("ticket_id"),
                                    margin_mode=str(meta.get("margin_mode", "ISOLATED") or "ISOLATED"))
                self._cancel_symbol_algos(symbol)
                closed_symbols.append(symbol)
                receipts.append({"order_id": first_cid, "action": "close_all", "symbol": symbol,
                                 "qty": float(amt), "pnl": total_pnl, "order_count": order_count,
                                 "fill_unconfirmed": close_all_unconfirmed})
            except Exception as e:
                if self.db:
                    self.db.log_risk_event(event_type="futures_close_all_error", severity="critical",
                                           details=f"{symbol}: {type(e).__name__} {e}",
                                           action_taken="manual attention required")
        self._invalidate_cache()
        # 只清掉确实平掉的 symbol；失败/未完成的仓位保留台账，下一轮继续追
        for symbol in closed_symbols:
            ledger.get("positions", {}).pop(symbol, None)
        ledger["pending_limits"] = [o for o in ledger.get("pending_limits", [])
                                    if str(o.get("symbol")) not in closed_symbols]
        self._save_ledger(ledger)
        return receipts

    def close_source(self, source: str, prices: dict[str, float],
                     db: AuditDatabase) -> list[dict[str, Any]]:
        """只平掉指定来源（strategy/ai）的仓位，供通道保证金超限强平使用。"""
        receipts: list[dict[str, Any]] = []
        ledger = self._load_ledger().get("positions", {})
        for p in self.positions():
            symbol = p["symbol"]
            if str(ledger.get(symbol, {}).get("source", "ai") or "ai") != source:
                continue
            amt = abs(float(p.get("positionAmt") or 0))
            if amt <= 0:
                continue
            side = "LONG" if float(p.get("positionAmt") or 0) > 0 else "SHORT"
            intent = TradeIntent(
                symbol=symbol, action="close", side=side, qty=amt,
                leverage=int(float(p.get("leverage") or 1)), order_type="MARKET",
                order_source=source,
                strategy_id=ledger.get(symbol, {}).get("strategy_id"),
                ticket_id=ledger.get(symbol, {}).get("ticket_id"),
                margin_mode=str(ledger.get(symbol, {}).get("margin_mode", "ISOLATED") or "ISOLATED"),
            )
            try:
                receipt = self._close_or_reduce(intent)
                if receipt:
                    receipts.append(receipt)
            except Exception as e:
                if self.db:
                    self.db.log_risk_event(event_type="futures_close_source_error", severity="critical",
                                           details=f"{symbol}: {type(e).__name__} {e}",
                                           action_taken="manual attention required")
        return receipts

    def sync_protections(self) -> None:
        """快循环：逐仓位核对 SL/TP Algo 单，缺失/数量不一致时自动重挂。"""
        try:
            for p in self.positions():
                self._resync_protection(str(p["symbol"]))
        except Exception as e:
            if self.db:
                self.db.log_risk_event(event_type="futures_sync_error", severity="warn",
                                       details=f"sync_protections: {type(e).__name__} {e}",
                                       action_taken="skip fast protection sync")

    def sync_positions(self) -> None:
        try:
            pos = self.positions()
            ledger = self._load_ledger()
            exchange_symbols = {p["symbol"] for p in pos}
            # 交易所里有、台账没有的旧仓位归为 AI legacy，避免误判成无主仓位
            for p in pos:
                if p["symbol"] not in ledger.get("positions", {}):
                    self._record_ledger_position(
                        p["symbol"], "ai", None, None, "ISOLATED")
                    if self.db:
                        self.db.log_risk_event(
                            event_type="futures_ledger_adopt_legacy", severity="warn",
                            details=f"{p['symbol']} 交易所已有仓位但来源台账缺失，已归为 ai legacy",
                            action_taken="ledger_adopt_ai")
            # 重新加载台账，避免上面的逐 symbol 保存被旧快照覆盖
            ledger = self._load_ledger()
            # 台账里有、交易所已经没有了：说明仓位已在系统视野之外被平掉
            closed_symbols = []
            for symbol in list(ledger.get("positions", {}).keys()):
                if symbol not in exchange_symbols:
                    ledger.get("positions", {}).pop(symbol, None)
                    closed_symbols.append(symbol)
            self._save_ledger(ledger)
            if closed_symbols:
                self._reconcile_external_closes(closed_symbols)
            if self.db:
                # 只在仓位真正变化（新开/加减仓/改杠杆/换来源）时才写流水，
                # 避免每个慢循环都重复插入同样的 OPEN 行撑爆表格。
                #
                # 注意 GROUP BY 必须带 side：本系统跑的是双向持仓（Hedge mode），
                # 同一个 symbol 可以同时有多头和空头。若只按 symbol 取 MAX(id)，
                # 子查询每个 symbol 只会返回一行（某一侧），而下面的判据又要求
                # prev.side == side——于是 LONG/SHORT 交替时永远有一侧匹配不上，
                # 每个慢循环都会为「匹配不上的那一侧」再插一条 OPEN，
                # 行 id 呈 251/253/255 与 252/254/256 交替递增，表被撑爆。
                last_rows: dict[tuple[str, str], dict[str, Any]] = {}
                for row in self.db.fetch_all(
                    "SELECT id, symbol, side, qty, entry_price, leverage, source, status "
                    "FROM positions WHERE id IN (SELECT MAX(id) FROM positions GROUP BY symbol, side)"
                ):
                    last_rows[(str(row["symbol"]), str(row["side"]))] = dict(row)
                # 本轮在交易所实际存在的 (symbol, side)，用于判断哪些旧 OPEN 行该收口
                live_keys: set[tuple[str, str]] = set()
                for p in pos:
                    amt = float(p.get("positionAmt") or 0)
                    if abs(amt) <= 1e-12:
                        continue
                    side = "LONG" if amt > 0 else "SHORT"
                    live_keys.add((p["symbol"], side))
                for p in pos:
                    amt = float(p.get("positionAmt") or 0)
                    meta = ledger.get("positions", {}).get(p["symbol"], {}) or {}
                    side = "LONG" if amt > 0 else "SHORT"
                    entry = round(float(p.get("entryPrice") or 0), 8)
                    qty = abs(amt)
                    leverage = int(float(p.get("leverage") or 1))
                    source = str(meta.get("source", "ai") or "ai")
                    prev = last_rows.get((p["symbol"], side))
                    if (prev and str(prev.get("status")) == "OPEN"
                            and str(prev.get("side")) == side
                            and abs(float(prev.get("qty") or 0) - qty) < 1e-12
                            and abs(float(prev.get("entry_price") or 0) - entry) < 1e-9
                            and int(prev.get("leverage") or 0) == leverage
                            and str(prev.get("source") or "ai") == source):
                        continue
                    # 仓位状态/数量/价格发生变化：先精确收口该 (symbol, side) 的旧 OPEN 行，
                    # 再插新的 OPEN 流水，避免「只 INSERT 不 CLOSE」堆积幽灵台账。
                    if prev and str(prev.get("status")) == "OPEN":
                        self.db.close_open_position(symbol=p["symbol"], side=side,
                                                    note="仓位变化，收口旧快照")
                    self.db.log_position(
                        symbol=p["symbol"],
                        side=side,
                        qty=qty,
                        entry_price=entry,
                        leverage=leverage,
                        stop_loss=None, take_profit=None,
                        status="OPEN",
                        source=source,
                        strategy_id=meta.get("strategy_id"),
                        ticket_id=meta.get("ticket_id"),
                        margin_mode=str(meta.get("margin_mode", "ISOLATED") or "ISOLATED"),
                    )
                # 交易所里已经消失的 (symbol, side)：其 OPEN 行也应收口
                for key, prev in last_rows.items():
                    if key in live_keys or str(prev.get("status")) != "OPEN":
                        continue
                    self.db.close_open_position(symbol=key[0], side=key[1],
                                                note="交易所已无此仓位，收口")
        except Exception as e:
            if self.db:
                self.db.log_risk_event(event_type="futures_sync_error", severity="warn",
                                       details=f"{type(e).__name__} {e}", action_taken="skip")

    # 一次最多补记几个 symbol：userTrades 是 weight 20 的重接口，demo 环境经代理可达
    # 十几秒，慢循环里必须设上限，否则一次同步就要卡上好几分钟。
    _RECONCILE_MAX_SYMBOLS = 3

    def _reconcile_external_closes(self, symbols: list[str]) -> None:
        """补记在系统视野之外发生的平仓。

        为什么需要：sync_positions 发现"台账有、交易所没了"，说明仓位已经被平掉，
        但系统自己没下过这笔单——保护单在交易所触发、或用户在交易所手动平仓，
        都不会走本地下单流程，orders 表里自然一片空白，
        页面"最近订单"也就永远看不到这笔平仓，看起来像"系统漏记了"。

        做法：回查交易所成交流水，把那笔平仓成交补进 orders。
        **只有查到真实成交才写**，查不到（接口慢/超时）就跳过，下次慢循环再试，
        绝不凭空估算一条记录出来。
        """
        if not self.db:
            return
        for symbol in list(symbols)[: self._RECONCILE_MAX_SYMBOLS]:
            try:
                self._reconcile_one_close(symbol)
            except Exception as e:
                try:
                    self.db.log_risk_event(
                        event_type="futures_close_reconcile_error", severity="warn",
                        details=f"{symbol}: 外部平仓补记失败 {type(e).__name__} {e}",
                        action_taken="skip")
                except Exception:
                    pass

    def _reconcile_one_close(self, symbol: str) -> None:
        """补记单个 symbol 最近一笔未被记录的平仓成交。"""
        rows = self.db.fetch_all(
            "SELECT id, side, qty, leverage FROM positions "
            "WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (symbol,))
        if not rows:
            return
        prev = dict(rows[0])
        open_side = str(prev.get("side") or "").upper()
        if open_side not in ("LONG", "SHORT"):
            return
        # 平多要卖出、平空要买回，方向与原仓位相反
        want_side = "SELL" if open_side == "LONG" else "BUY"

        # attempts=1：demo 环境这个接口经代理很慢，重试两次能把慢循环拖到几十秒
        trades = self._signed("GET", "/fapi/v1/userTrades",
                              {"symbol": symbol, "limit": 20}, attempts=1) or []
        if not isinstance(trades, list):
            return

        hit = None
        for t in trades:  # userTrades 默认按时间倒序，最新在前
            if str(t.get("side") or "").upper() != want_side:
                continue
            oid = str(t.get("orderId") or "")
            if not oid:
                continue
            # 去重：这笔成交已经记过（系统自己下的单）就不再补
            if self.db.fetch_all("SELECT id FROM orders WHERE exchange_order_id=? LIMIT 1", (oid,)):
                continue
            hit = t
            break
        if not hit:
            return

        oid = str(hit.get("orderId") or "")
        qty = abs(float(hit.get("qty") or 0.0))
        price = float(hit.get("price") or 0.0)
        realized = float(hit.get("realizedPnl") or 0.0)
        commission = abs(float(hit.get("commission") or 0.0))
        leverage = int(float(prev.get("leverage") or 1)) or 1

        self.db.log_order(
            client_order_id=f"mta-sync-{symbol}-{oid}",
            exchange_order_id=oid,
            symbol=symbol,
            # side 传原仓位方向，配合 raw.action='close'，
            # _enrich_orders 会据此显示成"平多"/"平空"
            side=open_side,
            order_type="MARKET",
            qty=qty,
            price=price,
            status="FILLED",
            raw={
                "action": "close",
                "net_pnl": realized - commission,
                "commission": -commission,
                "margin": round(qty * price / leverage, 8),
                "reconciled": True,
                "note": "保护单触发或交易所手动平仓，由成交流水补记",
            },
            order_source="exchange_sync",
            margin_mode="ISOLATED",
        )
        self.db.log_risk_event(
            event_type="futures_close_reconciled", severity="info",
            details=f"{symbol} {open_side} {qty} @ {price} 在交易所被平仓，已由成交流水补记",
            action_taken="order_reconciled")

    def close(self) -> None:
        self._client.close()
