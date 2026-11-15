"""tests_execution 公共夹具：临时库 + fake 上游 + 应用实例。

全部离线：不读 env 凭据（``use_env_credentials=False``）、不访问网络
（``httpx.MockTransport``）、不写旧库（临时目录）。
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from workbench_service.app import create_app  # noqa: E402
from workbench_service.config import Settings  # noqa: E402
from workbench_service.execution.client import DemoBinanceClient  # noqa: E402
from workbench_service.execution.credentials import DemoCredentials  # noqa: E402
from workbench_service.execution.service import build_execution_service  # noqa: E402
from workbench_service.risk.config import RiskLimits  # noqa: E402

MARK = "100000.0"
PLAN: dict[str, Any] = {
    "symbol": "BTCUSDT",
    "side": "LONG",
    "action": "open",
    "entry_type": "MARKET",
    "leverage": 8,
    "qty": "0.02",
    "sl": "99000.0",
    "tp": "102800.0",
    "confidence": 0.50,
    "reasoning_ref": "decision:test",
}

MARKET: dict[str, Any] = {
    "mark": MARK,
    "snapshot_age_ms": 1200,
    "mark_move_60s_pct": 0.004,
    "mark_index_deviation_pct": 0.001,
    "spread_bps": 3.0,
}


class ServiceCase:
    """一次性的临时数据目录 + settings + 可选 fake 服务。"""

    def __init__(
        self,
        *,
        with_fake: bool = True,
        ttl_s: int | None = None,
        limits: RiskLimits | None = None,
        clock: Any = None,
    ) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        base = Settings.from_env(data_dir=self.data_dir)
        self.settings = dataclasses.replace(
            base, auth_mode="dev-open", api_token=None, auth_token_source="none"
        )
        self.fake = None
        self.service = None
        self.app = None
        self.clock = clock or time.time
        if with_fake:
            from fake_demo import FakeDemoExchange

            self.fake = FakeDemoExchange()
            resolved_limits = limits or (RiskLimits(approval_ttl_s=ttl_s) if ttl_s else None)
            self.service = make_service(
                self.settings, self.fake, limits=resolved_limits, clock=self.clock
            )
        else:
            from workbench_service.execution.service import build_execution_service

            self.service = build_execution_service(
                self.settings, use_env_credentials=False
            )
        self.app = make_app(self.settings, self.service)

    def client(self) -> TestClient:
        assert self.app is not None
        return TestClient(self.app)

    def close(self) -> None:
        if self.app is not None:
            try:
                self.app.state.services and self.app.state.services.ledger.close()
            except Exception:  # noqa: BLE001
                pass
        self._tmp.cleanup()


def make_service(
    settings: Settings,
    fake: Any,
    *,
    limits: RiskLimits | None = None,
    clock: Any = None,
):
    credentials = DemoCredentials(
        api_key=fake.api_key, api_secret=fake.api_secret, source="test"
    )
    client = DemoBinanceClient(
        settings.demo_base_url,
        credentials,
        transport=fake.transport,
        clock=time.time,
    )
    resolved_clock = clock or time.time
    return build_execution_service(
        settings,
        client=client,
        credentials=credentials,
        limits=limits,
        broker_options={
            "poll_interval_s": 0.0,
            "unknown_query_interval_s": 0.0,
            "clock": resolved_clock,
        },
        use_env_credentials=False,
        clock=resolved_clock,
    )


def make_app(settings: Settings, service: Any):
    app = create_app(settings)
    app.state.services = service
    return app


def plan(**overrides: Any) -> dict[str, Any]:
    payload = dict(PLAN)
    payload.update(overrides)
    return payload


def market(**overrides: Any) -> dict[str, Any]:
    payload = dict(MARKET)
    payload.update(overrides)
    return payload


def seed_evidence(
    service: Any,
    cycle_id: str,
    *,
    age_ms: int = 1000,
    schema_version: str = "md-evidence/1",
    tamper: str | None = None,
) -> dict[str, Any]:
    """写入一条服务端 md-evidence/1 快照（文件 + evidence_snapshots 引用）。

    仅测试使用；生产由 ``market/evidence.py`` 生成。``tamper``：
    ``"hash"`` 使文件 hash 自校验失败，``"db"`` 使 DB sha256 与文件不一致。
    """

    from workbench_service.market.evidence import canonical_hash

    data_dir = Path(service.settings.data_dir)
    now_ms = int(service._clock() * 1000)
    produced_at_ms = max(0, now_ms - max(0, int(age_ms)))
    payload: dict[str, Any] = {
        "snapshot_id": f"snap-{cycle_id}",
        "schema_version": schema_version,
        "cycle_id": cycle_id,
        "produced_at_ms": produced_at_ms,
        "provenance": {"snapshot_hash": "sha256:placeholder"},
    }
    digest = canonical_hash(payload)
    payload["provenance"]["snapshot_hash"] = "sha256:" + digest
    if tamper == "hash":
        payload["provenance"]["snapshot_hash"] = "sha256:" + "0" * 64
    day = datetime.fromtimestamp(produced_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    relative = f"evidence/{day}/{cycle_id}/evidence.json"
    target = data_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    db_sha = payload["provenance"]["snapshot_hash"]
    if tamper == "db":
        db_sha = "sha256:" + "1" * 64
    db_path = data_dir / "workbench-v1.sqlite"
    connection = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_snapshots (
                cycle_id       TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                produced_at_ms INTEGER NOT NULL,
                verdict        TEXT NOT NULL,
                score          REAL NOT NULL,
                path           TEXT NOT NULL,
                sha256         TEXT NOT NULL,
                bytes          INTEGER NOT NULL,
                prompt_tokens  INTEGER NOT NULL,
                created_at     TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT OR REPLACE INTO evidence_snapshots "
            "(cycle_id, schema_version, produced_at_ms, verdict, score, path, sha256, bytes, "
            "prompt_tokens, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle_id,
                schema_version,
                produced_at_ms,
                "GOOD",
                1.0,
                relative,
                db_sha,
                target.stat().st_size,
                0,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"path": relative, "produced_at_ms": produced_at_ms, "sha256": db_sha}


def evaluate(
    client: TestClient,
    *,
    cycle_id: str = "testcycle1",
    plan_payload: dict[str, Any] | None = None,
    market_payload: dict[str, Any] | None = None,
    evidence_age_ms: int = 5000,
    seed: bool = True,
    evidence_override_ms: int | None = None,
) -> Any:
    if seed:
        seed_evidence(
            client.app.state.services,
            cycle_id,
            age_ms=1000 if evidence_override_ms is None else evidence_override_ms,
        )
    return client.post(
        "/api/risk/evaluate",
        json={
            "cycle_id": cycle_id,
            "plan": plan_payload or plan(),
            "market": market_payload or market(),
            "evidence_age_ms": evidence_age_ms,
        },
    )


def approve(
    client: TestClient,
    *,
    cycle_id: str = "testcycle1",
    plan_payload: dict[str, Any] | None = None,
    market_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = evaluate(
        client,
        cycle_id=cycle_id,
        plan_payload=plan_payload,
        market_payload=market_payload,
    )
    assert response.status_code == 200, response.text
    return response.json()


def submit(
    client: TestClient,
    approval: dict[str, Any],
    *,
    plan_payload: dict[str, Any] | None = None,
    market_payload: dict[str, Any] | None = None,
) -> Any:
    body: dict[str, Any] = {"approval_token": approval["approval"]["token"]}
    if plan_payload is not None:
        body["plan"] = plan_payload
    if market_payload is not None:
        body["market"] = market_payload
    return client.post("/api/execution/order", json=body)


def d(value: Any) -> Decimal:
    return Decimal(str(value))
