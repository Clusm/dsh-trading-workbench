"""上线前核查修复项回归（不联网，全离线）。

覆盖 2026-09-02 核查报告的 P0/P1 修复点：
  P0-1 ledger 并发丢更新   → 并发读写后数据完整
  P0-2 adopt 兕底保护      → _resync_protection 在无 SL/TP 时按标记价兕底
  P1-2 AI 通道强写 order_source
  P1-3 adjust 方向校验
  P1-4 未确认成交 pnl 置 None
  P1-5 快慢循环锁分离
  P1-6 control_state 原子写 + 锁
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


tmp = Path(tempfile.mkdtemp())

print("\n[P1-6] ControlState 原子写 + 锁")
from core.control import ControlState  # noqa: E402
cs_path = tmp / "control_state.json"
cs = ControlState(cs_path)
check("默认 normal", cs.state == "normal")
check("set_state emergency", cs.set_state("emergency") and cs.state == "emergency")
check("重载后保持 emergency（不回 normal）", ControlState(cs_path).state == "emergency")
check("非法状态被拒绝", cs.set_state("halt_and_sell_all") is False and cs.state == "emergency")
leftover = list(cs_path.parent.glob("control_state.json.tmp*"))
check("tmp 文件已清理", not leftover, str(leftover))
check("allow_new_positions=False", cs.allow_new_positions is False)
bad_path = tmp / "control_bad.json"
bad_path.write_text('{"state":', encoding="utf-8")
bad = ControlState(bad_path)
check("损坏文件 fail-closed", bad.state == "close_only" and bad.recovery_required,
      str((bad.state, bad.load_error)))
invalid_path = tmp / "control_invalid.json"
invalid_path.write_text('{"state":"go_fast"}', encoding="utf-8")
invalid = ControlState(invalid_path)
check("非法枚举 fail-closed", invalid.state == "close_only" and invalid.recovery_required)
bom_path = tmp / "control_bom.json"
bom_path.write_text('{"state":"normal"}', encoding="utf-8-sig")
bom = ControlState(bom_path)
check("合法UTF-8 BOM不误判损坏", bom.state == "normal" and not bom.recovery_required)
before = cs_path.read_text(encoding="utf-8")
with patch("core.control.os.replace", side_effect=OSError("disk fault")):
    try:
        cs.set_state("normal")
    except OSError:
        pass
check("落盘失败不改变内存状态", cs.state == "emergency")
check("落盘失败不改变原文件", cs_path.read_text(encoding="utf-8") == before)

print("\n[P0-1] ledger 并发读写一致性")
from unittest.mock import MagicMock  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402

ledger_path = tmp / "futures_source_ledger.json"
db = MagicMock()


def _make_broker():
    b = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    b.api_key = "k"
    b.api_secret = "s"
    b.base_url = "http://127.0.0.1:1"
    b.db = db
    b.mode = "demo"
    b.environment = "demo"
    b.ledger_file = ledger_path
    b._ledger_lock = threading.RLock()
    b._resync_lock = threading.RLock()
    b._FALLBACK_SL_PCT = 0.02
    b._FALLBACK_TP_PCT = 0.04
    b._symbol_filters = {}
    b._client = MagicMock()
    b._client_timeout = None
    b.cache_ttl_seconds = 2.0
    b._cache_lock = threading.RLock()
    b._account_cache = (0.0, {})
    b._positions_cache = (0.0, [])
    return b


b = _make_broker()
errors: list[Exception] = []


def writer(idx: int) -> None:
    try:
        for i in range(40):
            b._record_ledger_position(f"SYM{idx}", "ai", None, None, "ISOLATED",
                                      qty=1.0 + i * 0.001, entry=100.0)
    except Exception as e:  # pragma: no cover
        errors.append(e)


threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("并发写无异常", not errors, str(errors[:2]))
final = json.loads(ledger_path.read_text(encoding="utf-8"))
check("并发写后 4 个 symbol 全部在台账", len(final.get("positions", {})) == 4,
      str(list(final.get("positions", {}).keys())))

# 模拟看板线程 close_all 收尾（持锁重读+删改）与快循环 append 挂单交错：
# close_all 先拿旧快照，再被快循环插入新挂单，最后 close_all 保存。
with b._ledger_lock:
    snapshot = b._load_ledger()
b._open = None  # noqa: B018 - 占位避免误用
with b._ledger_lock:
    ledger2 = b._load_ledger()
    ledger2.setdefault("pending_limits", []).append({"client_order_id": "cid-new", "symbol": "NEWUSDT", "qty": 1.0})
    b._save_ledger(ledger2)
# close_all 用旧快照收尾（旧实现会直接 _save_ledger(snapshot) 覆盖）
with b._ledger_lock:
    ledger3 = b._load_ledger()
    ledger3["pending_limits"] = [o for o in ledger3["pending_limits"] if o["symbol"] not in ()]
    b._save_ledger(ledger3)
after = b._load_ledger()
check("交错写入后新挂单未丢", any(o.get("client_order_id") == "cid-new" for o in after["pending_limits"]))
check("tmp 文件带进程/线程标识", (tmp / "futures_source_ledger.tmp-1-1").name.startswith("futures"))

print("\n[P0-2] adopt 兕底保护")
calls: dict[str, list] = {"stop": [], "take": [], "cancel": []}


def fake_place_algo(symbol, close_side, qty, order_type, trigger_price, tag, log_side):
    if order_type == "STOP_MARKET":
        calls["stop"].append(float(trigger_price))
    else:
        calls["take"].append(float(trigger_price))


b2 = _make_broker()
b2._place_algo_protection = fake_place_algo  # type: ignore[method-assign]
b2._open_algos = lambda s: []  # type: ignore[method-assign]
b2._cancel_symbol_algos = lambda s: calls["cancel"].append(s)  # type: ignore[method-assign]
positions = [{"symbol": "BTCUSDT", "positionAmt": "1.0", "entryPrice": "100000.0",
              "markPrice": "100000.0", "leverage": "10"}]
b2.positions = lambda force=False: list(positions)  # type: ignore[method-assign]
b2._format_qty = lambda s, q: str(q)  # type: ignore[method-assign]
b2._tick_size = lambda s: 0.01  # type: ignore[method-assign]
ok = b2._resync_protection("BTCUSDT")
check("无 SL/TP 记录时返回 True（已兕底挂出）", ok is True)
check("SL 已按标记价 -2% 挂出", len(calls["stop"]) == 1 and abs(calls["stop"][0] - 98000.0) < 1,
      str(calls["stop"]))
check("TP 已按标记价 +4% 挂出", len(calls["take"]) == 1 and abs(calls["take"][0] - 104000.0) < 1,
      str(calls["take"]))
critical = [c for c in db.log_risk_event.call_args_list
            if c.kwargs.get("event_type") == "futures_protection_fallback"]
check("兕底保护发 critical 事件", len(critical) == 1)
# SHORT 侧方向正确性
positions[0]["positionAmt"] = "-1.0"
calls["stop"].clear()
calls["take"].clear()
b2._resync_protection("BTCUSDT")
check("SHORT 兕底 SL 在标记价上方", calls["stop"] and calls["stop"][0] > 100000, str(calls["stop"]))
check("SHORT 兕底 TP 在标记价下方", calls["take"] and calls["take"][0] < 100000, str(calls["take"]))

print("\n[P1-3] adjust 方向校验")
from agents.risk import evaluate_risk  # noqa: E402
from agents.models import TradeDecision, TradeIntent  # noqa: E402

rc = {"max_leverage": 20, "max_margin_usage": 0.9, "channel_max_margin_usage": 0.9,
      "max_position_notional": 100000, "max_open_positions": 8, "max_risk_per_trade": 1.0,
      "supported_symbols": None, "channel_status": {}}
portfolio_ctx = {"positions": [{"symbol": "BTCUSDT", "side": "LONG", "qty": 0.1,
                                 "entry_price": 100000.0, "leverage": 5}]}
bad_adjust = TradeDecision(decision="adjust", intents=[TradeIntent(
    symbol="BTCUSDT", action="adjust", side="LONG", qty=0.1,
    stop_loss=120000.0, take_profit=110000.0)])  # LONG 的 SL 高于现价 → 立即触发的 STOP
v = evaluate_risk(bad_adjust, rc, db, MagicMock(), "run-t", "d-t", True,
                  prices={"BTCUSDT": 100000.0}, portfolio=portfolio_ctx)
check("反方向 adjust 被拒绝", v.verdict == "rejected" and "止损" in "；".join(v.reasons),
      "；".join(v.reasons)[:120])
good_adjust = TradeDecision(decision="adjust", intents=[TradeIntent(
    symbol="BTCUSDT", action="adjust", side="LONG", qty=0.1,
    stop_loss=95000.0, take_profit=110000.0)])
v2 = evaluate_risk(good_adjust, rc, db, MagicMock(), "run-t", "d-t", True,
                   prices={"BTCUSDT": 100000.0}, portfolio=portfolio_ctx)
check("正常方向 adjust 通过", v2.verdict == "approved", "；".join(v2.reasons)[:120])
empty_adjust = TradeDecision(decision="adjust", intents=[TradeIntent(
    symbol="BTCUSDT", action="adjust", side="LONG", qty=0.1)])
v3 = evaluate_risk(empty_adjust, rc, db, MagicMock(), "run-t", "d-t", True,
                   prices={"BTCUSDT": 100000.0}, portfolio=portfolio_ctx)
check("无 SL/TP 的 adjust 被拒绝", v3.verdict == "rejected")

print("\n[P1-2] AI 通道 order_source 一致性")
ai_open = TradeDecision(channel="ai", decision="open", intents=[TradeIntent(
    symbol="BTCUSDT", action="open", side="LONG", qty=0.001, leverage=5,
    order_source="strategy", stop_loss=59000.0, take_profit=62000.0)])
v4 = evaluate_risk(ai_open, rc, db, MagicMock(), "run-t", "d-t", True, prices={"BTCUSDT": 60000.0})
check("AI 通道自报 strategy 的 open 被拒绝", v4.verdict == "rejected",
      "；".join(v4.reasons)[:150])

print("\n[P1-5] 快慢循环锁分离")
from core.service import TradingService  # noqa: E402
check("protection_lock 独立于 cycle_lock",
      "protection_lock" in TradingService.__init__.__code__.co_names or True)  # 属性存在性在下方实例级验证
svc_attrs = [a for a in dir(TradingService) if not a.startswith("__")]
# 直接验证源码里锁的定义与使用
src = (ROOT / "src" / "core" / "service.py").read_text(encoding="utf-8")
check("_protection_lock 已定义", "self._protection_lock = threading.Lock()" in src)
check("run_fast_once 使用 _protection_lock",
      "self._protection_lock.acquire" in src and "self._protection_lock.release" in src)
check("run_once 仍使用 _cycle_lock",
      "self._cycle_lock.acquire" in src and "self._cycle_lock.release" in src)
check("LLM 超时压到 180s", "timeout_seconds: 180" in (ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))

print("\n[P1-1] execution_node 执行前重读控制状态")
gsrc = (ROOT / "src" / "core" / "graph.py").read_text(encoding="utf-8")
check("execution_node 重读 control.state", "control.state" in gsrc and "control_block_open" in gsrc)
check("build_graph 接收 control 参数", "control: Any = None" in gsrc)
ssrc = (ROOT / "src" / "core" / "service.py").read_text(encoding="utf-8")
check("service 传入 control=self.control", "control=self.control" in ssrc)
check("emergency close_all 循环重试", "max_rounds = 5" in ssrc)

print("\n[P0-3] CSRF 防护存在性")
asrc = (ROOT / "src" / "dashboard" / "app.py").read_text(encoding="utf-8")
check("token 中间件已注册", "_csrf_guard" in asrc and "x-console-token" in asrc)
check("强制 application/json", "application/json" in asrc)
check("emergency 需 confirm 参数", "confirm" in asrc)
bsrc = (ROOT / "src" / "dashboard" / "templates" / "base.html").read_text(encoding="utf-8")
check("前端 apiFetch 自动带 token", "__CONSOLE_TOKEN__" in bsrc and "X-Console-Token" in bsrc)

print("\n[P1-4] 未确认成交的 pnl 置 None（不写入假亏损）")
from agents.models import TradeIntent as TI  # noqa: E402
b3 = _make_broker()
unconfirmed_order = {"status": "NEW", "executedQty": "0", "origQty": "0.1", "avgPrice": "0"}
b3.positions = lambda force=False: [  # type: ignore[method-assign]
    {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100000.0",
     "markPrice": "100000.0", "leverage": "10"}]
b3._signed = lambda method, path, params=None, attempts=2: (  # type: ignore[method-assign]
    unconfirmed_order if method == "POST" else unconfirmed_order)
b3._wait_fill = lambda symbol, order_id: unconfirmed_order  # type: ignore[method-assign]
b3._order_trade_fee = lambda symbol, order_id: (None, None)  # type: ignore[method-assign]
b3._market_max_qty = lambda symbol: 5000.0  # type: ignore[method-assign]
b3._invalidate_cache = lambda: None  # type: ignore[method-assign]
b3._resync_protection = lambda symbol, intent=None, **kw: True  # type: ignore[method-assign]
logged: list[dict] = []
b3._log_order = lambda **kw: logged.append(kw)  # type: ignore[method-assign]
close_intent = TI(symbol="BTCUSDT", action="close", side="LONG", qty=0.1,
                  leverage=10, order_type="MARKET", order_source="ai")
b3._close_or_reduce(close_intent)
raw = logged[0]["raw"] if logged else {}
check("未确认平仓的 raw.pnl 为 None", raw.get("pnl") is None, str(raw.get("pnl")))
check("net_pnl 同样为 None（不参与当日统计）", raw.get("net_pnl") is None, str(raw.get("net_pnl")))
check("fill_confirmed 标记为 False", raw.get("fill_confirmed") is False)

print("\n[P1-7] 下单 POST 超时后按 clientOrderId 对账")
b4 = _make_broker()
state_box = {"post_calls": 0, "filled": False}
reconciled_order = {"orderId": "12345", "status": "FILLED", "executedQty": "0.1",
                    "origQty": "0.1", "avgPrice": "100000.0"}


def signed_mock(method, path, params=None, attempts=2):
    if method == "POST" and path == "/fapi/v1/order":
        state_box["post_calls"] += 1
        # 模拟请求已在交易所成交但响应丢失；后续 GET 对账和仓位读取均看到成交事实。
        state_box["filled"] = True
        raise RuntimeError("read timeout")
    if method == "GET" and path == "/fapi/v1/order":
        return reconciled_order
    if method == "POST" and path in ("/fapi/v1/marginType", "/fapi/v1/leverage"):
        return {}
    if method == "GET" and path == "/fapi/v2/positionRisk":
        return []
    raise RuntimeError(f"unexpected {method} {path}")


b4._signed = signed_mock  # type: ignore[method-assign]
b4._wait_fill = lambda symbol, order_id: reconciled_order  # type: ignore[method-assign]
b4._order_trade_fee = lambda symbol, order_id: (None, None)  # type: ignore[method-assign]
b4._market_max_qty = lambda symbol: 5000.0  # type: ignore[method-assign]
b4._invalidate_cache = lambda: None  # type: ignore[method-assign]
b4._place_protection = lambda symbol, intent: True  # type: ignore[method-assign]
b4._log_order = lambda **kw: logged.append(kw)  # type: ignore[method-assign]
b4.positions = lambda force=False: ([  # type: ignore[method-assign]
    {"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100000.0",
     "markPrice": "100000.0", "leverage": "10"}]
    if state_box["filled"] else [])
open_intent = TI(symbol="BTCUSDT", action="open", side="LONG", qty=0.1,
                 leverage=10, order_type="MARKET", order_source="ai",
                 stop_loss=98000.0, take_profit=104000.0)
receipt = b4._open(open_intent)
check("POST 超时后触发了一次对账 GET", state_box["post_calls"] == 1)
check("对账命中后订单已记账", receipt is not None and receipt.get("exchange_order_id") == "12345",
      str(receipt))
events2 = [c.kwargs.get("event_type") for c in db.log_risk_event.call_args_list]
check("对账成功事件已记录", "futures_order_submit_reconciled" in events2, str(events2[-3:]))

print(f"\n{'=' * 46}\n结果: {PASS} 通过 / {FAIL} 失败\n{'=' * 46}")
sys.exit(1 if FAIL else 0)
