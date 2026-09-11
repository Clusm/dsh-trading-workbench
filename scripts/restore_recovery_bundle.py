"""校验并在全新目录中演练恢复包；默认只预览，不覆盖现有项目。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPECTED_PATHS = {
    "database": "agent_trader.sqlite",
    "capital_state.json": "state/capital_state.json",
    "channel_state.json": "state/channel_state.json",
    "control_state.json": "state/control_state.json",
    "account_risk_state.json": "state/account_risk_state.json",
    "futures_source_ledger.json": "state/futures_source_ledger.json",
    "market_universe_state.json": "state/market_universe_state.json",
    "risk_profile_lock": "risk_profile.lock.json",
}
REQUIRED_KEYS = {
    "database", "capital_state.json", "channel_state.json",
    "control_state.json", "account_risk_state.json",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(bundle: Path, relative: str) -> Path:
    member = Path(relative)
    if member.is_absolute() or ".." in member.parts or not member.parts:
        raise ValueError(f"恢复包包含不安全路径: {relative}")
    resolved = (bundle / member).resolve()
    if bundle != resolved and bundle not in resolved.parents:
        raise ValueError(f"恢复包路径越界: {relative}")
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"恢复包成员不是普通文件: {relative}")
    return resolved


def verify_recovery_bundle(bundle_dir: Path,
                           expected_environment: str | None = None) -> dict[str, Any]:
    """校验清单、哈希、文件白名单、SQLite 与环境一致性。"""
    bundle = Path(bundle_dir).resolve()
    if not bundle.is_dir() or bundle.is_symlink():
        raise ValueError("恢复包目录不存在或是符号链接")
    manifest_path = _safe_member(bundle, "manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    schema = int(manifest.get("bundle_schema") or 0)
    if schema not in {1, 2}:
        raise ValueError("不支持的恢复包版本")
    if schema >= 2:
        checksum_path = _safe_member(bundle, "manifest.sha256")
        expected = checksum_path.read_text(encoding="ascii").split()[0]
        if expected != hashlib.sha256(manifest_bytes).hexdigest():
            raise ValueError("manifest 哈希不匹配")
    if manifest.get("service_was_stopped") is not True:
        raise ValueError("恢复包未声明停写一致性")

    files = manifest.get("files")
    if not isinstance(files, dict) or "database" not in files:
        raise ValueError("恢复包缺少数据库清单")
    missing_keys = sorted(REQUIRED_KEYS - set(files))
    if missing_keys:
        raise ValueError(f"恢复包缺少关键成员: {', '.join(missing_keys)}")
    allowed = {"manifest.json"}
    if schema >= 2:
        allowed.add("manifest.sha256")
    verified: dict[str, Path] = {}
    for key, metadata in files.items():
        if key not in EXPECTED_PATHS:
            raise ValueError(f"恢复包包含未知成员类型: {key}")
        if not isinstance(metadata, dict):
            raise ValueError(f"文件清单格式错误: {key}")
        relative = str(metadata.get("path") or "")
        if relative.replace("\\", "/") != EXPECTED_PATHS[key]:
            raise ValueError(f"恢复包成员路径不符合契约: {key}")
        member = _safe_member(bundle, relative)
        if member.stat().st_size != int(metadata.get("size") or -1):
            raise ValueError(f"文件大小不匹配: {relative}")
        if _sha256(member) != str(metadata.get("sha256") or ""):
            raise ValueError(f"文件哈希不匹配: {relative}")
        allowed.add(relative.replace("\\", "/"))
        verified[str(key)] = member
    actual = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()}
    extras = sorted(actual - allowed)
    if extras:
        raise ValueError(f"恢复包包含未列入白名单的文件: {', '.join(extras)}")

    database = verified["database"]
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise ValueError("恢复数据库 quick_check 失败")
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        actual_schema = int(row[0]) if row and row[0] is not None else None
    finally:
        connection.close()
    expected_schema = files["database"].get("schema_version")
    if expected_schema is not None and actual_schema != int(expected_schema):
        raise ValueError("数据库迁移版本与清单不一致")

    for key, member in verified.items():
        if str(files[key]["path"]).startswith("state/"):
            payload = json.loads(member.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError(f"状态文件不是JSON对象: {member.name}")
    environment = str(manifest.get("environment") or "")
    capital = verified.get("capital_state.json")
    if capital:
        capital_mode = str(json.loads(capital.read_text(encoding="utf-8-sig")).get("mode") or "")
        if capital_mode != environment:
            raise ValueError("资金状态模式与恢复包环境不一致")
    if expected_environment and environment != expected_environment:
        raise ValueError(f"恢复包环境不匹配: {environment}")
    return {"bundle": str(bundle), "schema": schema, "environment": environment,
            "database_schema": actual_schema, "files": len(verified)}


def restore_to_new_directory(bundle_dir: Path, target_dir: Path, *, execute: bool = False,
                             expected_environment: str | None = None,
                             require_independent_volume: bool = False) -> dict[str, Any]:
    """仅恢复到不存在的新目录；绝不覆盖运行目录或既有文件。"""
    started = time.perf_counter()
    verified = verify_recovery_bundle(bundle_dir, expected_environment)
    bundle = Path(bundle_dir).resolve()
    target = Path(target_dir).resolve()
    source_volume = bundle.anchor.casefold()
    target_volume = target.anchor.casefold()
    independent_volume = bool(source_volume and target_volume and source_volume != target_volume)
    if require_independent_volume and not independent_volume:
        raise ValueError("恢复目标不在独立卷；请指定另一块磁盘或挂载卷")
    if target.exists():
        raise FileExistsError(f"恢复目标必须是不存在的新目录: {target}")
    if bundle == target or bundle in target.parents or target in bundle.parents:
        raise ValueError("恢复目标不能与恢复包互相嵌套")
    if not execute:
        return {**verified, "status": "dry_run", "target": str(target),
                "source_volume": source_volume, "target_volume": target_volume,
                "independent_volume": independent_volume}

    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.restore-{os.getpid()}"
    if stage.exists():
        raise FileExistsError(f"恢复暂存目录已存在: {stage}")
    stage.mkdir(parents=False)
    try:
        (stage / "data").mkdir()
        (stage / "config").mkdir()
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        for key, metadata in manifest["files"].items():
            source = _safe_member(bundle, str(metadata["path"]))
            if key == "database":
                destination = stage / "data" / "agent_trader.sqlite"
            elif key == "risk_profile_lock":
                destination = stage / "config" / "risk_profile.lock.json"
            else:
                destination = stage / "data" / source.name
            shutil.copy2(source, destination)
        receipt = {
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest_sha256": _sha256(bundle / "manifest.json"),
            "environment": verified["environment"],
            "database_schema": verified["database_schema"],
            "independent_volume": independent_volume,
        }
        (stage / "restore_receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        check = sqlite3.connect(stage / "data" / "agent_trader.sqlite")
        try:
            if str(check.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                raise ValueError("恢复后数据库 quick_check 失败")
        finally:
            check.close()
        os.replace(stage, target)
    except Exception:
        if stage.is_dir() and stage.parent == target.parent:
            shutil.rmtree(stage)
        raise
    return {**verified, "status": "restored", "target": str(target),
            "source_volume": source_volume, "target_volume": target_volume,
            "independent_volume": independent_volume,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3)}


def main() -> int:
    parser = argparse.ArgumentParser(description="校验并演练恢复包（默认dry-run）")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--expected-environment")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--require-independent-volume", action="store_true")
    args = parser.parse_args()
    result = restore_to_new_directory(
        args.bundle, args.target_dir, execute=args.execute,
        expected_environment=args.expected_environment,
        require_independent_volume=args.require_independent_volume)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
