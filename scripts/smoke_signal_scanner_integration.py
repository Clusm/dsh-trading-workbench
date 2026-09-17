"""策略研究模块接入冒烟测试：不访问网络、不写真实状态、不产生订单。"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import numpy as np
import pandas as pd
from pandas.errors import PerformanceWarning

warnings.filterwarnings("ignore", category=PerformanceWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.strategy_trader import _not_expired, run_strategy_trader  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core.config_loader import load_settings  # noqa: E402
from core.signal_ticket import ticket_order_id  # noqa: E402
from data.market_universe import MarketUniverse  # noqa: E402
from data.rate_limit import PublicMarketCooldown  # noqa: E402
from exchange.order_outbox import OrderOutbox  # noqa: E402
from signal_engine import SignalScanner  # noqa: E402
import signal_engine.scanner as scanner_module  # noqa: E402


# 来源研究运行时在 2026-09-11 纳入前后的合成 OHLCV 输出基线。哈希覆盖 signal/side
# 的完整序列，防止包化或后续整理悄然改变已验证策略的触发时点和方向。
EXPECTED_RUNTIME_SIGNATURES = {
    "old_keltner_v3": "0abb8a05212d71ed1aa77bbdb292fe7c241eaedf755a464bb75a32a9e4fb8a6d",
    "old_macd_rsi": "0abb8a05212d71ed1aa77bbdb292fe7c241eaedf755a464bb75a32a9e4fb8a6d",
    "old_po3_any": "c0f858c4c106974cc27568ca45d25e879ab8bc73c6ec2e663bddfa0b3edf8e0c",
    "s03_structure_ote": "0abb8a05212d71ed1aa77bbdb292fe7c241eaedf755a464bb75a32a9e4fb8a6d",
    "s07_supply_demand_range_v3": "0abb8a05212d71ed1aa77bbdb292fe7c241eaedf755a464bb75a32a9e4fb8a6d",
    "s08_volume_rsi_divergence_v3": "1efcbc69b143140daa894e03727268ebfd56e0b3f5f5663f32e73431e8e0ac50",
    "s09_climax_rebound_long_v2": "0abb8a05212d71ed1aa77bbdb292fe7c241eaedf755a464bb75a32a9e4fb8a6d",
}


def synthetic_ohlcv(rows: int = 300) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC")
    trend = np.linspace(3000.0, 3300.0, rows)
    wave = np.sin(np.arange(rows) / 8.0) * 25.0
    close = trend + wave
    open_ = close - np.cos(np.arange(rows) / 7.0) * 4.0
    high = np.maximum(open_, close) + 8.0
    low = np.minimum(open_, close) - 8.0
    volume = 1000.0 + np.sin(np.arange(rows) / 5.0) * 100.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def scanner_ohlcv(rows: int = 300) -> pd.DataFrame:
    """补齐交易所 K 线的 close_time 字段，供扫描器离线回归使用。"""
    frame = synthetic_ohlcv(rows)
    frame["close_time"] = [int((ts + pd.Timedelta(hours=1)).timestamp() * 1000) - 1
                           for ts in frame.index]
    return frame


def no_signal(frame: pd.DataFrame, _params: dict) -> pd.DataFrame:
    return pd.DataFrame({"signal": 0, "side": ""}, index=frame.index)


def main() -> None:
    with patch("core.config_loader.load_dotenv"):
        settings = load_settings(ROOT)
    with tempfile.TemporaryDirectory(prefix="mta-signal-smoke-") as tmp:
        scanner = SignalScanner(
            knowledge_dir=settings.knowledge_dir,
            # 不存在的旧研究路径不得影响内置运行时加载。
            strategy_lab_src="Z:/retired-strategy-lab/src",
            state_file=Path(tmp) / "signal_state.json",
        )
        scanner._load_strategy_lab()
        cards = json.loads(
            (settings.knowledge_dir / "strategy_cards.json").read_text(encoding="utf-8")
        ).get("cards", [])
        frame = synthetic_ohlcv()
        checked: set[str] = set()
        signatures: dict[str, str] = {}
        for card in cards:
            signal_fn = str(card.get("signal_fn") or "")
            if not card.get("indicatorized") or not signal_fn or signal_fn in checked:
                continue
            out = scanner._registry[signal_fn](frame.copy(), card.get("params") or {})
            assert len(out) == len(frame), signal_fn
            assert {"side", "signal"}.issubset(out.columns), signal_fn
            assert set(out["side"].dropna().unique()).issubset({"", "LONG", "SHORT"}), signal_fn
            signatures[signal_fn] = hashlib.sha256(
                out[["signal", "side"]].to_csv().encode("utf-8")
            ).hexdigest()
            checked.add(signal_fn)
        assert checked, "没有可验证的策略函数"
        assert signatures == EXPECTED_RUNTIME_SIGNATURES, signatures
        runtime_file = Path(str(scanner._features_mod.__file__)).resolve()
        assert runtime_file.parent.name == "strategy_runtime", runtime_file
        assert "signals" not in sys.modules, "不得导入无命名空间的外部 signals 模块"

    # 相同 symbol/timeframe 的策略卡共享一次行情失败，避免告警和请求扇出。
    with tempfile.TemporaryDirectory(prefix="mta-signal-cache-") as tmp:
        root = Path(tmp)
        (root / "strategy_cards.json").write_text(json.dumps({"cards": [
            {"id": "a", "indicatorized": True, "signal_fn": "same",
             "symbols": ["BTCUSDT"], "timeframes": ["1h", "2h"]},
            {"id": "b", "indicatorized": True, "signal_fn": "same",
             "symbols": ["BTCUSDT"], "timeframes": ["1h", "2h"]},
        ]}), encoding="utf-8")
        db = AuditDatabase(root / "audit.sqlite")
        cache_scanner = SignalScanner(
            knowledge_dir=root, strategy_lab_src="unused",
            state_file=root / "state.json", db=db,
        )
        cache_scanner._registry = {"same": lambda *_args: None}
        cache_scanner._features_mod = SimpleNamespace(clear_feature_cache=lambda: None)
        calls = 0
        original_fetch = scanner_module.fetch_klines

        def fail_fetch(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ConnectError("network down")

        scanner_module.fetch_klines = fail_fetch
        try:
            cache_scanner.scan()
        finally:
            scanner_module.fetch_klines = original_fetch
        errors = db.fetch_one(
            "SELECT COUNT(*) count FROM risk_events WHERE event_type='signal_scan_error'"
        )["count"]
        assert calls == 1 and errors == 1, (calls, errors)
        db.close()

    # 已进入全局冷却时属于受控让步，不得每轮继续制造 signal_scan_error。
    with tempfile.TemporaryDirectory(prefix="mta-signal-deferred-") as tmp:
        root = Path(tmp)
        (root / "strategy_cards.json").write_text(json.dumps({"cards": [
            {"id": "a", "indicatorized": True, "signal_fn": "same",
             "symbols": ["BTCUSDT"], "timeframes": ["1h"]},
        ]}), encoding="utf-8")
        db = AuditDatabase(root / "audit.sqlite")
        deferred_scanner = SignalScanner(
            knowledge_dir=root, strategy_lab_src="unused",
            state_file=root / "state.json", db=db,
        )
        deferred_scanner._registry = {"same": lambda *_args: None}
        deferred_scanner._features_mod = SimpleNamespace(clear_feature_cache=lambda: None)
        original_fetch = scanner_module.fetch_klines
        scanner_module.fetch_klines = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PublicMarketCooldown("cooldown")
        )
        try:
            deferred_scanner.scan()
        finally:
            scanner_module.fetch_klines = original_fetch
        errors = db.fetch_one(
            "SELECT COUNT(*) count FROM risk_events WHERE event_type='signal_scan_error'"
        )["count"]
        last_scan = json.loads((root / "state.json").read_text(encoding="utf-8"))["last_scan"]
        assert errors == 0 and last_scan["status"] == "deferred", (errors, last_scan)
        db.close()

    # 非通用模式只能扫描策略卡声明且在白名单内的品种，不能把动态池追加到卡片。
    with tempfile.TemporaryDirectory(prefix="mta-signal-scope-") as tmp:
        root = Path(tmp)
        (root / "strategy_cards.json").write_text(json.dumps({"cards": [
            {"id": "eth-only", "indicatorized": True, "signal_fn": "none",
             "symbols": ["ETHUSDT"], "timeframes": ["1h"]},
        ]}), encoding="utf-8")
        scope_scanner = SignalScanner(
            knowledge_dir=root, strategy_lab_src="unused",
            state_file=root / "state.json", universal_cards=False,
        )
        scope_scanner._registry = {"none": no_signal}
        scope_scanner._features_mod = SimpleNamespace(clear_feature_cache=lambda: None)
        calls: list[str] = []
        original_fetch = scanner_module.fetch_klines

        def record_fetch(symbol, *_args, **_kwargs):
            calls.append(symbol)
            return scanner_ohlcv()

        scanner_module.fetch_klines = record_fetch
        try:
            scope_scanner.scan(scan_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        finally:
            scanner_module.fetch_klines = original_fetch
        assert calls == ["ETHUSDT"], calls
        last_scan = json.loads((root / "state.json").read_text(encoding="utf-8"))["last_scan"]
        assert last_scan["mode"] == "validated_card_symbols"
        assert last_scan["symbols"] == ["ETHUSDT"]

    # 通用模式使用持久化游标轮转，不再每轮固定截取池头两个标的。
    with tempfile.TemporaryDirectory(prefix="mta-signal-rotation-") as tmp:
        root = Path(tmp)
        (root / "strategy_cards.json").write_text(json.dumps({"cards": [
            {"id": "universal", "indicatorized": True, "signal_fn": "none",
             "symbols": ["BTCUSDT"], "timeframes": ["1h"]},
        ]}), encoding="utf-8")
        rotation_scanner = SignalScanner(
            knowledge_dir=root, strategy_lab_src="unused", state_file=root / "state.json",
            universal_cards=True, max_scan_symbols_per_cycle=2,
        )
        rotation_scanner._registry = {"none": no_signal}
        rotation_scanner._features_mod = SimpleNamespace(clear_feature_cache=lambda: None)
        pool = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "SUIUSDT", "DOTUSDT"]
        calls = []
        original_fetch = scanner_module.fetch_klines
        scanner_module.fetch_klines = record_fetch
        try:
            for _ in range(3):
                rotation_scanner.scan(scan_symbols=pool)
        finally:
            scanner_module.fetch_klines = original_fetch
        assert calls == pool, calls
        rotation_state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        assert rotation_state["scan_cursor"] == 0, rotation_state

    # 市场池必须始终限制在加密白名单，不能让黄金/美股类合约从异动入口绕过。
    with tempfile.TemporaryDirectory(prefix="mta-market-scope-") as tmp:
        universe = MarketUniverse(
            state_file=Path(tmp) / "market.json",
            core_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            full_top_n=2, full_anomaly_n=1,
        )
        import data.market_universe as universe_module
        original_tickers = universe_module.get_all_24h_tickers
        universe_module.get_all_24h_tickers = lambda *args, **kwargs: [
            {"symbol": "XAUUSDT", "quoteVolume": "9999", "priceChangePercent": "20"},
            {"symbol": "BTCUSDT", "quoteVolume": "100", "priceChangePercent": "1"},
            {"symbol": "ETHUSDT", "quoteVolume": "80", "priceChangePercent": "2"},
            {"symbol": "SOLUSDT", "quoteVolume": "60", "priceChangePercent": "3"},
        ]
        try:
            pool = universe._full_scan(pd.Timestamp("2026-09-07", tz="UTC").to_pydatetime())
        finally:
            universe_module.get_all_24h_tickers = original_tickers
        assert pool == ["BTCUSDT", "ETHUSDT", "SOLUSDT"], pool
        universe.state["prev_prices"] = {"BTCUSDT": 100.0, "XAUUSDT": 100.0}
        universe._fetch_all_prices = lambda: {"BTCUSDT": 104.0, "XAUUSDT": 150.0}
        assert universe.detect_short_anomalies() == ["BTCUSDT"]

        # 受控冷却保留旧市场池且不产生伪故障事件。
        db = AuditDatabase(Path(tmp) / "universe.sqlite")
        universe.db = db
        universe.state["full_pool"] = ["BTCUSDT"]
        universe._fetch_all_prices = lambda: (_ for _ in ()).throw(
            PublicMarketCooldown("cooldown")
        )
        assert universe.detect_short_anomalies() == []
        universe_module.get_all_24h_tickers = lambda *args, **kwargs: (_ for _ in ()).throw(
            PublicMarketCooldown("cooldown")
        )
        try:
            assert universe._full_scan(pd.Timestamp("2026-09-07", tz="UTC").to_pydatetime()) == [
                "BTCUSDT"
            ]
        finally:
            universe_module.get_all_24h_tickers = original_tickers
        false_failures = db.fetch_one(
            "SELECT COUNT(*) count FROM risk_events "
            "WHERE event_type IN ('market_universe_price_fail','market_universe_full_scan_fail')"
        )["count"]
        assert false_failures == 0, false_failures
        db.close()

    # 历史 open ticket 即使不再发射，也要由扫描周期统一标记为 expired。
    with tempfile.TemporaryDirectory(prefix="mta-signal-expiry-") as tmp:
        db = AuditDatabase(Path(tmp) / "audit.sqlite")
        db.log_signal_ticket(
            ticket_id="old-ticket", signal_id="old-signal", strategy_id="old-strategy",
            symbol="BTCUSDT", side="LONG", signal_time="2026-01-01T00:00:00+00:00",
            signal_price=100.0, entry_zone_low=99.0, entry_zone_high=101.0,
            expires_at="2026-01-02T00:00:00+00:00", stop_loss_pct=0.03,
            take_profit_pct=0.08,
        )
        assert db.expire_open_signal_tickets("2026-09-07T00:00:00+00:00") == 1
        row = db.fetch_one("SELECT status FROM signal_tickets WHERE ticket_id='old-ticket'")
        assert row["status"] == "expired"
        db.close()
    # 跨周期回灌、白名单、严格过期和丢失扫描状态后的数据库去重。
    with tempfile.TemporaryDirectory(prefix="mta-signal-replay-") as tmp:
        root = Path(tmp)
        card = {"id": "replay", "indicatorized": True, "signal_fn": "trigger",
                "symbols": ["BTCUSDT"], "timeframes": ["1h"],
                "exits": {"sl_pct": 0.03, "tp_pct": 0.08}}
        (root / "strategy_cards.json").write_text(json.dumps({"cards": [card]}), encoding="utf-8")
        db = AuditDatabase(root / "audit.sqlite")
        replay = SignalScanner(knowledge_dir=root, strategy_lab_src="unused",
                               state_file=root / "state.json", db=db)
        replay._registry = {"trigger": lambda frame, _: pd.DataFrame(
            {"signal": 1, "side": "LONG"}, index=frame.index)}
        replay._features_mod = SimpleNamespace(
            clear_feature_cache=lambda: None,
            build_features=lambda frame: frame.assign(
                adx_14=20.0, rsi_14=50.0, ema_200=3000.0, macd_signal=1.0, atr_14=10.0))
        frame = scanner_ohlcv()
        now = (frame.index[-1] + pd.Timedelta(hours=1)).to_pydatetime()
        with patch.object(scanner_module, "fetch_klines", return_value=frame) as fetch:
            first = replay.scan(now=now, scan_symbols=["BTCUSDT"])
            assert len(first) == 1, first
            ticket = first[0]
            for _ in range(2):
                assert replay.scan(now=now, scan_symbols=[" btcusdt "]) == first
            assert replay.scan(now=now, scan_symbols=["ETHUSDT"]) == []
            fetch.reset_mock()
            assert replay.scan(now=now, scan_symbols=[]) == []
            fetch.assert_not_called()
            (root / "state.json").unlink()
            assert replay.scan(now=now, scan_symbols=["BTCUSDT"]) == first
            assert db.fetch_one("SELECT COUNT(*) n FROM signal_tickets")["n"] == 1
            for status in ["consumed", "expired", "rejected"]:
                db.update_signal_ticket(ticket_id=ticket["ticket_id"], status=status)
                (root / "state.json").unlink()
                assert replay.scan(now=now, scan_symbols=["BTCUSDT"]) == []
            db.update_signal_ticket(ticket_id=ticket["ticket_id"], status="open")
            # scan_symbols=None 不做白名单过滤，open 票据照常回灌。
            assert replay.scan(now=now, scan_symbols=None) == first
            # 已递交订单（orders 行或 outbox UNKNOWN）的 ticket 不得再次回灌执行。
            cid = ticket_order_id(ticket["ticket_id"])
            future = dict(ticket, expires_at=(datetime.now(timezone.utc)
                                              + timedelta(hours=1)).isoformat())
            prices = {"BTCUSDT": float(future["signal_price"])}
            assert run_strategy_trader(tickets=[future], prices=prices, db=db).intents
            db.log_order(client_order_id=cid, exchange_order_id=None, symbol="BTCUSDT",
                         side="LONG", order_type="MARKET", qty=0.01, price=3000.0,
                         status="NEW", order_source="strategy", strategy_id="replay",
                         ticket_id=future["ticket_id"])
            assert db.signal_ticket_has_order(future["ticket_id"])
            assert db.fetch_open_signal_tickets(now.isoformat()) == []
            (root / "state.json").unlink()
            assert replay.scan(now=now, scan_symbols=["BTCUSDT"]) == []
            assert run_strategy_trader(tickets=[future], prices=prices, db=db).intents == []
            db.conn.execute("DELETE FROM orders")
            db.conn.commit()
            outbox = OrderOutbox(db, environment="demo")
            outbox.prepare(client_order_id=f"{cid}-1", symbol="BTCUSDT", side="LONG",
                           position_side="LONG", order_type="MARKET", purpose="ENTRY",
                           qty=0.01, price=None, reduce_only=False,
                           endpoint="/fapi/v1/order", request={"newClientOrderId": f"{cid}-1"})
            outbox.mark_unknown(f"{cid}-1", RuntimeError("offline"))
            assert db.signal_ticket_has_order(future["ticket_id"])
            assert db.fetch_open_signal_tickets(now.isoformat()) == []
            assert run_strategy_trader(tickets=[future], prices=prices, db=db).intents == []
            db.conn.execute("DELETE FROM order_outbox")
            db.conn.commit()
            assert replay.scan(now=now, scan_symbols=["BTCUSDT"]) == first
            # 无时区或无法解析的失效时间必须 fail-closed。
            assert not _not_expired(dict(future, expires_at="2030-01-01T00:00:00"))
            assert not _not_expired({"expires_at": "not-a-time"})
            expiry = pd.Timestamp(ticket["expires_at"]).to_pydatetime()
            assert replay.scan(now=expiry, scan_symbols=["BTCUSDT"]) == []
            assert db.fetch_one("SELECT status FROM signal_tickets")["status"] == "expired"
            (root / "state.json").unlink()
            assert replay.scan(now=expiry, scan_symbols=["BTCUSDT"]) == []
        # 带时区偏移的绝对时间必须正确比较，损坏 raw 不能作为票据回灌。
        db.conn.execute("UPDATE signal_tickets SET status='open', expires_at=?",
                        ((pd.Timestamp(now) + pd.Timedelta(hours=1)).tz_convert("Asia/Shanghai").isoformat(),))
        db.conn.commit()
        assert len(db.fetch_open_signal_tickets(now.isoformat())) == 1
        db.conn.execute("UPDATE signal_tickets SET raw_json='not-json'")
        db.conn.commit()
        assert db.fetch_open_signal_tickets(now.isoformat()) == []
        # 无时区或损坏的失效时间不回灌；无法解析的时间在收口时按过期关闭。
        db.conn.execute("UPDATE signal_tickets SET raw_json=?, expires_at='2030-01-01T00:00:00'",
                        (json.dumps(ticket),))
        db.conn.commit()
        assert db.fetch_open_signal_tickets(now.isoformat()) == []
        db.conn.execute("UPDATE signal_tickets SET expires_at='not-a-time'")
        db.conn.commit()
        assert db.fetch_open_signal_tickets(now.isoformat()) == []
        assert db.expire_open_signal_tickets(now.isoformat()) == 1
        assert db.fetch_one("SELECT status FROM signal_tickets")["status"] == "expired"
        db.close()
    print(f"SIGNAL_SCANNER_INTEGRATION_OK functions={len(checked)} runtime=bundled")


if __name__ == "__main__":
    main()
