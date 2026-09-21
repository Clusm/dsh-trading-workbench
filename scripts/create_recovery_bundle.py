"""在服务停写时创建数据库与关键JSON状态的一致恢复包。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.instance_lock import InstanceLock  # noqa: E402
from core.ops.recovery import state_json_health  # noqa: E402
from core.runtime_paths import PROFILE_ALLOWED, RuntimePaths  # noqa: E402

REQUIRED_STATE_FILES = (
    "capital_state.json",
    "channel_state.json",
    "control_state.json",
    "account_risk_state.json",
)
OPTIONAL_STATE_FILES = (
    "futures_source_ledger.json",
    "market_universe_state.json",
    "public_rate_limit_state.json",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git_commit(project_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=project_root, check=True,
            capture_output=True, text=True, encoding="utf-8")
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _copy_verified_json(source: Path, destination: Path) -> dict[str, Any]:
    """读取原子JSON并在写入恢复包前完成语法与哈希校验。

    U13：清单条目额外记录 ``snapshot_at_ms``（本文件被复制的时刻），供
    ``restore_recovery_bundle`` 判定"DB 与 JSON 是否落在同一 epoch 窗口"。
    """
    data = source.read_bytes()
    # Windows PowerShell 历史脚本可能写入 UTF-8 BOM；它仍是合法 JSON 编码。
    # 校验时兼容 BOM，但复制和哈希始终保留原始字节。
    parsed = json.loads(data.decode("utf-8-sig"))
    if not isinstance(parsed, dict):
        raise ValueError(f"状态文件不是JSON对象: {source.name}")
    destination.write_bytes(data)
    copied = destination.read_bytes()
    if _sha256(copied) != _sha256(data):
        raise RuntimeError(f"状态文件复制校验失败: {source.name}")
    return {"path": f"state/{source.name}", "size": len(data), "sha256": _sha256(data),
            "snapshot_at_ms": int(time.time() * 1000)}


def create_recovery_bundle(project_root: Path, output_dir: Path, *,
                           profile: str | None = None) -> Path:
    """在持有服务单实例锁期间创建恢复包。

    与交易服务竞争同一把锁，可同时满足三件事：活动实例必定拒绝、确认死亡的 PID 锁可
    接管、备份过程中服务不能恰好启动。仅检查锁文件是否存在既会被陈旧锁永久阻塞，也
    存在检查通过后服务立即启动的竞态。

    U13-FU1/P1-2：``profile`` 决定状态根（``live`` → ``<root>/data/live``，与
    ``RuntimePaths.state_root`` 同口径）并写进 ``epoch.profile``。恢复包**必须**
    携带 profile，否则恢复端无法把状态与门禁放进 live 服务真正去读的状态根。
    ``profile=None`` 时与升级前逐字节一致（``<root>/data``）。
    """
    root = Path(project_root).resolve()
    paths = RuntimePaths.resolve(root, profile=profile)
    instance_lock = paths.lock_file
    guard = InstanceLock(instance_lock)
    try:
        guard.acquire()
    except RuntimeError as exc:
        raise RuntimeError("交易服务仍在运行；请先正常停机，再创建一致恢复包") from exc
    try:
        return _create_locked_bundle(root, output_dir, profile=profile)
    finally:
        guard.release()


def _create_locked_bundle(root: Path, output_dir: Path, *,
                          profile: str | None = None) -> Path:
    """调用方持有服务锁后的复制实现；异常时由外层保证释放锁。"""
    paths = RuntimePaths.resolve(root, profile=profile)
    data_dir = paths.data_dir
    db_source = data_dir / "agent_trader.sqlite"
    if not db_source.is_file():
        raise FileNotFoundError(f"数据库不存在: {db_source}")
    missing = [name for name in REQUIRED_STATE_FILES if not (data_dir / name).is_file()]
    if missing:
        raise FileNotFoundError(f"缺少关键状态文件: {', '.join(missing)}")
    # U13/F05：状态 JSON 损坏时 **不得** 建包——否则会把一份"看起来完整"的
    # 坏快照发布出去，之后再用它覆盖好备份。
    health = state_json_health(data_dir)
    if health["corrupt"]:
        raise ValueError("状态 JSON 损坏，拒绝创建恢复包: " + ", ".join(health["corrupt"]))
    epoch_id = uuid.uuid4().hex
    epoch_started_ms = int(time.time() * 1000)

    target_root = Path(output_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    final = target_root / f"recovery_{stamp}"
    stage = target_root / f".recovery_{stamp}.tmp-{os.getpid()}"
    state_dir = stage / "state"
    stage.mkdir(parents=False, exist_ok=False)
    state_dir.mkdir()
    try:
        db_target = stage / "agent_trader.sqlite"
        source_conn = sqlite3.connect(str(db_source))
        destination_conn = sqlite3.connect(str(db_target))
        try:
            source_conn.backup(destination_conn)
            check = destination_conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError("SQLite恢复副本完整性校验失败")
            version_row = destination_conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()
            schema_version = int(version_row[0]) if version_row and version_row[0] else None
        finally:
            destination_conn.close()
            source_conn.close()

        files: dict[str, Any] = {}
        db_bytes = db_target.read_bytes()
        files["database"] = {
            "path": db_target.name,
            "size": len(db_bytes),
            "sha256": _sha256(db_bytes),
            "schema_version": schema_version,
            "snapshot_at_ms": int(time.time() * 1000),
        }
        for name in REQUIRED_STATE_FILES + OPTIONAL_STATE_FILES:
            source = data_dir / name
            if source.is_file():
                files[name] = _copy_verified_json(source, state_dir / name)

        risk_lock = root / "config" / "risk_profile.lock.json"
        if risk_lock.is_file():
            lock_data = risk_lock.read_bytes()
            (stage / "risk_profile.lock.json").write_bytes(lock_data)
            files["risk_profile_lock"] = {
                "path": "risk_profile.lock.json", "size": len(lock_data),
                "sha256": _sha256(lock_data),
                "snapshot_at_ms": int(time.time() * 1000),
            }
        capital = json.loads((data_dir / "capital_state.json").read_text(encoding="utf-8"))
        snapshot_finished_ms = int(time.time() * 1000)
        # U13：epoch 块声明"DB 与 JSON 属于同一快照窗口"。bundle_schema 保持 2
        # （向后兼容既有恢复工具与回归），epoch 为**追加**字段：旧消费者忽略它，
        # 新消费者（restore 脚本 / core.ops.recovery）据此判定同 epoch。
        # 本脚本在持有单实例锁期间运行，窗口跨度天然极小。
        manifest = {
            "bundle_schema": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": _git_commit(root),
            "environment": capital.get("mode"),
            "capital_anchor": capital.get("anchored_at"),
            "service_was_stopped": True,
            "writer_stopped": True,
            "epoch": {
                "epoch_id": epoch_id,
                "snapshot_started_ms": epoch_started_ms,
                "snapshot_finished_ms": snapshot_finished_ms,
                "environment": capital.get("mode"),
                # U13-FU1/P1-2：恢复包**携带** profile，恢复端据此把状态 JSON
                # 与 ops/recovery_gate.json 放进同一个（profile 感知的）状态根。
                "profile": str(paths.profile or ""),
                "account_ref": "",
                "runtime_instance_id": "",
                "generation_id": "",
                "db_schema_version": schema_version,
                "tolerance_ms": 5000,
            },
            "files": files,
        }
        manifest_path = stage / "manifest.json"
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        manifest_path.write_bytes(manifest_bytes)
        (stage / "manifest.sha256").write_text(
            _sha256(manifest_bytes) + "  manifest.json\n", encoding="ascii", newline="\n")
        os.replace(stage, final)
        return final
    except Exception:
        if stage.exists() and stage.is_dir() and stage.parent == target_root:
            shutil.rmtree(stage)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="创建停写一致恢复包")
    parser.add_argument(
        "--output-dir", type=Path,
        default=PROJECT_ROOT / "data" / "recovery_bundles")
    parser.add_argument(
        "--profile", choices=PROFILE_ALLOWED, default=None,
        help="状态档位；live 时状态根为 <project>/data/live（U13-FU1/P1-2）")
    args = parser.parse_args()
    bundle = create_recovery_bundle(PROJECT_ROOT, args.output_dir,
                                    profile=args.profile)
    print(f"RECOVERY BUNDLE OK: {bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
