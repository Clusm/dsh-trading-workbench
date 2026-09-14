"""只读生成发布验收报告；严格模式失败时返回非零退出码。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.release_evidence import build_release_readiness_report  # noqa: E402


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="只读生成按 release_id 隔离的上线门禁报告")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "agent_trader.sqlite")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="由 release_manifest 生成的 JSON 清单")
    parser.add_argument("--environment", default="demo")
    parser.add_argument("--since", default=None)
    parser.add_argument("--until", default=None)
    parser.add_argument("--strict", action="store_true",
                        help="门禁不是 GO 时返回 1；schema/IO 错误返回 2")
    parser.add_argument("--out", type=Path, default=None,
                        help="可选报告输出路径；只写报告，不写数据库")
    args = parser.parse_args()
    try:
        if args.manifest is None:
            if args.strict:
                raise ValueError("--strict requires --manifest")
            from report_demo_acceptance import build_report
            report = build_report(args.db)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        manifest = _load_json(args.manifest)
        report = build_release_readiness_report(
            args.db, manifest, environment=args.environment,
            since=args.since, until=args.until,
        )
        if args.out:
            _write_json(args.out, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.strict and report.get("release_readiness") != "GO":
            return 1
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        error = {"read_only": True, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
