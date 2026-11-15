"""Read-only analysis of max-tokens failures in the trading workbench.

Prints aggregate counts only; never prints prompts, outputs, or API keys.
"""

from __future__ import annotations

import collections
import json
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUNDS = ROOT / "data" / "host-rounds.json"
DB = ROOT / "data" / "workbench-v1.sqlite"


def load_rounds() -> list[dict]:
    data = json.loads(ROUNDS.read_text(encoding="utf-8"))
    return data.get("rounds", data if isinstance(data, list) else [])


def call_status(call: dict) -> str:
    for err in call.get("validation_errors") or []:
        msg = str(err.get("message", ""))
        if "max-tokens" in msg:
            return "max-tokens"
    if call.get("schema_ok") is True:
        return "ok"
    if call.get("finish_kind"):
        return f"finish:{call['finish_kind']}"
    return "failed"


def main() -> None:
    rounds = load_rounds()
    print(f"rounds_total={len(rounds)}")
    if rounds:
        print(f"rounds_range={rounds[-1].get('startedAt')} .. {rounds[0].get('startedAt')}")
    reasons = collections.Counter(r.get("reasonCode") or r.get("status") for r in rounds)
    print("round_reasons=", dict(reasons))

    by_role = collections.defaultdict(lambda: {
        "calls": 0, "max_tokens": 0, "ok": 0, "other": 0,
        "out_ok": [], "out_fail": [], "attempts": collections.Counter(),
    })
    affected_rounds = []
    retry_success = collections.Counter()
    retry_fail = collections.Counter()

    for r in rounds:
        calls = (r.get("result") or {}).get("calls") or []
        mt = [c for c in calls if call_status(c) == "max-tokens"]
        if mt:
            affected_rounds.append((r.get("roundId"), r.get("reasonCode"), r.get("status"), len(mt)))
        for c in calls:
            role = c.get("role_key") or c.get("role") or "?"
            rec = by_role[role]
            rec["calls"] += 1
            rec["attempts"][c.get("attempt")] += 1
            st = call_status(c)
            if st == "max-tokens":
                rec["max_tokens"] += 1
                if c.get("output_tokens") is not None:
                    rec["out_fail"].append(c["output_tokens"])
            elif st == "ok":
                rec["ok"] += 1
                if c.get("output_tokens") is not None:
                    rec["out_ok"].append(c["output_tokens"])
            else:
                rec["other"] += 1
        # Retry outcome: same role_key + round, attempt 1 max-tokens, attempt 2 exists.
        grouped = collections.defaultdict(dict)
        for c in calls:
            grouped[(c.get("role_key") or c.get("role"), c.get("round"))][c.get("attempt")] = c
        for key, attempts in grouped.items():
            first = attempts.get(1)
            second = attempts.get(2)
            if first and call_status(first) == "max-tokens":
                if second and call_status(second) == "ok":
                    retry_success[key[0]] += 1
                elif second:
                    retry_fail[key[0]] += 1

    print(f"rounds_affected={len(affected_rounds)}")
    print("affected_rounds=", affected_rounds[:20])
    print("\nrole              calls  max-tok  ok  other  out_ok_med out_ok_max  retry_ok retry_fail")
    for role in sorted(by_role):
        rec = by_role[role]
        out_ok = rec["out_ok"]
        med = statistics.median(out_ok) if out_ok else None
        mx = max(out_ok) if out_ok else None
        print(f"{role:16s} {rec['calls']:6d} {rec['max_tokens']:7d} {rec['ok']:3d} {rec['other']:5d}"
              f" {str(med):>10s} {str(mx):>10s} {retry_success[role]:8d} {retry_fail[role]:10d}")
    print("\nrole attempts=", {k: dict(v["attempts"]) for k, v in by_role.items()})

    if DB.exists():
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cols = [r[1] for r in con.execute("PRAGMA table_info(ai_calls)")]
            print("\nai_calls_columns=", cols)
            if cols:
                rows = con.execute(
                    "SELECT role, phase, attempt, status, finish_reason, max_tokens, "
                    "input_tokens, output_tokens, error_code, error_message, started_at "
                    "FROM ai_calls ORDER BY started_at"
                ).fetchall()
                print(f"ai_calls_rows={len(rows)}")
                by = collections.Counter()
                for row in rows:
                    by[(row["role"], row["status"], row["finish_reason"], row["max_tokens"])] += 1
                for k, v in sorted(by.items(), key=lambda kv: str(kv[0])):
                    print("ai_calls", k, v)
                for row in rows:
                    if row["finish_reason"] == "max-tokens" or "max-tokens" in str(row["error_message"] or ""):
                        print("max-token row:",
                              {k: row[k] for k in ("role", "phase", "attempt", "status",
                                                   "finish_reason", "max_tokens", "input_tokens",
                                                   "output_tokens", "error_code", "started_at")})
            con.close()
        except Exception as exc:  # noqa: BLE001 - analysis helper
            print("db_error=", type(exc).__name__, str(exc)[:200])


if __name__ == "__main__":
    main()
