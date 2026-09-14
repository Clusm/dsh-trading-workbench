"""创建不含运行状态和凭据的可复核发布 ZIP 包。

发布包只面向代码部署，不是恢复包：不包含 ``data/``、``.env``、日志、虚拟环境、缓存或
未跟踪临时诊断脚本。输出文件已存在时直接失败，避免静默覆盖已有发布工件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAFE_CONFIGS = {
    "config/agents.yaml",
    "config/decision_policy.yaml",
    "config/risk_learning.yaml",
    "config/risk_profile.lock.json",
    "config/settings.yaml",
    "config/windows_service.json",
}
SAFE_TOP_LEVEL = {
    "CONTRIBUTING.md", "README.md", "pyproject.toml", "requirements.txt",
    "requirements-lock.txt", "requirements-dev.txt", "requirements-dev-lock.txt",
}
SAFE_DOCS = {
    "docs/代码维护规范.md",
    "docs/项目说明书.md",
    "docs/运行与故障恢复手册_2026-09-06.md",
    "docs/上线前最后一轮优化工作计划_2026-09-15.md",
    "docs/AI交易开源项目全景盘点与吸收建议_2026-09-15.md",
}
FORBIDDEN_NAMES = {".env", ".env.local", ".env.production"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tracked_files(root: Path) -> set[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files"], cwd=root, check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()}


def _safe_relative_files(root: Path) -> list[str]:
    """返回允许进入发布包的相对路径；源码/脚本优先取Git追踪集合。"""
    tracked = _tracked_files(root)
    candidates: set[str] = set(SAFE_TOP_LEVEL | SAFE_CONFIGS | SAFE_DOCS)
    for relative in tracked:
        if relative.startswith("src/") and relative.endswith(".py"):
            candidates.add(relative)
        elif relative.startswith("scripts/") and relative.endswith(".py"):
            candidates.add(relative)
    if not tracked:
        candidates.update(
            path.relative_to(root).as_posix()
            for base in (root / "src", root / "scripts")
            if base.exists()
            for path in base.rglob("*.py")
            if "__pycache__" not in path.parts
            and not path.name.startswith(("diag_", "probe_", "repair_", "verify_"))
        )
    safe: list[str] = []
    for relative in sorted(candidates):
        path = root / relative
        if path.is_symlink():
            raise ValueError(f"发布文件不得是符号链接: {relative}")
        if not path.is_file() or relative.split("/")[-1] in FORBIDDEN_NAMES:
            continue
        if any(part in {"data", ".venv", "__pycache__", ".git"} for part in Path(relative).parts):
            continue
        safe.append(relative)
    return safe


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"release manifest 无法读取: {path}") from exc
    if not isinstance(value, dict) or not value.get("release_id"):
        raise ValueError("release manifest 必须是含 release_id 的 JSON 对象")
    serialized = json.dumps(value, ensure_ascii=False).lower()
    if any(token in serialized for token in ("api_secret", "private_key", "authorization")):
        raise ValueError("release manifest 包含疑似凭据字段")
    return value


def _package_manifest(
    root: Path, files: list[str], release_manifest: dict[str, Any],
) -> dict[str, Any]:
    rows = [{"path": relative, "sha256": _sha256(root / relative)} for relative in files]
    return {
        "package_schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "release_id": str(release_manifest["release_id"]),
        "release_manifest_sha256": hashlib.sha256(
            json.dumps(release_manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "files": rows,
        "excluded": [".env", "data/", ".venv/", "logs/", "cache/", "untracked diagnostics"],
    }


def create_release_package(
    project_root: Path, output_path: Path, *, manifest_path: Path,
) -> dict[str, Any]:
    """原子生成发布包；目标存在、manifest越界或包内文件不安全时失败。"""
    root = Path(project_root).resolve()
    output = Path(output_path).resolve()
    manifest_file = Path(manifest_path).resolve()
    if output.exists():
        raise FileExistsError(f"发布包已存在，不覆盖: {output}")
    try:
        manifest_file.relative_to(root)
    except ValueError as exc:
        raise ValueError("manifest 必须位于项目根目录内") from exc
    release_manifest = _load_manifest(manifest_file)
    files = _safe_relative_files(root)
    if not files:
        raise ValueError("没有可打包的安全文件")
    forbidden = [relative for relative in files if any(
        part in {"data", ".venv", ".git"} for part in Path(relative).parts
    ) or Path(relative).name in FORBIDDEN_NAMES]
    if forbidden:
        raise ValueError(f"发布文件清单包含禁止路径: {forbidden[:3]}")

    package_root = output.parent
    package_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", suffix=".tmp", dir=package_root, delete=False,
    ) as handle:
        temporary = Path(handle.name)
    package_meta = _package_manifest(root, files, release_manifest)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative in files:
                archive.write(root / relative, arcname=relative)
            archive.writestr(
                "release_manifest.json",
                json.dumps(release_manifest, ensure_ascii=False, indent=2) + "\n",
            )
            archive.writestr(
                "package_manifest.json",
                json.dumps(package_meta, ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    package_meta["package_sha256"] = _sha256(output)
    package_meta["output"] = str(output)
    return package_meta


def main() -> int:
    parser = argparse.ArgumentParser(description="创建不含状态/凭据的代码发布包")
    parser.add_argument("--manifest", type=Path,
                        default=ROOT / "data" / "release_manifest.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = create_release_package(ROOT, args.out, manifest_path=args.manifest)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"RELEASE PACKAGE FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
