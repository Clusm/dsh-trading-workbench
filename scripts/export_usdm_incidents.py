"""按显式 manifest 从审计库导出去敏 USD-M 事故夹具。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.usdm_replay_export import export_incident_fixture, write_fixture  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", help="manifest JSON 文件；传 - 时从 stdin 读取", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = sys.stdin.read() if args.manifest == "-" else Path(args.manifest).read_text(encoding="utf-8")
    manifest = json.loads(raw)
    fixture = export_incident_fixture(args.db, list(manifest.get("incidents") or []))
    write_fixture(args.output, fixture)
    print(f"exported incidents={len(manifest.get('incidents') or [])} events={len(fixture['events'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
