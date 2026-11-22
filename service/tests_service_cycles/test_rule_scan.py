"""v3.1 P2：边沿触发扫描（/api/rules/scan）与派发回写 /api/rules/dispatched。

离线：FakeMarket 只按内存里的标记价 / K 线响应，TestClient 走进程内 ASGI 传输；
策略在真实 SQLite（临时目录）里创建、挂载、授权，扫描状态写进 rule_eval_state。

夹具自包含：不复用 tests_execution/support.py（那个目录不在本 PR 的改动范围内）。
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from workbench_service.api import strategies as strategies_api  # noqa: E402
from workbench_service.app import create_app  # noqa: E402
from workbench_service.config import (  # noqa: E402
    AUTH_MODE_BEARER,
    AUTH_MODE_DEV_OPEN,
    Settings,
    resolve_runtime_auth,
)
from workbench_service.strategies import scan as rule_scan  # noqa: E402

#: 固定的"当前时间"：1800000900000 // 900000 == 2000001，信号桶可预期。
NOW_MS = 1_800_000_900_000
INTERVAL_MS = 15_000

MARK_RULES: dict[str, Any] = {
    "symbols": ["BTCUSDT"],
    "timeframes": ["15m"],
    "entry": {"kind": "mark_threshold", "side": "long", "operator": "gt", "value": 100_000},
    "exit": {"stop_loss_pct": 0.01, "take_profit_pct": 0.03},
    "invalidation": {"kind": "stop_loss"},
    "position_constraints": {"max_positions": 1},
    "required_data": ["mark_price"],
    "requires_ai": False,
}

KLINE_RULES: dict[str, Any] = {
    **MARK_RULES,
    "symbols": ["SOLUSDT"],
    "entry": {"kind": "candle_close", "side": "long", "operator": "gt", "value": 100},
    "required_data": ["mark_price", "klines"],
}


class FakeMarket:
    """只读内存行情；不访问网络，标记价 / K 线都可精确控制。"""

    def __init__(self, *, now_ms: int = NOW_MS) -> None:
        self.settings = SimpleNamespace(
            whitelist=("BTCUSDT", "ETHUSDT", "SOLUSDT")
        )
        self._now_ms = int(now_ms)
        self.marks: dict[str, dict[str, Any]] = {}
        self.bars: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
        self.klines = SimpleNamespace(
            get_with_fallback=self._get_with_fallback,
            cached_bars=self._cached_bars,
        )

    # -- clock ---------------------------------------------------------
    def now_ms(self) -> int:
        return self._now_ms

    def set_now(self, now_ms: int) -> None:
        self._now_ms = int(now_ms)

    # -- marks ---------------------------------------------------------
    def set_mark(self, symbol: str, mark: float | None, *, stale: bool = False) -> None:
        if mark is None:
            self.marks[symbol.upper()] = {"mark": None, "age_ms": None, "stale": True}
            return
        self.marks[symbol.upper()] = {
            "mark": float(mark),
            "age_ms": 120,
            "stale": bool(stale),
            "source": "fake",
        }

    def mark_snapshot(self, symbols: Any) -> dict[str, Any]:
        wanted = sorted({str(symbol).upper() for symbol in symbols})
        if not wanted or len(wanted) > 10:
            raise ValueError("request 1 to 10 symbols")
        return {
            "prices": {
                symbol: dict(
                    self.marks.get(symbol)
                    or {"mark": None, "age_ms": None, "stale": True}
                )
                for symbol in wanted
            },
            "received_at_ms": self._now_ms,
            "source_base_url": "https://fake-market.invalid",
        }

    def price_snapshot(self, symbols: Any) -> dict[str, dict[str, Any]]:
        return {
            str(symbol).upper(): dict(self.marks.get(str(symbol).upper()) or {})
            for symbol in symbols
        }

    # -- klines --------------------------------------------------------
    def set_bars(self, symbol: str, timeframe: str, bars: list[dict[str, Any]]) -> None:
        self.bars[(symbol.upper(), timeframe)] = tuple(bars)

    def _get_with_fallback(
        self, symbol: str, timeframe: str, limit: int, *, now_ms: int | None = None
    ) -> Any:
        return SimpleNamespace(bars=self.bars.get((symbol.upper(), timeframe), ()))

    def _cached_bars(self, symbol: str, timeframe: str) -> tuple[Any, ...]:
        return self.bars.get((symbol.upper(), timeframe), ())

    def close(self) -> None:
        return None


class FakeExecution:
    """只读执行门面：没有凭据、没有交易所、没有订单。"""

    configured = False

    def status(self) -> dict[str, Any]:
        return {
            "venue": "demo",
            "demo_only": True,
            "credentials_configured": False,
            "account": {"available": False, "source": "none"},
            "equity": {"available": False, "source": "none"},
            "positions": [],
            "protection": {"ok": None, "positions": 0, "missing": []},
            "readiness": {"new_entry_allowed": False, "reasons": ["E_CREDENTIALS_MISSING"]},
            "checked_at": "2026-11-22T00:00:00Z",
        }

    def envelope(self) -> dict[str, Any]:
        return {
            "new_entry_allowed": False,
            "blockers": ["E_CREDENTIALS_MISSING"],
            "rules_version": "test",
            "limits": {"max_open_positions": 1},
        }


def make_settings(data_dir: Path, *, mode: str = AUTH_MODE_DEV_OPEN) -> Settings:
    keys = ("WORKBENCH_AUTH_MODE", "WORKBENCH_API_TOKEN")
    old = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["WORKBENCH_AUTH_MODE"] = mode
        os.environ.pop("WORKBENCH_API_TOKEN", None)
        settings = Settings.from_env(
            data_dir=str(data_dir), default_auth_mode=AUTH_MODE_BEARER
        )
        if mode == AUTH_MODE_DEV_OPEN:
            return settings
        return resolve_runtime_auth(settings)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ScanCase:
    """一次性的临时数据目录 + 应用实例 + 可控行情。"""

    def __init__(self, *, now_ms: int = NOW_MS) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.settings = make_settings(self.data_dir)
        self.app = create_app(self.settings)
        self.market = FakeMarket(now_ms=now_ms)
        self.app.state.market = self.market
        self.app.state.services = FakeExecution()
        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()
        self.store = self.app.state.strategy_store
        assert self.store is not None
        # 与生产一致：Host 只在 mounted 模式下调扫描；扫描本身不按 mode 过滤。
        self.store.set_config("mounted", 0)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "workbench-v1.sqlite"

    def close(self) -> None:
        try:
            self._client_cm.__exit__(None, None, None)
        finally:
            self._tmp.cleanup()


class RuleScanTestBase(unittest.TestCase):
    now_ms = NOW_MS

    def setUp(self) -> None:
        self.case = ScanCase(now_ms=self.now_ms)
        self.client = self.case.client
        self.store = self.case.store
        self.market = self.case.market
        self.market.set_mark("BTCUSDT", 99_000.0)
        self.market.set_mark("ETHUSDT", 99_000.0)
        self.market.set_mark("SOLUSDT", 100_000.0)

    def tearDown(self) -> None:
        self.case.close()

    # -- helpers -------------------------------------------------------
    def create_strategy(self, name: str, rules: dict[str, Any], *, enable: bool = True) -> dict[str, Any]:
        created = self.store.create(name)
        sid = created["strategy_id"]
        drafted = self.store.save_draft(
            sid,
            {
                "revision": 0,
                "source_text": f"{name} 的原始描述",
                "rules": copy.deepcopy(rules),
            },
        )
        mounted = self.store.mount(
            sid,
            {
                "revision": drafted["draft"]["revision"],
                "content_hash": drafted["draft"]["content_hash"],
                "request_id": f"req-{sid}",
                "confirmed": True,
            },
        )["strategy"]
        if enable:
            mounted = self.store.authorize(
                sid, enabled=True, generation=mounted["generation"]
            )
        return mounted

    def scan(self, now_ms: int, **extra: Any) -> dict[str, Any]:
        response = self.client.post(
            "/api/rules/scan", json={"now_ms": int(now_ms), **extra}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def alias(self, now_ms: int, **extra: Any) -> dict[str, Any]:
        response = self.client.post(
            "/api/strategies/evaluate-candidates", json={"now_ms": int(now_ms), **extra}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def dispatch(self, rule_key: str, symbol: str, outcome: str, at_ms: int, **extra: Any) -> dict[str, Any]:
        response = self.client.post(
            "/api/rules/dispatched",
            json={
                "rule_key": rule_key,
                "symbol": symbol,
                "outcome": outcome,
                "at_ms": int(at_ms),
                **extra,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def rule_key(self, strategy_id: str, version: int = 1) -> str:
        return f"strategy:{strategy_id}:{version}"

    def eval_state(self, rule_key: str, symbol: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.case.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM rule_eval_state WHERE rule_key=? AND symbol=?",
                (rule_key, symbol),
            ).fetchone()
            return None if row is None else dict(row)
        finally:
            conn.close()

    def sql(self, statement: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.case.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(statement, params).fetchall()
            conn.commit()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def signal_key(now_ms: int, timeframe: str = "15m") -> str:
        period = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[
            timeframe
        ]
        return f"{timeframe}:{int(now_ms) // period}"


class EdgeTriggerTests(RuleScanTestBase):
    def test_first_true_does_not_fire_then_edge_fires_exactly_once(self) -> None:
        strategy = self.create_strategy("标记价突破", MARK_RULES)
        sid = strategy["strategy_id"]
        key = self.rule_key(sid)

        # 1) 首次求值就为真：只记录状态，不触发（R5①/§6.2：不追已经发生的信号）。
        self.market.set_mark("BTCUSDT", 101_000.0)
        first = self.scan(NOW_MS)
        self.assertEqual(first["triggered"], [])
        waiting = {item["rule_key"]: item for item in first["waiting"]}
        self.assertTrue(waiting[key]["matched"])
        self.assertEqual(waiting[key]["blocked_by"], "first_eval")
        self.assertTrue(waiting[key]["state_reset"])
        state = self.eval_state(key, "BTCUSDT")
        self.assertEqual(state["last_matched"], 1)
        self.assertEqual(state["last_eval_ms"], NOW_MS)

        # 2) 持续为真：重复扫描不重复触发（水平保持，无新边沿）。
        for offset in (INTERVAL_MS, 2 * INTERVAL_MS):
            repeat = self.scan(NOW_MS + offset)
            self.assertEqual(repeat["triggered"], [])
            blocked = {item["rule_key"]: item["blocked_by"] for item in repeat["waiting"]}
            self.assertEqual(blocked[key], "level_hold")

        # 3) 观察到一次"不满足"：last_matched 归零。
        self.market.set_mark("BTCUSDT", 98_000.0)
        below = self.scan(NOW_MS + 3 * INTERVAL_MS)
        self.assertEqual(below["triggered"], [])
        transitions = {item["rule_key"]: item for item in below["transitioned"]}
        self.assertFalse(transitions[key]["to"]["last_matched"])
        self.assertEqual(self.eval_state(key, "BTCUSDT")["last_matched"], 0)

        # 4) 假 → 真：恰好触发一次，并带出 P1/P2 现有的 strategy_context（5 个键）。
        self.market.set_mark("BTCUSDT", 101_000.0)
        edge = self.scan(NOW_MS + 4 * INTERVAL_MS)
        self.assertEqual(len(edge["triggered"]), 1, edge)
        trigger = edge["triggered"][0]
        self.assertEqual(trigger["strategy_id"], sid)
        self.assertEqual(trigger["rule_key"], key)
        self.assertEqual(trigger["symbol"], "BTCUSDT")
        self.assertEqual(trigger["side"], "long")
        self.assertEqual(trigger["signal_key"], self.signal_key(NOW_MS + 4 * INTERVAL_MS))
        self.assertEqual(trigger["mark"], 101_000.0)
        self.assertEqual(trigger["trigger_ref"], 100_000.0)
        self.assertEqual(
            sorted(trigger["strategy_context"]),
            ["generation", "mode_revision", "signal_key", "strategy_id", "version"],
        )
        self.assertEqual(trigger["strategy_context"]["strategy_id"], sid)
        self.assertEqual(trigger["strategy_context"]["version"], 1)

        # 5) 同一边沿不会重复触发（同一 now_ms 重复扫描 / 后续扫描都不再触发）。
        again = self.scan(NOW_MS + 4 * INTERVAL_MS)
        self.assertEqual(again["triggered"], [])
        self.assertEqual(self.eval_state(key, "BTCUSDT")["last_matched"], 1)

    def test_same_now_ms_is_deterministic(self) -> None:
        strategy = self.create_strategy("确定性策略", MARK_RULES)
        self.market.set_mark("BTCUSDT", 101_000.0)
        self.scan(NOW_MS)  # 首次求值，把状态推进到"已匹配"
        first = self.scan(NOW_MS + INTERVAL_MS)
        second = self.scan(NOW_MS + INTERVAL_MS)
        # 同一 now_ms、同一状态、同一行情：除 last_eval_ms 刷新（这里值相同）外完全一致。
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(first["triggered"], [])
        self.assertEqual(
            [item["rule_key"] for item in first["waiting"]],
            [item["rule_key"] for item in second["waiting"]],
        )
        self.assertEqual(
            self.eval_state(self.rule_key(strategy["strategy_id"]), "BTCUSDT")[
                "last_eval_ms"
            ],
            NOW_MS + INTERVAL_MS,
        )

    def test_stale_gap_only_records_state(self) -> None:
        """R5① 停机保护：一次求值距今 ≥3 个节奏时按"首次求值"处理。"""

        strategy = self.create_strategy("停机保护", MARK_RULES)
        sid = strategy["strategy_id"]
        key = self.rule_key(sid)

        # 首次求值为假（last_matched=0），此时条件转为真，但中间停机了 3 个间隔。
        self.scan(NOW_MS)
        self.market.set_mark("BTCUSDT", 101_000.0)
        stale = self.scan(NOW_MS + 3 * INTERVAL_MS)
        self.assertEqual(stale["triggered"], [])
        waiting = {item["rule_key"]: item for item in stale["waiting"]}
        self.assertEqual(waiting[key]["blocked_by"], "stale_gap")
        self.assertTrue(waiting[key]["state_reset"])
        state = self.eval_state(key, "BTCUSDT")
        self.assertEqual(state["last_matched"], 1)
        self.assertEqual(state["last_eval_ms"], NOW_MS + 3 * INTERVAL_MS)

        # 乱序 / 时钟回拨：now_ms 早于上次求值时也只记录，且 last_eval_ms 不倒退。
        skewed = self.scan(NOW_MS + 3 * INTERVAL_MS - 5_000)
        self.assertEqual(skewed["triggered"], [])
        skew_waiting = {item["rule_key"]: item for item in skewed["waiting"]}
        self.assertEqual(skew_waiting[key]["blocked_by"], "clock_skew")
        self.assertEqual(
            self.eval_state(key, "BTCUSDT")["last_eval_ms"], NOW_MS + 3 * INTERVAL_MS
        )

        # 对照：只隔 2 个间隔（没到阈值）时，同样的假→真必须触发。
        eth = self.create_strategy("未停机对照", {**MARK_RULES, "symbols": ["ETHUSDT"]})
        eth_key = self.rule_key(eth["strategy_id"])
        self.market.set_mark("ETHUSDT", 99_000.0)
        self.scan(NOW_MS)
        self.market.set_mark("ETHUSDT", 101_000.0)
        contrast = self.scan(NOW_MS + 2 * INTERVAL_MS)
        self.assertEqual(
            [item["rule_key"] for item in contrast["triggered"]], [eth_key]
        )

    def test_kline_rule_uses_its_own_cadence_and_scope(self) -> None:
        """K 线类规则由 Host 在收盘后 +3s 求值，停机阈值 = 周期 + 3×扫描间隔。

        15m 规则阈值 945s：隔 1 个周期（900s）的正常节奏不能被判成停机；
        mark 规则仍是 3×扫描间隔（45s），见 test_stale_thresholds_match_rule_cadence。
        """

        strategy = self.create_strategy("K 线收盘", KLINE_RULES)
        sid = strategy["strategy_id"]
        key = self.rule_key(sid)

        def bars(close: float, *, end_ms: int) -> list[dict[str, Any]]:
            return [
                {
                    "open_time_ms": end_ms - 900_000,
                    "close_time_ms": end_ms - 1,
                    "close": close,
                    "final": True,
                }
            ]

        self.market.set_mark("SOLUSDT", 100_000.0)
        self.market.set_bars("SOLUSDT", "15m", bars(101.0, end_ms=NOW_MS))
        first = self.scan(NOW_MS, scope="all")
        self.assertEqual(first["triggered"], [])

        # 正常节奏（隔 1 个周期 = 900s < 945s）不算停机：状态保持为"已匹配"。
        self.market.set_bars("SOLUSDT", "15m", bars(101.0, end_ms=NOW_MS + 900_000))
        hold = self.scan(NOW_MS + 900_000, scope="all")
        self.assertEqual(hold["triggered"], [])
        waiting = {item["rule_key"]: item for item in hold["waiting"]}
        self.assertEqual(waiting[key]["blocked_by"], "level_hold")
        self.assertFalse(waiting[key]["state_reset"])

        # 收盘价转假 → 再转真：边沿触发（距上次求值只有 1 个周期，不触发停机保护）。
        self.market.set_bars("SOLUSDT", "15m", bars(99.0, end_ms=NOW_MS + 1_800_000))
        self.assertEqual(self.scan(NOW_MS + 1_800_000, scope="all")["triggered"], [])
        self.market.set_bars("SOLUSDT", "15m", bars(101.0, end_ms=NOW_MS + 2_700_000))
        edge = self.scan(NOW_MS + 2_700_000, scope="all")
        self.assertEqual([item["rule_key"] for item in edge["triggered"]], [key])
        self.assertEqual(edge["triggered"][0]["trigger_ref"], 100.0)
        self.assertEqual(
            self.eval_state(key, "SOLUSDT")["last_bar_open_ms"],
            NOW_MS + 2_700_000 - 900_000,
        )

        # mark 范围的扫描不求值 K 线规则（Host 每 15s 的 tick 不背 K 线查询成本）。
        mark_scope = self.scan(NOW_MS + 2_700_000 + INTERVAL_MS, scope="mark")
        self.assertEqual(mark_scope["triggered"], [])
        self.assertNotIn(key, [item["rule_key"] for item in mark_scope["waiting"]])
        filtered = [
            item
            for item in mark_scope["skipped"]
            if item.get("strategy_id") == sid and item["reason_code"] == "SCOPE_FILTERED"
        ]
        self.assertEqual(len(filtered), 1)

    def test_dispatch_rearm_and_rejected_cooldown(self) -> None:
        strategy = self.create_strategy("派发回写", MARK_RULES)
        sid = strategy["strategy_id"]
        key = self.rule_key(sid)
        self.market.set_mark("BTCUSDT", 99_000.0)
        self.scan(NOW_MS)  # 首次求值为假

        # 假→真：第一次派发。
        self.market.set_mark("BTCUSDT", 101_000.0)
        edge = self.scan(NOW_MS + INTERVAL_MS)
        self.assertEqual([item["rule_key"] for item in edge["triggered"]], [key])
        dispatched = self.dispatch(key, "BTCUSDT", "dispatched", NOW_MS + INTERVAL_MS)
        self.assertFalse(dispatched["idempotent"])
        self.assertTrue(dispatched["state"]["rearm_pending"])
        self.assertTrue(dispatched["state"]["last_matched"])
        self.assertIsNone(dispatched["state"]["cooldown_until_ms"])

        # 幂等重放：同一个 at_ms 再回写一次，状态不变、冷却不叠加。
        replay = self.dispatch(key, "BTCUSDT", "dispatched", NOW_MS + INTERVAL_MS)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["state"], dispatched["state"])

        # 持续为真：重新武装未清除前不再触发。
        hold = self.scan(NOW_MS + 2 * INTERVAL_MS)
        self.assertEqual(hold["triggered"], [])
        waiting = {item["rule_key"]: item for item in hold["waiting"]}
        self.assertTrue(waiting[key]["rearm_pending"])

        # 必须观察到一次"不满足"：此后才会重新武装。
        self.market.set_mark("BTCUSDT", 98_000.0)
        self.scan(NOW_MS + 3 * INTERVAL_MS)
        armed = self.eval_state(key, "BTCUSDT")
        self.assertEqual(armed["rearm_pending"], 0)
        self.assertEqual(armed["last_matched"], 0)
        self.market.set_mark("BTCUSDT", 101_000.0)
        second_edge = self.scan(NOW_MS + 4 * INTERVAL_MS)
        self.assertEqual([item["rule_key"] for item in second_edge["triggered"]], [key])

        # P1 拒绝：额外冷却 1 个扫描间隔；冷却期内即使出现边沿也不触发。
        rejected = self.dispatch(
            key, "BTCUSDT", "rejected", NOW_MS + 4 * INTERVAL_MS
        )
        self.assertFalse(rejected["idempotent"])
        self.assertEqual(
            rejected["state"]["cooldown_until_ms"],
            NOW_MS + 4 * INTERVAL_MS + INTERVAL_MS,
        )
        rejected_replay = self.dispatch(
            key, "BTCUSDT", "rejected", NOW_MS + 4 * INTERVAL_MS
        )
        self.assertTrue(rejected_replay["idempotent"])
        self.assertEqual(
            rejected_replay["state"]["cooldown_until_ms"],
            rejected["state"]["cooldown_until_ms"],
        )
        self.market.set_mark("BTCUSDT", 98_000.0)
        self.scan(NOW_MS + 4 * INTERVAL_MS + 2_000)  # 观察到不满足，清掉重新武装
        self.market.set_mark("BTCUSDT", 101_000.0)
        # 冷却到 NOW+5i 才结束；NOW+4i+5s 出现的新边沿必须被挡住。
        cooled = self.scan(NOW_MS + 4 * INTERVAL_MS + 5_000)
        self.assertEqual(cooled["triggered"], [])
        waiting = {item["rule_key"]: item for item in cooled["waiting"]}
        self.assertEqual(waiting[key]["blocked_by"], "cooldown")
        self.assertEqual(
            waiting[key]["cooldown_until_ms"],
            rejected["state"]["cooldown_until_ms"],
        )
        # 冷却结束后要等下一次"不满足 → 满足"才触发（被挡住的边沿按 fail-closed 丢弃）。
        self.market.set_mark("BTCUSDT", 98_000.0)
        self.scan(NOW_MS + 6 * INTERVAL_MS)
        self.market.set_mark("BTCUSDT", 101_000.0)
        after = self.scan(NOW_MS + 7 * INTERVAL_MS)
        self.assertEqual([item["rule_key"] for item in after["triggered"]], [key])

    def test_dispatch_validation(self) -> None:
        strategy = self.create_strategy("回写校验", MARK_RULES)
        sid = strategy["strategy_id"]
        bad_outcome = self.client.post(
            "/api/rules/dispatched",
            json={
                "rule_key": self.rule_key(sid),
                "symbol": "BTCUSDT",
                "outcome": "filled",
                "at_ms": NOW_MS,
            },
        )
        self.assertEqual(bad_outcome.status_code, 422)
        self.assertEqual(bad_outcome.json()["error"]["code"], "E_VALIDATION")

        bad_key = self.client.post(
            "/api/rules/dispatched",
            json={
                "rule_key": "setup:whatever",
                "symbol": "BTCUSDT",
                "outcome": "dispatched",
                "at_ms": NOW_MS,
            },
        )
        self.assertEqual(bad_key.status_code, 422)
        self.assertEqual(bad_key.json()["error"]["code"], "E_RULE_KEY_INVALID")

        unknown = self.client.post(
            "/api/rules/dispatched",
            json={
                "rule_key": "strategy:does-not-exist:1",
                "symbol": "BTCUSDT",
                "outcome": "dispatched",
                "at_ms": NOW_MS,
            },
        )
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "E_STRATEGY_NOT_FOUND")

    def test_consumed_signal_is_excluded(self) -> None:
        strategy = self.create_strategy("信号消费", MARK_RULES)
        sid = strategy["strategy_id"]
        key = self.rule_key(sid)
        self.scan(NOW_MS)  # 首次求值为假
        self.market.set_mark("BTCUSDT", 101_000.0)
        edge_at = NOW_MS + INTERVAL_MS
        # 模拟 P2 已消费该周期信号（strategy_signals 里有非 rejected 行）。
        self.sql(
            "INSERT INTO strategy_signals "
            "(strategy_id,version,symbol,side,signal_key,cycle_id,state,context_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sid,
                1,
                "BTCUSDT",
                "long",
                self.signal_key(edge_at),
                "cyc-consumed-1",
                "reserved",
                "{}",
                "2026-11-22T00:00:00Z",
            ),
        )
        blocked = self.scan(edge_at)
        self.assertEqual(blocked["triggered"], [])
        waiting = {item["rule_key"]: item for item in blocked["waiting"]}
        self.assertEqual(waiting[key]["blocked_by"], "already_signaled")
        # 兼容别名的旧 skipped 语义保持：ALREADY_SIGNALED。
        alias = self.alias(edge_at)
        codes = {
            item["reason_code"]
            for item in alias["skipped"]
            if item["strategy_id"] == sid
        }
        self.assertIn("ALREADY_SIGNALED", codes)
        self.assertEqual(alias["candidates"], [])

    def test_disabled_not_ready_and_non_whitelisted_are_never_triggered(self) -> None:
        active = self.create_strategy("已授权", MARK_RULES)
        disabled = self.create_strategy("未授权", {**MARK_RULES, "symbols": ["ETHUSDT"]}, enable=False)
        self.market.set_mark("BTCUSDT", 101_000.0)
        self.market.set_mark("ETHUSDT", 101_000.0)
        payload = self.scan(NOW_MS)
        self.assertEqual(payload["triggered"], [])
        active_id = active["strategy_id"]
        disabled_id = disabled["strategy_id"]
        appearing = {
            item.get("strategy_id")
            for item in payload["waiting"] + payload["skipped"]
        }
        self.assertIn(active_id, appearing)
        self.assertNotIn(disabled_id, appearing)

        # 历史版本规则不再具备执行条件（enabled 仍为 1）：不进 triggered / waiting。
        not_ready = self.create_strategy("未就绪", {**MARK_RULES, "symbols": ["SOLUSDT"]})
        not_ready_id = not_ready["strategy_id"]
        tampered = copy.deepcopy(MARK_RULES)
        tampered["requires_ai"] = True
        self.tamper_version(not_ready_id, 1, tampered)
        self.market.set_mark("SOLUSDT", 101_000.0)
        after = self.scan(NOW_MS + INTERVAL_MS)
        self.assertNotIn(
            not_ready_id,
            {item.get("strategy_id") for item in after["waiting"] + after["triggered"]},
        )
        skipped_codes = {
            (item.get("strategy_id"), item["reason_code"]) for item in after["skipped"]
        }
        self.assertIn((not_ready_id, "NOT_READY"), skipped_codes)

        # 白名单收窄：不在白名单的标的不参与求值，也不出现在 waiting / triggered。
        mixed = self.create_strategy(
            "混合标的", {**MARK_RULES, "symbols": ["BTCUSDT", "ETHUSDT"]}
        )
        with mock.patch.object(
            strategies_api, "_WHITELIST_SET", frozenset({"BTCUSDT"})
        ):
            narrowed = self.scan(NOW_MS + 2 * INTERVAL_MS)
        symbols_seen = {
            (item.get("strategy_id"), item.get("symbol"))
            for item in narrowed["waiting"] + narrowed["triggered"]
        }
        self.assertIn((mixed["strategy_id"], "BTCUSDT"), symbols_seen)
        self.assertNotIn((mixed["strategy_id"], "ETHUSDT"), symbols_seen)
        self.assertIn(
            (mixed["strategy_id"], "SYMBOL_NOT_ALLOWED"),
            {(item.get("strategy_id"), item["reason_code"]) for item in narrowed["skipped"]},
        )

    def test_priority_and_created_at_ordering(self) -> None:
        first = self.create_strategy("A 优先 5", {**MARK_RULES, "priority": 5})
        second = self.create_strategy(
            "B 优先 5", {**MARK_RULES, "symbols": ["ETHUSDT"], "priority": 5}
        )
        third = self.create_strategy(
            "C 优先 1", {**MARK_RULES, "symbols": ["SOLUSDT"], "priority": 1}
        )
        # 用显式 created_at 验证"创建时间升序"，不依赖秒级时间戳的偶然差异。
        self.sql(
            "UPDATE strategies SET created_at=? WHERE strategy_id=?",
            ("2026-01-01T00:00:00Z", first["strategy_id"]),
        )
        self.sql(
            "UPDATE strategies SET created_at=? WHERE strategy_id=?",
            ("2026-02-01T00:00:00Z", second["strategy_id"]),
        )
        self.sql(
            "UPDATE strategies SET created_at=? WHERE strategy_id=?",
            ("2025-12-01T00:00:00Z", third["strategy_id"]),
        )
        # 首次求值全部为假，随后同一轮里全部转为真：一次看到排序。
        self.scan(NOW_MS)
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
            self.market.set_mark(symbol, 101_000.0)
        payload = self.scan(NOW_MS + INTERVAL_MS)
        self.assertEqual(
            [item["strategy_id"] for item in payload["triggered"]],
            [first["strategy_id"], second["strategy_id"], third["strategy_id"]],
        )
        self.assertEqual(
            [item["priority"] for item in payload["triggered"]], [5.0, 5.0, 1.0]
        )

    def test_scan_validation_and_interval_clamp(self) -> None:
        strategy = self.create_strategy("参数校验", MARK_RULES)
        self.market.set_mark("BTCUSDT", 99_000.0)
        low = self.scan(NOW_MS, scan_interval_ms=1_000)
        self.assertEqual(low["scan_interval_ms"], 5_000)
        high = self.scan(NOW_MS, scan_interval_ms=999_999)
        self.assertEqual(high["scan_interval_ms"], 60_000)
        default = self.scan(NOW_MS)
        self.assertEqual(default["scan_interval_ms"], 15_000)
        self.assertEqual(default["mode"], "mounted")
        self.assertEqual(default["mode_revision"], 1)

        bad_scope = self.client.post(
            "/api/rules/scan", json={"now_ms": NOW_MS, "scope": "ticker"}
        )
        self.assertEqual(bad_scope.status_code, 422)
        self.assertEqual(bad_scope.json()["error"]["code"], "E_VALIDATION")
        # 空策略表也必须返回结构化的空结果。
        self.store.authorize(
            strategy["strategy_id"],
            enabled=False,
            generation=self.store.get(strategy["strategy_id"])["generation"],
        )
        empty = self.scan(NOW_MS)
        self.assertEqual(empty["triggered"], [])
        self.assertEqual(empty["waiting"], [])
        self.assertEqual(empty["prices"], {})

    def test_scan_has_no_dispatch_side_effects(self) -> None:
        self.create_strategy("无副作用", MARK_RULES)
        self.market.set_mark("BTCUSDT", 101_000.0)
        before = {
            table: self.sql(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            for table in ("strategy_signals", "strategy_decisions", "cycle_runs", "strategy_events")
        }
        for offset in (0, INTERVAL_MS, 2 * INTERVAL_MS):
            self.scan(NOW_MS + offset)
        after = {
            table: self.sql(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
            for table in ("strategy_signals", "strategy_decisions", "cycle_runs", "strategy_events")
        }
        self.assertEqual(before, after)

    def test_evaluate_candidates_alias_keeps_level_view(self) -> None:
        true_strategy = self.create_strategy("恒为真", MARK_RULES)
        false_strategy = self.create_strategy(
            "恒为假", {**MARK_RULES, "symbols": ["ETHUSDT"]}
        )
        self.market.set_mark("BTCUSDT", 101_000.0)
        self.market.set_mark("ETHUSDT", 99_000.0)
        payload = self.alias(NOW_MS)
        # 旧字段：水平候选（首次为真也在内），与 /rules/scan 的 triggered 不同。
        self.assertEqual(payload["triggered"], [])
        self.assertEqual(
            [item["strategy_id"] for item in payload["candidates"]],
            [true_strategy["strategy_id"]],
        )
        self.assertEqual(payload["candidates"][0]["execution_mode"], "ai_assisted")
        self.assertEqual(payload["candidates"][0]["version"], 1)
        self.assertIn("rules", payload["candidates"][0])
        self.assertIn("confirmed_version", payload["candidates"][0])
        reason_codes = {
            (item["strategy_id"], item["reason_code"]) for item in payload["skipped"]
        }
        self.assertIn((false_strategy["strategy_id"], "CONDITION_FALSE"), reason_codes)
        # 规则级跳过清单改名 rule_skipped，避免覆盖旧 skipped。
        self.assertIn("rule_skipped", payload)
        self.assertEqual(payload["rule_skipped"], [])
        self.assertEqual(payload["prices"]["BTCUSDT"]["mark"], 101_000.0)

    def test_stale_thresholds_match_rule_cadence(self) -> None:
        """停机阈值口径（t8）：mark = 3×扫描间隔；K 线 = K 线周期 + 3×扫描间隔。"""

        self.assertEqual(
            rule_scan.stale_after_ms(INTERVAL_MS, MARK_RULES), 3 * INTERVAL_MS
        )
        self.assertEqual(
            rule_scan.stale_after_ms(INTERVAL_MS, KLINE_RULES),
            900_000 + 3 * INTERVAL_MS,
        )
        hourly = {**KLINE_RULES, "timeframes": ["1h"]}
        self.assertEqual(
            rule_scan.stale_after_ms(INTERVAL_MS, hourly),
            3_600_000 + 3 * INTERVAL_MS,
        )
        # 缺省 15s；小于下限的间隔按 5s 夹紧（mark 15s / K 线 900s+15s）。
        self.assertEqual(rule_scan.stale_after_ms(None, MARK_RULES), 45_000)
        self.assertEqual(rule_scan.stale_after_ms(1_000, MARK_RULES), 15_000)
        self.assertEqual(
            rule_scan.stale_after_ms(1_000, KLINE_RULES), 900_000 + 15_000
        )

    def test_kline_rule_restart_after_20_minutes_only_records_state(self) -> None:
        """R5①：15m 规则停机 20 分钟后首次扫描只写状态；正常逐周期求值仍能触发。"""

        def bars(close: float, *, end_ms: int) -> list[dict[str, Any]]:
            return [
                {
                    "open_time_ms": end_ms - 900_000,
                    "close_time_ms": end_ms - 1,
                    "close": close,
                    "final": True,
                }
            ]

        normal = self.create_strategy(
            "K 线正常节奏", {**KLINE_RULES, "symbols": ["SOLUSDT"]}
        )
        dormant = self.create_strategy(
            "K 线停机 20 分钟", {**KLINE_RULES, "symbols": ["ETHUSDT"]}
        )
        normal_key = self.rule_key(normal["strategy_id"])
        dormant_key = self.rule_key(dormant["strategy_id"])
        self.market.set_mark("SOLUSDT", 100_000.0)
        self.market.set_mark("ETHUSDT", 100_000.0)
        self.market.set_bars("SOLUSDT", "15m", bars(99.0, end_ms=NOW_MS))
        self.market.set_bars("ETHUSDT", "15m", bars(99.0, end_ms=NOW_MS))
        # 首次求值都为假（last_matched=0），两台规则各自进入观察。
        self.assertEqual(self.scan(NOW_MS, scope="all")["triggered"], [])

        # 正常节奏：下一根收盘 +3s 求值，假→真必须触发（gap=903s < 945s）。
        next_close = NOW_MS + 900_000 + 3_000
        self.market.set_bars("SOLUSDT", "15m", bars(101.0, end_ms=next_close))
        fired = self.scan(next_close, scope="all")
        self.assertEqual([item["rule_key"] for item in fired["triggered"]], [normal_key])

        # 停机 20 分钟（1200s ≥ 945s）：条件在这段时间里已经为真，重启后只写状态。
        restart_at = next_close + 1_200_000
        self.market.set_bars("ETHUSDT", "15m", bars(101.0, end_ms=restart_at))
        stale = self.scan(restart_at, scope="all")
        self.assertEqual(stale["triggered"], [])
        waiting = {item["rule_key"]: item for item in stale["waiting"]}
        self.assertEqual(waiting[dormant_key]["blocked_by"], "stale_gap")
        self.assertTrue(waiting[dormant_key]["state_reset"])
        state = self.eval_state(dormant_key, "ETHUSDT")
        self.assertEqual(state["last_matched"], 1)
        self.assertEqual(state["last_eval_ms"], restart_at)

    def test_rules_routes_registered_with_include_router(self) -> None:
        """C2：/api/rules 由 app.include_router 装配——OpenAPI、404、鉴权都正常。"""

        spec = self.client.get("/openapi.json")
        self.assertEqual(spec.status_code, 200)
        paths = spec.json()["paths"]
        self.assertIn("/api/rules/scan", paths)
        self.assertIn("/api/rules/dispatched", paths)
        self.assertEqual(paths["/api/rules/scan"]["post"]["tags"], ["rules"])

        unknown = self.client.post("/api/rules/nope", json={})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "E_NOT_FOUND")

        # bearer 模式：两个端点都必须先过鉴权；未知路径仍是 404。
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), mode=AUTH_MODE_BEARER)
            app = create_app(settings)
            app.state.market = FakeMarket(now_ms=NOW_MS)
            app.state.services = FakeExecution()
            with TestClient(app) as client:
                self.assertEqual(client.post("/api/rules/scan", json={}).status_code, 401)
                unauthorized = client.post(
                    "/api/rules/dispatched",
                    json={
                        "rule_key": "strategy:whatever:1",
                        "symbol": "BTCUSDT",
                        "outcome": "dispatched",
                        "at_ms": NOW_MS,
                    },
                )
                self.assertEqual(unauthorized.status_code, 401)
                headers = {"Authorization": f"Bearer {settings.api_token}"}
                authorized = client.post(
                    "/api/rules/scan", json={"now_ms": NOW_MS}, headers=headers
                )
                self.assertEqual(authorized.status_code, 200, authorized.text)

    # -- 测试专用：把确认版本改成"不再具备执行条件"（模拟历史版本规则退化为不支持）
    def tamper_version(self, strategy_id: str, version: int, rules: dict[str, Any]) -> None:
        conn = sqlite3.connect(self.case.db_path)
        try:
            # strategy_versions 的不可变触发器只挡 UPDATE；测试里临时移除它。
            conn.execute("DROP TRIGGER IF EXISTS strategy_versions_no_update")
            row = conn.execute(
                "SELECT payload_json FROM strategy_versions WHERE strategy_id=? AND version=?",
                (strategy_id, version),
            ).fetchone()
            payload = json.loads(row[0])
            payload["rules"] = rules
            conn.execute(
                "UPDATE strategy_versions SET payload_json=? WHERE strategy_id=? AND version=?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), strategy_id, version),
            )
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
