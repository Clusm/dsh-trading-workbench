"""v3.1 P2 cycle purpose（docs/19 R2）：research / trigger / manual 并存。

覆盖：
* 同 purpose 只允许 1 个 RUNNING，同 purpose 第二个 create 返回 409 E_CYCLE_RUNNING；
* 不同 purpose 可以同时 RUNNING；finalize 只作用于给定 cycle_id，不误伤其它 purpose；
* POST /api/cycles 支持 {purpose, symbols}：symbols 只限制证据生成范围（单币 cycle），
  不扩大可交易标的；trigger cycle 只给 symbol 时也按单币生成证据；
* cycle_runs.purpose 的增量迁移（旧库补列 + 旧行按 manual 读）。

离线：FakeMarket 只写内存/临时目录里的 md-evidence/1，FakeExecution 没有凭据、不下单。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from workbench_service.app import create_app  # noqa: E402
from workbench_service.config import AUTH_MODE_DEV_OPEN, Settings  # noqa: E402
from workbench_service.cycles.schema import ensure_cycle_schema  # noqa: E402
from workbench_service.market.config import WHITELIST  # noqa: E402
from workbench_service.market.evidence import canonical_hash  # noqa: E402


class FakeMarket:
    """确定性 md-evidence/1 写入器；记录 deep_symbols 以便断言"单币证据"。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.calls: list[dict[str, Any]] = []
        self.settings = SimpleNamespace(whitelist=WHITELIST)

    def build_evidence(
        self,
        *,
        cycle_id: str,
        held_symbols: list[str] | tuple[str, ...] = (),
        prev_pick: str | None = None,
        deep_symbols: Any = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "cycle_id": cycle_id,
                "held": list(held_symbols),
                "prev_pick": prev_pick,
                "deep_symbols": None if deep_symbols is None else list(deep_symbols),
            }
        )
        produced_ms = int(time.time() * 1000)
        payload: dict[str, Any] = {
            "schema_version": "md-evidence/1",
            "cycle_id": cycle_id,
            "produced_at_ms": produced_ms,
            "produced_at": "2026-11-22T00:00:00Z",
            "snapshot_id": f"ev-{cycle_id}",
            "data_health": {"verdict": "GOOD", "score": 100.0, "issues": []},
            "limits": {"prompt_token_estimate": 12, "truncated": False},
            "symbols": {},
            "provenance": {
                "service": "fake-market",
                "snapshot_hash": "sha256:" + "0" * 64,
            },
        }
        digest = canonical_hash(payload)
        payload["provenance"]["snapshot_hash"] = "sha256:" + digest
        directory = self.data_dir / "evidence" / "2026-11-22" / cycle_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "evidence.json"
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        target.write_bytes(encoded)
        return {
            "cycle_id": cycle_id,
            "snapshot_id": payload["snapshot_id"],
            "verdict": "GOOD",
            "score": 100.0,
            "sha256": payload["provenance"]["snapshot_hash"],
            "path": target.relative_to(self.data_dir).as_posix(),
            "abs_path": str(target),
            "bytes": len(encoded),
            "prompt_token_estimate": 12,
            "deep": list(deep_symbols or ["BTCUSDT"]),
            "index_count": 0,
            "duration_ms": 1.0,
            "evidence": payload,
        }


class FakeExecution:
    """只读执行门面；没有凭据、没有交易所、没有订单。"""

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


def make_settings(data_dir: Path) -> Settings:
    keys = ("WORKBENCH_AUTH_MODE", "WORKBENCH_API_TOKEN")
    old = {key: os.environ.get(key) for key in keys}
    try:
        os.environ["WORKBENCH_AUTH_MODE"] = AUTH_MODE_DEV_OPEN
        os.environ.pop("WORKBENCH_API_TOKEN", None)
        return Settings.from_env(
            data_dir=str(data_dir), default_auth_mode=AUTH_MODE_DEV_OPEN
        )
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def client_for(
    tmp: str,
) -> Iterator[tuple[TestClient, Settings, FakeMarket]]:
    settings = make_settings(Path(tmp))
    app = create_app(settings)
    market = FakeMarket(Path(tmp))
    app.state.market = market
    app.state.services = FakeExecution()
    with TestClient(app) as client:
        yield client, settings, market


def create_cycle(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/cycles", json=body or None)
    assert response.status_code == 200, response.text
    return response.json()


def finalize_cycle(client: TestClient, cycle_id: str, **body: Any) -> Any:
    payload = {"state": "DONE", "outcome": "no_trade", **body}
    return client.post(f"/api/cycles/{cycle_id}/finalize", json=payload)


class CyclePurposeTests(unittest.TestCase):
    def test_research_and_trigger_run_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                research = create_cycle(client, purpose="research", symbol="BTCUSDT", trigger="auto")
                self.assertEqual(research["purpose"], "research")
                self.assertEqual(research["state"], "RUNNING")

                trigger = create_cycle(
                    client,
                    purpose="trigger",
                    symbol="ETHUSDT",
                    symbols=["ETHUSDT"],
                    trigger="auto",
                )
                self.assertEqual(trigger["purpose"], "trigger")
                manual = create_cycle(client, purpose="manual")
                self.assertEqual(manual["purpose"], "manual")
                # 不传 purpose 的旧调用方仍是 manual（向后兼容）。
                legacy = client.post("/api/cycles", json={"slot_id": "slot-legacy"})
                self.assertEqual(legacy.status_code, 409)
                self.assertEqual(legacy.json()["error"]["code"], "E_CYCLE_RUNNING")

                # 同 purpose 第二个 create：409，并且 details 说清是哪个 purpose / 哪个 cycle。
                conflict = client.post(
                    "/api/cycles", json={"purpose": "trigger", "symbol": "BTCUSDT"}
                )
                self.assertEqual(conflict.status_code, 409)
                body = conflict.json()["error"]
                self.assertEqual(body["code"], "E_CYCLE_RUNNING")
                self.assertEqual(body["details"]["purpose"], "trigger")
                self.assertEqual(body["details"]["cycle_id"], trigger["cycle_id"])
                other_purposes = {
                    item["purpose"] for item in body["details"]["other_running"]
                }
                self.assertEqual(other_purposes, {"research", "manual"})

                conflict_research = client.post("/api/cycles", json={"purpose": "research"})
                self.assertEqual(conflict_research.status_code, 409)
                self.assertEqual(conflict_research.json()["error"]["code"], "E_CYCLE_RUNNING")

                # purpose 落库；GET /api/cycles?purpose= 只看自己的 purpose。
                research_list = client.get("/api/cycles?purpose=research")
                self.assertEqual(research_list.status_code, 200)
                self.assertEqual(research_list.json()["count"], 1)
                self.assertEqual(
                    research_list.json()["cycles"][0]["cycle_id"], research["cycle_id"]
                )
                trigger_list = client.get("/api/cycles?purpose=trigger").json()
                self.assertEqual(
                    [item["cycle_id"] for item in trigger_list["cycles"]],
                    [trigger["cycle_id"]],
                )
                conn = sqlite3.connect(Path(tmp) / "workbench-v1.sqlite")
                try:
                    rows = dict(
                        conn.execute(
                            "SELECT cycle_id, purpose FROM cycle_runs ORDER BY started_at"
                        ).fetchall()
                    )
                finally:
                    conn.close()
                self.assertEqual(rows[research["cycle_id"]], "research")
                self.assertEqual(rows[trigger["cycle_id"]], "trigger")
                self.assertEqual(rows[manual["cycle_id"]], "manual")

    def test_finalize_one_purpose_does_not_touch_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                research = create_cycle(client, purpose="research", symbol="BTCUSDT")
                trigger = create_cycle(
                    client,
                    purpose="trigger",
                    symbol="ETHUSDT",
                    symbols=["ETHUSDT"],
                    trigger="auto",
                )
                done = finalize_cycle(client, research["cycle_id"])
                self.assertEqual(done.status_code, 200, done.text)
                self.assertEqual(done.json()["cycle"]["state"], "DONE")
                self.assertEqual(done.json()["cycle"]["purpose"], "research")

                survivor = client.get(f"/api/cycles/{trigger['cycle_id']}")
                self.assertEqual(survivor.status_code, 200)
                self.assertEqual(survivor.json()["cycle"]["state"], "RUNNING")
                self.assertEqual(survivor.json()["cycle"]["purpose"], "trigger")

                # research 结束后同 purpose 可以再开；trigger 仍在跑，所以 trigger 仍冲突。
                second_research = create_cycle(client, purpose="research")
                self.assertEqual(second_research["purpose"], "research")
                still_conflict = client.post("/api/cycles", json={"purpose": "trigger"})
                self.assertEqual(still_conflict.status_code, 409)
                self.assertEqual(
                    still_conflict.json()["error"]["details"]["cycle_id"],
                    trigger["cycle_id"],
                )
                # finalize 未知 cycle：404，不影响任何 RUNNING。
                missing = finalize_cycle(client, "cyc-does-not-exist")
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(missing.json()["error"]["code"], "E_CYCLE_NOT_FOUND")
                self.assertEqual(
                    client.get(f"/api/cycles/{trigger['cycle_id']}").json()["cycle"]["state"],
                    "RUNNING",
                )
                self.assertEqual(
                    client.get("/api/cycles?purpose=trigger").json()["cycles"][0]["state"],
                    "RUNNING",
                )
                # finalize 是幂等的：同 state/outcome 再来一次不报错。
                replay = finalize_cycle(client, research["cycle_id"])
                self.assertEqual(replay.status_code, 200)
                self.assertTrue(replay.json()["idempotent"])

    def test_trigger_cycle_evidence_is_single_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, market):
                first = create_cycle(
                    client,
                    purpose="trigger",
                    symbol="BTCUSDT",
                    symbols=["BTCUSDT"],
                    trigger="auto",
                )
                self.assertEqual(market.calls[-1]["deep_symbols"], ["BTCUSDT"])
                self.assertEqual(first["symbol"], "BTCUSDT")
                self.assertEqual(first["symbols"], ["BTCUSDT"])
                finalize_cycle(client, first["cycle_id"])

                # 只给 symbol、purpose=trigger：服务端仍按单币生成证据（R14 ≤400s）。
                second = create_cycle(client, purpose="trigger", symbol="ETHUSDT", trigger="auto")
                self.assertEqual(market.calls[-1]["deep_symbols"], ["ETHUSDT"])
                self.assertEqual(second["symbol"], "ETHUSDT")
                finalize_cycle(client, second["cycle_id"])

                # 手动 cycle 只给 symbol：保持 v0.2.1 的全池证据（不传 deep_symbols）。
                third = create_cycle(client, symbol="BTCUSDT")
                self.assertIsNone(market.calls[-1]["deep_symbols"])
                finalize_cycle(client, third["cycle_id"])

                # 研判 cycle 显式给多个标的：按其限制证据范围。
                fourth = create_cycle(
                    client, purpose="research", symbols=["BTCUSDT", "SOLUSDT"]
                )
                self.assertEqual(
                    market.calls[-1]["deep_symbols"], ["BTCUSDT", "SOLUSDT"]
                )
                self.assertEqual(fourth["symbol"], "BTCUSDT")
                self.assertEqual(fourth["symbols"], ["BTCUSDT", "SOLUSDT"])

    def test_cycle_symbols_and_purpose_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with client_for(tmp) as (client, _settings, _market):
                bad_purpose = client.post("/api/cycles", json={"purpose": "analyse"})
                self.assertEqual(bad_purpose.status_code, 422)
                self.assertEqual(bad_purpose.json()["error"]["code"], "E_VALIDATION")

                outside = client.post("/api/cycles", json={"symbols": ["FOOUSDT"]})
                self.assertEqual(outside.status_code, 422)
                self.assertEqual(outside.json()["error"]["code"], "E_SYMBOL_NOT_ALLOWED")

                mismatch = client.post(
                    "/api/cycles", json={"symbol": "ETHUSDT", "symbols": ["BTCUSDT"]}
                )
                self.assertEqual(mismatch.status_code, 422)
                self.assertEqual(mismatch.json()["error"]["code"], "E_VALIDATION")

                not_array = client.post("/api/cycles", json={"symbols": "BTCUSDT"})
                self.assertEqual(not_array.status_code, 422)
                self.assertEqual(not_array.json()["error"]["code"], "E_VALIDATION")

                too_many = client.post(
                    "/api/cycles", json={"symbols": [symbol for symbol in WHITELIST[:9]]}
                )
                self.assertEqual(too_many.status_code, 422)
                self.assertEqual(too_many.json()["error"]["code"], "E_VALIDATION")
                # 校验失败不留下 RUNNING cycle。
                self.assertEqual(client.get("/api/cycles").json()["count"], 0)

    def test_purpose_migration_adds_column_and_keeps_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "workbench-v1.sqlite"
            conn = sqlite3.connect(db)
            try:
                # v0.2.1 的 cycle_runs 形状（没有 purpose 列）。
                conn.execute(
                    "CREATE TABLE cycle_runs ("
                    "cycle_id TEXT PRIMARY KEY, slot_id TEXT, state TEXT NOT NULL, outcome TEXT, "
                    "trigger TEXT NOT NULL DEFAULT 'manual', symbol TEXT, started_at TEXT NOT NULL, "
                    "finished_at TEXT, reason_code TEXT, error_code TEXT, execution_json TEXT, "
                    "decision_json TEXT, evidence_snapshot_id TEXT, account_json TEXT, "
                    "risk_envelope_json TEXT, created_at TEXT, updated_at TEXT)"
                )
                conn.execute(
                    "INSERT INTO cycle_runs (cycle_id, state, started_at, created_at) "
                    "VALUES ('cyc-legacy','DONE','2026-11-01T00:00:00Z','2026-11-01T00:00:00Z')"
                )
                conn.commit()
                ensure_cycle_schema(conn)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(cycle_runs)")}
                self.assertIn("purpose", columns)
                row = conn.execute(
                    "SELECT purpose FROM cycle_runs WHERE cycle_id='cyc-legacy'"
                ).fetchone()
                self.assertEqual(row[0], "manual")
                indexes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertIn("idx_cycle_runs_purpose", indexes)
                # 幂等：重复迁移不改变旧行。
                ensure_cycle_schema(conn)
                self.assertEqual(
                    conn.execute(
                        "SELECT purpose FROM cycle_runs WHERE cycle_id='cyc-legacy'"
                    ).fetchone()[0],
                    "manual",
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
