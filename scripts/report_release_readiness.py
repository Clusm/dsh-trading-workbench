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

#: U09 接线标记：下游据此判断本脚本是否已支持"声明窗口/报告时刻/显式声明状态"。
U09_INTEGRATION = "u09.report_release_readiness.v1"


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


def _default_db(environment: str) -> Path:
    """按运行剖面选择只读报告默认账本，避免 live 误读 Demo 库。"""
    if str(environment or "").strip().lower() == "live":
        return ROOT / "data" / "live" / "agent_trader.sqlite"
    return ROOT / "data" / "agent_trader.sqlite"


def main() -> int:
    parser = argparse.ArgumentParser(description="只读生成按 release_id 隔离的上线门禁报告")
    parser.add_argument("--db", type=Path, default=None,
                        help="数据库路径；省略时按 environment 选择 Demo/Live 账本")
    parser.add_argument("--manifest", type=Path, default=None,
                        help="由 release_manifest 生成的 JSON 清单")
    parser.add_argument("--environment", default="demo")
    parser.add_argument("--since", default=None,
                        help="发布观察窗口起点（缺省时由清单声明，仍缺则 WINDOW_MISSING）")
    parser.add_argument("--until", default=None,
                        help="发布观察窗口终点（缺省时由清单声明，再缺则取报告时刻）")
    parser.add_argument("--report-at", default=None,
                        help="报告时刻（ISO/epoch 毫秒）；仅在窗口终点无处声明时使用")
    parser.add_argument("--declaration", type=Path, default=None,
                        help="K06 声明状态 JSON（{group: PASS|FAIL|NO_DATA}）；"
                             "声明 PASS 而证据不完整由契约判 FAIL + PASS_WITHOUT_EVIDENCE")
    parser.add_argument("--strict", action="store_true",
                        help="门禁不是 GO 时返回 1；schema/IO 错误返回 2")
    parser.add_argument("--strict-evidence", action="store_true",
                        help="K06 存在阻断项时返回 1（NO_DATA != PASS）；schema/IO 错误返回 2")
    parser.add_argument("--out", type=Path, default=None,
                        help="可选报告输出路径；只写报告，不写数据库")
    args = parser.parse_args()
    db_path = args.db if args.db is not None else _default_db(args.environment)
    try:
        if args.manifest is None:
            if args.strict:
                raise ValueError("--strict requires --manifest")
            from report_demo_acceptance import build_report
            report = build_report(db_path)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        manifest = _load_json(args.manifest)
        declaration = _load_json(args.declaration) if args.declaration else None
        report = build_release_readiness_report(
            db_path, manifest, environment=args.environment,
            since=args.since, until=args.until,
            report_at=args.report_at, declaration=declaration,
        )
        if args.out:
            _write_json(args.out, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        # 阻断项写 stderr，保持 stdout 的 JSON 仍可被管道消费。
        summary = _blocking_summary(report)
        if summary:
            print(summary, file=sys.stderr)
        if args.strict and report.get("release_readiness") != "GO":
            return 1
        if args.strict_evidence and report.get("blocking_item_count"):
            return 1
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        error = {"read_only": True, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(error, ensure_ascii=False, indent=2))
        return 2


def _blocking_summary(report: dict[str, Any]) -> str:
    """把 K06 五组状态与阻断项列成人读文本（规格 §12：列具体阻断项）。"""
    k06 = report.get("k06") or {}
    groups = k06.get("groups") or []
    if not groups:
        return ""
    lines = ["K06 证据组: " + ", ".join(
        f"{item.get('group')}={item.get('status')}" for item in groups)]
    blockers = report.get("blocking_items") or []
    if blockers:
        lines.append(f"K06 阻断项 {len(blockers)}:")
        for item in blockers[:20]:
            lines.append(
                f"  - [{item.get('severity')}] {item.get('group')}/{item.get('code')}: "
                f"{item.get('detail')}")
    window = report.get("window") or {}
    if window.get("state") != "PASS":
        codes = ",".join(window.get("reason_codes") or []) or "WINDOW_UNAVAILABLE"
        lines.append(f"观察窗口: {codes} "
                     f"(since={window.get('since')}, until={window.get('until')})")
    continuity = report.get("run_continuity") or {}
    if continuity.get("verdict"):
        lines.append(f"运行连续性: {continuity.get('verdict')}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
