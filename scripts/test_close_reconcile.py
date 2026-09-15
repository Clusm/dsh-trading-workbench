"""回归测试：交易所外部平仓补记（_reconcile_one_close）。

背景（2026-09-02 真实事故）：
    UNIUSDT SHORT 10 @ 6.2405，止盈 5.9。交易所于 09-02 11:18:22 触发止盈，
    真实成交 10 张 @ 5.8830、realizedPnl=+3.575。但本地补记成了
    「3.0 @ 4.919、net_pnl=-0.0059」——价格、数量、盈亏全错。

原实现的两个错误假设：
    1. 以为 /fapi/v1/userTrades 不传 startTime 时按时间倒序返回。
       实测是**正序**，且从账户史上最早的成交开始，limit=20 拿不到最近的成交。
    2. 以为一笔订单只对应一笔成交。实测市价单常拆成多笔（3+8+9=20 张）。

本测试用当时抓到的真实流水结构构造用例，不联网。
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402

MS = lambda s: int(s)  # noqa: E731  # 毫秒时间戳直接用整数常量


class FakeDB:
    """只实现 _reconcile_one_close 用到的接口。"""

    def __init__(self, position: dict | None, known_order_ids: set[str] | None = None):
        self.position = position
        self.known_order_ids = known_order_ids or set()
        self.orders: list[dict] = []
        self.events: list[dict] = []

    def fetch_all(self, sql: str, params=()):
        sql_s = " ".join(sql.split())
        # 查 OPEN 持仓
        if "FROM positions" in sql_s:
            return [self.position] if self.position else []
        # 按 exchange_order_id 去重
        if "FROM orders WHERE exchange_order_id" in sql_s:
            oid = str(params[0])
            return [{"id": 1}] if oid in self.known_order_ids else []
        return []

    def log_order(self, **kw):
        self.orders.append(kw)
        return len(self.orders)

    def log_risk_event(self, **kw):
        self.events.append(kw)


def make_broker(position: dict | None, trades: list, known: set[str] | None = None):
    """绕过 __init__ 构造 broker：本测试不需要真实网络与鉴权。"""
    broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker.db = FakeDB(position, known)
    calls: list[dict] = []

    def fake_signed(method, path, params=None, attempts=1):
        calls.append({"method": method, "path": path, "params": dict(params or {})})
        return trades

    broker._signed = fake_signed
    return broker, calls


# ── 真实流水结构：正序返回（旧→新），且市价单被拆成多笔 ──────────────────
# 09-02 05:51:45 开空  10 张（拆成 5 + 5）
# 09-02 11:18:22 止盈  10 张 @ 5.8830，realizedPnl=+3.575
# 08-30 10:52:46 历史陈账 BUY 3.00 @ 4.9190（就是原来被误抓的那笔）
OLD_BUY = {
    "symbol": "UNIUSDT", "side": "BUY", "qty": "3.00", "price": "4.9190",
    "realizedPnl": "0", "commission": "0.0059028",
    "orderId": 460044828, "time": 1_788_000_000_000,
}
OLD_BUY_2 = {  # 同一个陈年 oid 的第二、三笔，验证聚合
    "symbol": "UNIUSDT", "side": "BUY", "qty": "8.00", "price": "4.9190",
    "realizedPnl": "0", "commission": "0.0157408",
    "orderId": 460044828, "time": 1_788_000_000_000,
}
OPEN_SELL_1 = {
    "symbol": "UNIUSDT", "side": "SELL", "qty": "5.00", "price": "6.2410",
    "realizedPnl": "0", "commission": "0.012482",
    "orderId": 462115907, "time": 1_788_328_305_170,
}
OPEN_SELL_2 = {
    "symbol": "UNIUSDT", "side": "SELL", "qty": "5.00", "price": "6.2400",
    "realizedPnl": "0", "commission": "0.012480",
    "orderId": 462115907, "time": 1_788_328_305_170,
}
TP_BUY = {  # 真实止盈成交
    "symbol": "UNIUSDT", "side": "BUY", "qty": "10.00", "price": "5.8830",
    "realizedPnl": "3.575", "commission": "0.023532",
    "orderId": 462253131, "time": 1_788_347_902_045,
}

POSITION = {
    "id": 282, "symbol": "UNIUSDT", "side": "SHORT", "qty": 10.0,
    "entry_price": 6.2405, "leverage": 10,
    "opened_at": "2026-09-02T09:36:06.958+00:00",
}


def test_reconciles_real_tp_fill() -> None:
    """核心用例：必须从正序流水里抓到真实止盈成交，而不是历史陈账。"""
    broker, calls = make_broker(POSITION, [OLD_BUY, OLD_BUY_2, OPEN_SELL_1, OPEN_SELL_2, TP_BUY])
    broker._reconcile_one_close("UNIUSDT")
    db = broker.db

    assert len(db.orders) == 1, f"应只补记 1 条，实际 {len(db.orders)}"
    o = db.orders[0]
    assert o["exchange_order_id"] == "462253131", f"抓错了单：{o['exchange_order_id']}"
    assert abs(o["qty"] - 10.0) < 1e-9, f"数量应为 10，实际 {o['qty']}"
    assert abs(o["price"] - 5.8830) < 1e-6, f"价格应为 5.8830，实际 {o['price']}"
    raw = o["raw"]
    # realized 3.575 - commission 0.023532
    assert abs(raw["net_pnl"] - (3.575 - 0.023532)) < 1e-6, f"盈亏错：{raw['net_pnl']}"
    assert o["side"] == "SHORT", "side 应传原仓位方向，供前端显示『平空』"

    # 必须显式传 startTime，否则接口返回最老的成交，最近的平仓根本取不到
    params = calls[0]["params"]
    assert "startTime" in params, "未传 startTime，会退化成从最早成交开始扫"
    opened_ms = BinanceFuturesBroker._parse_iso_ms(POSITION["opened_at"])
    assert params["startTime"] < opened_ms, "startTime 应早于开仓时刻"


def test_aggregates_split_fills() -> None:
    """同一 orderId 的平仓被拆成多笔时，必须合并数量并算加权均价。"""
    fill_a = dict(TP_BUY, qty="3.00", price="5.9000", realizedPnl="1.0",
                  commission="0.007", time=1_788_347_902_000)
    fill_b = dict(TP_BUY, qty="7.00", price="5.8757", realizedPnl="2.5",
                  commission="0.016", time=1_788_347_902_100)
    broker, _ = make_broker(POSITION, [OLD_BUY, fill_a, fill_b])
    broker._reconcile_one_close("UNIUSDT")
    db = broker.db

    assert len(db.orders) == 1
    o = db.orders[0]
    assert abs(o["qty"] - 10.0) < 1e-9, f"应合并成 10 张，实际 {o['qty']}"
    expect_px = (3.0 * 5.9 + 7.0 * 5.8757) / 10.0
    assert abs(o["price"] - expect_px) < 1e-6, f"应为加权均价 {expect_px}，实际 {o['price']}"
    assert abs(o["raw"]["net_pnl"] - (3.5 - 0.023)) < 1e-6, "盈亏应累加"


def test_skips_when_qty_far_exceeds() -> None:
    """数量远超持仓量 → 判定抓错单，宁可不记也不污染 order 台账。"""
    weird = dict(TP_BUY, qty="100.00", orderId=999)
    broker, _ = make_broker(POSITION, [OLD_BUY, weird])  # 只有异常单一个候选
    broker._reconcile_one_close("UNIUSDT")
    db = broker.db
    assert len(db.orders) == 0, "抓错的单不应被补记"
    assert any(e["event_type"] == "futures_close_reconcile_mismatch" for e in db.events), \
        "应留下 mismatch 告警事件"


def test_allows_partial_close() -> None:
    """交易所端的部分平仓是真实场景，少平几张也要如实补记并标记 partial。"""
    part = dict(TP_BUY, qty="4.00", realizedPnl="1.43", orderId=555)
    broker, _ = make_broker(POSITION, [OLD_BUY, part])
    broker._reconcile_one_close("UNIUSDT")
    db = broker.db
    assert len(db.orders) == 1, "部分平仓应如实补记"
    o = db.orders[0]
    assert abs(o["qty"] - 4.0) < 1e-9
    assert o["raw"]["partial"] is True, "应标记为部分平仓"


def test_skips_already_recorded() -> None:
    """已经记过的 orderId（系统自己下的单）不应重复补记。"""
    broker, _ = make_broker(POSITION, [OLD_BUY, TP_BUY], known={"462253131"})
    broker._reconcile_one_close("UNIUSDT")
    assert len(broker.db.orders) == 0, "已记录的成交不应重复补记"


def test_no_open_position_noop() -> None:
    """没有 OPEN 持仓时直接返回。"""
    broker, calls = make_broker(None, [TP_BUY])
    broker._reconcile_one_close("UNIUSDT")
    assert len(broker.db.orders) == 0
    assert len(calls) == 0, "无持仓不该去查交易所"


def test_long_position_wants_sell() -> None:
    """多头平仓要抓 SELL 方向的成交。"""
    pos = dict(POSITION, side="LONG", qty=10.0, entry_price=5.0)
    sell = dict(TP_BUY, side="SELL", qty="10.00", price="5.5",
                realizedPnl="5.0", orderId=777)
    buy = dict(TP_BUY, side="BUY", qty="10.00", price="5.5", orderId=778)
    broker, _ = make_broker(pos, [buy, sell])
    broker._reconcile_one_close("UNIUSDT")
    db = broker.db
    assert len(db.orders) == 1
    assert db.orders[0]["exchange_order_id"] == "777", "多头平仓应抓 SELL 成交"
    assert db.orders[0]["side"] == "LONG"


def test_realized_pnl_fallback() -> None:
    """realizedPnl 为 0（交易所对非平仓成交返回 0）时按开仓价手算。"""
    fill = dict(TP_BUY, realizedPnl="0", commission="0", orderId=888)
    broker, _ = make_broker(POSITION, [fill])
    broker._reconcile_one_close("UNIUSDT")
    o = broker.db.orders[0]
    # SHORT：entry 6.2405 → exit 5.8830，每张赚 0.3575，10 张赚 3.575
    assert abs(o["raw"]["net_pnl"] - 3.575) < 1e-6, f"应手算出 3.575，实际 {o['raw']['net_pnl']}"


def test_parse_iso_ms() -> None:
    """opened_at 的两种常见格式都要能解析。"""
    f = BinanceFuturesBroker._parse_iso_ms
    assert f("2026-09-02T09:36:06.958+00:00") is not None
    assert f("2026-09-02 09:36:06") is not None
    assert f("2026-09-02T09:36:06Z") is not None
    assert f("") is None
    assert f(None) is None
    assert f("not-a-date") is None
    # 带时区与不带时区解析结果应一致（无时区按 UTC）
    a = f("2026-09-02T09:36:06.958+00:00")
    b = f("2026-09-02 09:36:06")
    assert a is not None and b is not None and abs(a - b) < 1000, (a, b)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__} {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    print("== 外部平仓补记回归测试 ==")
    main()
