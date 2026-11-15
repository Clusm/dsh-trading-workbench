"""Cross-process execution lease (A01/A02).

The lease is persisted in the Service's SQLite ``meta`` table, not in Host
memory. A Host must acquire it before P1 and present the same lease id to P2.
Control changes and watchdog revocation bump the generation, so an old lease
cannot execute even after a Host restart or after another Host acquires a new
round.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ..ledger.store import Ledger

LEASE_KEY = "execution_lease"
GENERATION_KEY = "execution_generation"


class LeaseError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class ExecutionLeaseStore:
    ledger: Ledger
    clock: Callable[[], float] = time.time

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def generation(self) -> int:
        raw = self.ledger.get_meta(GENERATION_KEY)
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def _read(self) -> dict[str, Any] | None:
        raw = self.ledger.get_meta(LEASE_KEY)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _write(self, lease: dict[str, Any]) -> str:
        return json.dumps(lease, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _active(self, lease: dict[str, Any] | None, generation: int) -> bool:
        return bool(
            lease
            and not lease.get("revoked")
            and int(lease.get("generation", -1)) == generation
            and int(lease.get("expires_at_ms", 0)) > self._now_ms()
        )

    def acquire(
        self,
        run_id: str,
        *,
        owner: str = "host",
        ttl_s: float = 600.0,
    ) -> dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id or len(run_id) > 160:
            raise LeaseError("E_LEASE_RUN_ID", "run_id is required", status=422)
        ttl_ms = int(max(10.0, min(float(ttl_s), 3600.0)) * 1000)
        for _ in range(3):
            generation = self.generation()
            raw = self.ledger.get_meta(LEASE_KEY)
            current = self._read()
            if self._active(current, generation):
                raise LeaseError(
                    "E_LEASE_HELD",
                    "another execution lease is active",
                    details={"run_id": current.get("run_id"), "expires_at_ms": current.get("expires_at_ms")},
                )
            now = self._now_ms()
            lease = {
                "lease_id": uuid.uuid4().hex,
                "run_id": run_id,
                "owner": str(owner or "host")[:80],
                "generation": generation,
                "acquired_at_ms": now,
                "expires_at_ms": now + ttl_ms,
                "revoked": False,
                "revoked_reason": None,
            }
            encoded = self._write(lease)
            if raw is None:
                ok = self.ledger.insert_meta_if_absent(LEASE_KEY, encoded)
            else:
                ok = self.ledger.compare_and_set_meta(LEASE_KEY, raw, encoded)
            if ok:
                return lease
        raise LeaseError("E_LEASE_CONTENDED", "execution lease could not be acquired; retry")

    def renew(self, lease_id: str, *, ttl_s: float = 600.0) -> dict[str, Any]:
        ttl_ms = int(max(10.0, min(float(ttl_s), 3600.0)) * 1000)
        for _ in range(3):
            raw = self.ledger.get_meta(LEASE_KEY)
            lease = self._read()
            if not lease or str(lease.get("lease_id")) != str(lease_id):
                raise LeaseError("E_LEASE_INVALID", "execution lease is not current")
            if lease.get("revoked"):
                raise LeaseError("E_LEASE_REVOKED", "execution lease was revoked")
            if int(lease.get("generation", -1)) != self.generation():
                raise LeaseError("E_LEASE_STALE", "execution lease belongs to an older generation")
            if int(lease.get("expires_at_ms", 0)) <= self._now_ms():
                raise LeaseError("E_LEASE_EXPIRED", "execution lease expired")
            updated = dict(lease)
            updated["expires_at_ms"] = self._now_ms() + ttl_ms
            if raw is not None and self.ledger.compare_and_set_meta(LEASE_KEY, raw, self._write(updated)):
                return updated
        raise LeaseError("E_LEASE_CONTENDED", "execution lease could not be renewed; retry")

    def validate(self, lease_id: str | None) -> dict[str, Any]:
        lease = self._read()
        if not lease or not lease_id or str(lease.get("lease_id")) != str(lease_id):
            raise LeaseError("E_LEASE_REQUIRED", "an active execution lease is required")
        if lease.get("revoked"):
            raise LeaseError("E_LEASE_REVOKED", "execution lease was revoked")
        if int(lease.get("generation", -1)) != self.generation():
            raise LeaseError("E_LEASE_STALE", "execution lease belongs to an older generation")
        if int(lease.get("expires_at_ms", 0)) <= self._now_ms():
            raise LeaseError("E_LEASE_EXPIRED", "execution lease expired")
        return lease

    def revoke(self, lease_id: str | None, reason: str = "released") -> bool:
        raw = self.ledger.get_meta(LEASE_KEY)
        lease = self._read()
        if not lease or not lease_id or str(lease.get("lease_id")) != str(lease_id):
            return False
        updated = dict(lease)
        updated["revoked"] = True
        updated["revoked_reason"] = str(reason)[:160]
        return raw is not None and self.ledger.compare_and_set_meta(LEASE_KEY, raw, self._write(updated))

    def revoke_run(self, run_id: str, reason: str = "stale_run") -> bool:
        """Revoke the current lease only if it belongs to ``run_id``."""

        raw = self.ledger.get_meta(LEASE_KEY)
        lease = self._read()
        if not lease or str(lease.get("run_id")) != str(run_id):
            return False
        updated = dict(lease)
        updated["revoked"] = True
        updated["revoked_reason"] = str(reason)[:160]
        if raw is not None and self.ledger.compare_and_set_meta(LEASE_KEY, raw, self._write(updated)):
            self.ledger.bump_meta_int(GENERATION_KEY)
            return True
        return False

    def revoke_all(self, reason: str) -> int:
        """Invalidate every current lease and advance the generation."""

        generation = self.ledger.bump_meta_int(GENERATION_KEY)
        raw = self.ledger.get_meta(LEASE_KEY)
        lease = self._read()
        if lease and raw is not None:
            updated = dict(lease)
            updated["revoked"] = True
            updated["revoked_reason"] = str(reason)[:160]
            self.ledger.compare_and_set_meta(LEASE_KEY, raw, self._write(updated))
        return generation
