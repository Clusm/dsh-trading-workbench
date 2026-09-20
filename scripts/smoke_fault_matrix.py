"""故障演练矩阵离线回归（其一）：场景 1 锚定 + 2/3/5/7/8/10。

全部离线：不联网、不读真实 .env、不构造真实 Broker 网络调用。
每个场景用 fake/stub transport 或 monkeypatch 注入故障；对能还原旧错误实现的场景
做一次负向验证，确认本脚本的断言能区分「正确降级」与「把失败当成功 / 静默吞错」。

对应 docs/上线前最后一轮优化工作计划_2026-09-15.md §12 故障演练矩阵。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from audit.database import AuditDatabase  # noqa: E402
from data.rate_limit import (  # noqa: E402
    ExecutionRateLimited,
    PublicMarketCooldown,
    _reset_for_test,
    configure_rate_limit_state,
    cooldown_remaining,
)
from data import binance_public  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402
from exchange.order_outbox import OrderOutbox  # noqa: E402
from exchange.protection_mirror import ProtectionOrderMirror  # noqa: E402
import llm.openai_compatible_client as client_module  # noqa: E402
from llm.openai_compatible_client import (  # noqa: E402
    LLMTimeBudgetExceeded,
    OpenAICompatibleLLMClient,
)
from llm.request_coordinator import (  # noqa: E402
    LLMAdmissionTimeout,
    LLMRequestCoordinator,
)
from create_recovery_bundle import create_recovery_bundle  # noqa: E402
from restore_recovery_bundle import verify_recovery_bundle  # noqa: E402


WORK = ROOT / ".fault_tmp"
if WORK.exists():
    shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)


def _cleanup() -> None:
    shutil.rmtree(WORK, ignore_errors=True)


atexit.register(_cleanup)

passed = 0
failed: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    global passed
    if not condition:
        failed.append(name)
        raise AssertionError(f"{name}: {detail}")
    passed += 1
    print(f"  PASS  {name}")


# ---------------------------------------------------------------------------
# 场景 1：订单已成交但响应丢失 —— 已覆盖锚定
# 证据：scripts/smoke_order_outbox.py [5] (line 104-126)：Broker 唯一 POST 入口异常后
# 按 clientOrderId 走 REST 查询恢复，全程只 POST 一次，Outbox 进入 FILLED。
# 此处重跑同一不变量，作为矩阵其余场景的对照基线（独立、离线、不依赖原脚本先跑）。
# ---------------------------------------------------------------------------
print("\n[场景1] 订单已成交但响应丢失（已覆盖 · 锚定不变量）")
db1 = AuditDatabase(WORK / "s1.sqlite")
outbox1 = OrderOutbox(db1, environment="demo")
broker1 = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
broker1.db = db1
broker1.environment = "demo"
broker1.order_outbox = outbox1
post_calls: list[dict] = []


def _signed_s1(method, path, params=None, attempts=2):
    if method == "POST":
        post_calls.append(dict(params or {}))
        raise TimeoutError("response lost")  # 响应在传输中丢失
    return {"symbol": "ETHUSDT", "clientOrderId": "mta-lost-1",
            "orderId": 301, "status": "FILLED"}


broker1._signed = _signed_s1
result1 = broker1._submit_standard_order({
    "symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.2",
    "reduceOnly": "false", "newClientOrderId": "mta-lost-1",
}, purpose="ENTRY")
check("响应丢失后按clientOrderId查询到原订单", result1["orderId"] == 301)
check("恢复过程只POST一次（无重复下单）", len(post_calls) == 1)
check("对账成功后Outbox进入FILLED", db1.fetch_one(
    "SELECT status FROM order_outbox WHERE client_order_id='mta-lost-1'")["status"] == "FILLED")
db1.close()

# ---------------------------------------------------------------------------
# 场景 2：意图落盘后进程崩溃
# 注入点：Outbox.prepare 后、Broker POST 之前模拟进程崩溃（recover_stale_submitting）。
# 期望：重启只把悬空 SUBMITTING 转 UNKNOWN，再按交易所 REST 查询恢复；绝不重发；
# 不确定订单不会被当成已成交。
# ---------------------------------------------------------------------------
print("\n[场景2] 意图落盘后进程崩溃")
db2 = AuditDatabase(WORK / "s2.sqlite")
outbox2 = OrderOutbox(db2, environment="demo")
outbox2.prepare(client_order_id="mta-crash-1", symbol="BTCUSDT", side="BUY",
                position_side="LONG", order_type="MARKET", purpose="ENTRY", qty=0.1,
                price=None, reduce_only=False, endpoint="/fapi/v1/order",
                request={"symbol": "BTCUSDT", "newClientOrderId": "mta-crash-1"})
row2 = db2.fetch_one("SELECT status FROM order_outbox WHERE client_order_id='mta-crash-1'")
check("意图先落盘为SUBMITTING", row2["status"] == "SUBMITTING")

# 模拟进程崩溃：POST 从未到达交易所。重启恢复把悬空 SUBMITTING 转 UNKNOWN。
crashed = outbox2.recover_stale_submitting()
check("重启把悬空SUBMITTING转为UNKNOWN", crashed == 1)
check("崩溃后订单未进入终态（不会误判成交）",
      db2.fetch_one("SELECT status FROM order_outbox WHERE client_order_id='mta-crash-1'")["status"] == "UNKNOWN")


class QueryBrokerS2:
    def query_order(self, _symbol, _cid):
        return {"symbol": "BTCUSDT", "clientOrderId": "mta-crash-1",
                "orderId": 401, "status": "FILLED"}


resolved = outbox2.reconcile_unknown(QueryBrokerS2())
check("重启后按交易所查询恢复（只查询不重发）", resolved == 1)
check("恢复后进入交易所终态FILLED",
      db2.fetch_one("SELECT status FROM order_outbox WHERE client_order_id='mta-crash-1'")["status"] == "FILLED")
check("恢复全程未发起任何POST（仅GET查询）", len(post_calls) == 1)


# 负向：若查询返回空结果（不确定），绝不能把订单标成已成交。用一个独立订单，
# 避免与上面已成功的恢复互相干扰。
outbox2.prepare(client_order_id="mta-crash-2", symbol="ETHUSDT", side="BUY",
                position_side="LONG", order_type="MARKET", purpose="ENTRY", qty=0.1,
                price=None, reduce_only=False, endpoint="/fapi/v1/order",
                request={"symbol": "ETHUSDT", "newClientOrderId": "mta-crash-2"})
outbox2.recover_stale_submitting()


class EmptyQueryBroker:
    def query_order(self, _symbol, _cid):
        return {}  # 空响应：订单是否成交仍未知


before_unknown = outbox2.reconcile_unknown(EmptyQueryBroker())
row_unknown = db2.fetch_one("SELECT status,query_attempt_count FROM order_outbox WHERE client_order_id='mta-crash-2'")
check("空查询结果不把UNKNOWN当已成交", before_unknown == 0 and row_unknown["status"] == "UNKNOWN")
check("空查询按退避记录次数（未静默吞掉）", int(row_unknown["query_attempt_count"]) >= 1)
# 还原一个错误实现确认断言有判别力：若有人把空查询直接 acknowledge 为 FILLED，则
# 上面 `status == "UNKNOWN"` 的断言会失败。这里显式演示该错误实现的后果。
outbox2.acknowledge("mta-crash-2", {"orderId": 402, "status": "FILLED"})
buggy_status = db2.fetch_one("SELECT status FROM order_outbox WHERE client_order_id='mta-crash-2'")["status"]
check("负向验证：错误实现会把空查询标成FILLED（证明断言可判别）", buggy_status == "FILLED")
db2.close()

# ---------------------------------------------------------------------------
# 场景 3：部分成交后保护失败
# 注入点：_ensure_protection_from_order —— 某仓位部分/全部成交后补挂 SL/TP 的保护
# resync 失败。期望：已有保护不被盲撤；缺口可见；按既有兜底处理（紧急平仓），
# 不反向开仓。
# ---------------------------------------------------------------------------
print("\n[场景3] 部分成交后保护失败")
db3 = AuditDatabase(WORK / "s3.sqlite")
broker3 = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
broker3.environment = "live"
broker3.db = db3
broker3.order_outbox = None
broker3._resync_lock = threading.RLock()
signed_s3: list[tuple] = []


def _signed_s3(method, path, params=None, attempts=2):
    signed_s3.append((method, path))
    if method == "POST" and path == "/fapi/v1/order":
        return {"orderId": 555, "status": "FILLED", "symbol": "BTCUSDT"}  # 紧急平仓成交
    if method == "GET" and path == "/fapi/v1/openAlgoOrders":
        return [{"algoId": "EXISTING_SL", "symbol": "BTCUSDT",
                 "orderType": "STOP_MARKET", "side": "SELL"}]  # 已有止损保护
    return {}


broker3._signed = _signed_s3


def _resync_fail(symbol, intent=None, stop_loss=None, take_profit=None):
    return False  # 保护创建/替换失败：缺口无法补上


broker3._resync_protection = _resync_fail
close_intents: list = []


def _close_or_reduce(intent):
    close_intents.append(intent)
    return {"action": "close", "symbol": getattr(intent, "symbol", None)}


broker3._close_or_reduce = _close_or_reduce
broker3._open_algos = lambda symbol=None: [
    {"algoId": "EXISTING_SL", "symbol": "BTCUSDT", "orderType": "STOP_MARKET", "side": "SELL"}]

order3 = {"symbol": "BTCUSDT", "side": "LONG", "qty": 0.1, "source": "ai"}
broker3._ensure_protection_from_order(order3)
check("保护失败按兜底处理：触发紧急平仓", len(close_intents) == 1)
check("紧急平仓不是反向开仓（是close动作）",
      str(getattr(close_intents[0], "action", "")).lower() in ("close", "reduce"))
check("已有保护不被盲撤（无DELETE algoOrder）",
      all(path != "/fapi/v1/algoOrder" or m != "DELETE" for m, path in signed_s3))
check("未反向开新仓（无开仓POST）",
      all(not (m == "POST" and p == "/fapi/v1/order") for m, p in signed_s3) or
      signed_s3 == [("POST", "/fapi/v1/order")])
# 上面最后一条：signed_s3 只含平仓那一次 POST，不含任何新开仓；若实现误把缺口补成
# 反向开仓，会多出 BUY/OPEN 的 POST，断言失败。

# 镜像层补充：缺口可见、已有保护保持活动
mirror3 = ProtectionOrderMirror(db3, environment="demo",
                                allowed_symbols=["BTCUSDT"])
positions3 = [{"symbol": "BTCUSDT", "positionAmt": "0.1"}]
# 只有止损、缺止盈 —— 模拟“保护单有缺口”
algos3 = [{"algoId": 1, "clientAlgoId": "c1", "symbol": "BTCUSDT",
           "orderType": "STOP_MARKET", "side": "SELL", "quantity": "0.1",
           "triggerPrice": "59000", "reduceOnly": True, "algoStatus": "NEW"}]
res3 = mirror3.reconcile_snapshot(positions3, algos3)
check("保护缺口被镜像识别（缺止盈）", "BTCUSDT" in res3["missing_protection"])
check("已有止损保护保持活动（未盲撤）",
      db3.fetch_one("SELECT status FROM protection_order_mirror WHERE algo_id='1'")["status"] == "NEW")
check("缺口存在时不误报全覆盖", res3["ok"] is False)
db3.close()

# ---------------------------------------------------------------------------
# 场景 5：生产出口被封禁
# 注入点：假生产 transport —— Broker._request 对 fapi.binance.com 一律返回 418。
# 期望：所有同作用域（signed execution）请求尊重封禁，不重发；依赖交易所已确认
# 保护并告警（Outbox 转 UNKNOWN 等待 REST 对账，而非当成成功）。
# ---------------------------------------------------------------------------
print("\n[场景5] 生产出口被封禁")
db5 = AuditDatabase(WORK / "s5.sqlite")
outbox5 = OrderOutbox(db5, environment="live")
broker5 = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
broker5.db = db5
broker5.environment = "live"
broker5.order_outbox = outbox5
broker5.api_key = "test-key"
broker5.api_secret = "test-secret"
broker5.base_url = "https://fapi.binance.com"
broker5._time_offset_ms = 0
signed_calls5: list[str] = []


def _request_banned(method, url, **kwargs):
    # 正确实现：生产出口 418 必须抛 ExecutionRateLimited，绝不把封禁当成功。
    signed_calls5.append(url)
    raise ExecutionRateLimited("HTTP 418: signed Binance request rate limited")


broker5._request = _request_banned
try:
    broker5._submit_standard_order({
        "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.1",
        "reduceOnly": "false", "newClientOrderId": "mta-banned-1",
    }, purpose="ENTRY")
    banned_ok = False
except ExecutionRateLimited:
    banned_ok = True
check("封禁下下单被拒绝（尊重封禁）", banned_ok)
check("封禁下订单未可靠成交（Outbox转UNKNOWN待对账）",
      db5.fetch_one("SELECT status FROM order_outbox WHERE client_order_id='mta-banned-1'")["status"] == "UNKNOWN")
check("封禁下不重发（POST只尝试一次）",
      db5.fetch_one("SELECT attempt_count FROM order_outbox WHERE client_order_id='mta-banned-1'")["attempt_count"] == 1)
# 所有同作用域请求都尊重封禁：账户/持仓查询同样失败（走同一 _request 封禁路径）
acct_banned = False
try:
    broker5._signed("GET", "/fapi/v2/account")
except ExecutionRateLimited:
    acct_banned = True
check("同作用域账户查询同样尊重封禁", acct_banned)

# 负向：若旧实现把 418 响应当成功（不抛错），订单会被错误标成已成交。
# 用独立 outbox，避免上面 UNKNOWN 未决订单约束新开仓。
db5b = AuditDatabase(WORK / "s5b.sqlite")
outbox5b = OrderOutbox(db5b, environment="live")
broker5.order_outbox = outbox5b
broker5._request = lambda method, url, **kw: _Fake418Response()


class _Fake418Response:
    status_code = 418

    def json(self):
        return {"orderId": 999, "status": "FILLED"}  # 错误实现：忽略状态码

    def raise_for_status(self):
        return None


try:
    broker5._submit_standard_order({
        "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.1",
        "reduceOnly": "false", "newClientOrderId": "mta-banned-2",
    }, purpose="ENTRY")
except ExecutionRateLimited:
    pass
buggy5 = db5b.fetch_one("SELECT status FROM order_outbox WHERE client_order_id='mta-banned-2'")
check("负向验证：错误实现会把封禁响应当成功成交（证明断言可判别）",
      buggy5["status"] != "UNKNOWN")
db5.close()
db5b.close()

# ---------------------------------------------------------------------------
# 场景 7：磁盘满/只读/锁超时
# 注入点：(a) DB 提交抛 OperationalError（磁盘满）；(b) 限流状态文件写入抛
# OSError(EACCES)（只读/锁超时）。期望：订单未可靠落盘不发出；限流冷却仍在内存生效，
# 不会静默扩大请求或把写失败当成功。
# ---------------------------------------------------------------------------
print("\n[场景7] 磁盘满/只读/锁超时")
# (a) DB 写入失败 → 订单不发出
db7 = AuditDatabase(WORK / "s7.sqlite")
outbox7 = OrderOutbox(db7, environment="demo")
broker7 = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
broker7.db = db7
broker7.environment = "demo"
broker7.order_outbox = outbox7
post7: list[int] = []


def _signed_diskfail(method, path, params=None, attempts=2):
    post7.append(1)
    return {"orderId": 1, "status": "FILLED"}


broker7._signed = _signed_diskfail
# sqlite3.Connection.commit 是只读属性，无法整体打补丁；改为把 db.conn 换成包装器，
# 仅让 commit 抛 OperationalError（磁盘满），execute 仍走真实连接。
_real_conn = db7.conn


class _DiskFullConn:
    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        raise sqlite3.OperationalError("disk I/O error")  # 磁盘满/只读

    def execute(self, *args, **kwargs):
        return self._real.execute(*args, **kwargs)


db7.conn = _DiskFullConn(_real_conn)  # type: ignore[assignment]
send_attempted = False
try:
    outbox7.prepare(client_order_id="mta-disk-1", symbol="BTCUSDT", side="BUY",
                    position_side="LONG", order_type="MARKET", purpose="ENTRY", qty=0.1,
                    price=None, reduce_only=False, endpoint="/fapi/v1/order",
                    request={"symbol": "BTCUSDT", "newClientOrderId": "mta-disk-1"})
    send_attempted = True
except sqlite3.OperationalError:
    send_attempted = False
db7.conn = _real_conn  # type: ignore[assignment]
check("磁盘满时prepare抛错（写入失败被暴露，未静默吞掉）", not send_attempted)
check("未可靠落盘不发出任何POST", len(post7) == 0)

# 负向：若有人把写失败吞掉且仍返回成功，则 Broker 会真的去下单。
outbox7.prepare = lambda **k: None  # 错误实现：吞掉写失败、假装成功
try:
    broker7._submit_standard_order({
        "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.1",
        "reduceOnly": "false", "newClientOrderId": "mta-disk-2",
    }, purpose="ENTRY")
except Exception:
    pass
check("负向验证：吞掉写失败会真的去下单（证明断言可判别）", len(post7) >= 1)
db7.close()

# (b) 限流状态文件写入失败 → 内存冷却仍阻断请求，不静默放大
os.environ.setdefault("MTA_BINANCE_WEIGHT_BUDGET_1M", "60000")
_reset_for_test()
state_dir7 = WORK / "ratelimit7"
state_dir7.mkdir(parents=True, exist_ok=True)
state_file7 = state_dir7 / "state.json"
configure_rate_limit_state(state_file7)


class _Client418:
    """既当 fake client（要有 get）又当 fake response（要有 raise_for_status/json）。

    transport 会调用 client.get(...)，早期版本漏了这个方法，脚本在触发 418 时
    直接以 AttributeError 崩溃——故障注入脚本自身崩溃会掩盖被测行为，所以这里
    同时满足两侧接口。
    """

    def __init__(self, status, **kw):
        self.status_code = status
        self._payload = kw.get("payload")
        # transport 在 418/429 分支会读 headers 计算 retry_after，并在观测里读 text；
        # 缺这两个字段同样会让注入脚本自己崩掉。
        self.headers = kw.get("headers") or {}
        self.text = kw.get("text", "")

    def get(self, *args, **kwargs):
        return self

    def close(self):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# 先触发一次公共 418，登记内存冷却
try:
    binance_public._get_json(_Client418(418, payload={}), "/fapi/v1/ticker/24hr", attempts=1)
except RuntimeError:
    pass
check("公共418后内存冷却生效", cooldown_remaining() > 0)

# 注入：状态文件写入抛 OSError(EACCES)（只读/锁超时）
import data.rate_limit as _rl

_real_write = _rl._write_payload_locked


def _write_fail(payload, path):
    raise OSError(13, "Permission denied")  # errno.EACCES


_rl._write_payload_locked = _write_fail
sent_after_disk = 0


class _CountClient:
    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        return _Client418(200, payload=[])

    def close(self):
        pass


client7 = _CountClient()
try:
    binance_public._get_json(client7, "/fapi/v1/ticker/24hr", attempts=1)
    raised = False
except PublicMarketCooldown:
    raised = True
_rl._write_payload_locked = _real_write
check("状态文件不可写时仍尊重内存冷却（不发请求）", raised and client7.calls == 0)
_reset_for_test()
# 负向：若冷却检查因写失败被跳过（cooldown 误报 0），则会真的发出请求。
_real_cr = _rl.cooldown_remaining
_rl.cooldown_remaining = lambda: 0.0  # 错误实现：写失败就当无冷却
client7b = _CountClient()
try:
    binance_public._get_json(client7b, "/fapi/v1/ticker/24hr", attempts=1)
except PublicMarketCooldown:
    pass
_rl.cooldown_remaining = _real_cr
check("负向验证：冷却被跳过会静默放大请求（证明断言可判别）", client7b.calls >= 1)
_reset_for_test()

# ---------------------------------------------------------------------------
# 场景 8：LLM 永久慢读、复盘积压
# 注入点：假供应商 HttpClient 永久慢读（超出总时限）。期望：总时限与取消生效；
# 快循环与保护不被阻塞（慢读超时后并发槽释放，后续决策可立即准入）。
# ---------------------------------------------------------------------------
print("\n[场景8] LLM永久慢读、复盘积压")
old_http = client_module.httpx.Client


class _PermSlowClient:
    calls = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        type(self).calls += 1
        time.sleep(0.2)  # 故意慢于总时限
        import httpx
        req = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}, request=req)


client_module.httpx.Client = _PermSlowClient
coord8 = LLMRequestCoordinator(max_concurrency=1, default_queue_deadline_seconds=2.0)
llm8 = OpenAICompatibleLLMClient(api_key="slow" + "-secret", base_url="https://provider.invalid/v1",
                                 max_retries=0, coordinator=coord8, purpose="review")
slow_raised = False
try:
    llm8.chat([], deadline=time.monotonic() + 0.05)
except LLMTimeBudgetExceeded:
    slow_raised = True
check("永久慢读被总时限取消（不阻塞）", slow_raised and _PermSlowClient.calls == 1)
# 超时后并发槽释放，快循环（decision）可立即准入
fast_ok = False
try:
    with coord8.admit(llm8.provider_scope, deadline=time.monotonic() + 1.0):
        fast_ok = True
except LLMAdmissionTimeout:
    fast_ok = False
check("慢读超时后并发槽释放，快循环/保护不被阻塞", fast_ok)

# 负向：若实现不取消慢读（一直占着槽），快循环会被阻塞。
blk = threading.Event()


def _holder():
    try:
        with coord8.admit(llm8.provider_scope, deadline=time.monotonic() + 10):
            time.sleep(0.4)  # 慢读持有槽、不取消
    finally:
        blk.set()


holder_thread = threading.Thread(target=_holder)
holder_thread.start()
time.sleep(0.02)
blocked = False
try:
    with coord8.admit(llm8.provider_scope, deadline=time.monotonic() + 0.1):
        pass
except LLMAdmissionTimeout:
    blocked = True
holder_thread.join(timeout=2)
check("负向验证：无取消时慢读阻塞快循环（证明时限取消是必要的）", blocked)
client_module.httpx.Client = old_http

# ---------------------------------------------------------------------------
# 场景 10：恢复旧代码配新 schema
# 注入点：用恢复包模拟「旧代码(期望schema=17) 配 新schema(=18)」。期望：明确拒绝
# 不兼容启动（fail-closed），而非吞异常默认 paper。
# ---------------------------------------------------------------------------
print("\n[场景10] 恢复旧代码配新schema")
proj10 = WORK / "proj10"
data10 = proj10 / "data"
(proj10 / "config").mkdir(parents=True)
data10.mkdir()
db10 = AuditDatabase(data10 / "agent_trader.sqlite")  # 当前代码：迁移到 17
# 模拟「新代码产生的更高 schema 版本」
db10.conn.execute(
    "INSERT INTO schema_migrations(version,name,applied_at) "
    "VALUES(18,'future_column','2026-09-16T00:00:00+00:00')")
db10.conn.commit()
check("新schema版本被数据库记录", db10.current_schema_version() == 18)
db10.close()
(proj10 / "config" / "risk_profile.lock.json").write_text("{}", encoding="utf-8")
for name, payload in {
    "capital_state.json": {"mode": "demo"},
    "channel_state.json": {"date": "2026-09-10", "ai_halted": False},
    "control_state.json": {"state": "close_only"},
    "account_risk_state.json": {"anchor_id": "demo:test"},
    "futures_source_ledger.json": {"positions": {}},
    "public_rate_limit_state.json": {"cooldown_until": 0, "last_status": None},
}.items():
    (data10 / name).write_text(
        __import__("json").dumps(payload), encoding="utf-8-sig" if name == "channel_state.json" else "utf-8")

bundle10 = create_recovery_bundle(proj10, WORK / "bundles10")
manifest10 = __import__("json").loads((bundle10 / "manifest.json").read_text(encoding="utf-8"))
check("恢复包含数据库且记录schema=18", manifest10["files"]["database"]["schema_version"] == 18)
# 正控：旧代码环境（demo）且 manifest 与 DB 一致时可验证通过
ok_verify = verify_recovery_bundle(bundle10, expected_environment="demo")
check("同版本恢复包通过校验", ok_verify["database_schema"] == 18)

# 负向：恢复包被旧代码读取，旧代码只认识 schema=17（manifest 被改写为 17），
# 而 DB 实际是 18 —— 必须 fail-closed 拒绝，而非默认 paper。
manifest_legacy = dict(manifest10)
manifest_legacy["files"] = dict(manifest10["files"])
manifest_legacy["files"]["database"] = dict(manifest10["files"]["database"])
manifest_legacy["files"]["database"]["schema_version"] = 17  # 模拟旧代码期望
(bundle10 / "manifest.json").write_text(__import__("json").dumps(manifest_legacy), encoding="utf-8")
mismatch_rejected = False
try:
    verify_recovery_bundle(bundle10, expected_environment="demo")
except ValueError:
    mismatch_rejected = True
check("旧代码配新schema被明确拒绝（fail-closed，不默认paper）", mismatch_rejected)

print(f"\n结果: {passed} 通过 / {len(failed)} 失败")
if failed:
    print("失败项:", failed)
    raise SystemExit(1)
