"""从审计库导出去敏的 USD-M 事故回放夹具。

导出器只读取调用者明确列出的开/平仓 client_order_id。它不会复制 raw_json、
模型提示词、账户余额或交易所标识；价格和数量分别相对首笔入场价、总开仓量归一化。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _fetch_orders(connection: sqlite3.Connection, ids: list[str]) -> list[sqlite3.Row]:
    if not ids:
        raise ValueError("incident must name at least one order")
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""SELECT o.client_order_id,o.symbol,o.side,o.qty,o.price,o.status,o.created_at,
                   i.action,i.strategy_id
              FROM orders o
              LEFT JOIN trade_intents i ON i.client_order_id=o.client_order_id
             WHERE o.client_order_id IN ({placeholders})""",
        tuple(ids),
    ).fetchall()
    found = {str(row["client_order_id"]) for row in rows}
    missing = sorted(set(ids) - found)
    if missing:
        raise ValueError(f"orders not found: {len(missing)}")
    return sorted(rows, key=lambda row: str(row["created_at"]))


def export_incident_fixture(db_path: Path, incidents: list[dict[str, Any]]) -> dict[str, Any]:
    """导出白名单事故；manifest 中的原始订单 ID 不会进入返回值。"""
    connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    events: list[dict[str, Any]] = []
    instruments: dict[str, dict[str, float]] = {}
    provenance_rows: list[dict[str, Any]] = []
    try:
        for item in incidents:
            label = str(item.get("incident") or "").strip()
            open_ids = [str(value) for value in item.get("open_client_order_ids") or []]
            close_ids = [str(value) for value in item.get("close_client_order_ids") or []]
            if not label or not open_ids or not close_ids:
                raise ValueError("incident/open_client_order_ids/close_client_order_ids are required")
            rows = _fetch_orders(connection, open_ids + close_ids)
            by_id = {str(row["client_order_id"]): row for row in rows}
            ordered = [by_id[value] for value in open_ids + close_ids]
            if any(str(row["status"]).upper() != "FILLED" for row in ordered):
                raise ValueError(f"{label}: only FILLED orders can be exported")
            symbols = {str(row["symbol"]) for row in ordered}
            sides = {str(row["side"]).upper() for row in ordered}
            if len(symbols) != 1 or len(sides) != 1:
                raise ValueError(f"{label}: symbol and side must remain stable")
            symbol, side = symbols.pop(), sides.pop()
            opens = [by_id[value] for value in open_ids]
            total_open_qty = sum(float(row["qty"]) for row in opens)
            anchor_price = float(opens[0]["price"])
            if total_open_qty <= 0 or anchor_price <= 0:
                raise ValueError(f"{label}: invalid fill price or quantity")
            instruments[symbol] = {"tick_size": 0.000001, "step_size": 0.000001,
                                   "min_qty": 0.000001}
            evidence = dict(item.get("evidence") or {})
            evidence_time = _timestamp_ms(str(opens[0]["created_at"])) - 1
            events.append({"type": "decision_evidence", "variant": label, "incident": label,
                           "symbol": symbol, "timestamp_ms": evidence_time,
                           "source_at_ms": evidence_time, **evidence})
            for action, selected in (("open", opens),
                                     ("close", [by_id[value] for value in close_ids])):
                for row in selected:
                    created_at = _timestamp_ms(str(row["created_at"]))
                    events.append({
                        "type": "order", "variant": label, "incident": label,
                        "strategy_id": item.get("strategy_id") or row["strategy_id"] or "legacy:unknown",
                        "symbol": symbol, "side": side, "action": action,
                        "qty": round(float(row["qty"]) / total_open_qty, 6),
                        "reference_price": round(float(row["price"]) / anchor_price * 100.0, 6),
                        "order_type": "TAKER", "timestamp_ms": created_at,
                        "observed_at_ms": created_at, "declared_fill_ratio": 1.0,
                    })
                    provenance_rows.append({
                        "incident": label, "action": action, "symbol": symbol, "side": side,
                        "qty": float(row["qty"]), "price": float(row["price"]),
                        "created_at": str(row["created_at"]),
                    })
    finally:
        connection.close()
    events.sort(key=lambda event: int(event["timestamp_ms"]))
    for index in range(1, len(events)):
        if int(events[index]["timestamp_ms"]) <= int(events[index - 1]["timestamp_ms"]):
            events[index]["timestamp_ms"] = int(events[index - 1]["timestamp_ms"]) + 1
            events[index]["observed_at_ms"] = events[index]["timestamp_ms"]
    provenance_hash = hashlib.sha256(json.dumps(
        provenance_rows, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "fixture_id": "deidentified-real-audit-incidents-v1",
        "market": "BINANCE_USDM",
        "description": "由本地真实审计成交导出；价格/数量已归一化，未包含原始ID、提示词或账户字段。",
        "provenance": {"kind": "local_audit_allowlist", "selected_rows_sha256": provenance_hash},
        "instruments": instruments,
        "costs": {"maker_fee_bps": 2, "taker_fee_bps": 5, "slippage_bps": 4},
        "events": events,
    }


def write_fixture(path: Path, fixture: dict[str, Any]) -> None:
    # 固定 LF，确保同一夹具在 Windows/Linux 上具有相同内容哈希和 Git 差异。
    with Path(path).open("w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n")
