"""把 u03 接线补丁重建成 git 可应用的标准形态（就地重算 hunk 头与 index 行）。

只用标准库；在 CD 工作区根运行。

关键点：目标文件是**混合行尾**（工作区里既有 LF 又有 CRLF），补丁本身是 LF。
本脚本按"去 CR 后比较、保留原行字节"的方式重放纯插入 hunk，插入行的行尾沿用
其所在上下文前一行 ⇒ 生成的后像与 `git apply` 的结果一致，可用 git hash-object
复算 index 行（pre 侧等于 HEAD blob 短哈希）。

用法：
    python _scratch_author_r3/rebuild_patch.py            # 重建补丁（就地写回）
    python _scratch_author_r3/rebuild_patch.py --check     # 只校验，不写回
"""
from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "tests" / "fixtures" / "evidence" / "u03_orchestrator_integration.patch"
TARGET = "src/agents/orchestrator.py"


def parse_hunks(text: str) -> list[list[str]]:
    hunks: list[tuple[int, list[str]]] = []
    cur: list[str] | None = None
    for line in text.split("\n"):
        if line.startswith("@@"):
            old_start = int(line.split()[1].split(",")[0].lstrip("-"))
            cur = []
            hunks.append((old_start, cur))
            continue
        if cur is None:
            continue
        if line[:1] in (" ", "+", "-"):
            cur.append(line)
        elif line == "":
            break  # hunk 体结束（补丁里空行不会出现在 hunk 内不带前缀）
    return hunks


def apply_hunks(pre_lines: list[str], hunks: list[tuple[int, list[str]]]) -> list[str]:
    """纯插入重放；pre_lines 的每项保留其原始行尾（\\r 可能有）。"""
    keys = [ln.rstrip("\r") for ln in pre_lines]
    out: list[str] = []
    cursor = 0
    for old_start, hunk in hunks:
        assert not [ln for ln in hunk if ln[:1] == "-"], "本补丁应为纯插入"
        # 以 context 行（去 CR）定位锚点：取 hunk 的首段连续 context，
        # 在多个候选里取**离 hunk 声明起点最近**的那个（文件里可能有重复片段）。
        head_ctx: list[str] = []
        for ln in hunk:
            if ln[:1] != " ":
                break
            head_ctx.append(ln[1:])
        candidates = [pos for pos in range(cursor, len(keys) - len(head_ctx) + 1)
                      if keys[pos:pos + len(head_ctx)] == head_ctx]
        if not candidates:
            raise SystemExit(f"锚点未命中（cursor={cursor}）: {head_ctx[:2]!r}")
        anchor = min(candidates, key=lambda pos: abs(pos - (old_start - 1)))
        out.extend(pre_lines[cursor:anchor])
        idx = anchor
        pending: list[str] = []
        for ln in hunk:
            ending = pre_lines[idx - 1][-1:] if idx > 0 else ""
            ending = "\r" if ending == "\r" else ""
            if ln[:1] == " ":
                if pending:
                    out.extend(p + ending for p in pending)
                    pending = []
                assert keys[idx] == ln[1:], f"context 不匹配 @{idx}: {ln[1:]!r}"
                out.append(pre_lines[idx])
                idx += 1
            else:
                pending.append(ln[1:])
        if pending:
            ending = "\r" if idx > 0 and pre_lines[idx - 1].endswith("\r") else ""
            out.extend(p + ending for p in pending)
        cursor = idx
    out.extend(pre_lines[cursor:])
    return out


def git(*args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, input=stdin,
                          capture_output=True).stdout


def main() -> int:
    check_only = "--check" in sys.argv
    pre_bytes = git("show", f"HEAD:{TARGET}")
    pre_text = pre_bytes.decode("utf-8")
    pre_lines = pre_text.split("\n")
    hunks = parse_hunks(PATCH.read_text(encoding="utf-8"))
    post_lines = apply_hunks(pre_lines, hunks)
    post_text = "\n".join(post_lines)

    post_path = ROOT / "_scratch_author_r3" / "orch_post.py"
    post_path.write_text(post_text, encoding="utf-8", newline="")
    pre_blob = git("hash-object", "--stdin", stdin=pre_bytes).decode().strip()
    post_blob = git("hash-object", "--stdin", stdin=post_text.encode("utf-8")).decode().strip()
    print(f"pre_blob={pre_blob[:7]} post_blob={post_blob[:7]} "
          f"inserted={len(post_lines) - len(pre_lines)}")

    if check_only:
        return 0
    # 用原始行重建 unified diff：**CRLF 文件的 context 行必须自带 \r**，
    # 否则 git apply 在这些行上失配（本仓 orchestrator.py 是混合行尾）。
    diff = list(difflib.unified_diff(
        pre_lines, post_lines,
        fromfile=f"a/{TARGET}", tofile=f"b/{TARGET}", lineterm=""))
    header = (f"diff --git a/{TARGET} b/{TARGET}\n"
              f"index {pre_blob[:7]}..{post_blob[:7]} 100644\n")
    PATCH.write_text(header + "\n".join(diff) + "\n", encoding="utf-8", newline="\n")
    print(f"written: {PATCH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
