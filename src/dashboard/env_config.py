"""Dashboard 执行模式配置文件读写边界。"""
from __future__ import annotations

import os
from pathlib import Path


def read_env_file(path: Path) -> dict[str, str]:
    """读取简单 KEY=VALUE 文件；文件缺失或不可读时返回空字典。"""
    values: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except (OSError, UnicodeError):
        return {}
    return values


def set_env_mode(path: Path, mode: str) -> None:
    """保留其他配置与注释，原子写入或追加 ``EXECUTION_MODE``。"""
    target = Path(path)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("EXECUTION_MODE="):
            lines[index] = f"EXECUTION_MODE={mode}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"EXECUTION_MODE={mode}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(f"{target.suffix}.tmp-{os.getpid()}")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp_path, target)
