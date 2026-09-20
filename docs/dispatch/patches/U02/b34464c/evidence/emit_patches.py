"""生成 r3 的**字节精确**共享适配补丁（目标文件 mixed EOL，与 r2 同一套做法）。

  sb_gen（= 集成 HEAD 字节 + r1/r2/r3 生成器，内容正确但 EOL 被归一化）
  → 取其"归一化后的文本"作为目标内容
  → 用 difflib 把该文本映射回**基线字节**上（保留每行原始 EOL）
  → 写入 sb_diff（基线 = I@<HEAD>）后 `git -c core.autocrlf=false diff` 出补丁

跑法：``python _scratch_B/r3/emit_patches.py <out_dir>``（out_dir 已存在时会被覆盖）
"""
from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GEN = HERE / "sb_gen"
DIFF_REPO = HERE / "sb_diff"
FILES = {
    "01_exchange_trade_reconciliation_account_ref_u15.diff":
        "src/exchange/trade_reconciliation.py",
    "02_exchange_binance_futures_account_ref_and_exit_reservation.diff":
        "src/exchange/binance_futures.py",
    "03_core_service_u02_wiring.diff": "src/core/service.py",
    "04_core_decision_pipeline_management_auth.diff": "src/core/decision_pipeline.py",
    "05_agents_execution_paper_exit_idempotency.diff": "src/agents/execution.py",
    "06_dashboard_app_event_labels.diff": "src/dashboard/app.py",
}


def eol_preserving(base_bytes: bytes, new_text: str) -> bytes:
    """把 new_text（LF 归一化）映射回 base_bytes 的字节风格。"""
    base_raw = base_bytes.decode("utf-8")
    base_lines = base_raw.split("\n")
    new_lines = new_text.split("\n")
    base_tail_empty = bool(base_lines) and base_lines[-1] == ""
    new_tail_empty = bool(new_lines) and new_lines[-1] == ""
    if base_tail_empty:
        base_lines = base_lines[:-1]
    if new_tail_empty:
        new_lines = new_lines[:-1]
    base_norm = [line[:-1] if line.endswith("\r") else line for line in base_lines]
    matcher = difflib.SequenceMatcher(None, base_norm, new_lines, autojunk=False)
    out: list[str] = []
    last_ending = "\n"
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                ending = "\r\n" if base_lines[k].endswith("\r") else "\n"
                out.append(base_norm[k] + ending)
                last_ending = ending
        else:
            for k in range(j1, j2):
                out.append(new_lines[k] + last_ending)
    result = "".join(out)
    if result:
        if new_tail_empty:
            if not result.endswith("\n"):
                result += "\r\n" if base_raw.endswith("\r\n") else "\n"
        elif result.endswith("\r\n"):
            result = result[:-2]
        elif result.endswith("\n"):
            result = result[:-1]
    return result.encode("utf-8")


def main() -> int:
    out_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE / "patches"
    assert (DIFF_REPO / ".git").exists(), f"缺少基线仓库: {DIFF_REPO}"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, rel in FILES.items():
        base_bytes = (DIFF_REPO / rel).read_bytes()
        new_text = (GEN / rel).read_text(encoding="utf-8")
        if new_text == base_bytes.decode("utf-8"):
            print(f"SKIP {rel}（无改动）")
            continue
        (DIFF_REPO / rel).write_bytes(eol_preserving(base_bytes, new_text))
        out = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "diff", "--", rel],
            cwd=DIFF_REPO, check=True, capture_output=True)
        (out_dir / name).write_bytes(out.stdout)
        print(f"OK {name} lines={out.stdout.count(b'\\n')} "
              f"cr_lines={out.stdout.count(bytes([13]))}")
    print("\nEMIT R3 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
