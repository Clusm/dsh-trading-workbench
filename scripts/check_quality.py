"""对仓库中待发布的 Python 文件执行静态检查与语法编译。

覆盖范围 = Git 已跟踪文件 + **未跟踪但未被 .gitignore 排除**的文件。

2026-09-15 修正：旧实现只读 ``git ls-files``，于是"新增但尚未提交"的脚本完全
不进入质量门禁。这与 L00 第 1 条直接冲突——"新文件必须进入发布文件清单，避免
质量脚本仅扫描 tracked 文件时漏检新增代码"。实际后果是 workspace 里一度存在 9 个
既不在离线回归、也不在质量门禁里的诊断/修复脚本。

未跟踪文件的处理策略是**纳入检查、但不因未跟踪而失败**：此时开发者通常正在迭代，
报错应指向代码问题本身，而不是"文件还没 add"。发布清单另有单独的文件白名单校验。

脚本不访问网络、不创建交易服务，也不会读取或修改运行状态。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git_python_files(*extra_args: str) -> list[str]:
    """按给定视图列出 Python 文件，统一返回仓库相对路径。"""
    result = subprocess.run(
        ["git", "ls-files", *extra_args, "--", "*.py"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return [
        line
        for line in result.stdout.splitlines()
        if line and (PROJECT_ROOT / line).is_file()
    ]


def release_python_files() -> tuple[list[str], list[str]]:
    """返回 ``(全部待检查文件, 其中未跟踪的文件)``，各自有序去重。"""
    tracked = set(_git_python_files())
    untracked = set(_git_python_files("--others", "--exclude-standard"))
    return sorted(tracked | untracked), sorted(untracked - tracked)


def _run(command: list[str]) -> None:
    """运行质量检查命令；失败时原样传播退出码供 CI 或开发者处理。"""
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def main() -> int:
    """依次运行代码、密钥、风险档和依赖锁检查。"""
    files, untracked = release_python_files()
    if untracked:
        print(f"NOTE: {len(untracked)} 个未跟踪 Python 文件已纳入本轮检查：")
        for name in untracked:
            print(f"  - {name}")
    _run([sys.executable, "-m", "ruff", "check", *files])
    _run([sys.executable, "-m", "py_compile", *files])
    _run([sys.executable, "scripts/verify_profile_lock.py"])
    _run([sys.executable, "scripts/verify_dependency_lock.py"])
    _run([sys.executable, "scripts/scan_tracked_secrets.py"])
    print(f"QUALITY CHECK OK files={len(files)} untracked={len(untracked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
