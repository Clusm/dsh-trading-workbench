"""Transactional strategy versions, circuit breakers, decisions, and generations.

Transactions use BEGIN IMMEDIATE across independent store instances. Authorization
validation is a snapshot: P1/P2 callers must recheck at their execution boundary.
Historical version retrieval never grants permission to manage arbitrary positions.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..db import _connect, utc_now_iso
from ..ledger.numbers import dec_text, to_decimal
from . import scan as rule_scan
from .attribution import _closed_trades_by_strategy, compute_strategy_attribution
from .evaluator import describe_rules
from .locking import execution_guard
from .schema import ensure_schema
from .validation import EXECUTION_MODES, canonical, digest, draft_payload, fail, integer, text, validate


def _precise_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _opt_price_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    dec = to_decimal(value)
    if dec is None or dec <= 0:
        return None
    return dec_text(dec)


class StrategyStore:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        ensure_schema(self.path)

    @contextmanager
    def transaction(self, *, write=False):
        with execution_guard(self.path) if write else nullcontext():
            with self._transaction(write=write) as conn:
                yield conn

    @contextmanager
    def _transaction(self, *, write=False):
        conn = _connect(self.path)
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _row(self, conn, strategy_id):
        row = conn.execute("SELECT * FROM strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
        if row is None:
            fail("E_STRATEGY_NOT_FOUND", "策略不存在", 404)
        return dict(row)

    def _version(self, conn, strategy_id, version):
        row = conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_id=? AND version=?",
            (strategy_id, version),
        ).fetchone()
        if row is None:
            fail("E_STRATEGY_VERSION_NOT_FOUND", "策略版本不存在", 404)
        data = dict(row)
        data.update(json.loads(data.pop("payload_json")))
        data["confirmed"] = True
        data["validation"] = validate(data)
        return data

    def _get(self, conn, strategy_id, *, preloaded_trades=None):
        data = self._row(conn, strategy_id)
        data["enabled"] = bool(data["enabled"])
        data["status"] = (
            "enabled" if data["enabled"] else "mounted" if data["current_version"] else "draft"
        )
        draft = conn.execute(
            "SELECT * FROM strategy_drafts WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
        data["draft"] = None
        if draft:
            d = dict(draft)
            d.update(json.loads(d.pop("payload_json")))
            d["validation"] = validate(d)
            data["draft"] = d
        data["confirmed_version"] = (
            self._version(conn, strategy_id, data["current_version"])
            if data["current_version"]
            else None
        )
        confirmed_rules = (
            data["confirmed_version"]["rules"] if data["confirmed_version"] else {}
        )
        draft_rules = data["draft"]["rules"] if data["draft"] else {}
        active_rules = confirmed_rules or draft_rules
        data["execution_mode"] = (
            active_rules.get("execution_mode", "ai_assisted")
            if isinstance(active_rules, dict)
            else "ai_assisted"
        )
        data["rule_summary"] = describe_rules(active_rules)
        data["circuit"] = {
            "state": data.get("circuit_state") or "ok",
            "reason": data.get("circuit_reason"),
            "tripped_at": data.get("circuit_tripped_at"),
            "reset_at": data.get("circuit_reset_at"),
            "config": (
                confirmed_rules.get("circuit_breaker")
                if isinstance(confirmed_rules.get("circuit_breaker"), dict)
                else {}
            ),
        }
        data["stats"] = compute_strategy_attribution(
            conn, strategy_id, strategy_row=data, preloaded_trades=preloaded_trades
        )
        return data

    def _audit(self, conn, strategy_id, action, generation, details):
        now, event_id = utc_now_iso(), uuid.uuid4().hex
        conn.execute(
            "INSERT INTO strategy_events VALUES (?,?,?,?,?,?)",
            (event_id, strategy_id, action, generation, canonical(details), now),
        )
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_history'"
        ).fetchone():
            conn.execute(
                "INSERT INTO operation_history (request_id,action,target,status,started_at,finished_at,summary_json) VALUES (?,?,?,?,?,?,?)",
                (
                    f"strategy:{event_id}",
                    f"strategy_{action}",
                    strategy_id,
                    "succeeded",
                    now,
                    now,
                    canonical({"generation": generation, **details}),
                ),
            )

    def create(self, name, source_text="", *, rules=None):
        name = text(name, "name", 100)
        now, sid = utc_now_iso(), uuid.uuid4().hex
        with self.transaction(write=True) as conn:
            conn.execute(
                "INSERT INTO strategies(strategy_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                (sid, name, now, now),
            )
            if source_text:
                payload = draft_payload({"source_text": source_text, "rules": rules or {}})
                conn.execute(
                    "INSERT INTO strategy_drafts VALUES (?,?,?,?,?)",
                    (sid, 1, canonical(payload), digest(payload), now),
                )
            self._audit(conn, sid, "create", 0, {"name": name})
            return self._get(conn, sid)

    def get(self, strategy_id):
        with self.transaction() as conn:
            return self._get(conn, strategy_id)

    def list(self, *, authorized=False):
        with self.transaction() as conn:
            trades_map = _closed_trades_by_strategy(conn)
            query = (
                "SELECT strategy_id FROM strategies"
                + (" WHERE enabled=1" if authorized else "")
                + " ORDER BY created_at,strategy_id"
            )
            return [
                self._get(conn, row[0], preloaded_trades=trades_map)
                for row in conn.execute(query).fetchall()
            ]

    def authorized_rules(self) -> list[dict[str, Any]]:
        """已启用策略的确认版本规则（不含归因统计）。

        v3.1 P2 的 ``/api/rules/scan`` 每 15s 调一次；``_get`` 里的归因统计是界面
        查询的负担，扫描刻意跳过它，只读策略行 + 确认版本（含 ``validation``）。
        """

        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT strategy_id,name,current_version,generation,created_at "
                "FROM strategies WHERE enabled=1 ORDER BY created_at,strategy_id"
            ).fetchall()
            result: list[dict[str, Any]] = []
            for raw in rows:
                row = dict(raw)
                confirmed = (
                    self._version(conn, row["strategy_id"], row["current_version"])
                    if row["current_version"]
                    else None
                )
                result.append(
                    {
                        "strategy_id": row["strategy_id"],
                        "name": row["name"],
                        "current_version": row["current_version"],
                        "generation": int(row["generation"]),
                        "created_at": row["created_at"],
                        "confirmed_version": confirmed,
                    }
                )
            return result

    def rename(self, strategy_id, name):
        name = text(name, "name", 100)
        with self.transaction(write=True) as conn:
            row = self._row(conn, strategy_id)
            conn.execute(
                "UPDATE strategies SET name=?,updated_at=? WHERE strategy_id=?",
                (name, utc_now_iso(), strategy_id),
            )
            self._audit(conn, strategy_id, "rename", row["generation"], {"name": name})
            return self._get(conn, strategy_id)

    def save_draft(self, strategy_id, body):
        revision = integer(body.get("revision"), "revision")
        payload = draft_payload(body)
        with self.transaction(write=True) as conn:
            self._row(conn, strategy_id)
            old = conn.execute(
                "SELECT revision FROM strategy_drafts WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
            if revision != (old[0] if old else 0):
                fail("E_STRATEGY_REVISION_CONFLICT", "草稿已更新，请重新读取", 409)
            conn.execute(
                "INSERT INTO strategy_drafts VALUES (?,?,?,?,?) "
                "ON CONFLICT(strategy_id) DO UPDATE SET "
                "revision=excluded.revision,payload_json=excluded.payload_json,"
                "content_hash=excluded.content_hash,updated_at=excluded.updated_at",
                (strategy_id, revision + 1, canonical(payload), digest(payload), utc_now_iso()),
            )
            return self._get(conn, strategy_id)

    def _within_symbol_budget(self, conn, strategy_id, rules) -> bool:
        symbols = set(rules["symbols"])
        for row in conn.execute(
            "SELECT strategy_id,current_version FROM strategies WHERE enabled=1 AND strategy_id!=?",
            (strategy_id,),
        ):
            symbols.update(
                self._version(conn, row["strategy_id"], row["current_version"])["rules"]["symbols"]
            )
        return len(symbols) <= 10

    def _check_symbol_budget(self, conn, strategy_id, rules):
        if not self._within_symbol_budget(conn, strategy_id, rules):
            fail("E_STRATEGY_SYMBOL_BUDGET", "已启用策略合计最多监测 10 个不同标的", 409)

    def mount(self, strategy_id, body):
        revision = integer(body.get("revision"), "revision", 1)
        content_hash = text(body.get("content_hash"), "content_hash", 64)
        request_id = text(body.get("request_id"), "request_id", 128)
        if body.get("confirmed") is not True:
            fail("E_STRATEGY_CONFIRMATION_REQUIRED", "必须明确确认策略内容")
        with self.transaction(write=True) as conn:
            row = self._row(conn, strategy_id)
            previous = conn.execute(
                "SELECT * FROM strategy_mount_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if previous:
                if (previous["strategy_id"], previous["revision"], previous["content_hash"]) != (
                    strategy_id,
                    revision,
                    content_hash,
                ):
                    fail("E_STRATEGY_IDEMPOTENCY_CONFLICT", "request_id 已用于其他确认内容", 409)
                return {
                    "strategy": self._get(conn, strategy_id),
                    "version": self._version(conn, strategy_id, previous["version"]),
                    "replayed": True,
                    "budget_downgraded": False,
                }
            draft = conn.execute(
                "SELECT * FROM strategy_drafts WHERE strategy_id=?", (strategy_id,)
            ).fetchone()
            if not draft or draft["revision"] != revision or draft["content_hash"] != content_hash:
                fail("E_STRATEGY_REVISION_CONFLICT", "确认内容与当前草稿不一致", 409)
            payload = json.loads(draft["payload_json"])
            validation = validate(payload)
            if not validation["valid"]:
                fail("E_STRATEGY_INCOMPLETE", "；".join(validation["issues"]))
            version = (row["current_version"] or 0) + 1
            now = utc_now_iso()
            conn.execute(
                "INSERT INTO strategy_versions VALUES (?,?,?,?,?)",
                (strategy_id, version, draft["payload_json"], content_hash, now),
            )
            # P2-12: If the strategy was enabled and the new version exceeds the 10-symbol
            # monitoring budget, mount cleanly while downgrading enabled=False.
            enabled = bool(row["enabled"] and validation["execution_ready"])
            budget_downgraded = False
            if enabled and not self._within_symbol_budget(conn, strategy_id, payload["rules"]):
                enabled = False
                budget_downgraded = True
            conn.execute(
                "UPDATE strategies SET current_version=?,generation=generation+1,enabled=?,updated_at=? WHERE strategy_id=?",
                (version, int(enabled), now, strategy_id),
            )
            conn.execute(
                "INSERT INTO strategy_mount_requests VALUES (?,?,?,?,?)",
                (request_id, strategy_id, revision, content_hash, version),
            )
            audit_details: dict[str, Any] = {"version": version, "enabled": bool(enabled)}
            if budget_downgraded:
                audit_details["budget_downgraded"] = True
            self._audit(conn, strategy_id, "mount", row["generation"] + 1, audit_details)
            return {
                "strategy": self._get(conn, strategy_id),
                "version": self._version(conn, strategy_id, version),
                "replayed": False,
                "budget_downgraded": budget_downgraded,
            }

    def authorize(self, strategy_id, *, enabled, generation):
        if not isinstance(enabled, bool):
            fail("E_STRATEGY_VALIDATION", "enabled 必须为布尔值")
        generation = integer(generation, "generation")
        with self.transaction(write=True) as conn:
            row = self._row(conn, strategy_id)
            if row["generation"] != generation:
                fail("E_STRATEGY_GENERATION_CONFLICT", "授权已变化，请重新读取", 409)
            if enabled:
                if row["current_version"] is None:
                    fail("E_STRATEGY_NOT_MOUNTED", "请先确认挂载策略", 409)
                version = self._version(conn, strategy_id, row["current_version"])
                if not version["validation"]["execution_ready"]:
                    fail("E_STRATEGY_UNSUPPORTED", "；".join(version["validation"]["unsupported"]))
                count = conn.execute(
                    "SELECT COUNT(*) FROM strategies WHERE enabled=1 AND strategy_id!=?",
                    (strategy_id,),
                ).fetchone()[0]
                if count >= 5:
                    fail("E_STRATEGY_ENABLED_LIMIT", "最多启用 5 个策略", 409)
                self._check_symbol_budget(conn, strategy_id, version["rules"])
            if bool(row["enabled"]) != enabled:
                now = utc_now_iso()
                if enabled:
                    reset_at = _precise_now_iso()
                    conn.execute(
                        "UPDATE strategies SET enabled=1,generation=generation+1,"
                        "circuit_state='ok',circuit_reason=NULL,circuit_tripped_at=NULL,"
                        "circuit_reset_at=?,updated_at=? WHERE strategy_id=?",
                        (reset_at, now, strategy_id),
                    )
                    self._audit(conn, strategy_id, "enable", generation + 1, {"circuit_reset_at": reset_at})
                else:
                    conn.execute(
                        "UPDATE strategies SET enabled=0,generation=generation+1,updated_at=? WHERE strategy_id=?",
                        (now, strategy_id),
                    )
                    self._audit(conn, strategy_id, "disable", generation + 1, {})
            return self._get(conn, strategy_id)

    def trip_circuit(self, strategy_id: str, reason: str, details: dict[str, Any] | None = None):
        reason_text = text(reason, "reason", 500)
        with self.transaction(write=True) as conn:
            row = self._row(conn, strategy_id)
            if int(row["enabled"]) == 1:
                tripped_at = _precise_now_iso()
                conn.execute(
                    "UPDATE strategies SET enabled=0,generation=generation+1,"
                    "circuit_state='tripped',circuit_reason=?,circuit_tripped_at=?,updated_at=? "
                    "WHERE strategy_id=?",
                    (reason_text, tripped_at, utc_now_iso(), strategy_id),
                )
                self._audit(
                    conn,
                    strategy_id,
                    "circuit_trip",
                    int(row["generation"]) + 1,
                    {"reason": reason_text, **(details or {})},
                )
            return self._get(conn, strategy_id)

    def is_signal_consumed(
        self,
        conn,
        strategy_id: str,
        version: int,
        symbol: str,
        side: str,
        signal_key: str,
    ) -> bool:
        row = conn.execute(
            "SELECT 1 FROM strategy_signals "
            "WHERE strategy_id=? AND version=? AND symbol=? AND UPPER(side)=? AND signal_key=? "
            "AND LOWER(state) != 'rejected'",
            (
                str(strategy_id),
                int(version),
                str(symbol).upper(),
                str(side).upper(),
                str(signal_key),
            ),
        ).fetchone()
        return row is not None

    def _parse_decision_row(self, row: Any, *, replayed: bool) -> dict[str, Any]:
        item = dict(row)
        item["details"] = json.loads(item.pop("details_json") or "{}")
        item["replayed"] = replayed
        return item

    def _record_decision_in_tx(self, conn, strategy_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._row(conn, strategy_id)
        cycle_id = text(body.get("cycle_id"), "cycle_id", 128)
        existing = conn.execute(
            "SELECT * FROM strategy_decisions WHERE strategy_id=? AND cycle_id=?",
            (strategy_id, cycle_id),
        ).fetchone()
        if existing is not None:
            return self._parse_decision_row(existing, replayed=True)

        version_num = integer(body.get("version"), "version", 1)
        symbol = text(body.get("symbol"), "symbol", 20).upper()
        side = text(body.get("side"), "side", 16).upper()
        if side not in ("LONG", "SHORT"):
            fail("E_STRATEGY_VALIDATION", "side 必须为 LONG/SHORT 或 long/short")
        signal_key = text(body.get("signal_key"), "signal_key", 128)
        execution_mode = str(body.get("execution_mode") or "ai_assisted").strip()
        if execution_mode not in EXECUTION_MODES:
            fail("E_STRATEGY_VALIDATION", "execution_mode 必须为 ai_assisted 或 rule_direct")
        outcome = text(body.get("outcome"), "outcome", 64)
        reason_code = (
            str(body["reason_code"]).strip()[:64]
            if body.get("reason_code") not in (None, "")
            else None
        )
        entry_mark = _opt_price_text(body.get("entry_mark"))
        sl_price = _opt_price_text(body.get("sl_price"))
        tp_price = _opt_price_text(body.get("tp_price"))

        if entry_mark is not None and (sl_price is None or tp_price is None):
            v_row = conn.execute(
                "SELECT payload_json FROM strategy_versions WHERE strategy_id=? AND version=?",
                (strategy_id, version_num),
            ).fetchone()
            if v_row:
                rules = json.loads(v_row["payload_json"]).get("rules") or {}
                exit_cfg = rules.get("exit") if isinstance(rules.get("exit"), dict) else {}
                mark_dec = Decimal(entry_mark)
                direction = Decimal(1) if side == "LONG" else Decimal(-1)
                if sl_price is None and exit_cfg.get("stop_loss_pct") is not None:
                    sl_price = dec_text(
                        mark_dec * (Decimal(1) - direction * Decimal(str(exit_cfg["stop_loss_pct"])))
                    )
                if tp_price is None and exit_cfg.get("take_profit_pct") is not None:
                    tp_price = dec_text(
                        mark_dec * (Decimal(1) + direction * Decimal(str(exit_cfg["take_profit_pct"])))
                    )

        details = body.get("details")
        if details is None:
            details = {}
        if not isinstance(details, dict):
            fail("E_STRATEGY_VALIDATION", "details 必须为对象")

        decision_id = uuid.uuid4().hex
        now = _precise_now_iso()
        conn.execute(
            "INSERT INTO strategy_decisions "
            "(decision_id,strategy_id,version,cycle_id,symbol,side,signal_key,execution_mode,"
            "outcome,reason_code,entry_mark,sl_price,tp_price,details_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                strategy_id,
                version_num,
                cycle_id,
                symbol,
                side,
                signal_key,
                execution_mode,
                outcome,
                reason_code,
                entry_mark,
                sl_price,
                tp_price,
                canonical(details),
                now,
            ),
        )
        inserted = conn.execute(
            "SELECT * FROM strategy_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        return self._parse_decision_row(inserted, replayed=False)

    def record_decision(self, strategy_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            fail("E_STRATEGY_VALIDATION", "请求体必须为对象")
        with self.transaction(write=True) as conn:
            return self._record_decision_in_tx(conn, strategy_id, body)

    def apply_rule_dispatch(
        self,
        rule_key: str,
        symbol: str,
        outcome: str,
        *,
        at_ms: int,
        scan_interval_ms: int | None = None,
    ) -> dict[str, Any]:
        """回写 Host 的派发结果（``POST /api/rules/dispatched``），幂等。

        只写 ``rule_eval_state``：``dispatched`` 之后必须观察到一次"不满足"才能
        重新武装；``rejected`` 额外冷却 1 个扫描间隔。冷却用 max() 合并，所以
        同一个 at_ms 重复回写不会叠加。
        """

        parsed = rule_scan.split_rule_key(str(rule_key or "").strip())
        if parsed is None:
            fail("E_RULE_KEY_INVALID", "rule_key 必须为 strategy:<strategy_id>:<version>", 422)
        strategy_id, version = parsed
        if outcome not in rule_scan.DISPATCH_OUTCOMES:
            fail("E_RULE_OUTCOME_INVALID", "outcome 必须为 dispatched 或 rejected", 422)
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            fail("E_STRATEGY_VALIDATION", "symbol 不能为空")
        with self.transaction(write=True) as conn:
            # 未知策略直接 404：不让一个拼错的 rule_key 在状态表里留下垃圾行。
            self._row(conn, strategy_id)
            state, idempotent = rule_scan.apply_dispatch(
                conn,
                rule_key=rule_scan.rule_key_for(strategy_id, version),
                symbol=symbol,
                outcome=outcome,
                at_ms=int(at_ms),
                scan_interval_ms=scan_interval_ms,
            )
        return {
            "state": state.view(),
            "idempotent": idempotent,
            "outcome": outcome,
            "at_ms": int(at_ms),
            "scan_interval_ms": rule_scan.clamp_interval_ms(scan_interval_ms),
        }

    def version(self, strategy_id, version):
        integer(version, "version", 1)
        with self.transaction() as conn:
            return self._version(conn, strategy_id, version)

    def config(self):
        with self.transaction() as conn:
            return dict(
                conn.execute("SELECT mode,revision,updated_at FROM strategy_config WHERE id=1").fetchone()
            )

    def set_config(self, mode, revision):
        if mode not in ("native", "mounted"):
            fail("E_STRATEGY_VALIDATION", "mode 必须为 native 或 mounted")
        integer(revision, "revision")
        with self.transaction(write=True) as conn:
            current = conn.execute("SELECT * FROM strategy_config WHERE id=1").fetchone()
            if current["revision"] != revision:
                fail("E_STRATEGY_REVISION_CONFLICT", "运行模式已变化", 409)
            if current["mode"] != mode:
                conn.execute(
                    "UPDATE strategy_config SET mode=?,revision=revision+1,updated_at=? WHERE id=1",
                    (mode, utc_now_iso()),
                )
                conn.execute("UPDATE strategies SET generation=generation+1")
                self._audit(conn, "config", "mode", revision + 1, {"mode": mode})
            return dict(
                conn.execute("SELECT mode,revision,updated_at FROM strategy_config WHERE id=1").fetchone()
            )

    def validate_authorization(
        self,
        strategy_id,
        version,
        generation,
        *,
        purpose="entry",
        position_strategy_id=None,
        position_strategy_version=None,
    ):
        integer(version, "version", 1)
        integer(generation, "generation")
        with self.transaction() as conn:
            row = self._row(conn, strategy_id)
            confirmed = self._version(conn, strategy_id, version)
            if purpose == "manage":
                if (position_strategy_id, position_strategy_version) != (strategy_id, version):
                    fail("E_STRATEGY_POSITION_OWNERSHIP", "策略与仓位所属版本不一致", 409)
            elif purpose == "entry":
                if (
                    not row["enabled"]
                    or row["generation"] != generation
                    or row["current_version"] != version
                ):
                    fail("E_STRATEGY_NOT_AUTHORIZED", "策略已停用或版本授权已失效", 409)
                if not confirmed["validation"]["execution_ready"]:
                    fail("E_STRATEGY_UNSUPPORTED", "策略规则尚不支持执行")
            else:
                fail("E_STRATEGY_VALIDATION", "purpose 必须为 entry 或 manage")
            return {
                "authorized": True,
                "strategy_id": strategy_id,
                "version": version,
                "generation": row["generation"],
                "purpose": purpose,
                "rules": confirmed["rules"],
                "content_hash": confirmed["content_hash"],
            }
