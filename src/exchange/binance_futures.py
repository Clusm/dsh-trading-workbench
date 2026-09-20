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
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any, Mapping

import httpx

from agents.models import RiskVerdict, TradeDecision, TradeIntent
from audit.database import AuditDatabase, utc_now
from core.contracts import decimal_from_float
from data.binance_transport import endpoint_group
from data.binance_request_policy import parse_retry_after, policy_for
from data.rate_limit import (
    ExecutionRateLimited,
    PublicMarketCooldown,
    note_418,
    note_rate_limit,
    observe_failure,
    observe_response,
    reserve_request,
)
from exchange.order_outbox import OrderOutbox
from exchange.protection_commands import (
    ProtectionSyncReasons,
    defers_flat_close,
    execute_protection_plan,
    plan_protection_refresh,
    protection_view_from_algo,
    real_position_from_snapshot,
)
from exchange.protection_mirror import ProtectionOrderMirror
from exchange.protocol import normalize_algo_orders_payload, parse_iso as _parse_iso, parse_iso_ms
from exchange.trade_reconciliation import (
    TradeReconciliationBook,
    account_ref_source,
    account_reference,
    normalize_execution_fill,
    select_close_fills,
)
from concurrent.futures import ThreadPoolExecutor


def _request_purpose(method: str, path: str, params: dict[str, Any]) -> str:
    """按请求语义分配优先级；不能把所有签名请求笼统标成 critical。"""
    normalized_method = str(method or "GET").upper()
    normalized_path = str(path or "").split("?", 1)[0]
    if normalized_path in {"/fapi/v1/time", "/fapi/v1/exchangeInfo"}:
        return "startup"
    if normalized_path in {"/fapi/v1/account", "/fapi/v2/account", "/fapi/v3/account",
                           "/fapi/v1/positionRisk", "/fapi/v2/positionRisk",
                           "/fapi/v1/positionSide/dual",
                           # U01：收入流水是账户对账输入，不是普通请求；缺此分支会被
                           # 判成 normal，预算/优先级与语义不符（P3-8）。
                           "/fapi/v1/income"}:
        return "account_reconcile"
    if normalized_path in {"/fapi/v1/userTrades"}:
        return "trade_reconcile"
    if normalized_path in {"/fapi/v1/openOrders", "/fapi/v1/order"}:
        position_side = str(params.get("positionSide") or "").upper()
        order_side = str(params.get("side") or "").upper()
        hedge_exit = ((position_side == "LONG" and order_side == "SELL") or
                      (position_side == "SHORT" and order_side == "BUY"))
        return "order_reconcile" if normalized_method == "GET" else (
            "exit" if params.get("reduceOnly") or params.get("closePosition") or hedge_exit
            else "entry")
    if normalized_path in {"/fapi/v1/openAlgoOrders", "/fapi/v1/algoOrder"}:
        return "protection_reconcile" if normalized_method == "GET" else "protect"
    if normalized_path in {"/fapi/v1/marginType", "/fapi/v1/leverage",
                           "/fapi/v1/leverageBracket"}:
        return "setup"
    if normalized_path == "/fapi/v1/listenKey":
        return "user_stream"
    return "normal"


def _exchange_bool(value: Any) -> bool:
    """兼容 Binance 在不同接口中返回的 bool、数字与字符串布尔值。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _decimal_text(value: Any) -> str | None:
    """把数量/价格转成十进制文本；非有限值返回 None（绝不写脏文本列）。"""
    try:
        parsed = decimal_from_float(value)
    except Exception:  # noqa: BLE001 - 文本列只是尽力精确，不能因此中断下单
        return None
    if not parsed.is_finite():
        return None
    return format(parsed.normalize(), "f") if parsed != 0 else "0"


def _exchange_decimal(value: Any) -> Any:
    """交易所来源字段 -> Decimal；字符串/整数直接解析，浮点显式转换。

    ``normalize_algo_orders_payload`` 原样透传 JSON 值，所以 ``quantity``/
    ``positionAmt``/``triggerPrice`` 可能是 JSON 数字。``protection_view_from_algo``
    与 ``real_position_from_snapshot`` 对浮点一律硬拒，若不在这里显式转换，只要
    交易所返回数字型字段就会让**整轮保护刷新**抛错（V1 的回归点）。
    """
    return decimal_from_float(value) if isinstance(value, float) else value


def assess_takeover_compatibility(
    position_mode: Mapping[str, Any] | None,
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    algo_orders: list[dict[str, Any]],
    *,
    local_entry_client_ids: set[str] | None = None,
) -> dict[str, Any]:
    """纯函数评估实盘接管是否兼容，绝不修改交易所或本地账本。

    当前执行器的来源台账以 ``symbol`` 为唯一键，因此允许账户保持 Hedge Mode，
    但同一合约出现 LONG/SHORT 双边仓位时必须在任何撤单或补保护前 fail-closed。
    外部增仓挂单不能被 ``close_only`` 阻止成交，必须保留为显式上线阻断项；
    系统自己的本地 LIMIT 挂单通过 client id 排除。
    """
    payload = dict(position_mode or {})
    mode_known = "dualSidePosition" in payload
    hedge_mode = _exchange_bool(payload.get("dualSidePosition")) if mode_known else None
    explicit_sides = sorted({
        str(row.get("positionSide") or "").upper()
        for row in positions
        if str(row.get("positionSide") or "").upper() in {"LONG", "SHORT"}
    })
    sides_by_symbol: dict[str, set[str]] = {}
    malformed_hedge_symbols: set[str] = set()
    for row in positions:
        symbol = str(row.get("symbol") or "").upper()
        side = str(row.get("positionSide") or "").upper()
        if not symbol:
            continue
        if hedge_mode and side not in {"LONG", "SHORT"}:
            malformed_hedge_symbols.add(symbol)
        elif side in {"LONG", "SHORT"}:
            sides_by_symbol.setdefault(symbol, set()).add(side)
    duplicate_hedge_symbols = sorted(
        symbol for symbol, sides in sides_by_symbol.items() if len(sides) > 1
    )
    # 账户可以保持 Hedge Mode，但当前系统只支持“每个 symbol 最多一个方向”。
    # 这样所有本地来源账本仍能以 symbol 为键，同时订单必须携带 positionSide。
    position_mode_supported = bool(
        mode_known
        and not malformed_hedge_symbols
        and not duplicate_hedge_symbols
        and (hedge_mode or not explicit_sides)
    )
    local_ids = {str(value) for value in (local_entry_client_ids or set()) if value}

    unmanaged_entries: list[dict[str, Any]] = []
    for kind, rows in (("order", orders), ("algo", algo_orders)):
        for row in rows:
            if _exchange_bool(row.get("reduceOnly")) or _exchange_bool(row.get("closePosition")):
                continue
            client_id = str(
                row.get("clientOrderId") or row.get("origClientOrderId")
                or row.get("clientAlgoId") or ""
            )
            if client_id and client_id in local_ids:
                continue
            unmanaged_entries.append({
                "kind": kind,
                "symbol": str(row.get("symbol") or ""),
                "side": str(row.get("side") or ""),
                "position_side": str(row.get("positionSide") or "BOTH"),
                "type": str(row.get("type") or row.get("orderType") or ""),
                "client_id": client_id,
            })

    reasons: list[str] = []
    if not mode_known:
        reasons.append("无法证明账户持仓模式")
    elif malformed_hedge_symbols:
        reasons.append("Hedge Mode 仓位缺少明确 positionSide")
    elif duplicate_hedge_symbols:
        reasons.append("同一合约存在 LONG/SHORT 双边仓位，当前执行器禁止接管")
    elif not hedge_mode and explicit_sides:
        reasons.append("账户持仓模式与仓位 positionSide 不一致")
    if unmanaged_entries:
        symbols = ", ".join(sorted({row["symbol"] for row in unmanaged_entries if row["symbol"]}))
        reasons.append(f"存在 {len(unmanaged_entries)} 个未纳管增仓挂单" +
                       (f"（{symbols}）" if symbols else ""))
    return {
        "ok": position_mode_supported and not unmanaged_entries,
        "position_mode": "hedge" if hedge_mode else "one_way" if mode_known else "unknown",
        "position_mode_supported": position_mode_supported,
        "explicit_position_sides": explicit_sides,
        "duplicate_hedge_symbols": duplicate_hedge_symbols,
        "unmanaged_entry_orders": unmanaged_entries,
        "reason": "；".join(reasons),
    }


class BinanceFuturesBroker:
    """币安 USD-M 合约边界，封装签名请求、订单幂等、持仓台账与保护单。

    Broker 只接受交易所返回的合约过滤规则，并通过 Outbox 在 POST 前持久化意图。
    网络结果不确定时不重复提交；开仓来源台账用于 strategy/ai 通道归因。该类会进行真实
    网络和订单操作，测试必须使用临时状态与替身，不能连接生产凭据。
    """
    mode = "testnet"

    def __init__(self, api_key: str, api_secret: str,
                 base_url: str = "https://demo-fapi.binance.com",
                 db: AuditDatabase | None = None, environment: str = "demo",
                 ledger_file: Path | None = None,
                 protection_mirror_enabled: bool = False,
                 declared_account_ref: str | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.db = db
        self.order_outbox = OrderOutbox(db, environment=environment) if db is not None else None
        if self.order_outbox is not None:
            self.order_outbox.recover_stale_submitting()
        # U04：最近一轮保护同步的失败类别。必须**按标的**保存、并在每轮同步入口复位
        # （``ProtectionSyncReasons.begin``）：否则某一标的/某一轮的 QUERY_FAILED 会
        # 残留下来，把之后真实的保护缺口误判成观测故障而抑制兜底平仓（保留裸仓）。
        self._protection_sync = ProtectionSyncReasons()
        self.mode = environment
        self.environment = environment
        # U02/U15：账户引用**只在这一处派生一次**，账本/生命周期/K03 收入幂等键与
        # 决策身份共用同一个值。声明了账户时它等于 ``<env>:<declared>`` 且与密钥
        # 轮换解耦；未声明时仍是旧密钥摘要形态，由 ``account_ref_source`` 标记为
        # ``legacy-key-digest`` 供启动自检发现（U15/B_r1 P1-4）。
        self.account_ref = account_reference(
            api_key, environment, declared_account_ref=declared_account_ref)
        self.account_ref_source = account_ref_source(
            api_key, environment, declared_account_ref=declared_account_ref)
        self.trade_reconciliation = (
            TradeReconciliationBook(
                db, environment=environment, account_ref=self.account_ref,
            ) if db is not None else None
        )
        self.ledger_file = Path(ledger_file) if ledger_file is not None else (
            Path(__file__).resolve().parents[2] / "data" / "futures_source_ledger.json")
        self.ledger_file.parent.mkdir(parents=True, exist_ok=True)
        # 看板会周期性读取账户；30 秒超时叠加重试会把一次请求拖到 60 秒以上。
        # 读请求采用较短超时，失败后由看板保留最近有效快照。
        self._client_timeout = httpx.Timeout(12.0, connect=5.0)
        # MTA_NO_PROXY 不只清环境变量，还要禁用 httpx 的系统代理发现；
        # 否则 Windows 的遗留代理/PAC 仍会被读取，直连启动照样卡在账户查询。
        self._trust_env = os.getenv("MTA_NO_PROXY", "").strip().lower() not in {"1", "true", "yes", "on"}
        # 客户端重建锁：dashboard 线程（紧急平仓/资金重置）与交易/同步循环
        # 必须同时覆盖“发请求”和“关闭+重建”：只锁重建仍可能关闭其他线程正在
        # 使用的共享句柄，继而产生 Cannot send a request, as the client has been closed。
        self._client_lock = threading.RLock()
        self._client = self._new_client()
        self._symbol_filters: dict[str, dict[str, Any]] = {}
        self._load_exchange_info()
        self.protection_order_mirror = (
            ProtectionOrderMirror(
                db, environment=environment, allowed_symbols=self._symbol_filters.keys())
            if db is not None and protection_mirror_enabled else None)
        self.last_error: str | None = None
        # 账户/持仓短缓存：页面和决策循环共用，减少 Binance 请求与限流
        self.cache_ttl_seconds = 2.0
        self._cache_lock = threading.RLock()
        self._account_cache: tuple[float, dict[str, Any]] = (0.0, {})
        self._positions_cache: tuple[float, list[dict[str, Any]]] = (0.0, [])
        # None 表示尚未用签名接口证明账户持仓模式。任何真实下单前都会惰性读取；
        # takeover_compatibility 在启动核验阶段会先填充该值。
        self._hedge_mode: bool | None = None
        # 来源台账锁：dashboard 线程（紧急平仓/资金重置）与交易循环（挂单/平仓收口）
        # 并发 load-modify-save 之前会互相覆盖丢更新。RLock 允许锁内重入调用
        # _record/_remove 等辅助方法。所有 ledger 读→改→写必须全程持锁。
        self._ledger_lock = threading.RLock()
        # 保护单兜底比例：adopt 的无主仓位按标记价 ±比例补挂初始 SL/TP
        self._FALLBACK_SL_PCT = 0.02
        self._FALLBACK_TP_PCT = 0.04
        # 保护单同步串行化：快循环 sync_protections（service 持 _protection_lock）
        # 与慢循环执行节点开仓后的 _place_protection 双线程并发调用
        # _resync_protection，会同时"撤旧挂新"产生瞬时重复挂单/多余 critical 告警。
        # RLock 允许同线程重入（sync 循环内逐 symbol 调用），跨线程串行。
        self._resync_lock = threading.RLock()
        # 服务器时钟偏移（毫秒）：代理环境本机时钟漂移 >5s 时，固定 recvWindow
        # 会让全部签名请求撞 -1021。启动时用 /fapi/v1/time 校准，之后每次签名
        # 都加上偏移；校准失败（离线）时退回 0 并放宽 recvWindow 兼容小漂移。
        self._time_offset_ms = 0
        self._calibrate_time()

    # ---------- 基础设施 ----------
    def _new_client(self) -> httpx.Client:
        return httpx.Client(timeout=self._client_timeout, trust_env=self._trust_env)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """串行使用共享客户端，并在传输层故障后原子替换连接池。

        Broker 的签名读写需要严格顺序与幂等保证，牺牲少量并发度换取不会在另一个
        请求执行期间关闭连接池。公开行情仍走独立客户端，不受此锁影响。
        """
        with self._client_lock:
            request_id: str | None = None
            response_received = False
            try:
                parsed = urllib.parse.urlparse(url)
                params = kwargs.get("params") if isinstance(kwargs.get("params"), dict) else {}
                policy = policy_for(
                    method, parsed.path, params,
                    purpose=_request_purpose(method, parsed.path, params),
                    environment=str(getattr(self, "environment", "live") or "live"),
                    # 离线传输测试使用 example.invalid 的虚拟路径；真实 Binance
                    # 主机必须严格登记，不能用 fallback 逃过发布检查。
                    strict="binance" in str(parsed.hostname or "").lower(),
                )
                # 只有生产 fapi 与公共行情共用出口 IP，必须在发送前加入同一预算/冷却
                # 临界区。Demo/Testnet 走独立主机，保持原有模拟盘行为不受公共池影响。
                if parsed.hostname == "fapi.binance.com":
                    request_id = reserve_request(
                        parsed.path, weight=policy.ip_weight,
                        priority=policy.priority, endpoint_group=endpoint_group(parsed.path),
                        source="signed_execution",
                        environment=str(getattr(self, "environment", "live") or "live"),
                        purpose=policy.purpose,
                    )
                response = self._client.request(method, url, **kwargs)
                response_received = True
                # 只有生产 USD-M 执行与公开行情共享 fapi.binance.com 的 IP 权重池；
                # demo/testnet 属于独立环境，不能用它们的响应头覆盖生产公开预算。
                if parsed.hostname == "fapi.binance.com":
                    path = parsed.path
                    retry_after = None
                    if response.status_code == 418:
                        retry_after = parse_retry_after(
                            response.headers, default=480.0,
                            maximum=604800.0, minimum=60.0)
                    elif response.status_code == 429:
                        retry_after = parse_retry_after(
                            response.headers, default=60.0,
                            maximum=3600.0, minimum=5.0)
                    observe_response(
                        response.headers, path=path,
                        weight=policy.ip_weight, request_id=request_id,
                        status=response.status_code,
                        retry_after_seconds=retry_after,
                        error_code=response.status_code if response.status_code >= 400 else None,
                        error_message=getattr(response, "text", None),
                    )
                    if response.status_code == 418:
                        note_418(retry_after)
                    elif response.status_code == 429:
                        note_rate_limit(retry_after, status=429)
                if response.status_code in {418, 429}:
                    raise ExecutionRateLimited(
                        f"HTTP {response.status_code}: signed Binance request rate limited"
                    )
                return response
            except httpx.TransportError as exc:
                if request_id is not None and not response_received:
                    observe_failure(
                        request_id, path=parsed.path, weight=policy.ip_weight,
                        error_type=type(exc).__name__, error_message=exc,
                    )
                old_client = self._client
                self._client = self._new_client()
                try:
                    old_client.close()
                except Exception:
                    pass
                raise

    def _calibrate_time(self) -> None:
        """用交易所服务器时间校准本机时钟偏移（P2 recvWindow）。失败静默，不影响启动。"""
        try:
            r = self._request("GET", f"{self.base_url}/fapi/v1/time")
            server_ms = int(r.json().get("serverTime") or 0)
            if server_ms > 0:
                self._time_offset_ms = server_ms - int(time.time() * 1000)
        except Exception:
            self._time_offset_ms = 0

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
            # 每次尝试重新生成 timestamp/signature，避免重试必撞 recvWindow；
            # timestamp 按启动时校准的服务器时钟偏移修正
            # 必须先删除上一轮 signature，否则第二轮会把旧签名本身也参与 HMAC，
            # 稳定触发 Binance -1022。
            params.pop("signature", None)
            params["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            params["recvWindow"] = 5000
            params["signature"] = self._sign(params)
            try:
                if method not in {"GET", "POST", "DELETE"}:
                    raise ValueError(f"unsupported method {method}")
                r = self._request(
                    method, f"{self.base_url}{path}", params=params, headers=headers
                )
                if r.status_code >= 500:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                # demo 环境 DELETE algoOrder 成功时返回 code='200'（字符串）。
                # 直接与 int 200 比较恒不等，把成功当失败抛错（实测每轮
                # adjust 平仓保护单产生 10+ 次 futures_adjust_error 误报）。
                if isinstance(data, dict) and data.get("code") and int(data["code"]) != 200:
                    raise RuntimeError(f"Binance {data.get('code')}: {data.get('msg')}")
                return data
            except Exception as e:
                last_err = e
                if isinstance(e, (PublicMarketCooldown, ExecutionRateLimited)):
                    raise
                msg = str(e)
                # -1021(时间戳超窗)/-1022(签名无效，多由时间戳过期引起)：
                # 重校准服务器时间偏移后重试（仅 GET 会进入第二次循环；
                # 实测校准偏移随时间漂移 5ms/小时，长期运行后会零星失败）
                if "-1021" in msg or "-1022" in msg:
                    self._calibrate_time()
                if i < max_attempts - 1:
                    time.sleep(1.0)
        raise last_err if last_err else RuntimeError("request failed")

    @property
    def protection_sync(self) -> ProtectionSyncReasons:
        """按标的保存的保护同步失败类别；惰性创建。

        惰性而不是只在 ``__init__`` 里赋值：单元测试与离线回放会用 ``__new__``
        构造实例（绕开真实客户端/网络），此时该属性必须**依然由生产代码自己产生**
        失败类别，而不是让测试注入一个"本轮新鲜"的内部字段。
        """
        reasons = self.__dict__.get("_protection_sync")
        if reasons is None:
            reasons = ProtectionSyncReasons()
            self.__dict__["_protection_sync"] = reasons
        return reasons

    def _load_exchange_info(self) -> None:
        try:
            data = self._request(
                "GET", f"{self.base_url}/fapi/v1/exchangeInfo", timeout=20
            ).json()
            for s in data.get("symbols", []):
                if s.get("status") != "TRADING" or not s.get("symbol", "").endswith("USDT"):
                    continue
                self._symbol_filters[s["symbol"]] = {f["filterType"]: f for f in s.get("filters", [])}
        except Exception as e:
            self.last_error = f"exchangeInfo: {type(e).__name__} {e}"

    def _load_ledger(self) -> dict[str, Any]:
        with self._ledger_lock:
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
        # tmp 文件名带 pid+线程 id：即使跨进程（诊断脚本）并发写也不会
        # 互相踩同一个 .tmp 导致写坏文件；os.replace 保证读到的一定是完整 JSON。
        tmp = self.ledger_file.with_name(
            f"{self.ledger_file.stem}.tmp-{os.getpid()}-{threading.get_ident()}")
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
                                margin_mode: str = "ISOLATED",
                                qty: float | None = None,
                                entry: float | None = None,
                                open_client_order_id: str | None = None,
                                position_side: str | None = None) -> dict[str, Any]:
        """把交易所真实仓位快照写入来源台账（纯本地写，全程持台账锁）。

        qty/entry 由调用方提供（多数调用点手上已有仓位快照）。之前这里内部
        调 positions(force=True)：锁内嵌套网络请求既放大延迟也制造锁序风险，
        现在彻底移出，网络请求由调用方在进锁前完成。
        """
        with self._ledger_lock:
            ledger = self._load_ledger()
            positions = ledger.setdefault("positions", {})
            old = positions.get(symbol) or {}
            if qty is not None and qty <= 0:
                positions.pop(symbol, None)
            else:
                positions[symbol] = {
                    "symbol": symbol,
                    "source": source,
                    "strategy_id": strategy_id or old.get("strategy_id"),
                    "ticket_id": ticket_id or old.get("ticket_id"),
                    "open_client_order_id": (open_client_order_id
                                             or old.get("open_client_order_id")),
                    "position_side": (str(position_side or old.get("position_side") or "BOTH")
                                      .upper()),
                    "qty": float(qty if qty is not None else (old.get("qty") or 0.0)),
                    "entry_price": float(entry if entry is not None else (old.get("entry_price") or 0.0)),
                    "margin_mode": margin_mode,
                    "updated_at": utc_now(),
                }
            self._save_ledger(ledger)
            return dict(positions.get(symbol) or {})

    def _remove_ledger_position(self, symbol: str) -> None:
        with self._ledger_lock:
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

    def _step_size(self, symbol: str) -> Decimal | None:
        """读取 LOT_SIZE.stepSize；生命周期归零判定不能用经验百分比。"""
        filters = self._symbol_filters.get(symbol, {})
        value = filters.get("LOT_SIZE", {}).get("stepSize")
        try:
            step = Decimal(str(value))
            return step if step > 0 else None
        except (TypeError, ValueError, ArithmeticError):
            return None

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

    def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        """按clientOrderId强制查询普通订单；UNKNOWN命令只能走此入口恢复。"""
        data = self._signed("GET", "/fapi/v1/order", {
            "symbol": symbol, "origClientOrderId": client_order_id,
        }, attempts=2)
        return dict(data or {}) if isinstance(data, dict) else {}

    def query_algo_order(self, client_algo_id: str) -> dict[str, Any]:
        """按 clientAlgoId 查询保护单，供响应丢失后的幂等恢复使用。"""
        data = self._signed("GET", "/fapi/v1/algoOrder", {
            "clientAlgoId": client_algo_id,
        }, attempts=2)
        return dict(data or {}) if isinstance(data, dict) else {}

    def _submit_algo_order(self, params: dict[str, Any], *, purpose: str) -> dict[str, Any]:
        """Algo保护单唯一POST入口：先落Outbox，结果未知时按clientAlgoId查询。"""
        request = dict(params)
        client_id = str(request.get("clientAlgoId") or "")
        if not client_id:
            raise ValueError("clientAlgoId is required for algo outbox submission")
        symbol = str(request.get("symbol") or "").upper()
        side = str(request.get("side") or "").upper()
        outbox = getattr(self, "order_outbox", None)
        if outbox is not None:
            outbox.prepare(
                client_order_id=client_id, symbol=symbol, side=side,
                position_side=str(request.get("positionSide") or "BOTH"),
                order_type=str(request.get("type") or "UNKNOWN"), purpose=purpose,
                qty=float(request.get("quantity") or 0.0),
                price=float(request.get("triggerPrice", request.get("triggerprice")))
                if request.get("triggerPrice", request.get("triggerprice")) is not None else None,
                reduce_only=_exchange_bool(
                    request.get("reduceOnly", request.get("reduceonly", False)))
                or _exchange_bool(request.get("closePosition")),
                endpoint="/fapi/v1/algoOrder", request=request,
                command_kind="ALGO",
                # 文本列从十进制转换而来，不用 str(float)：str(0.1+0.2) 的尾差会
                # 直接写进"精确"列，勾稽结果不再可复核（U04/DC04）。
                qty_text=(_decimal_text(request["quantity"])
                          if request.get("quantity") is not None else None),
            )
        try:
            data = self._signed("POST", "/fapi/v1/algoOrder", request)
        except Exception as exc:
            definite_rejection = str(exc).startswith("Binance ") and "HTTP 5" not in str(exc)
            if outbox is not None:
                if definite_rejection:
                    outbox.reject(client_id, exc)
                else:
                    outbox.mark_unknown(client_id, exc)
            if definite_rejection:
                raise
            try:
                reconciled = self.query_algo_order(client_id)
            except Exception:
                reconciled = {}
            if not reconciled.get("algoId"):
                raise
            data = reconciled
            if self.db:
                self.db.log_risk_event(
                    event_type="futures_algo_submit_reconciled", severity="warn",
                    details=(f"{symbol}: Algo POST异常后按clientAlgoId对账成功 "
                             f"(algoId={data.get('algoId')})"),
                    action_taken="outbox reconciled; no repost")
        result = dict(data or {}) if isinstance(data, dict) else {}
        if outbox is not None:
            outbox.acknowledge(client_id, result)
        return result

    def _submit_standard_order(self, params: dict[str, Any], *, purpose: str) -> dict[str, Any]:
        """普通USD-M订单的唯一POST入口：先写Outbox，异常后只查询不重发。"""
        request = dict(params)
        client_id = str(request.get("newClientOrderId") or "")
        if not client_id:
            raise ValueError("newClientOrderId is required for outbox submission")
        symbol = str(request.get("symbol") or "").upper()
        side = str(request.get("side") or "").upper()
        outbox = getattr(self, "order_outbox", None)
        if outbox is not None:
            outbox.prepare(
                client_order_id=client_id, symbol=symbol, side=side,
                position_side=str(request.get("positionSide") or "BOTH"),
                order_type=str(request.get("type") or "UNKNOWN"), purpose=purpose,
                qty=float(request.get("quantity") or 0.0),
                price=float(request["price"]) if request.get("price") is not None else None,
                reduce_only=str(request.get("reduceOnly", "false")).lower() == "true",
                endpoint="/fapi/v1/order", request=request,
                command_kind="STANDARD",
                qty_text=(_decimal_text(request["quantity"])
                          if request.get("quantity") is not None else None),
            )
        try:
            data = self._signed("POST", "/fapi/v1/order", request)
        except Exception as exc:
            definite_rejection = str(exc).startswith("Binance ") and "HTTP 5" not in str(exc)
            if outbox is not None:
                if definite_rejection:
                    outbox.reject(client_id, exc)
                else:
                    outbox.mark_unknown(client_id, exc)
            if definite_rejection:
                raise
            try:
                reconciled = self.query_order(symbol, client_id)
            except Exception:
                reconciled = {}
            if not reconciled.get("orderId"):
                raise
            data = reconciled
            if self.db:
                self.db.log_risk_event(
                    event_type="futures_order_submit_reconciled", severity="warn",
                    details=(f"{symbol}: POST异常后按clientOrderId对账成功 "
                             f"(orderId={data.get('orderId')})"),
                    action_taken="outbox reconciled; no repost")
        result = dict(data or {}) if isinstance(data, dict) else {}
        if outbox is not None:
            outbox.acknowledge(client_id, result)
        return result

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

    def fetch_income_page(self, *, income_type: str | None = None, start_ms: int,
                          end_ms: int, page: int = 1, limit: int = 1000) -> list[dict[str, Any]]:
        """官方 Get Income History 单页只读读取（U01）。

        官方 ``startTime``/``endTime`` 均为**包含**端点；内部半开区间 ``[start,end)``
        必须映射为 ``endTime = end - 1ms``。返回原始行交给
        ``exchange.income_reconciliation`` 做规范化/幂等/分页判定，本方法不排序、
        不去重、不写库、不改任何交易状态。
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("income limit 必须在 1..1000")
        if int(end_ms) <= int(start_ms):
            raise ValueError("income window 必须是正区间")
        params: dict[str, Any] = {
            "startTime": int(start_ms),
            "endTime": int(end_ms) - 1,
            "page": max(1, int(page)),
            "limit": int(limit),
        }
        if income_type:
            params["incomeType"] = str(income_type).upper()
        data = self._signed("GET", "/fapi/v1/income", params)
        return [dict(row) for row in (data or [])]

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
            reconciler = getattr(self, "trade_reconciliation", None)
            if reconciler is not None:
                try:
                    reconciler.ingest(rows, source="rest_order_fee")
                except Exception as exc:
                    if self.db:
                        self.db.log_risk_event(
                            event_type="trade_fill_ingest_error", severity="warn",
                            details=f"{symbol} orderId={order_id}: 成交入库失败 {type(exc).__name__}",
                            action_taken="retain raw response; retry reconciliation")
            pnl = 0.0
            fee = 0.0
            for r in rows:
                pnl += float(r.get("realizedPnl") or 0.0)
                # userTrades 的 commission 是正数金额，按成本计为负数
                fee += -abs(float(r.get("commission") or 0.0))
            return pnl, fee
        except Exception:
            return None, None

    def _record_trade_outcome(
        self, *, outcome_id: str, symbol: str, side: str, source: str,
        entry_price: float, exit_price: float, qty: float, net_pnl: float | None,
        exit_reason: str, status: str = "CLOSED", ledger_meta: dict[str, Any] | None = None,
        opened_at: str | None = None, closed_at: str | None = None,
        lifecycle_id: str | None = None, environment: str | None = None,
        release_id: str | None = None, attribution_status: str = "UNKNOWN",
        cost_status: str = "UNKNOWN", actual_closed_at: str | None = None,
        gross_realized: float | None = None, entry_fee: float | None = None,
        exit_fee: float | None = None, funding_cashflow: float | None = None,
        allow_legacy_fallback: bool = True,
    ) -> None:
        """把币安已确认的平仓成交连回原始开仓论点。

        只由确认成交路径调用；网络结果不确定时宁可等待后续交易所流水对账，也不把
        估算结果写入策略评价。优先使用来源台账保存的开仓 clientOrderId 精确绑定，
        旧仓位缺少该字段时才退回同标的、同方向、同来源的最近开仓意图。
        """
        if not self.db or qty <= 0 or entry_price <= 0 or exit_price <= 0:
            return
        meta = dict(ledger_meta or {})
        try:
            open_client_id = str(meta.get("open_client_order_id") or "")
            row = (self.db.fetch_open_intent_by_client_order_id(open_client_id)
                   if open_client_id else None)
            if row is None and allow_legacy_fallback:
                row = self.db.fetch_latest_open_intent(symbol, side, source)

            raw: dict[str, Any] = {}
            if row is not None and row["raw_json"]:
                try:
                    parsed = json.loads(row["raw_json"])
                    raw = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw = {}
            opened = opened_at or (str(row["created_at"]) if row is not None else None)
            holding_minutes = None
            opened_dt = _parse_iso(opened)
            effective_closed_at = closed_at or utc_now()
            closed_dt = _parse_iso(effective_closed_at)
            if opened_dt is not None and closed_dt is not None:
                holding_minutes = max(0, int((closed_dt - opened_dt).total_seconds() // 60))
            notional = entry_price * qty
            self.db.log_trade_outcome(
                outcome_id=outcome_id,
                intent_id=row["intent_id"] if row is not None else None,
                decision_id=row["decision_id"] if row is not None else None,
                symbol=symbol,
                side=side,
                source=source,
                strategy_id=raw.get("strategy_id") or meta.get("strategy_id"),
                ticket_id=raw.get("ticket_id") or meta.get("ticket_id"),
                confidence=raw.get("confidence"),
                entry_price=entry_price,
                exit_price=exit_price,
                qty=qty,
                pnl=net_pnl,
                pnl_pct=(net_pnl / notional * 100.0)
                if net_pnl is not None and notional else None,
                holding_minutes=holding_minutes,
                expected_holding_minutes=raw.get("expected_holding_minutes"),
                invalidation=row["invalidation"] if row is not None else None,
                exit_reason=exit_reason,
                status=status,
                opened_at=opened,
                closed_at=effective_closed_at,
                lifecycle_id=lifecycle_id,
                environment=environment or str(getattr(self, "environment", "unknown") or "unknown"),
                release_id=release_id or meta.get("release_id"),
                attribution_status=attribution_status,
                cost_status=cost_status,
                actual_closed_at=actual_closed_at or effective_closed_at,
                recorded_at=utc_now(),
                gross_realized=gross_realized,
                entry_fee=entry_fee,
                exit_fee=exit_fee,
                funding_cashflow=funding_cashflow,
            )
        except Exception as exc:
            self.db.log_risk_event(
                event_type="outcome_log_error", severity="warn",
                details=f"{symbol}: {type(exc).__name__}: {exc}",
                action_taken="skip outcome; retry through reconciliation",
            )

    def _position_margin(self, symbol: str) -> float:
        try:
            pos = self._single_symbol_position(symbol)
            if pos:
                return self._margin_from_position(pos)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _margin_from_position(position: Mapping[str, Any]) -> float:
        """统一估算仓位初始保证金，覆盖 isolated 与 cross 两种返回结构。"""
        for key in ("isolatedMargin", "isolatedWallet", "positionInitialMargin"):
            try:
                value = float(position.get(key) or 0.0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                continue
        try:
            leverage = max(1.0, float(position.get("leverage") or 1.0))
            notional = abs(float(position.get("notional") or 0.0))
            if notional <= 0:
                qty = abs(float(position.get("positionAmt") or 0.0))
                mark = float(position.get("markPrice") or position.get("entryPrice") or 0.0)
                notional = qty * mark
            return notional / leverage
        except (TypeError, ValueError):
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

    def open_orders(self) -> list[dict[str, Any]]:
        """强制读取当前普通 USD-M 挂单，供启动和周期性最终一致性校验。"""
        data = self._signed("GET", "/fapi/v1/openOrders")
        return list(data or []) if isinstance(data, list) else []

    @staticmethod
    def _normalize_algo_orders_payload(data: Any) -> list[dict[str, Any]]:
        """兼容 Algo 列表的裸数组和 code/msg/data 包装，未知结构必须报错。"""
        return normalize_algo_orders_payload(data)

    def open_algo_orders(self) -> list[dict[str, Any]]:
        """强制读取当前USD-M Algo保护单；失败必须向上抛，不能伪装为空。"""
        data = self._signed("GET", "/fapi/v1/openAlgoOrders", {})
        return self._normalize_algo_orders_payload(data)

    def takeover_compatibility(self) -> dict[str, Any]:
        """读取接管前事实；仅 GET，未知状态或双向持仓一律 fail-closed。"""
        position_mode = self._signed("GET", "/fapi/v1/positionSide/dual", {})
        if not isinstance(position_mode, Mapping) or "dualSidePosition" not in position_mode:
            self._hedge_mode = None
        else:
            self._hedge_mode = _exchange_bool(position_mode.get("dualSidePosition"))
        positions = self.positions(force=True)
        orders = self.open_orders()
        algo_orders = self.open_algo_orders()
        ledger = self._load_ledger()
        local_ids = {
            str(row.get("client_order_id") or "")
            for row in ledger.get("pending_limits", [])
            if row.get("client_order_id")
        }
        return assess_takeover_compatibility(
            position_mode if isinstance(position_mode, Mapping) else None,
            positions, orders, algo_orders,
            local_entry_client_ids=local_ids,
        )

    def _is_hedge_mode(self) -> bool:
        """返回已验证的持仓模式；未知时只读查询，失败不得猜测。"""
        # 仅兼容通过 __new__ 构造的离线替身；正式实例在 __init__ 中总有该字段。
        if not hasattr(self, "_hedge_mode"):
            return False
        if self._hedge_mode is None:
            payload = self._signed("GET", "/fapi/v1/positionSide/dual", {})
            if not isinstance(payload, Mapping) or "dualSidePosition" not in payload:
                raise RuntimeError("无法证明 Binance 持仓模式")
            self._hedge_mode = _exchange_bool(payload.get("dualSidePosition"))
        return self._hedge_mode

    @staticmethod
    def _position_side(position: Mapping[str, Any]) -> str:
        explicit = str(position.get("positionSide") or "").upper()
        if explicit in {"LONG", "SHORT"}:
            return explicit
        return "LONG" if float(position.get("positionAmt") or 0.0) > 0 else "SHORT"

    def _single_symbol_position(self, symbol: str, *, force: bool = False) -> dict[str, Any] | None:
        """返回该合约唯一仓位；同 symbol 双边时拒绝猜测应操作哪一侧。"""
        matches = [
            row for row in self.positions(force=force)
            if str(row.get("symbol") or "") == symbol
            and abs(float(row.get("positionAmt") or 0.0)) > 1e-12
        ]
        if len(matches) > 1:
            sides = ",".join(sorted({self._position_side(row) for row in matches}))
            raise RuntimeError(f"{symbol} 存在双边仓位({sides})，拒绝自动操作")
        return matches[0] if matches else None

    def _standard_order_mode_params(self, position_side: str, *, reduce: bool) -> dict[str, str]:
        """生成与账户模式一致的普通订单参数。

        Binance 明确禁止 Hedge Mode 发送 reduceOnly；此时 positionSide 本身限定
        被操作的仓位方向。One-way Mode 则继续使用 reduceOnly 防止平仓单反向开仓。
        """
        if self._is_hedge_mode():
            side = str(position_side or "").upper()
            if side not in {"LONG", "SHORT"}:
                raise RuntimeError("Hedge Mode 下单缺少 LONG/SHORT positionSide")
            return {"positionSide": side}
        return {"reduceOnly": "true" if reduce else "false"}

    def order_submission_ready(self) -> bool:
        """UNKNOWN/SUBMITTING存在时禁止新开仓；减仓和平仓不受此门禁影响。"""
        order_outbox = getattr(self, "order_outbox", None)
        if order_outbox is None:
            return True
        status = order_outbox.status()
        return (int(status.get("unknown_orders", 0)) == 0 and
                int(status.get("submitting_orders", 0)) == 0)

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
                total += self._margin_from_position(p)
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
                _algos = self._open_algos(None)
                if _algos is not None:
                    for algo in _algos:
                        sym = str(algo.get("symbol") or "")
                        otype = str(algo.get("orderType") or "")
                        if sym and otype in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                            try:
                                algo_by_symbol.setdefault(sym, {})[otype] = float(algo.get("triggerPrice") or 0.0)
                            except (TypeError, ValueError):
                                pass
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
                            "position_side": self._position_side(p),
                            "qty": abs(float(p.get("positionAmt") or 0)),
                            "entry_price": float(p.get("entryPrice") or 0),
                            "mark_price": float(p.get("markPrice") or 0),
                            "upnl": float(p.get("unRealizedProfit") or 0),
                            "leverage": int(float(p.get("leverage") or 1)),
                            "margin": self._margin_from_position(p),
                            "stop_loss": algo_by_symbol.get(p["symbol"], {}).get("STOP_MARKET"),
                            "take_profit": algo_by_symbol.get(p["symbol"], {}).get("TAKE_PROFIT_MARKET"),
                            "source": str(ledger.get("positions", {}).get(p["symbol"], {}).get("source", "ai") or "ai"),
                            "strategy_id": ledger.get("positions", {}).get(p["symbol"], {}).get("strategy_id"),
                            "ticket_id": ledger.get("positions", {}).get(p["symbol"], {}).get("ticket_id"),
                            "open_client_order_id": ledger.get("positions", {}).get(p["symbol"], {}).get("open_client_order_id"),
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
        if getattr(self, "require_pipeline_authorization", False):
            from core.decision_pipeline import filter_unauthorized_opens
            decision, verdict = filter_unauthorized_opens(
                decision, verdict, db, getattr(self, "_pipeline_secret", None),
                {
                    "environment": getattr(self, "environment", None),
                    "scope_hash": getattr(getattr(self, "trading_scope", None), "scope_hash", None),
                    "runtime_instance_id": getattr(self, "runtime_instance_id", None),
                })
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
        symbol = str(order["symbol"])
        # 关键：失败类别从**本次**同步的结果里取（``_resync_protection_impl`` 在入口
        # 先 ``begin(symbol)`` 复位、失败时 ``mark(symbol, ...)``），不再读进程级标量
        # 的残余值。只有"已确证的观测故障"才推迟平仓；未归类/保护确证失败仍按 P0
        # 兜底立即平仓（绝不允许裸仓）。
        if defers_flat_close(self.protection_sync.get(symbol)):
            fault = self.protection_sync.get(symbol)
            if self.db:
                # 复用已登记的事件类型（dashboard 事件映射表无需新增键）；
                # 语义由 action_taken 明确表达：只告警，不因观测故障平仓。
                self.db.log_risk_event(
                    event_type="futures_sync_error", severity="critical",
                    details=(f"{symbol}: 保护状态无法核验（{fault}），"
                             f"本轮不平仓，等待下轮重试"),
                    action_taken="alert_only_no_flat_close")
            return
        try:
            close_intent = TradeIntent(
                symbol=symbol, action="close",
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
        # 先在锁内取快照，随后网络对账不持锁（避免阻塞其他台账读写），
        # 最后在锁内用「全新加载」的台账应用结果：并发新增的挂单不会被旧快照覆盖。
        with self._ledger_lock:
            pending = list(self._load_ledger().get("pending_limits", []))
        if not pending:
            return receipts
        now_ts = _parse_iso(utc_now())
        remove_cids: set[str] = set()
        qty_updates: dict[str, float] = {}
        for order in pending:
            symbol = str(order.get("symbol") or "")
            cid = str(order.get("client_order_id") or "")
            exchange_id = order.get("exchange_order_id")
            try:
                if exchange_id:
                    data = self._signed("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": exchange_id})
                else:
                    data = self._signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
            except Exception:
                # 查询失败（含 -2013）：本轮跳过，留在台账里下轮继续
                continue

            status = str(data.get("status") or "")
            original_qty = float(data.get("origQty") or order.get("qty") or 0.0)
            executed_qty = float(data.get("executedQty") or 0.0)
            avg_price = float(data.get("avgPrice") or data.get("price") or 0.0)

            if executed_qty > 0:
                # 部分或全部成交：先把真实仓位记入来源台账并补保护
                ex_pos = self._single_symbol_position(symbol)
                self._record_ledger_position(
                    symbol,
                    str(order.get("source", "ai") or "ai"),
                    order.get("strategy_id"),
                    order.get("ticket_id"),
                    str(order.get("margin_mode", "ISOLATED") or "ISOLATED"),
                    qty=abs(float((ex_pos or {}).get("positionAmt") or 0.0)),
                    entry=float((ex_pos or {}).get("entryPrice") or 0.0),
                    open_client_order_id=cid,
                    position_side=self._position_side(ex_pos or {
                        "positionAmt": executed_qty if str(order.get("side")) == "LONG"
                        else -executed_qty,
                    }),
                )
                if order.get("ticket_id") and self.db:
                    self.db.update_signal_ticket(ticket_id=str(order["ticket_id"]), status="filled")
                self._ensure_protection_from_order(order)
                receipts.append({"order_id": cid, "action": "limit_partial_or_filled",
                                 "symbol": symbol, "qty": executed_qty, "price": avg_price,
                                 "source": order.get("source", "ai")})

            terminal = status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED")
            if terminal:
                if self.db:
                    self.db.update_order(client_order_id=cid, status=status, price=avg_price or None)
                if status == "FILLED" and order.get("ticket_id") and self.db:
                    self.db.update_signal_ticket(ticket_id=str(order["ticket_id"]), status="filled")
                remove_cids.add(cid)
                continue

            # 尚未结束：检查 AI 设置的过期时间，到期主动撤单
            expires = _parse_iso(order.get("expires_at"))
            if expires is not None and expires <= now_ts:
                try:
                    self._signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
                except Exception:
                    # 撤单失败必须留在台账里，下一轮继续处理；否则可能成交后无 SL/TP
                    continue
                if self.db:
                    self.db.update_order(client_order_id=cid, status="CANCELED")
                receipts.append({"order_id": cid, "action": "cancel_expired_limit",
                                 "symbol": symbol, "source": order.get("source", "ai")})
                remove_cids.add(cid)
                continue

            qty_updates[cid] = max(0.0, original_qty - executed_qty)

        with self._ledger_lock:
            ledger = self._load_ledger()
            kept = []
            for o in ledger.get("pending_limits", []):
                ocid = str(o.get("client_order_id") or "")
                if ocid in remove_cids:
                    continue
                if ocid in qty_updates:
                    o["qty"] = qty_updates[ocid]
                if float(o.get("qty") or 0.0) > 0:
                    kept.append(o)
            ledger["pending_limits"] = kept
            self._save_ledger(ledger)
        return receipts

    def _open(self, intent: Any) -> dict[str, Any] | None:
        symbol = intent.symbol
        # 最终执行层再次校验业务开仓白名单，防止策略/AI/脚本绕过上游 schema
        # 直接调用 Broker。该检查只放在 _open，绝不影响已有仓位的平仓和减仓。
        scope = getattr(self, "trading_scope", None)
        if scope is not None and not scope.allows_open(symbol, self._symbol_filters.keys()):
            if self.db:
                self.db.log_risk_event(
                    event_type="futures_scope_open_blocked", severity="warn",
                    details=f"{symbol} 不在当前业务开仓白名单内，最终执行层拒绝",
                    action_taken="skip open")
            return None
        if self._symbol_filters and symbol not in self._symbol_filters:
            if self.db:
                self.db.log_risk_event(event_type="futures_unsupported_symbol", severity="warn",
                                       details=f"{symbol} 不在当前交易所支持列表中，跳过开仓",
                                       action_taken="skip")
            return None
        # 来源账本以 symbol 为键，执行语义也只允许一个合约一个方向。即使账户
        # 开启 Hedge Mode，也不能通过 open 给现有 symbol 叠加或建立反向仓位。
        existing_positions = [
            row for row in self.positions()
            if str(row.get("symbol") or "") == symbol
            and abs(float(row.get("positionAmt") or 0.0)) > 1e-12
        ]
        if existing_positions:
            if self.db:
                self.db.log_risk_event(
                    event_type="futures_symbol_position_open_blocked", severity="warn",
                    details=f"{symbol} 已有仓位，禁止再次 open 或建立双边仓位",
                    action_taken="skip open",
                )
            return None
        margin_mode = str(getattr(intent, "margin_mode", "ISOLATED") or "ISOLATED").upper()
        try:
            self._signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_mode})
        except Exception:
            pass
        leverage_ready = False
        try:
            self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(intent.leverage)})
            leverage_ready = True
        except Exception as e:
            # 逐币种杠杆上限不同（BTC 125x，山寨常为 20x/50x），AI 的 25x 强信心档会撞上限：
            # 降档到交易所实际允许的最大杠杆重试；仍失败则放弃本单，避免以错误杠杆静默成交。
            max_lev = self._symbol_max_leverage(symbol)
            target = int(intent.leverage)
            if max_lev and max_lev > 0:
                target = min(target, max_lev)
                try:
                    self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": target})
                    leverage_ready = True
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
        if leverage_ready and self.db:
            self.db.resolve_risk_incidents_by_type("futures_leverage_error")
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
                "newClientOrderId": cid,
            }
            order_params.update(self._standard_order_mode_params(str(intent.side), reduce=False))
            if intent.order_type == "LIMIT":
                limit_price = float(getattr(intent, "limit_price", 0.0) or 0.0)
                if limit_price <= 0:
                    raise ValueError(f"{symbol} LIMIT 单缺少有效 limit_price")
                order_params["price"] = self._format_price(symbol, limit_price)
                order_params["timeInForce"] = "GTC"
            try:
                data = self._submit_standard_order(order_params, purpose="ENTRY")
            except Exception as e:
                if self.db:
                    self.db.log_risk_event(
                        event_type="futures_order_submit_lost", severity="critical",
                        details=(f"{symbol}: Outbox下单失败或UNKNOWN对账无果，"
                                 f"禁止重发: {type(e).__name__} {e}"),
                        action_taken="outbox retained; no repost")
                raise
            if intent.order_type == "MARKET":
                data = self._wait_fill(symbol, data.get("orderId")) or data
            chunk_confirmed = self._fill_confirmed(data)
            fill_unconfirmed = fill_unconfirmed or not chunk_confirmed
            price = float(data.get("avgPrice") or data.get("price") or 0.0)
            # 开仓手续费：通道权益对账必需。通道侧 day_pnl 只累计平仓单的
            # net_pnl（含平仓费），若开仓费不入账，渠道合计会比交易所钱包
            # 差值多出全部开仓手续费（实测 0.254/500U ≈ 0.05%）。成交确认后
            # 从成交流水取 commission 写入 open raw，由 channels 统计扣回。
            open_commission = None
            if chunk_confirmed:
                try:
                    _, open_commission = self._order_trade_fee(symbol, data.get("orderId"))
                except Exception:
                    open_commission = None
            margin = self._position_margin(symbol)
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
                        "commission": open_commission,
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
                qty=float(total_qty),
                entry=price if price > 0 else None,
                open_client_order_id=first_cid,
                position_side=str(intent.side),
            )
            try:
                self._ensure_lifecycle_from_snapshot(
                    {"symbol": symbol, "positionAmt": total_qty,
                     "entryPrice": price, "leverage": int(intent.leverage),
                     "positionSide": str(intent.side)},
                    {"source": order_source,
                     "strategy_id": getattr(intent, "strategy_id", None),
                     "ticket_id": getattr(intent, "ticket_id", None),
                     "margin_mode": margin_mode,
                     "open_client_order_id": first_cid},
                )
            except Exception as exc:
                if self.db:
                    self.db.log_risk_event(
                        event_type="trade_lifecycle_snapshot_error", severity="warn",
                        details=f"{symbol}: 开仓后生命周期建立失败 {type(exc).__name__}",
                        action_taken="retain order; reconcile later")
            if self.db and getattr(intent, "ticket_id", None):
                self.db.update_signal_ticket(ticket_id=str(intent.ticket_id), status="filled")
        else:
            # LIMIT：挂到交易所后进入本地 pending 台账，由 _sync_pending_limits 监控成交/过期。
            with self._ledger_lock:
                ledger = self._load_ledger()
                ledger.setdefault("pending_limits", []).append({
                    "client_order_id": first_cid,
                    "exchange_order_id": str(first_exchange_id or ""),
                    "symbol": symbol,
                    "side": intent.side,
                    "position_side": str(intent.side),
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
        position_side = "LONG" if close_side == "SELL" else "SHORT"
        params: dict[str, Any] = {
            "symbol": symbol,
            "clientAlgoId": cid,
            "side": close_side,
            "type": order_type,
            "triggerPrice": self._format_price(symbol, trigger_price),
            "algoType": "CONDITIONAL",
            "timeInForce": "GTC",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
        }
        if self._is_hedge_mode():
            # 官方接口禁止 Hedge Mode 发送 reduceOnly。closePosition=true 会在触发时
            # 关闭指定 positionSide 的全部当前数量，也天然覆盖后续部分减仓变化。
            params.update({"positionSide": position_side, "closePosition": "true"})
        else:
            params.update({"quantity": qty, "reduceOnly": "true"})
        data = self._submit_algo_order(params, purpose="PROTECTION")
        algo_id = str(data.get("algoId") or "")
        self._log_order(symbol=symbol, side=log_side, order_type=order_type,
                        qty=float(qty), price=float(trigger_price), status="NEW",
                        exchange_id=algo_id,
                         raw={"mode": self.environment, "action": "algo_protection",
                              "algoType": "CONDITIONAL", "algoId": algo_id,
                              "positionSide": position_side,
                              "closePosition": bool(params.get("closePosition"))},
                         client_id=cid)

    def _open_algos(self, symbol: str | None = None) -> list[dict[str, Any]] | None:
        """查询未触发 Algo 单。接口失败时返回 None（区别于"确实无挂单"的 []）：

        快循环 sync 把 None 当"查询失败跳过本轮"，绝不当作"无保护"去重挂——
        实测 -1022 签名错误被吞成 [] 后，sync 误判仓位无保护，兕底重挂，
        造成同一仓位 2 分钟内出现两套 SL/TP（critical futures_protection_fallback）。
        """
        try:
            data = self._signed("GET", "/fapi/v1/openAlgoOrders", {})
        except Exception:
            return None
        try:
            rows = self._normalize_algo_orders_payload(data)
        except Exception:
            return None
        if symbol is None:
            return rows
        return [o for o in rows if o.get("symbol") == symbol]

    def _cancel_symbol_algos(self, symbol: str) -> None:
        algos = self._open_algos(symbol)
        if not algos:
            return  # None=查询失败（无从撤单）或 [] =确实无挂单
        for algo in algos:
            algo_id = str(algo.get("algoId") or "")
            client_algo_id = str(algo.get("clientAlgoId") or "")
            try:
                self._signed("DELETE", "/fapi/v1/algoOrder", {"algoId": algo.get("algoId")})
                # 撤单成功：把本地流水里对应的保护单收口成 CANCELED，
                # 避免「移动止损反复重设」在 orders 表堆积一堆永远 NEW 的幽灵单。
                if self.db and algo_id:
                    self.db.cancel_order_by_exchange_id(exchange_order_id=algo_id)
                if client_algo_id and getattr(self, "order_outbox", None) is not None:
                    self.order_outbox.acknowledge(client_algo_id, {
                        "algoId": algo_id, "algoStatus": "CANCELED",
                    })
            except Exception as e:
                msg = str(e)
                # 已经触发/已不存在的旧单无需处理，也不算错误
                if "-2011" in msg or "Unknown order" in msg:
                    # 旧单已不在交易所（已触发或已被外部撤销），本地流水同样要收口。
                    if self.db and algo_id:
                        self.db.cancel_order_by_exchange_id(exchange_order_id=algo_id)
                    if client_algo_id and getattr(self, "order_outbox", None) is not None:
                        self.order_outbox.acknowledge(client_algo_id, {
                            "algoId": algo_id, "algoStatus": "CANCELED",
                        })
                    continue
                if self.db:
                    self.db.log_risk_event(event_type="futures_adjust_error", severity="warn",
                                           details=f"{symbol}: 取消Algo单失败 {type(e).__name__} {e}",
                                           action_taken="继续")

    def _cleanup_closed_symbol_algos(self, symbol: str) -> int | None:
        """二次确认已无仓位后，串行撤销保护触发后遗留的另一侧Algo单。"""
        with self._resync_lock:
            current = [p for p in self.positions(force=True)
                       if str(p.get("symbol") or "") == symbol and
                       abs(float(p.get("positionAmt") or 0.0)) > 1e-12]
            if current:
                return 0
            algos = self._open_algos(symbol)
            if algos is None:
                return None
            canceled = 0
            failed = False
            for algo in algos:
                algo_id = str(algo.get("algoId") or "")
                if not algo_id:
                    continue
                try:
                    self._signed("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
                    canceled += 1
                    if self.db:
                        self.db.cancel_order_by_exchange_id(exchange_order_id=algo_id)
                except Exception as exc:
                    failed = True
                    if self.db:
                        self.db.log_risk_event(
                            event_type="futures_adjust_error", severity="warn",
                            details=f"{symbol}: 孤儿Algo撤销失败 {algo_id}: {type(exc).__name__}",
                            action_taken="retry next sync")
            if canceled and self.db:
                self.db.log_risk_event(
                    event_type="futures_orphan_protection_canceled", severity="info",
                    details=f"{symbol}: 仓位归零后二次确认，已撤销 {canceled} 张残留Algo保护单",
                    action_taken="cancel orphan algo orders")
            return None if failed else canceled

    def _retry_orphan_algo_cleanup(self, position_symbols: set[str]) -> int | None:
        """每轮重试清理历史孤儿单，弥补首次平仓收口遇到网络故障的情况。"""
        algos = self._open_algos()
        if algos is None:
            return None
        orphan_symbols = {
            str(row.get("symbol") or "")
            for row in algos
            if str(row.get("symbol") or "") not in position_symbols
        }
        total = 0
        for symbol in sorted(orphan_symbols):
            if not symbol:
                continue
            result = self._cleanup_closed_symbol_algos(symbol)
            if result is None:
                return None
            total += result
        return total

    def _algo_prices(self, symbol: str) -> dict[str, float] | None:
        """当前交易所侧 SL/TP 价格。查询失败返回 None（区别于"无挂单"的 {}）。"""
        algos = self._open_algos(symbol)
        if algos is None:
            return None
        prices: dict[str, float] = {}
        for algo in algos:
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
        """串行化包装：见 _resync_lock 注释。所有撤旧挂新路径必须经此进入。"""
        with self._resync_lock:
            return self._resync_protection_impl(symbol, intent=intent,
                                                stop_loss=stop_loss,
                                                take_profit=take_profit)

    def _resync_protection_impl(self, symbol: str, intent: Any = None,
                                stop_loss: float | None = None,
                                take_profit: float | None = None) -> bool:
        """统一同步止损/止盈：只在数量或价格变化时重挂，避免每轮刷大量重复 Algo 单。"""
        # 本轮开始：复位该标的的失败类别。任何提前 return（含下面"保护已匹配"
        # 的成功路径）都不会留下上一轮的残余类别。
        self.protection_sync.begin(symbol)
        existing = self._open_algos(symbol)
        if existing is None:
            # 交易所查询失败：无法确认当前保护单状态，绝不能当作"无保护"处理。
            # 带明确 intent 的主动挂单路径除外（调用方明确要求挂新保护，即使
            # 查询失败也应尝试撤旧挂新，由 -2011 等错误码兜底）。
            if intent is None and stop_loss is None and take_profit is None:
                self.protection_sync.mark(symbol, "QUERY_FAILED")
                if self.db:
                    self.db.log_risk_event(
                        event_type="futures_sync_error", severity="warn",
                        details=f"{symbol}: 查询 Algo 单失败，本轮跳过保护同步（防误判重复挂单）",
                        action_taken="skip resync")
                return False
            existing = []
        old_prices = self._algo_prices(symbol)
        if old_prices is None:
            old_prices = {}

        pos = self._single_symbol_position(symbol)
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

        # 兕底保护（P0-2）：adopt 的无主仓位（下单响应丢失/交易所手动开仓）没有任何
        # SL/TP 记录时，绝不允许裸仓静默存活。按标记价 ±固定比例兕底挂初始保护，
        # 并发 critical 告警。之后 LLM 仍可通过 adjust 单把 SL/TP 调到合理水平。
        if sl is None or tp is None:
            mark = float(pos.get("markPrice") or pos.get("entryPrice") or 0.0)
            if mark > 0:
                if sl is None:
                    sl = round(mark * (1 - self._FALLBACK_SL_PCT), 8) if close_side == "SELL" \
                        else round(mark * (1 + self._FALLBACK_SL_PCT), 8)
                if tp is None:
                    tp = round(mark * (1 + self._FALLBACK_TP_PCT), 8) if close_side == "SELL" \
                        else round(mark * (1 - self._FALLBACK_TP_PCT), 8)
                if self.db:
                    self.db.log_risk_event(
                        event_type="futures_protection_fallback", severity="critical",
                        details=f"{symbol}: 仓位无 SL/TP 记录，已按标记价兕底挂保护 "
                                f"(SL={sl}, TP={tp})，来源可能为外部开仓或下单响应丢失",
                        action_taken="fallback_protection")

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
                    if self._is_hedge_mode():
                        qty_ok = (_exchange_bool(a.get("closePosition")) and
                                  str(a.get("positionSide") or "").upper() ==
                                  ("LONG" if close_side == "SELL" else "SHORT"))
                    else:
                        qty_ok = (abs(float(a.get("quantity") or 0) - qty_f)
                                  <= float(qty_eps) and _exchange_bool(a.get("reduceOnly")))
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

        # 有变化/重复单：交由 U04 保护规划按“先挂新、后撤旧”执行；任一 PLACE 失败即
        # 停止，绝不先把旧保护撤掉留下裸仓，也不撤销最后一道保护。
        #
        # 交易所来源字段逐条容错：``normalize_algo_orders_payload`` 原样透传 JSON，
        # 某一行的数量/触发价若是浮点或畸形，只跳过该行并计数，绝不让整轮保护刷新
        # 抛错（那样等于该轮完全没有保护对账）。浮点走 ``_exchange_decimal`` 显式
        # 转成 Decimal，与 ``desired_sl/tp`` 的口径一致。
        protection_views = []
        skipped_rows = 0
        for item in existing:
            try:
                protection_views.append(protection_view_from_algo({
                    "clientAlgoId": item.get("clientAlgoId"),
                    "algoId": item.get("algoId"),
                    "orderType": item.get("orderType"),
                    "quantity": _exchange_decimal(item.get("quantity")),
                    "triggerPrice": _exchange_decimal(item.get("triggerPrice")),
                    "reduceOnly": item.get("reduceOnly"),
                    "closePosition": item.get("closePosition"),
                }))
            except Exception:  # noqa: BLE001 - 单行畸形不拖垮整轮对账
                skipped_rows += 1
        if skipped_rows and self.db:
            self.db.log_risk_event(
                event_type="futures_adjust_error", severity="warn",
                details=f"{symbol}: {skipped_rows} 行保护单字段畸形已跳过（本轮仍继续对账）",
                action_taken="skip malformed protection rows")

        try:
            position_snapshot = real_position_from_snapshot({
                "symbol": symbol,
                "positionAmt": _exchange_decimal(pos.get("positionAmt")),
                "positionSide": pos.get("positionSide"),
            })
        except Exception as exc:  # noqa: BLE001 - 仓位字段畸形：按未确证处理，不平仓
            self.protection_sync.mark(symbol, "QUERY_FAILED")
            if self.db:
                self.db.log_risk_event(
                    event_type="futures_sync_error", severity="critical",
                    details=(f"{symbol}: 仓位快照字段无法精确解析"
                             f"（{type(exc).__name__}），本轮不平仓"),
                    action_taken="alert_only_no_flat_close")
            return False

        plan = plan_protection_refresh(
            position=position_snapshot,
            protections=protection_views,
            desired_sl=(decimal_from_float(sl) if sl is not None else None),
            desired_tp=(decimal_from_float(tp) if tp is not None else None),
            flat_confirmed=False,
        )

        def _place_action(action: Any) -> str:
            is_stop = action.order_type == "STOP_MARKET"
            self._place_algo_protection(
                symbol, close_side, qty, action.order_type,
                float(action.trigger_price), "sl" if is_stop else "tp",
                "STOP" if is_stop else "TAKE")
            return action.order_type

        def _cancel_action(action: Any) -> bool:
            target = None
            for item in existing:
                if str(item.get("clientAlgoId") or "") == str(action.client_id):
                    target = item
                    break
            algo_id = str((target or {}).get("algoId") or "") or None
            if algo_id is None:
                # 只知 clientAlgoId、匹配不到交易所 algoId -> 发不出 DELETE。
                # 必须返回 False：调用方会记入 cancel_failed 而不是 canceled，
                # 否则账本会宣称"旧保护已撤"而交易所侧旧保护仍在。
                return False
            try:
                self._signed("DELETE", "/fapi/v1/algoOrder", {"algoId": algo_id})
            except Exception as e:
                # 撤旧失败不致命：新保护已挂上，旧保护留在交易所不构成裸仓。
                if self.db:
                    self.db.log_risk_event(
                        event_type="futures_adjust_error", severity="warn",
                        details=f"{symbol}: 撤销被替换保护 {algo_id} 失败: "
                                f"{type(e).__name__} {e}",
                        action_taken="保留旧保护")
                return False
            if self.db:
                self.db.cancel_order_by_exchange_id(exchange_order_id=algo_id)
            if action.client_id and getattr(self, "order_outbox", None) is not None:
                self.order_outbox.acknowledge(str(action.client_id), {
                    "algoId": algo_id, "algoStatus": "CANCELED",
                })
            return True

        execution = execute_protection_plan(plan, place=_place_action,
                                            cancel=_cancel_action)
        failed = bool(execution.failed_place)
        if failed and self.db:
            self.db.log_risk_event(
                event_type=("futures_sl_failed"
                            if execution.failed_place == "STOP_MARKET"
                            else "futures_tp_failed"),
                severity="critical",
                details=(f"{symbol}: {execution.failed_place} 挂新保护失败，"
                         f"已按计划保留旧保护（cancel_skipped={execution.cancel_skipped}）"),
                action_taken="protection_failed")
        if execution.cancel_failed and self.db:
            self.db.log_risk_event(
                event_type="futures_adjust_error", severity="warn",
                details=(f"{symbol}: 保护单撤销回调未执行 "
                         f"{list(execution.cancel_failed)}（{execution.cancel_unexecuted_reason}），"
                         f"交易所侧旧保护可能仍在"),
                action_taken="取消未生效已记录")
        # 只写"已确证的保护失败"，成功即复位（begin 已在入口清过，这里显式置空）。
        self.protection_sync.mark(symbol, "PROTECTION_FAILED" if failed else "")
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
        pos = self._single_symbol_position(symbol)
        if not pos:
            if self.db:
                self.db.log_risk_event(event_type="futures_close_nopos", severity="warn",
                                       details=f"{symbol} 无仓位可平")
            return None
        current = abs(float(pos.get("positionAmt") or 0))
        pos_side = self._position_side(pos)
        side = "SELL" if pos_side == "LONG" else "BUY"
        # U02：退出先在管理预留账本里原子占量。重复的退出请求返回 duplicate
        # skipped 而不是再发一次减仓；在途退出已占掉的仓量不会再被本轮读到，
        # 因此重复退出既不会二次减仓、更不可能被做成反向开仓（R09）。
        reservation_key = self._reserve_exit(symbol, pos_side, intent, current)
        if reservation_key is not None and reservation_key.get("skip"):
            return reservation_key.get("receipt")
        close_qty = float(
            reservation_key["submit_qty"] if reservation_key
            else min(float(intent.qty), current))
        if close_qty <= 0:
            self._release_exit(reservation_key, reason="NO_QTY_TO_CLOSE")
            return None
        # U02/A_r1 P0-1：占量成功之后的**所有**退出路径（含异常）都必须在 finally
        # 里结算或释放预留。`_submit_standard_order` 在"确定性拒绝"（HTTP 429/418、
        # reduceOnly 被拒、4xx）或 POST 异常后对账失败时会重新抛出，异常被逐 intent
        # 的 except Exception 兜住——若此处不释放，这份预留会把可用量永久占满，
        # 之后每一次退出都只得到误导性的"无仓可平"，该标的再也不能自动退出
        #（F03/R09 的并发 TP 拒绝场景）。
        settled = False
        try:
            entry = float(pos.get("entryPrice") or 0.0)
            ledger_source, ledger_meta = self._ledger_source(symbol)
            source = str(getattr(intent, "order_source", ledger_source) or ledger_source or "ai")
            reconciler = getattr(self, "trade_reconciliation", None)
            if reconciler is not None:
                try:
                    self._ensure_lifecycle_from_snapshot(pos, ledger_meta)
                except Exception as exc:
                    if self.db:
                        self.db.log_risk_event(
                            event_type="trade_lifecycle_snapshot_error", severity="warn",
                            details=f"{symbol}: 平仓前生命周期建立失败 {type(exc).__name__}",
                            action_taken="retain close; reconcile after fill")

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
            total_net_pnl = 0.0
            exit_notional = 0.0
            order_count = 0
            close_unconfirmed = False
            base_cid = getattr(intent, "client_order_id", None) or f"mta-{uuid.uuid4().hex[:28]}"
            for chunk in chunks:
                qty = self._format_qty(symbol, chunk)
                cid = base_cid if len(chunks) == 1 else f"{base_cid}-{order_count}"
                order_params: dict[str, Any] = {
                    "symbol": symbol, "side": side, "type": intent.order_type,
                    "quantity": qty, "newClientOrderId": cid,
                }
                order_params.update(self._standard_order_mode_params(pos_side, reduce=True))
                data = self._submit_standard_order(order_params, purpose="EXIT")
                if intent.order_type == "MARKET":
                    data = self._wait_fill(symbol, data.get("orderId")) or data
                chunk_confirmed = self._fill_confirmed(data)
                close_unconfirmed = close_unconfirmed or not chunk_confirmed
                close_price = float(data.get("avgPrice") or data.get("price") or 0.0)
                exact_pnl, commission = self._order_trade_fee(symbol, data.get("orderId"))
                if exact_pnl is not None:
                    pnl = exact_pnl
                elif chunk_confirmed and close_price > 0:
                    # 用成交均价估算：仅当订单确认成交且价格有效时才可信
                    pnl = chunk * (close_price - entry) if pos_side == "LONG" else chunk * (entry - close_price)
                else:
                    # 响应丢失/未确认：估算值会污染复盘与熔断（曾造成假 -18000U 写库，
                    # 误触发 AI -30% 熔断）。置 None 标记待对账，由下一轮收口或人工处理。
                    pnl = None
                fee = commission if commission is not None else 0.0
                if pnl is not None:
                    total_pnl += pnl
                if chunk_confirmed and pnl is not None and close_price > 0:
                    total_filled += float(qty)
                    total_net_pnl += pnl + fee
                    exit_notional += float(qty) * close_price
                order_count += 1
                if first_cid is None:
                    first_cid = cid
                raw = dict(data or {})
                raw.update({"mode": self.environment, "action": "close",
                            "trade_level": getattr(intent, "trade_level", "short"),
                            "margin": self._position_margin(symbol), "pnl": pnl,
                            "commission": fee,
                            "net_pnl": (pnl + fee) if pnl is not None else None,
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
            remaining_pos = self._single_symbol_position(symbol, force=True)
            remaining_qty = abs(float(remaining_pos.get("positionAmt") or 0)) if remaining_pos else 0.0
            lifecycle_result = None
            if not close_unconfirmed and reconciler is not None:
                try:
                    lifecycle_result = self._reconcile_stored_lifecycle(
                        symbol, pos_side, Decimal(str(remaining_qty)))
                except Exception as exc:
                    if self.db:
                        self.db.log_risk_event(
                            event_type="trade_fill_reconcile_error", severity="warn",
                            details=f"{symbol}: 已确认平仓成交归因失败 {type(exc).__name__}",
                            action_taken="retain lifecycle; retry reconciliation")
            if not close_unconfirmed and reconciler is not None and lifecycle_result is None:
                active_lifecycle = self.db.get_active_position_lifecycle(
                    environment=str(self.environment), account_ref=str(reconciler.account_ref),
                    symbol=symbol, position_side=pos_side,
                )
                if active_lifecycle is not None:
                    self._reconcile_job_retry(
                        active_lifecycle,
                        lower_bound_ms=max(0, int(active_lifecycle["opened_at_ms"] or 0)
                                           - 10 * 60 * 1000),
                        upper_bound_ms=int(time.time() * 1000),
                        reason="CLOSED_ORDER_FILL_NOT_AVAILABLE",
                    )
            if not close_unconfirmed and total_filled > 0 and exit_notional > 0:
                if lifecycle_result is not None and lifecycle_result.flat_evidence:
                    lifecycle_row = self.db.fetch_one(
                        "SELECT * FROM position_lifecycles WHERE lifecycle_id=?",
                        (lifecycle_result.lifecycle_id,))
                    actual_closed = self._iso_from_ms(lifecycle_result.actual_closed_at_ms)
                    self._record_trade_outcome(
                        outcome_id=f"bn-{first_cid}", symbol=symbol, side=pos_side, source=source,
                        entry_price=entry, exit_price=exit_notional / total_filled,
                        qty=float(lifecycle_result.allocated_qty),
                        net_pnl=float(lifecycle_result.net_pnl)
                        if lifecycle_result.net_pnl is not None else None,
                        exit_reason="ai_decision", status="CLOSED", ledger_meta=ledger_meta,
                        opened_at=self._iso_from_ms(int(lifecycle_row["opened_at_ms"] or 0))
                        if lifecycle_row else None,
                        closed_at=actual_closed, lifecycle_id=lifecycle_result.lifecycle_id,
                        attribution_status=lifecycle_result.attribution_status,
                        cost_status=lifecycle_result.cost_status, actual_closed_at=actual_closed,
                        gross_realized=float(lifecycle_result.gross_realized)
                        if lifecycle_result.gross_realized is not None else None,
                        entry_fee=float(lifecycle_row["entry_fee"])
                        if lifecycle_row and lifecycle_row["entry_fee"] is not None else None,
                        exit_fee=float(lifecycle_result.exit_fee)
                        if lifecycle_result.exit_fee is not None else None,
                        funding_cashflow=float(lifecycle_row["funding_cashflow"])
                        if lifecycle_row and lifecycle_row["funding_cashflow"] is not None else None,
                        allow_legacy_fallback=False,
                    )
                elif reconciler is None:
                    # 兼容没有L06账本的纸面/旧替身路径；生产 Broker 有账本时，
                    # 成交未完成归因不再用订单均价伪造可复盘 outcome。
                    self._record_trade_outcome(
                        outcome_id=f"bn-{first_cid}", symbol=symbol, side=pos_side, source=source,
                        entry_price=entry, exit_price=exit_notional / total_filled,
                        qty=total_filled, net_pnl=total_net_pnl,
                        exit_reason="ai_decision",
                        status="CLOSED" if remaining_qty <= 0 else "REDUCED",
                        ledger_meta=ledger_meta,
                    )
            if remaining_qty <= 0:
                self._remove_ledger_position(symbol)
            else:
                self._record_ledger_position(
                    symbol, source,
                    ledger_meta.get("strategy_id"),
                    ledger_meta.get("ticket_id"),
                    str(ledger_meta.get("margin_mode", "ISOLATED") or "ISOLATED"),
                    qty=remaining_qty,
                    entry=float(remaining_pos.get("entryPrice") or 0.0),
                    position_side=pos_side,
                )
            self._resync_protection(symbol, intent=intent)
            # U02：结算本轮预留。确认成交则按实际扣减；未确认（可能部分成交/
            # 响应丢失）则整笔释放，交给下一轮 UNKNOWN 收口重新规划——释放会让
            # 该 exit_key 恢复可重发，因此"重新规划"这一句话才真正成立。
            if close_unconfirmed:
                self._release_exit(reservation_key, reason="FILL_UNCONFIRMED")
            else:
                self._settle_exit(reservation_key)
            settled = True
            return {"order_id": first_cid, "action": "close", "symbol": symbol,
                    "side": pos_side, "qty": float(close_qty), "pnl": total_pnl,
                    "order_count": order_count, "fill_unconfirmed": close_unconfirmed}
        finally:
            if not settled:
                # 走不完的路径必须释放：占死可用量比"多一次可重发的退出"危险得多。
                try:
                    self._release_exit(reservation_key, reason="EXIT_PATH_ERROR")
                    if self.db:
                        self.db.log_risk_event(
                            event_type="futures_exit_reservation_released",
                            severity="warn",
                            details=(f"{symbol}: 退出路径未走完，已释放本次预留"
                                     "（下一轮可重发，不会占死该标的的可减量）"),
                            action_taken="release reservation")
                except Exception:  # noqa: BLE001 - 兜底回收在下一轮 sweep
                    pass

    #: 在途退出预留的超时（毫秒）。一次退出提交+等成交是秒级；超过这个时长仍未
    #: 结算的预留只可能是"结算与释放都没跑到"的泄漏，按泄漏回收（U02/A_r1 P0-1）。
    EXIT_RESERVATION_TTL_MS = 900_000

    def _sweep_stale_exit_reservations(self, manager: Any) -> tuple[str, ...]:
        """回收超过 TTL 仍在途的退出预留（泄漏兜底），并留下审计事件。"""
        reclaim = getattr(manager, "reclaim_stale_exits", None)
        if reclaim is None:
            return ()
        try:
            reclaimed = tuple(reclaim(now_ms=int(time.time() * 1000),
                                      ttl_ms=self.EXIT_RESERVATION_TTL_MS))
        except Exception as exc:  # noqa: BLE001 - 巡检失败不得影响本次退出
            # 巡检失败不能静默：本轮不再回收泄漏预留，未过期的同键退出会退化为
            # `duplicate_skipped`（fail-closed，不放行）。"巡检长期失败 -> 该标的
            # 不可退出"这条链必须能被运维看到（U02/A_r2 NEW-5）。
            if self.db:
                try:
                    self.db.log_risk_event(
                        event_type="futures_exit_reservation_sweep_failed",
                        severity="warn",
                        details=(f"退出预留巡检失败（{type(exc).__name__}）：本轮不做"
                                 "泄漏回收，未过期的同键退出将退化为重复退出"),
                        action_taken="skip sweep")
                except Exception:  # noqa: BLE001 - 审计失败不得影响本次退出
                    pass
            return ()
        if reclaimed and self.db:
            self.db.log_risk_event(
                event_type="futures_stale_exit_reservation_reclaimed", severity="warn",
                details=(f"{len(reclaimed)} 笔退出预留超过 "
                         f"{self.EXIT_RESERVATION_TTL_MS}ms 未结算，已按泄漏回收："
                         "上一轮退出既未成交也未释放，现恢复可退出"),
                action_taken="reclaim stale reservation")
        return reclaimed

    def _reserve_exit(self, symbol: str, pos_side: str, intent: Any,
                      current: float) -> dict[str, Any] | None:
        """经 U02 管理预留注册退出；未接入管理组件时保持既有行为。"""
        manager = getattr(self, "strategy_position_manager", None)
        if manager is None:
            return None
        # 先回收上一轮泄漏的在途预留：稳定 key（guardian 的 mta-g-<sha24>）会被
        # `_exit_keys` 记忆，不回收就等于该标的永久不可退出。
        self._sweep_stale_exit_reservations(manager)
        domain = self._exit_domain_key(symbol, pos_side)
        exit_key = str(getattr(intent, "client_order_id", None)
                       or f"exit:{domain}:{uuid.uuid4().hex[:16]}")
        outcome = manager.request_exit(
            exit_key=exit_key, lifecycle_id=domain, symbol=symbol,
            position_side=pos_side, qty=float(intent.qty), current_qty=current,
            now_ms=int(time.time() * 1000),
            in_flight_ttl_ms=self.EXIT_RESERVATION_TTL_MS)
        if outcome.duplicate:
            if self.db:
                self.db.log_risk_event(
                    event_type="futures_duplicate_exit_skipped", severity="warn",
                    details=f"{symbol}: 重复退出请求被幂等忽略（不会反向开仓）",
                    action_taken="skip duplicate exit")
            return {"skip": True, "receipt": {
                "order_id": exit_key, "action": "duplicate_skipped", "symbol": symbol,
                "side": pos_side, "reason_codes": list(outcome.reason_codes)}}
        if not outcome.accepted or outcome.submit_qty <= 0:
            if self.db:
                # 复用既有事件类型 `futures_close_nopos`：语义相同（无仓量可减），
                # 避免为一个已存在的语义新增事件类型与看板映射。
                self.db.log_risk_event(
                    event_type="futures_close_nopos", severity="warn",
                    details=f"{symbol}: 退出请求无可减量（在途退出已占量）",
                    action_taken="skip exit")
            return {"skip": True, "receipt": {
                "order_id": exit_key, "action": "exit_noop", "symbol": symbol,
                "side": pos_side, "reason_codes": list(outcome.reason_codes)}}
        return {"skip": False, "exit_key": exit_key, "domain": domain,
                "submit_qty": outcome.submit_qty}

    def _settle_exit(self, reservation: dict[str, Any] | None) -> None:
        if not reservation or reservation.get("skip"):
            return
        manager = getattr(self, "strategy_position_manager", None)
        if manager is None:
            return
        manager.confirm_exit(reservation["exit_key"],
                             engaged_qty=reservation["submit_qty"])

    def _release_exit(self, reservation: dict[str, Any] | None, *, reason: str) -> None:
        if not reservation or reservation.get("skip"):
            return
        manager = getattr(self, "strategy_position_manager", None)
        if manager is None:
            return
        manager.release_exit(reservation["exit_key"], reason=reason)

    def _exit_domain_key(self, symbol: str, pos_side: str) -> str:
        """退出的仓位域键：与生命周期唯一索引同口径（环境/账户/标的/持仓侧）。"""
        return ":".join((
            str(self.environment or "").lower(),
            str(getattr(self, "account_ref", "") or ""),
            str(symbol or "").upper(), str(pos_side or "").upper(),
        ))

    def _adjust(self, intent: Any) -> dict[str, Any] | None:
        symbol = intent.symbol
        pos = self._single_symbol_position(symbol)
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
            pos_side = self._position_side(pos)
            entry = float(pos.get("entryPrice") or 0.0)
            try:
                reconciler = getattr(self, "trade_reconciliation", None)
                if reconciler is not None:
                    try:
                        self._ensure_lifecycle_from_snapshot(pos, meta)
                    except Exception as exc:
                        if self.db:
                            self.db.log_risk_event(
                                event_type="trade_lifecycle_snapshot_error", severity="warn",
                                details=f"{symbol}: 紧急平仓前生命周期建立失败 {type(exc).__name__}",
                                action_taken="retain close; reconcile after fill")
                max_qty = self._market_max_qty(symbol)
                chunks: list[float] = []
                remaining = amt
                while remaining > 1e-12:
                    chunk = min(remaining, max_qty)
                    chunks.append(chunk)
                    remaining -= chunk
                first_cid = None
                total_pnl = 0.0
                total_net_pnl = 0.0
                total_filled = 0.0
                exit_notional = 0.0
                order_count = 0
                close_all_unconfirmed = False
                for chunk in chunks:
                    qty = self._format_qty(symbol, chunk)
                    cid = f"mta-{uuid.uuid4().hex[:28]}"
                    order_params: dict[str, Any] = {
                        "symbol": symbol, "side": side, "type": "MARKET",
                        "quantity": qty, "newClientOrderId": cid,
                    }
                    order_params.update(self._standard_order_mode_params(pos_side, reduce=True))
                    data = self._submit_standard_order(order_params, purpose="EXIT")
                    data = self._wait_fill(symbol, data.get("orderId")) or data
                    chunk_confirmed = self._fill_confirmed(data)
                    close_all_unconfirmed = close_all_unconfirmed or not chunk_confirmed
                    close_price = float(data.get("avgPrice") or data.get("price") or 0.0)
                    exact_pnl, commission = self._order_trade_fee(symbol, data.get("orderId"))
                    if exact_pnl is not None:
                        pnl = exact_pnl
                    elif chunk_confirmed and close_price > 0:
                        pnl = chunk * (close_price - entry) if float(pos.get("positionAmt") or 0) > 0 \
                            else chunk * (entry - close_price)
                    else:
                        # 紧急平仓响应丢失时同样不写估算 pnl（同 _close_or_reduce）
                        pnl = None
                    fee = commission if commission is not None else 0.0
                    if pnl is not None:
                        total_pnl += pnl
                    if chunk_confirmed and pnl is not None and close_price > 0:
                        total_filled += float(qty)
                        total_net_pnl += pnl + fee
                        exit_notional += float(qty) * close_price
                    order_count += 1
                    if first_cid is None:
                        first_cid = cid
                    raw = dict(data or {})
                    raw.update({"mode": self.environment, "action": "close_all", "margin": 0.0,
                                "pnl": pnl, "commission": fee,
                                "net_pnl": (pnl + fee) if pnl is not None else None,
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
                lifecycle_result = None
                if not close_all_unconfirmed and reconciler is not None:
                    try:
                        lifecycle_result = self._reconcile_stored_lifecycle(
                            symbol, pos_side, Decimal("0"))
                    except Exception as exc:
                        if self.db:
                            self.db.log_risk_event(
                                event_type="trade_fill_reconcile_error", severity="warn",
                                details=f"{symbol}: 紧急平仓成交归因失败 {type(exc).__name__}",
                                action_taken="retain lifecycle; retry reconciliation")
                if (not close_all_unconfirmed and total_filled > 0
                        and exit_notional > 0 and first_cid):
                    if lifecycle_result is not None and lifecycle_result.flat_evidence:
                        lifecycle_row = self.db.fetch_one(
                            "SELECT * FROM position_lifecycles WHERE lifecycle_id=?",
                            (lifecycle_result.lifecycle_id,))
                        actual_closed = self._iso_from_ms(lifecycle_result.actual_closed_at_ms)
                        self._record_trade_outcome(
                            outcome_id=f"bn-{first_cid}", symbol=symbol, side=pos_side,
                            source=source, entry_price=entry,
                            exit_price=exit_notional / total_filled,
                            qty=float(lifecycle_result.allocated_qty),
                            net_pnl=float(lifecycle_result.net_pnl)
                            if lifecycle_result.net_pnl is not None else None,
                            exit_reason="risk_close_all", status="CLOSED", ledger_meta=meta,
                            opened_at=self._iso_from_ms(int(lifecycle_row["opened_at_ms"] or 0))
                            if lifecycle_row else None,
                            closed_at=actual_closed, lifecycle_id=lifecycle_result.lifecycle_id,
                            attribution_status=lifecycle_result.attribution_status,
                            cost_status=lifecycle_result.cost_status,
                            actual_closed_at=actual_closed,
                            gross_realized=float(lifecycle_result.gross_realized)
                            if lifecycle_result.gross_realized is not None else None,
                            entry_fee=float(lifecycle_row["entry_fee"])
                            if lifecycle_row and lifecycle_row["entry_fee"] is not None else None,
                            exit_fee=float(lifecycle_result.exit_fee)
                            if lifecycle_result.exit_fee is not None else None,
                            funding_cashflow=float(lifecycle_row["funding_cashflow"])
                            if lifecycle_row and lifecycle_row["funding_cashflow"] is not None else None,
                            allow_legacy_fallback=False,
                        )
                    elif reconciler is None:
                        self._record_trade_outcome(
                            outcome_id=f"bn-{first_cid}", symbol=symbol, side=pos_side,
                            source=source, entry_price=entry,
                            exit_price=exit_notional / total_filled, qty=total_filled,
                            net_pnl=total_net_pnl, exit_reason="risk_close_all",
                            status="CLOSED", ledger_meta=meta,
                        )
                if not close_all_unconfirmed and reconciler is not None and lifecycle_result is None:
                    active_lifecycle = self.db.get_active_position_lifecycle(
                        environment=str(self.environment), account_ref=str(reconciler.account_ref),
                        symbol=symbol, position_side=pos_side,
                    )
                    if active_lifecycle is not None:
                        self._reconcile_job_retry(
                            active_lifecycle,
                            lower_bound_ms=max(0, int(active_lifecycle["opened_at_ms"] or 0)
                                               - 10 * 60 * 1000),
                            upper_bound_ms=int(time.time() * 1000),
                            reason="CLOSED_ORDER_FILL_NOT_AVAILABLE",
                        )
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
        # 只清掉确实平掉的 symbol；失败/未完成的仓位保留台账，下一轮继续追。
        # 必须用全新加载的台账做删改（不能沿用方法开头那份旧快照），
        # 否则平仓期间快循环新挂的 LIMIT 会被旧快照覆盖丢失。
        with self._ledger_lock:
            ledger = self._load_ledger()
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
            positions = self.positions()
            all_ok = True
            for position in positions:
                # 即使前一个仓位失败，也必须继续核对后续仓位，不能被 all() 短路。
                current_ok = self._resync_protection(str(position["symbol"]))
                all_ok = current_ok and all_ok
            if positions and all_ok and self.db:
                self.db.resolve_risk_incidents_by_type(
                    "futures_sync_error",
                    "futures_sl_failed",
                    "futures_tp_failed",
                    "futures_protection_fallback",
                )
        except Exception as e:
            if self.db:
                self.db.log_risk_event(event_type="futures_sync_error", severity="warn",
                                       details=f"sync_protections: {type(e).__name__} {e}",
                                       action_taken="skip fast protection sync")

    def protection_coverage(self) -> dict[str, Any]:
        """读取交易所当前保护单，供启动/开仓闸门使用。

        查询失败必须明确返回 ``ok=False``，不能把“不知道”解释成“没有仓位风险”。
        """
        protection_mirror = getattr(self, "protection_order_mirror", None)
        if protection_mirror is not None:
            return protection_mirror.reconcile_broker(self)
        positions = self.positions(force=True)
        missing: list[str] = []
        for pos in positions:
            symbol = str(pos.get("symbol") or "")
            algos = self._open_algos(symbol)
            if algos is None:
                return {"ok": False, "positions": len(positions), "missing": [symbol],
                        "reason": f"{symbol} 保护单查询失败"}
            stop = [a for a in algos if str(a.get("orderType") or "") == "STOP_MARKET"]
            take = [a for a in algos if str(a.get("orderType") or "") == "TAKE_PROFIT_MARKET"]
            if len(stop) != 1 or len(take) != 1:
                missing.append(symbol)
        return {
            "ok": not missing,
            "positions": len(positions),
            "missing": missing,
            "reason": "" if not missing else f"缺少或重复保护单: {', '.join(missing)}",
        }

    def _ensure_lifecycle_from_snapshot(self, position: dict[str, Any],
                                        ledger_meta: dict[str, Any]) -> None:
        """从当前仓位快照建立/补齐生命周期，不把快照本身当成平仓证据。"""
        reconciler = getattr(self, "trade_reconciliation", None)
        if self.db is None or reconciler is None:
            return
        symbol = str(position.get("symbol") or "").upper()
        amount = Decimal(str(position.get("positionAmt") or 0))
        if not symbol or amount == 0:
            return
        explicit_side = str(position.get("positionSide") or "").upper()
        side = explicit_side if explicit_side in {"LONG", "SHORT"} else (
            "LONG" if amount > 0 else "SHORT")
        qty = abs(amount)
        entry_price = Decimal(str(position.get("entryPrice") or 0))
        if entry_price <= 0:
            return
        open_client_id = str(ledger_meta.get("open_client_order_id") or "")
        open_order = (self.db.fetch_one(
            "SELECT created_at,raw_json FROM orders WHERE client_order_id=?", (open_client_id,))
            if open_client_id else None)
        opened_ms = self._parse_iso_ms(open_order["created_at"]) if open_order else None
        exact_intent = (self.db.fetch_open_intent_by_client_order_id(open_client_id)
                        if open_client_id else None)
        lifecycle = reconciler.ensure_lifecycle(
            symbol=symbol, position_side=side, entry_qty=qty, entry_price=entry_price,
            opened_at_ms=opened_ms,
            attribution_status="ATTRIBUTED" if exact_intent is not None else "UNATTRIBUTED",
            entry_release_id=open_client_id or None, external=not bool(open_client_id),
            raw={
                "source": str(ledger_meta.get("source") or "ai"),
                "strategy_id": ledger_meta.get("strategy_id"),
                "ticket_id": ledger_meta.get("ticket_id"),
                "leverage": position.get("leverage"),
            },
        )
        existing_qty = Decimal(str(lifecycle.get("entry_qty") or 0))
        if qty > existing_qty:
            exit_qty = Decimal(str(lifecycle.get("exit_qty") or 0))
            reconciler.db.update_position_lifecycle(
                lifecycle_id=str(lifecycle["lifecycle_id"]),
                entry_qty=float(qty), remaining_qty=float(max(Decimal("0"), qty - exit_qty)),
                entry_notional=float(qty * entry_price),
            )
        if lifecycle.get("entry_fee") is None and open_order:
            try:
                raw_order = json.loads(open_order["raw_json"] or "{}")
                if raw_order.get("commission") is not None:
                    reconciler.db.update_position_lifecycle(
                        lifecycle_id=str(lifecycle["lifecycle_id"]),
                        entry_fee=abs(float(raw_order["commission"])),
                        cost_status="INCOMPLETE",
                    )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

    def sync_positions(self) -> None:
        try:
            pos = self.positions()
            counts: dict[str, int] = {}
            for row in pos:
                symbol = str(row.get("symbol") or "")
                counts[symbol] = counts.get(symbol, 0) + 1
            duplicate_symbols = sorted(symbol for symbol, count in counts.items() if count > 1)
            if duplicate_symbols:
                raise RuntimeError(
                    "同一合约存在双边仓位，拒绝同步来源台账: " + ", ".join(duplicate_symbols))
            ledger = self._load_ledger()
            exchange_symbols = {p["symbol"] for p in pos}
            # 交易所里有、台账没有的旧仓位归为 AI legacy，避免误判成无主仓位
            for p in pos:
                if p["symbol"] not in ledger.get("positions", {}):
                    self._record_ledger_position(
                        p["symbol"], "ai", None, None,
                        str(p.get("marginType") or "ISOLATED").upper(),
                        qty=abs(float(p.get("positionAmt") or 0.0)),
                        entry=float(p.get("entryPrice") or 0.0),
                        position_side=self._position_side(p))
                    if self.db:
                        self.db.log_risk_event(
                            event_type="futures_ledger_adopt_legacy", severity="warn",
                            details=f"{p['symbol']} 交易所已有仓位但来源台账缺失，已归为 ai legacy",
                            action_taken="ledger_adopt_ai")
            # 重新加载台账，避免上面的逐 symbol 保存被旧快照覆盖；删改全程持锁
            with self._ledger_lock:
                ledger = self._load_ledger()
                # 台账里有、交易所已经没有了：说明仓位已在系统视野之外被平掉
                closed_symbols = []
                for symbol in list(ledger.get("positions", {}).keys()):
                    if symbol not in exchange_symbols:
                        ledger.get("positions", {}).pop(symbol, None)
                        closed_symbols.append(symbol)
                self._save_ledger(ledger)
            # 不能只在台账第一次发现平仓时清理：若当时网络失败，symbol 已从台账
            # 删除，后续循环将永远没有重试机会。以交易所当前仓位为真值，每轮重新
            # 发现并二次确认所有无仓位 Algo 单。
            orphan_cleanup = self._retry_orphan_algo_cleanup(exchange_symbols)
            reconcile_targets = list(closed_symbols)
            if self.db and getattr(self, "trade_reconciliation", None) is not None:
                # 已创建但上轮失败的补账任务不能因为 ledger 已删除而失去入口；
                # 只取到期任务，且沿用每轮最多三个 symbol 的预算上限。
                due_jobs = self.db.fetch_all(
                    """SELECT DISTINCT symbol FROM trade_reconciliation_jobs
                       WHERE status IN ('PENDING','RETRY','RUNNING')
                         AND (next_retry_at IS NULL OR next_retry_at<=?)
                       ORDER BY updated_at LIMIT ?""",
                    (utc_now(), self._RECONCILE_MAX_SYMBOLS),
                )
                reconcile_targets.extend(str(row["symbol"]) for row in due_jobs)
            if reconcile_targets:
                self._reconcile_external_closes(list(dict.fromkeys(reconcile_targets)))
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
                    try:
                        self._ensure_lifecycle_from_snapshot(p, meta)
                    except Exception as exc:
                        self.db.log_risk_event(
                            event_type="trade_lifecycle_snapshot_error", severity="warn",
                            details=f"{p['symbol']}: 生命周期快照建立失败 {type(exc).__name__}",
                            action_taken="retain position snapshot; reconcile later")
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
                # 主同步和 Algo 查询都成功，旧的传输故障才可视为恢复。若账户已经
                # 空仓且没有残留保护单，历史 SL/TP/杠杆失败也不再构成当前风险。
                if orphan_cleanup is not None:
                    self.db.resolve_risk_incidents_by_type(
                        "futures_sync_error",
                        "futures_close_nopos",
                    )
                    if orphan_cleanup == 0:
                        self.db.resolve_risk_incidents_by_type(
                            "futures_orphan_protection_canceled"
                        )
                    if not pos and orphan_cleanup == 0:
                        self.db.resolve_risk_incidents_by_type(
                            "futures_sl_failed",
                            "futures_tp_failed",
                            "futures_protection_fallback",
                            "futures_leverage_error",
                        )
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

    @staticmethod
    def _parse_iso_ms(value: object) -> int | None:
        """把 opened_at 之类的 ISO 串解析成毫秒时间戳；解析不了返回 None。

        SQLite 里可能存 '2026-09-02T09:36:06.958+00:00'（带时区）、
        也可能存 '2026-09-02 09:36:06'（datetime('now') 的空格格式），都要能吃下。
        """
        return parse_iso_ms(value)

    @staticmethod
    def _iso_from_ms(value: int | None) -> str | None:
        """把交易所成交时间转换为审计可读时间；不使用本地补账时间冒充成交时间。"""
        if value is None or int(value) <= 0:
            return None
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(
            timespec="milliseconds")

    _RECONCILE_PAGE_SIZE = 1000
    _RECONCILE_MAX_PAGES = 20

    def _fetch_user_trades_window(
        self, symbol: str, *, start_ms: int, end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """按明确时间边界和稳定游标分页读取 userTrades。

        Binance 的省略边界排序不能作为业务契约；首请求使用 start/end，后续有
        数字成交ID时使用 ``fromId`` 推进。每页保留重叠去重，最多读取固定页数，
        防止异常响应让慢循环无限抓历史。
        """
        upper = int(end_ms or time.time() * 1000)
        page_size = int(self._RECONCILE_PAGE_SIZE)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        from_id: int | None = None
        time_cursor = int(start_ms)
        for _ in range(max(1, int(self._RECONCILE_MAX_PAGES))):
            params: dict[str, Any] = {"symbol": symbol, "limit": page_size}
            if from_id is None:
                params["startTime"] = max(0, time_cursor)
            else:
                params["fromId"] = from_id
            params["endTime"] = upper
            page = self._signed("GET", "/fapi/v1/userTrades", params, attempts=1) or []
            if not isinstance(page, list):
                raise TypeError("userTrades 返回结构不是列表")
            if not page:
                break
            numeric_ids: list[int] = []
            max_time = time_cursor
            added = 0
            for item in page:
                if not isinstance(item, dict):
                    raise TypeError("userTrades 列表含非法成交条目")
                raw_id = item.get("id", item.get("tradeId"))
                try:
                    numeric_id = int(raw_id) if raw_id not in (None, "", "-1") else None
                except (TypeError, ValueError):
                    numeric_id = None
                if numeric_id is not None:
                    numeric_ids.append(numeric_id)
                    key = f"id:{numeric_id}"
                else:
                    key = "raw:" + json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
                try:
                    max_time = max(max_time, int(item.get("time") or item.get("T") or 0))
                except (TypeError, ValueError):
                    pass
                if key in seen:
                    continue
                seen.add(key)
                rows.append(dict(item))
                added += 1
            if len(page) < page_size:
                break
            if numeric_ids:
                next_from_id = max(numeric_ids) + 1
                if from_id is not None and next_from_id <= from_id:
                    break
                from_id = next_from_id
            else:
                # 没有稳定ID时不能证明同毫秒满页已经前进；若本页没有新事实，
                # 立即停止并把未完窗口留给补账任务，不能冒险跳过同毫秒成交。
                if added == 0 or max_time <= time_cursor:
                    break
                time_cursor = max_time
        return rows

    def _reconcile_job_retry(
        self, lifecycle: Mapping[str, Any], *, lower_bound_ms: int,
        upper_bound_ms: int | None, reason: str, error: str | None = None,
        attempt_count: int | None = None,
    ) -> None:
        """持久化补账失败/证据不足状态，下一轮仍可继续查询。"""
        if not self.db:
            return
        lifecycle_id = str(lifecycle["lifecycle_id"])
        existing = self.db.fetch_one(
            "SELECT attempt_count FROM trade_reconciliation_jobs WHERE job_id=?",
            (f"mta-reconcile-{lifecycle_id}",),
        )
        attempts = int(attempt_count if attempt_count is not None else (
            int(existing["attempt_count"] or 0) + 1 if existing else 1))
        delay = min(300.0, 5.0 * (2 ** min(attempts - 1, 6)))
        next_retry = datetime.fromtimestamp(
            time.time() + delay, timezone.utc).isoformat(timespec="milliseconds")
        self.db.upsert_trade_reconciliation_job(
            job_id=f"mta-reconcile-{lifecycle_id}", lifecycle_id=lifecycle_id,
            environment=str(lifecycle["environment"]), account_ref=str(lifecycle["account_ref"]),
            symbol=str(lifecycle["symbol"]), position_side=str(lifecycle["position_side"]),
            lower_bound_ms=lower_bound_ms, upper_bound_ms=upper_bound_ms,
            status="RETRY", reason=reason, attempt_count=attempts,
            next_retry_at=next_retry, last_error=error,
        )

    def _lifecycle_context(self, symbol: str) -> tuple[dict[str, Any], dict[str, Any], Decimal] | None:
        """从旧仓位流水或已持久生命周期恢复补账上下文。"""
        if not self.db:
            return None
        rows = self.db.fetch_all(
            """SELECT id,side,qty,leverage,entry_price,opened_at,source,
                      strategy_id,ticket_id FROM positions
               WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1""", (symbol,))
        if rows:
            previous = dict(rows[0])
            position_qty = Decimal("0")
        else:
            lifecycle = self.db.get_active_position_lifecycle(
                environment=str(self.environment),
                account_ref=str(self.trade_reconciliation.account_ref),
                symbol=symbol, position_side="LONG",
            )
            if lifecycle is None:
                lifecycle = self.db.get_active_position_lifecycle(
                    environment=str(self.environment),
                    account_ref=str(self.trade_reconciliation.account_ref),
                    symbol=symbol, position_side="SHORT",
                )
            if lifecycle is None:
                return None
            raw: dict[str, Any] = {}
            try:
                parsed = json.loads(lifecycle["raw_json"] or "{}")
                raw = parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            qty = float(lifecycle["entry_qty"] or 0.0)
            notional = float(lifecycle["entry_notional"] or 0.0)
            previous = {
                "id": None, "side": str(lifecycle["position_side"]), "qty": qty,
                "leverage": int(raw.get("leverage") or 1),
                "entry_price": notional / qty if qty > 0 else 0.0,
                "opened_at": self._iso_from_ms(int(lifecycle["opened_at_ms"] or 0)),
                "source": raw.get("source") or "ai",
                "strategy_id": raw.get("strategy_id"), "ticket_id": raw.get("ticket_id"),
            }
            position_qty = Decimal("0")
        return previous, {}, position_qty

    def _reconcile_stored_lifecycle(
        self, symbol: str, position_side: str, position_qty: Decimal,
    ) -> Any:
        """用已入库的 WS/REST 成交完成本地已确认平仓的生命周期分配。"""
        book = getattr(self, "trade_reconciliation", None)
        if self.db is None or book is None:
            return None
        lifecycle = self.db.get_active_position_lifecycle(
            environment=str(self.environment), account_ref=str(book.account_ref),
            symbol=symbol, position_side=position_side,
        )
        if lifecycle is None:
            return None
        opened_ms = int(lifecycle["opened_at_ms"] or 0)
        upper_ms = int(time.time() * 1000)
        rows = self.db.fetch_execution_fills(
            environment=str(self.environment), account_ref=str(book.account_ref),
            symbol=symbol, lower_bound_ms=max(0, opened_ms - 10 * 60 * 1000),
            upper_bound_ms=upper_ms,
        )
        normalized = []
        for row in rows:
            try:
                normalized.append(normalize_execution_fill(
                    {
                        "symbol": row["symbol"], "trade_id": row["trade_id"],
                        "order_id": row["order_id"], "side": row["side"],
                        "position_side": row["position_side"], "qty": row["qty"],
                        "price": row["price"], "realized_pnl": row["realized_pnl"],
                        "commission": row["commission"],
                        "commission_asset": row["commission_asset"],
                        "trade_time_ms": row["trade_time_ms"],
                    }, environment=str(row["environment"]),
                    account_ref=str(row["account_ref"]), source=str(row["source"]),
                ))
            except Exception:
                # 已经入库的行理论上经过规范化；若旧库夹带坏行，保留其审计事实，
                # 但不让它参与新归因，等待人工/只读修复报告。
                continue
        candidates = select_close_fills(
            normalized, symbol=symbol, position_side=position_side,
            opened_at_ms=opened_ms, upper_bound_ms=upper_ms,
        )
        if not candidates:
            return None
        return book.reconcile(
            str(lifecycle["lifecycle_id"]), candidates,
            position_qty=position_qty, step_size=self._step_size(symbol),
        )

    def _reconcile_one_close_lifecycle(self, symbol: str) -> None:
        """新生命周期路径：成交表、分页任务和闭合 outcome 的唯一入口。"""
        if not self.db or getattr(self, "trade_reconciliation", None) is None:
            return
        context = self._lifecycle_context(symbol)
        if context is None:
            return
        previous, _, position_qty = context
        open_side = str(previous.get("side") or "").upper()
        if open_side not in {"LONG", "SHORT"}:
            return
        open_qty = Decimal(str(previous.get("qty") or 0.0))
        entry_price = Decimal(str(previous.get("entry_price") or 0.0))
        if open_qty <= 0 or entry_price <= 0:
            return
        ledger_meta = {}
        try:
            _, ledger_meta = self._ledger_source(symbol)
        except Exception:
            ledger_meta = {}
        open_client_id = str(ledger_meta.get("open_client_order_id") or "")
        exact_intent = (self.db.fetch_open_intent_by_client_order_id(open_client_id)
                        if open_client_id else None)
        attribution_status = "ATTRIBUTED" if exact_intent is not None else "UNATTRIBUTED"
        opened_ms = self._parse_iso_ms(previous.get("opened_at"))
        if opened_ms is None:
            opened_ms = int(time.time() * 1000) - 24 * 3600 * 1000
        last_closed = self.db.fetch_one(
            """SELECT lifecycle_id,opened_at_ms,entry_release_id FROM position_lifecycles
               WHERE environment=? AND account_ref=? AND symbol=? AND position_side=?
                 AND status='CLOSED' ORDER BY closed_at_ms DESC,recorded_at DESC LIMIT 1""",
            (str(self.environment), str(self.trade_reconciliation.account_ref), symbol, open_side),
        )
        if last_closed is not None and (
                (open_client_id and str(last_closed["entry_release_id"] or "") == open_client_id)
                or int(opened_ms) <= int(last_closed["opened_at_ms"] or 0)):
            # 本地 positions 流水可能尚未在 sync_positions 中收口；若同一开仓
            # 已经完成生命周期，不得再次生成 lifecycle outcome。
            return
        origin_source = str(previous.get("source") or ledger_meta.get("source") or "ai").lower()
        if origin_source not in {"ai", "strategy"}:
            origin_source = "ai"
        lifecycle = self.trade_reconciliation.ensure_lifecycle(
            symbol=symbol, position_side=open_side, entry_qty=open_qty,
            entry_price=entry_price, opened_at_ms=opened_ms,
            attribution_status=attribution_status,
            entry_release_id=open_client_id or None,
            external=not bool(open_client_id),
            raw={
                "source": origin_source, "strategy_id": previous.get("strategy_id")
                or ledger_meta.get("strategy_id"),
                "ticket_id": previous.get("ticket_id") or ledger_meta.get("ticket_id"),
                "leverage": previous.get("leverage"),
            },
        )
        # positions 是快照流水，部分平仓后最新 OPEN 行的 qty/entry 时间可能已经
        # 变化；闭合归因必须以 lifecycle 的累计开仓数量和首次开仓时间为准。
        lifecycle_qty = Decimal(str(lifecycle.get("entry_qty") or open_qty))
        lifecycle_entry_notional = Decimal(str(lifecycle.get("entry_notional") or 0))
        lifecycle_entry_price = (
            lifecycle_entry_notional / lifecycle_qty
            if lifecycle_qty > 0 and lifecycle_entry_notional > 0 else entry_price
        )
        # 开仓订单已确认时，保留真实开仓手续费；缺失则不猜 0。
        if lifecycle.get("entry_fee") is None and open_client_id:
            open_order = self.db.fetch_one(
                "SELECT raw_json FROM orders WHERE client_order_id=?", (open_client_id,))
            if open_order:
                try:
                    raw_order = json.loads(open_order["raw_json"] or "{}")
                    commission = raw_order.get("commission")
                    if commission is not None:
                        self.db.update_position_lifecycle(
                            lifecycle_id=str(lifecycle["lifecycle_id"]),
                            entry_fee=abs(float(commission)), cost_status="INCOMPLETE")
                        lifecycle = dict(self.db.fetch_one(
                            "SELECT * FROM position_lifecycles WHERE lifecycle_id=?",
                            (lifecycle["lifecycle_id"],)))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
        end_ms = int(time.time() * 1000)
        start_ms = max(0, int(opened_ms) - 10 * 60 * 1000)
        job_id = f"mta-reconcile-{lifecycle['lifecycle_id']}"
        existing_job = self.db.fetch_one(
            "SELECT attempt_count FROM trade_reconciliation_jobs WHERE job_id=?", (job_id,))
        attempt = int(existing_job["attempt_count"] or 0) + 1 if existing_job else 1
        self.db.upsert_trade_reconciliation_job(
            job_id=job_id, lifecycle_id=str(lifecycle["lifecycle_id"]),
            environment=str(lifecycle["environment"]), account_ref=str(lifecycle["account_ref"]),
            symbol=symbol, position_side=open_side, lower_bound_ms=start_ms,
            upper_bound_ms=end_ms, status="RUNNING", reason="fetch_user_trades",
            attempt_count=attempt,
        )
        try:
            raw_trades = self._fetch_user_trades_window(
                symbol, start_ms=start_ms, end_ms=end_ms)
            normalized, _ = self.trade_reconciliation.ingest(
                raw_trades, source="rest_reconcile", default_position_side=open_side)
            candidates = select_close_fills(
                normalized, symbol=symbol, position_side=open_side,
                opened_at_ms=int(opened_ms), upper_bound_ms=end_ms)
            if not candidates:
                self._reconcile_job_retry(
                    lifecycle, lower_bound_ms=start_ms, upper_bound_ms=end_ms,
                    reason="NO_MATCHING_CLOSE_FILL", attempt_count=attempt)
                return
            result = self.trade_reconciliation.reconcile(
                str(lifecycle["lifecycle_id"]), candidates,
                position_qty=position_qty, step_size=self._step_size(symbol))
            if result.status == "RECONCILING" or not result.flat_evidence:
                self._reconcile_job_retry(
                    lifecycle, lower_bound_ms=start_ms, upper_bound_ms=end_ms,
                    reason=("CLOSE_QUANTITY_MISMATCH" if result.status == "RECONCILING"
                            else "POSITION_NOT_FLAT_WITH_COMPLETE_FILL_EVIDENCE"),
                    attempt_count=attempt)
                if result.status == "RECONCILING":
                    self.db.log_risk_event(
                        event_type="futures_close_reconcile_mismatch", severity="warn",
                        details=f"{symbol} 生命周期成交数量与仓位证据不一致，保留补账任务",
                        action_taken="retry reconciliation")
                return

            allocations = self.db.fetch_lifecycle_allocations(str(lifecycle["lifecycle_id"]))
            normalized_by_key = {row.fill_key: row for row in normalized}
            groups: dict[str, list[tuple[Any, Decimal]]] = {}
            for allocation in allocations:
                key = str(allocation["fill_key"])
                fill = normalized_by_key.get(key)
                if fill is None:
                    continue
                order_key = fill.order_id or f"fill:{key}"
                groups.setdefault(order_key, []).append(
                    (fill, Decimal(str(allocation["allocated_qty"]))))
            for order_key, group in groups.items():
                if self.db.fetch_one(
                        "SELECT id FROM orders WHERE exchange_order_id=? LIMIT 1", (order_key,)):
                    continue
                qty = sum((allocated for _, allocated in group), Decimal("0"))
                notional = sum((allocated * fill.price for fill, allocated in group), Decimal("0"))
                gross: Decimal | None = Decimal("0")
                fee: Decimal | None = Decimal("0")
                assets: set[str] = set()
                for fill, allocated in group:
                    ratio = allocated / fill.qty if fill.qty else Decimal("0")
                    if fill.realized_pnl is None:
                        gross = None
                    elif gross is not None:
                        gross += fill.realized_pnl * ratio
                    if fill.commission is None or not fill.commission_asset:
                        fee = None
                    elif fee is not None:
                        fee += fill.commission * ratio
                    assets.add(str(fill.commission_asset or "").upper())
                net = gross - fee if gross is not None and fee is not None and assets <= {"USDT"} else None
                average_price = notional / qty if qty else Decimal("0")
                self.db.log_order(
                    client_order_id=f"mta-sync-{symbol}-{order_key}",
                    exchange_order_id=order_key, symbol=symbol, side=open_side,
                    order_type="MARKET", qty=float(qty), price=float(average_price),
                    status="FILLED",
                    raw={
                        "mode": self.environment, "action": "close", "reconciled": True,
                        "sync_source": "exchange_sync", "fills": float(qty),
                        "trade_ids": [fill.trade_id for fill, _ in group if fill.trade_id],
                        "gross_realized": float(gross) if gross is not None else None,
                        "commission": -float(fee) if fee is not None else None,
                        "net_pnl": float(net) if net is not None else None,
                        "partial": False,
                        "note": "保护单触发或交易所手动平仓，由生命周期成交流水补记",
                    },
                    order_source=origin_source,
                    strategy_id=previous.get("strategy_id") or ledger_meta.get("strategy_id"),
                    ticket_id=previous.get("ticket_id") or ledger_meta.get("ticket_id"),
                    margin_mode=str(ledger_meta.get("margin_mode", "ISOLATED") or "ISOLATED"),
                )
            actual_closed = self._iso_from_ms(result.actual_closed_at_ms)
            self._record_trade_outcome(
                outcome_id=f"lifecycle-{lifecycle['lifecycle_id']}", symbol=symbol,
                side=open_side, source=origin_source, entry_price=float(lifecycle_entry_price),
                exit_price=float(sum((fill.price * allocated for fill, allocated in
                                      [(normalized_by_key.get(str(a["fill_key"])),
                                        Decimal(str(a["allocated_qty"])))
                                       for a in allocations]
                                      if fill is not None), Decimal("0")) / lifecycle_qty),
                qty=float(lifecycle_qty), net_pnl=float(result.net_pnl)
                if result.net_pnl is not None else None,
                exit_reason="exchange_sync", status="CLOSED", ledger_meta={
                    "open_client_order_id": open_client_id,
                    "strategy_id": previous.get("strategy_id") or ledger_meta.get("strategy_id"),
                    "ticket_id": previous.get("ticket_id") or ledger_meta.get("ticket_id"),
                }, opened_at=self._iso_from_ms(int(lifecycle["opened_at_ms"] or 0))
                or (str(previous.get("opened_at") or "") or None),
                closed_at=actual_closed, lifecycle_id=str(lifecycle["lifecycle_id"]),
                attribution_status=result.attribution_status,
                cost_status=result.cost_status, actual_closed_at=actual_closed,
                gross_realized=float(result.gross_realized)
                if result.gross_realized is not None else None,
                entry_fee=float(lifecycle["entry_fee"])
                if lifecycle.get("entry_fee") is not None else None,
                exit_fee=float(result.exit_fee) if result.exit_fee is not None else None,
                funding_cashflow=float(lifecycle["funding_cashflow"])
                if lifecycle.get("funding_cashflow") is not None else None,
                allow_legacy_fallback=False,
            )
            self.db.update_trade_reconciliation_job(
                job_id=job_id, status="DONE", reason="lifecycle_closed",
                last_error=None, next_retry_at=None, attempt_count=attempt)
        except Exception as exc:
            self._reconcile_job_retry(
                lifecycle, lower_bound_ms=start_ms, upper_bound_ms=end_ms,
                reason="RECONCILE_ERROR", error=f"{type(exc).__name__}: {exc}",
                attempt_count=attempt)
            raise

    def _reconcile_one_close(self, symbol: str) -> None:
        """补记单个 symbol 最近一笔未被记录的平仓成交。

        2026-09-02 修：原实现有两个错误假设，导致补记出来的数据是**错的**
        （实测把 08-30 的陈年成交当成 09-02 的平仓，记成 3.0 @ 4.919、净亏手续费；
         交易所真实成交是 10.0 @ 5.8830、realizedPnl=+3.575）：

        1. 以为 `/fapi/v1/userTrades` 不传 startTime 时「按时间倒序、最新在前」。
           实测恰好相反——它从**账户史上最早的成交**开始正序返回。于是 limit=20
           取到的是最老的 20 笔，最近的平仓根本不在列表里；再加上 break 取第一个，
           命中的就是历史深处第一笔方向相反的成交。
        2. 以为一笔订单只会产生一笔成交。市价单常被拆成多笔
           （实测同一 orderId 拆成 3+8+9 共 20 张），只取遍历到的第一笔必然漏量。

        修法：显式传 startTime 卡时间窗 → 显式按 time 倒序 → 按 orderId 聚合成交
        → 校验时间晚于开仓、数量能覆盖持仓量，任一不过就放弃（宁缺毋滥）。
        """
        if getattr(self, "trade_reconciliation", None) is not None:
            self._reconcile_one_close_lifecycle(symbol)
            return
        rows = self.db.fetch_all(
            "SELECT id, side, qty, leverage, entry_price, opened_at, source, "
            "strategy_id, ticket_id FROM positions "
            "WHERE symbol=? AND status='OPEN' ORDER BY id DESC LIMIT 1", (symbol,))
        if not rows:
            return
        prev = dict(rows[0])
        open_side = str(prev.get("side") or "").upper()
        if open_side not in ("LONG", "SHORT"):
            return
        # 平多要卖出、平空要买回，方向与原仓位相反
        want_side = "SELL" if open_side == "LONG" else "BUY"
        open_qty = abs(float(prev.get("qty") or 0.0))
        entry_price = float(prev.get("entry_price") or 0.0)
        leverage = int(float(prev.get("leverage") or 1)) or 1
        if open_qty <= 0:
            return
        # 归属：补记的平仓必须回到原开仓通道（strategy/ai），否则通道盈亏
        # 统计漏掉这笔（实测 AI 仓被交易所侧平掉后，+20.24 净利不入任何
        # 通道，渠道合计比总权益少同样金额）。exchange_sync 只作来源标记
        # 写进 raw，供区分“通道自己平”与“同步补记”。
        origin_source = str(prev.get("source") or "ai").strip().lower()
        if origin_source not in ("strategy", "ai"):
            origin_source = "ai"

        # 时间窗下界：开仓时刻往前 10 分钟。不传 startTime 时接口会从最早成交开始
        # 返回，最近的成交反而取不到，所以必须显式卡窗口。
        opened_ms = self._parse_iso_ms(prev.get("opened_at"))
        start_ms = (opened_ms if opened_ms is not None
                    else int(time.time() * 1000) - 24 * 3600 * 1000) - 10 * 60 * 1000

        # attempts=1：demo 环境这个接口经代理很慢，重试两次能把慢循环拖到几十秒
        trades = self._signed("GET", "/fapi/v1/userTrades",
                              {"symbol": symbol, "limit": 100,
                               "startTime": start_ms}, attempts=1) or []
        if not isinstance(trades, list):
            return

        # 按 orderId 聚合：同一笔平仓单常被拆成多笔成交，合并后才是真实数量与均价
        groups: dict[str, dict[str, float]] = {}
        for t in trades:
            if str(t.get("side") or "").upper() != want_side:
                continue
            t_ms = int(t.get("time") or 0)
            if t_ms <= start_ms:  # 开仓之前的成交，与本次平仓无关
                continue
            oid = str(t.get("orderId") or "")
            if not oid:
                continue
            # 去重：这笔成交已经记过（系统自己下的单）就整单跳过
            if self.db.fetch_all("SELECT id FROM orders WHERE exchange_order_id=? LIMIT 1", (oid,)):
                continue
            q = abs(float(t.get("qty") or 0.0))
            px = float(t.get("price") or 0.0)
            g = groups.setdefault(oid, {"qty": 0.0, "notional": 0.0,
                                        "realized": 0.0, "commission": 0.0,
                                        "time": float(t_ms)})
            g["qty"] += q
            g["notional"] += q * px
            g["realized"] += float(t.get("realizedPnl") or 0.0)
            g["commission"] += abs(float(t.get("commission") or 0.0))
            g["time"] = max(g["time"], float(t_ms))
        if not groups:
            return

        # 时间最新优先；数量/时间对不上的一律放弃，避免污染台账
        for oid, g in sorted(groups.items(), key=lambda kv: kv[1]["time"], reverse=True):
            qty = g["qty"]
            if qty <= 0 or g["notional"] <= 0:
                continue
            price = g["notional"] / qty
            realized = g["realized"]
            commission = g["commission"]

            # 只挡「数量远超持仓」——那说明抓到的是别的单（历史陈账、他人的成交）。
            # 不挡下界：交易所端的部分平仓是真实场景，少平几张也要如实补记。
            if qty > open_qty * 1.05:
                self.db.log_risk_event(
                    event_type="futures_close_reconcile_mismatch", severity="warn",
                    details=(f"{symbol} 补记数量 {qty:g} 远超持仓 {open_qty:g}，判定为抓错单，"
                             f"放弃补记（宁缺毋滥，避免污染 order 台账）"),
                    action_taken="skip")
                continue
            partial = qty < open_qty * 0.98

            # 交易所对非平仓成交返回的 realizedPnl 恒为 0，此时按开仓价手算，
            # 免得把一笔真实盈利记成「只亏了手续费」。
            if abs(realized) < 1e-12 and entry_price > 0:
                diff = (price - entry_price) if open_side == "LONG" else (entry_price - price)
                realized = diff * qty

            self.db.log_order(
                client_order_id=f"mta-sync-{symbol}-{oid}",
                exchange_order_id=oid,
                symbol=symbol,
                # side 传原仓位方向，配合 raw.action='close'，
                # _enrich_orders 会据此显示成"平多"/"平空"
                side=open_side,
                order_type="MARKET",
                qty=qty,
                price=round(price, 8),
                status="FILLED",
                raw={
                    "action": "close",
                    "net_pnl": realized - commission,
                    "commission": -commission,
                    "margin": round(qty * price / leverage, 8),
                    "reconciled": True,
                    "sync_source": "exchange_sync",
                    "fills": round(qty, 8),
                    "partial": partial,
                    "note": "保护单触发或交易所手动平仓，由成交流水补记",
                },
                order_source=origin_source,
                margin_mode="ISOLATED",
            )
            self._record_trade_outcome(
                outcome_id=f"bn-sync-{symbol}-{oid}", symbol=symbol, side=open_side,
                source=origin_source, entry_price=entry_price, exit_price=price, qty=qty,
                net_pnl=realized - commission, exit_reason="exchange_sync",
                status="REDUCED" if partial else "CLOSED",
                ledger_meta={
                    "strategy_id": prev.get("strategy_id"),
                    "ticket_id": prev.get("ticket_id"),
                },
                opened_at=str(prev.get("opened_at") or "") or None,
            )
            self.db.log_risk_event(
                event_type="futures_close_reconciled", severity="info",
                details=(f"{symbol} {open_side} {qty:g} @ {price:.6f} 在交易所被平仓"
                         f"{'（部分平仓）' if partial else ''}，"
                         f"已由成交流水补记（已实现盈亏 {realized - commission:+.4f}）"),
                action_taken="order_reconciled")
            return

    def close(self) -> None:
        with self._client_lock:
            self._client.close()
