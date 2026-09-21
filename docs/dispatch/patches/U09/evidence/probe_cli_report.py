"""U09 证据：CLI（``scripts/report_release_readiness.py``）两态可用性探针。

只写系统临时目录里的合成 SQLite 与 JSON 清单，不读真实 ``data``/``.env``、
不联网、不启服务。用法（在仓库根目录）：

    PYTHONUTF8=1 <venv>/python.exe docs/dispatch/patches/U09/evidence/probe_cli_report.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from intel import shadow_status  # noqa: E402


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def build_fixture(root: Path) -> tuple[Path, Path, datetime, datetime]:
    db_path = root / "cli.sqlite"
    db = AuditDatabase(db_path)
    db.set_release_context(release_id="rel-cli", environment="demo",
                           account_ref="demo:cli")
    start = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    for index in range(200):
        db.log_shadow_decision(
            decision_id=f"a-{index}", symbol="BTCUSDT", signal="bullish",
            reference_price=100.0, confidence=0.7, source=shadow_status.SOURCE_A,
            execution_weight=0.0,
            feature_json=json.dumps({"opportunity_id": f"opp-{index}"}))
    for index in range(50):
        db.log_shadow_decision(
            decision_id=f"b-{index}", symbol="BTCUSDT", signal="bullish",
            reference_price=100.0, confidence=0.7, source=shadow_status.SOURCE_B,
            execution_weight=0.0,
            feature_json=json.dumps({"opportunity_id": f"b-opp-{index}"}))
    with db.transaction() as connection:
        # 影子样本写入时刻是"现在"；把窗口内的样本回填到观察窗口内，模拟真实发布。
        connection.execute("UPDATE shadow_decisions SET created_at=?",
                           (iso(start + timedelta(minutes=5)),))
        for index in range(31):
            observed = start + timedelta(minutes=index)
            connection.execute(
                """INSERT OR IGNORE INTO release_observations(
                    observation_id,release_id,environment,account_ref,session_id,
                    observed_at,observed_minute,health_state,component_status_json,
                    config_hash,rate_limit_json,coverage_json,entry_readiness_json,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"obs-{index}", "rel-cli", "demo", "demo:cli", "s1",
                 observed.isoformat(timespec="milliseconds"),
                 observed.isoformat(timespec="minutes"), "healthy", "{}", "cfg",
                 json.dumps({"responses_by_status": {}, "coordination_available": True}),
                 "{}", json.dumps({"new_entry_allowed": True}), "{}"))
    schema = db.current_schema_version()
    db.close()
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps({
        "release_id": "rel-cli", "environment": "demo", "account_ref": "demo:cli",
        "manifest_schema": 1, "schema_version": schema,
        "artifact_hash": "sha256:cli-artifact", "content_hash": "sha256:cli-artifact",
        "runtime_instance_id": "run-cli-1",
    }, ensure_ascii=False), encoding="utf-8")
    return db_path, manifest_path, start, end


def run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "report_release_readiness.py"), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180)
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="u09-cli-"))
    db_path, manifest_path, start, end = build_fixture(root)
    report_out = root / "report.json"
    common = ["--db", str(db_path), "--manifest", str(manifest_path),
              "--environment", "demo"]
    window = ["--since", start.isoformat(timespec="milliseconds"),
              "--until", end.isoformat(timespec="milliseconds")]
    cases = (
        ("窗口声明 + 完整证据", [*common, *window, "--out", str(report_out)]),
        ("窗口声明 + --strict", [*common, *window, "--strict"]),
        ("窗口声明 + --strict-evidence", [*common, *window, "--strict-evidence"]),
        ("未声明窗口（应 WINDOW_MISSING 阻断）", [*common, "--strict-evidence"]),
        ("畸形窗口（应 WINDOW_INVALID，rc=0 不崩）",
         [*common, "--since", "1e400", "--until", end.isoformat()]),
    )
    failures = 0
    for name, args in cases:
        code, out, err = run_cli(*args)
        print(f"\n--- {name}: rc={code}")
        for line in (err or "").strip().splitlines()[:6]:
            print(f"    {line}")
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = {}
            print("    stdout 不是 JSON（异常）")
            failures += 1
        if parsed:
            print(f"    release_readiness={parsed.get('release_readiness')} "
                  f"blocking={parsed.get('blocking_item_count')} "
                  f"window={list((parsed.get('window') or {}).get('reason_codes') or [])} "
                  f"read_only={parsed.get('read_only')}")
    if report_out.is_file():
        written = json.loads(report_out.read_text(encoding="utf-8"))
        print(f"\n落盘报告 keys={sorted(written)[:8]} ...")
        print(f"directional_candidates={written['release_evidence']['directional_candidates']} "
              f"shadow_rows={written['release_evidence']['shadow_rows']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
