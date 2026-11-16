"""Transactional strategy versions and authorization generations.

Transactions use BEGIN IMMEDIATE across independent store instances. Authorization
validation is a snapshot: P1/P2 callers must recheck at their execution boundary.
Historical version retrieval never grants permission to manage arbitrary positions.
"""
from __future__ import annotations

import json
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path

from ..db import _connect, utc_now_iso
from .schema import ensure_schema
from .locking import execution_guard
from .validation import canonical, digest, draft_payload, fail, integer, text, validate


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
        row = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id=? AND version=?", (strategy_id, version)).fetchone()
        if row is None:
            fail("E_STRATEGY_VERSION_NOT_FOUND", "策略版本不存在", 404)
        data = dict(row)
        data.update(json.loads(data.pop("payload_json")))
        data["confirmed"] = True
        data["validation"] = validate(data)
        return data

    def _get(self, conn, strategy_id):
        data = self._row(conn, strategy_id)
        data["enabled"] = bool(data["enabled"])
        data["status"] = "enabled" if data["enabled"] else "mounted" if data["current_version"] else "draft"
        draft = conn.execute("SELECT * FROM strategy_drafts WHERE strategy_id=?", (strategy_id,)).fetchone()
        data["draft"] = None
        if draft:
            d = dict(draft)
            d.update(json.loads(d.pop("payload_json")))
            d["validation"] = validate(d)
            data["draft"] = d
        data["confirmed_version"] = self._version(conn, strategy_id, data["current_version"]) if data["current_version"] else None
        return data

    def _audit(self, conn, strategy_id, action, generation, details):
        now, event_id = utc_now_iso(), uuid.uuid4().hex
        conn.execute("INSERT INTO strategy_events VALUES (?,?,?,?,?,?)", (event_id, strategy_id, action, generation, canonical(details), now))
        # Reuse operation history when the ledger is initialized; isolated store
        # tests retain the same event in strategy_events without starting brokers.
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_history'").fetchone():
            conn.execute("INSERT INTO operation_history (request_id,action,target,status,started_at,finished_at,summary_json) VALUES (?,?,?,?,?,?,?)", (f"strategy:{event_id}", f"strategy_{action}", strategy_id, "succeeded", now, now, canonical({"generation": generation, **details})))

    def create(self, name, source_text=""):
        name = text(name, "name", 100)
        now, sid = utc_now_iso(), uuid.uuid4().hex
        with self.transaction(write=True) as conn:
            conn.execute("INSERT INTO strategies(strategy_id,name,created_at,updated_at) VALUES (?,?,?,?)", (sid, name, now, now))
            if source_text:
                payload = draft_payload({"source_text": source_text})
                conn.execute("INSERT INTO strategy_drafts VALUES (?,?,?,?,?)", (sid, 1, canonical(payload), digest(payload), now))
            self._audit(conn, sid, "create", 0, {"name": name})
            return self._get(conn, sid)

    def get(self, strategy_id):
        with self.transaction() as conn:
            return self._get(conn, strategy_id)

    def list(self, *, authorized=False):
        with self.transaction() as conn:
            query = "SELECT strategy_id FROM strategies" + (" WHERE enabled=1" if authorized else "") + " ORDER BY created_at,strategy_id"
            return [self._get(conn, row[0]) for row in conn.execute(query).fetchall()]

    def rename(self, strategy_id, name):
        name = text(name, "name", 100)
        with self.transaction(write=True) as conn:
            row = self._row(conn, strategy_id)
            conn.execute("UPDATE strategies SET name=?,updated_at=? WHERE strategy_id=?", (name, utc_now_iso(), strategy_id))
            self._audit(conn, strategy_id, "rename", row["generation"], {"name": name})
            return self._get(conn, strategy_id)

    def save_draft(self, strategy_id, body):
        revision = integer(body.get("revision"), "revision")
        payload = draft_payload(body)
        with self.transaction(write=True) as conn:
            self._row(conn, strategy_id)
            old = conn.execute("SELECT revision FROM strategy_drafts WHERE strategy_id=?", (strategy_id,)).fetchone()
            if revision != (old[0] if old else 0):
                fail("E_STRATEGY_REVISION_CONFLICT", "草稿已更新，请重新读取", 409)
            conn.execute("INSERT INTO strategy_drafts VALUES (?,?,?,?,?) ON CONFLICT(strategy_id) DO UPDATE SET revision=excluded.revision,payload_json=excluded.payload_json,content_hash=excluded.content_hash,updated_at=excluded.updated_at", (strategy_id, revision + 1, canonical(payload), digest(payload), utc_now_iso()))
            return self._get(conn, strategy_id)

    def mount(self, strategy_id, body):
        revision = integer(body.get("revision"), "revision", 1)
        content_hash = text(body.get("content_hash"), "content_hash", 64)
        request_id = text(body.get("request_id"), "request_id", 128)
        if body.get("confirmed") is not True:
            fail("E_STRATEGY_CONFIRMATION_REQUIRED", "必须明确确认策略内容")
        with self.transaction(write=True) as conn:
            row = self._row(conn, strategy_id)
            previous = conn.execute("SELECT * FROM strategy_mount_requests WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                if (previous["strategy_id"], previous["revision"], previous["content_hash"]) != (strategy_id, revision, content_hash):
                    fail("E_STRATEGY_IDEMPOTENCY_CONFLICT", "request_id 已用于其他确认内容", 409)
                return {"strategy": self._get(conn, strategy_id), "version": self._version(conn, strategy_id, previous["version"]), "replayed": True}
            draft = conn.execute("SELECT * FROM strategy_drafts WHERE strategy_id=?", (strategy_id,)).fetchone()
            if not draft or draft["revision"] != revision or draft["content_hash"] != content_hash:
                fail("E_STRATEGY_REVISION_CONFLICT", "确认内容与当前草稿不一致", 409)
            payload = json.loads(draft["payload_json"])
            validation = validate(payload)
            if not validation["valid"]:
                fail("E_STRATEGY_INCOMPLETE", "；".join(validation["issues"]))
            version = (row["current_version"] or 0) + 1
            now = utc_now_iso()
            conn.execute("INSERT INTO strategy_versions VALUES (?,?,?,?,?)", (strategy_id, version, draft["payload_json"], content_hash, now))
            # A newly unsupported version must never inherit execution authority.
            enabled = row["enabled"] and validation["execution_ready"]
            if enabled:
                self._check_symbol_budget(conn, strategy_id, payload["rules"])
            conn.execute("UPDATE strategies SET current_version=?,generation=generation+1,enabled=?,updated_at=? WHERE strategy_id=?", (version, int(enabled), now, strategy_id))
            conn.execute("INSERT INTO strategy_mount_requests VALUES (?,?,?,?,?)", (request_id, strategy_id, revision, content_hash, version))
            self._audit(conn, strategy_id, "mount", row["generation"] + 1, {"version": version, "enabled": bool(enabled)})
            return {"strategy": self._get(conn, strategy_id), "version": self._version(conn, strategy_id, version), "replayed": False}

    def _check_symbol_budget(self, conn, strategy_id, rules):
        symbols = set(rules["symbols"])
        for row in conn.execute("SELECT strategy_id,current_version FROM strategies WHERE enabled=1 AND strategy_id!=?", (strategy_id,)):
            symbols.update(self._version(conn, row["strategy_id"], row["current_version"])["rules"]["symbols"])
        if len(symbols) > 10:
            fail("E_STRATEGY_SYMBOL_BUDGET", "已启用策略合计最多监测 10 个不同标的", 409)

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
                count = conn.execute("SELECT COUNT(*) FROM strategies WHERE enabled=1 AND strategy_id!=?", (strategy_id,)).fetchone()[0]
                if count >= 5:
                    fail("E_STRATEGY_ENABLED_LIMIT", "最多启用 5 个策略", 409)
                self._check_symbol_budget(conn, strategy_id, version["rules"])
            if bool(row["enabled"]) != enabled:
                conn.execute("UPDATE strategies SET enabled=?,generation=generation+1,updated_at=? WHERE strategy_id=?", (int(enabled), utc_now_iso(), strategy_id))
                self._audit(conn, strategy_id, "enable" if enabled else "disable", generation + 1, {})
            return self._get(conn, strategy_id)

    def version(self, strategy_id, version):
        integer(version, "version", 1)
        with self.transaction() as conn:
            return self._version(conn, strategy_id, version)

    def config(self):
        with self.transaction() as conn:
            return dict(conn.execute("SELECT mode,revision,updated_at FROM strategy_config WHERE id=1").fetchone())

    def set_config(self, mode, revision):
        if mode not in ("native", "mounted"):
            fail("E_STRATEGY_VALIDATION", "mode 必须为 native 或 mounted")
        integer(revision, "revision")
        with self.transaction(write=True) as conn:
            current = conn.execute("SELECT * FROM strategy_config WHERE id=1").fetchone()
            if current["revision"] != revision:
                fail("E_STRATEGY_REVISION_CONFLICT", "运行模式已变化", 409)
            if current["mode"] != mode:
                conn.execute("UPDATE strategy_config SET mode=?,revision=revision+1,updated_at=? WHERE id=1", (mode, utc_now_iso()))
                conn.execute("UPDATE strategies SET generation=generation+1")
                self._audit(conn, "config", "mode", revision + 1, {"mode": mode})
            return dict(conn.execute("SELECT mode,revision,updated_at FROM strategy_config WHERE id=1").fetchone())

    def validate_authorization(self, strategy_id, version, generation, *, purpose="entry", position_strategy_id=None, position_strategy_version=None):
        integer(version, "version", 1)
        integer(generation, "generation")
        with self.transaction() as conn:
            row = self._row(conn, strategy_id)
            confirmed = self._version(conn, strategy_id, version)
            if purpose == "manage":
                if (position_strategy_id, position_strategy_version) != (strategy_id, version):
                    fail("E_STRATEGY_POSITION_OWNERSHIP", "策略与仓位所属版本不一致", 409)
            elif purpose == "entry":
                if not row["enabled"] or row["generation"] != generation or row["current_version"] != version:
                    fail("E_STRATEGY_NOT_AUTHORIZED", "策略已停用或版本授权已失效", 409)
                if not confirmed["validation"]["execution_ready"]:
                    fail("E_STRATEGY_UNSUPPORTED", "策略规则尚不支持执行")
            else:
                fail("E_STRATEGY_VALIDATION", "purpose 必须为 entry 或 manage")
            return {"authorized": True, "strategy_id": strategy_id, "version": version, "generation": row["generation"], "purpose": purpose, "rules": confirmed["rules"], "content_hash": confirmed["content_hash"]}
