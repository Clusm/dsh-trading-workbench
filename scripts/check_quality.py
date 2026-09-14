"""对 Git 已跟踪的 Python 文件执行静态检查与语法编译。

只检查版本库文件，避免开发者本地尚未整理的临时诊断脚本影响项目质量门禁。
脚本不访问网络、不创建交易服务，也不会读取或修改运行状态。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _tracked_python_files() -> list[str]:
    """返回 Git 已跟踪的 Python 文件路径，统一使用仓库相对路径。"""
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
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


def _run(command: list[str]) -> None:
    """运行质量检查命令；失败时原样传播退出码供 CI 或开发者处理。"""
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def main() -> int:
    """依次运行代码、密钥、风险档和依赖锁检查。"""
    files = _tracked_python_files()
    _run([sys.executable, "-m", "ruff", "check", *files])
    _run([sys.executable, "-m", "py_compile", *files])
    _run([sys.executable, "scripts/verify_profile_lock.py"])
    _run([sys.executable, "scripts/verify_dependency_lock.py"])
    _run([sys.executable, "scripts/scan_tracked_secrets.py"])
    print(f"QUALITY CHECK OK files={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
