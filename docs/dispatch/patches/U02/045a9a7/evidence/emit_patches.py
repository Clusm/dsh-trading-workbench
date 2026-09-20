"""生成**字节精确**的共享适配补丁（关键：目标文件是 mixed EOL）。

集成工作区用的配置是 ``core.autocrlf=false``（U05 的教训：依赖 ``autocrlf=true``
的补丁在集成时会整体打不上）。``src/exchange/binance_futures.py`` 等文件里既有
CRLF 行也有 LF 行，因此补丁的上下文行必须与目标**逐字节相同**：
`git diff` 一律以 ``-c core.autocrlf=false`` 生成，且"新内容"的每一行都从
基线行的行尾继承 EOL（新增行沿用前一行），不做整文件 EOL 归一化。

流程：
  sb_gen（= 集成 HEAD 字节 + 旧补丁 + r2 生成器，内容正确但 EOL 被归一化为 CRLF）
  → 取其"归一化后的文本"作为目标内容
  → 用 difflib 把该文本映射回**基线字节**上（保留每行原始 EOL）
  → 写入 sb_diff（基线 = I@HEAD 字节）后 `git -c core.autocrlf=false diff` 出补丁
"""
from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
B_ROOT = HERE.parents[1]
GEN = HERE / "sb_gen"
DIFF_REPO = HERE / "sb_diff"
OUT = B_ROOT / "docs" / "dispatch" / "patches" / "U02" / "045a9a7"
I_ROOT = Path(r"C:/Users/Tao/Desktop/币安/mat-wt/I")
I_HEAD = "1207cd3"
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
    """把 new_text（LF 归一化）映射回 base_bytes 的字节风格。

    未改动的行逐字节沿用基线（含它自己的行尾），新增行沿用前一行的行尾；
    文件末尾换行由**新内容**决定，风格由基线决定——避免凭空多出一个空行。
    """
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
            if not result.endswith("\n"):  # 新内容以换行结尾但重建结果没带
                result += "\r\n" if base_raw.endswith("\r\n") else "\n"
        elif result.endswith("\r\n"):
            result = result[:-2]
        elif result.endswith("\n"):
            result = result[:-1]
    return result.encode("utf-8")


def main() -> int:
    # 1) sb_diff 必须已由外部准备成"I@HEAD 字节的 clean git 仓库"（见 README 复现步骤）。
    assert (DIFF_REPO / ".git").exists(), f"缺少基线仓库: {DIFF_REPO}"

    # 2) 逐文件：基线字节 + 生成器产出的归一化文本 -> 字节精确新内容。
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
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_bytes(out.stdout)
        cr = out.stdout.count(b"\r")
        print(f"OK {name} lines={out.stdout.count(chr(10).encode())} cr_lines={cr}")

    print("\nEMIT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
