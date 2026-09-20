"""U13 备份恢复：DB + JSON **同 epoch** 恢复包、可演练、恢复后先对账再开仓。

任务卡必达要点 4 与 6 在这里落地：

- **同 epoch 恢复包**：数据库快照与全部关键 JSON 状态必须在**同一个快照窗口**
  内取到，且清单里逐文件记录 ``snapshot_at_ms``；窗口跨度超过容差即
  ``EPOCH_MISMATCH``（拒绝）。这样"数据库里的持仓"与"JSON 里的资金/通道/风控
  水位"不会来自两个不同的时刻。
- **可演练**：:func:`drill_epoch_bundle` 把恢复包还原到一个 **全新的隔离目录**
  并复核（含 ``PRAGMA quick_check``），产出回执；``dry_run`` 不落盘。
- **同盘备份 ≠ 灾难恢复（R21）**：同卷演练会显式标注
  ``SAME_VOLUME_NOT_DISASTER_RECOVERY``；``require_independent_volume=True`` 时
  直接拒绝同卷目标。
- **恢复后先对账再开仓（F14）**：还原完成后门禁处于 ``RECONCILE_REQUIRED``，
  必须提交**结构化对账证据**才允许新开仓——"恢复完就直接开仓"在代码层面
  不可达。

F05 相关：写盘采用"暂存目录 + 原子发布"，中断的恢复包**不会被发布**；目标已
存在时**拒绝覆盖**（坏包不得覆盖好包）；状态 JSON 损坏时 ``STATE_CORRUPT``
fail-closed。

只在本机文件系统上工作：不联网、不读 ``.env``/密钥、不启动服务、不下单。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.instance_lock import InstanceLock
from core.runtime_paths import REQUIRED_STATE_FILES, RuntimePaths

__all__ = [
    "BUNDLE_SCHEMA",
    "EPOCH_MISMATCH",
    "EVIDENCE_MISSING",
    "EVIDENCE_STALE",
    "EVIDENCE_TYPE_INVALID",
    "GATE_CLEARED",
    "GATE_NOT_INSTALLED",
    "OPTIONAL_STATE_FILES",
    "RECONCILE_REQUIRED_BEFORE_ENTRY",
    "RECOVERY_INCOMPLETE",
    "RECOVERY_WRITER_RUNNING",
    "SAME_VOLUME_NOT_DISASTER_RECOVERY",
    "STATE_CORRUPT",
    "RecoveryEpoch",
    "RecoveryGate",
    "RecoveryGateDecision",
    "check_writer_stopped",
    "create_epoch_bundle",
    "drill_epoch_bundle",
    "recovery_plan",
    "state_json_health",
    "verify_epoch_bundle",
]

BUNDLE_SCHEMA = 3

#: 关键状态（缺一即 ``RECOVERY_INCOMPLETE``，与 RuntimePaths 同源）。
OPTIONAL_STATE_FILES = (
    "futures_source_ledger.json",
    "market_universe_state.json",
    "public_rate_limit_state.json",
)

EPOCH_MISMATCH = "EPOCH_MISMATCH"
RECOVERY_INCOMPLETE = "RECOVERY_INCOMPLETE"
RECOVERY_WRITER_RUNNING = "RECOVERY_WRITER_RUNNING"
STATE_CORRUPT = "STATE_CORRUPT"
SAME_VOLUME_NOT_DISASTER_RECOVERY = "SAME_VOLUME_NOT_DISASTER_RECOVERY"
RECONCILE_REQUIRED_BEFORE_ENTRY = "RECONCILE_REQUIRED_BEFORE_ENTRY"
GATE_CLEARED = "GATE_CLEARED"
#: 未安装门禁（从未恢复过）：新开仓不受恢复门禁约束。
GATE_NOT_INSTALLED = "GATE_NOT_INSTALLED"
EVIDENCE_MISSING = "EVIDENCE_MISSING"
EVIDENCE_TYPE_INVALID = "EVIDENCE_TYPE_INVALID"
EVIDENCE_STALE = "EVIDENCE_STALE"

#: 同 epoch 容差：DB 快照与全部 JSON 的 ``snapshot_at_ms`` 必须落在同一窗口。
DEFAULT_EPOCH_TOLERANCE_MS = 5000
#: 对账证据可接受的最大年龄（默认 15 分钟）：过期证据必须重新对账。
DEFAULT_EVIDENCE_TTL_MS = 15 * 60 * 1000


class RecoveryError(RuntimeError):
    """恢复包不可安全创建/校验/演练（fail-closed）。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        # 原因码必须出现在异常文本里：调用方/回归断言按原因码比对，
        # 只留自然语言消息会让"确实被拒绝"与"拒绝原因正确"两件事分不开。
        super().__init__(f"{code}: {message}" if message else code)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_bytes(data)
    os.replace(temp, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(
        path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
               + "\n").encode("utf-8"))


def _open_immutable_readonly(path: Path) -> sqlite3.Connection:
    """只读不可变快照连接：绝不在介质旁生成 WAL/SHM（与既有 restore 脚本同口径）。"""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def check_writer_stopped(paths: RuntimePaths) -> None:
    """确认交易服务**没有**在写这个状态根（单实例锁语义）。

    复用 ``core.instance_lock.InstanceLock`` 的只读存活判定（唯一事实源，不在
    这里重写 Windows 句柄逻辑）。锁文件里的 PID 仍存活 → 拒绝创建恢复包。
    """
    lock_file = paths.lock_file
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and InstanceLock._pid_alive(pid):
        raise RecoveryError(
            RECOVERY_WRITER_RUNNING,
            f"交易服务仍在运行（PID={pid}，锁文件={lock_file.name}）；"
            "请先正常停机，再创建同 epoch 恢复包",
        )


def state_json_health(data_dir: Path, *, names: tuple[str, ...] | None = None) -> dict[str, Any]:
    """只读巡检关键状态 JSON（F05：状态 JSON 损坏 → 停新仓，不静默 normal）。"""
    checked = names or (REQUIRED_STATE_FILES + OPTIONAL_STATE_FILES)
    broken: list[str] = []
    missing: list[str] = []
    for name in checked:
        path = data_dir / name
        if not path.is_file():
            if name in REQUIRED_STATE_FILES:
                missing.append(name)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            broken.append(name)
            continue
        if not isinstance(payload, dict):
            broken.append(name)
    problems = [STATE_CORRUPT] if broken else []
    if missing:
        problems.append(RECOVERY_INCOMPLETE)
    return {
        "checked": list(checked),
        "corrupt": broken,
        "missing_required": missing,
        "problems": problems,
        "new_entry_allowed": not problems,
    }


@dataclass(frozen=True)
class RecoveryEpoch:
    """一次恢复包的 epoch 标识：它是"这一包状态属于同一时刻"的主张。"""

    epoch_id: str
    snapshot_started_ms: int
    snapshot_finished_ms: int
    environment: str
    profile: str
    account_ref: str
    runtime_instance_id: str
    generation_id: str
    db_schema_version: int | None = None
    tolerance_ms: int = DEFAULT_EPOCH_TOLERANCE_MS

    def as_dict(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "snapshot_started_ms": self.snapshot_started_ms,
            "snapshot_finished_ms": self.snapshot_finished_ms,
            "environment": self.environment,
            "profile": self.profile,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "generation_id": self.generation_id,
            "db_schema_version": self.db_schema_version,
            "tolerance_ms": self.tolerance_ms,
        }

    def accepts(self, snapshot_at_ms: int) -> bool:
        return (self.snapshot_started_ms - self.tolerance_ms
                <= snapshot_at_ms
                <= self.snapshot_finished_ms + self.tolerance_ms)


def _copy_state_json(source: Path, destination: Path, epoch: RecoveryEpoch) -> dict[str, Any]:
    """校验并复制一个状态 JSON；返回清单条目（含 ``snapshot_at_ms``）。"""
    started = _now_ms()
    try:
        raw = source.read_bytes()
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RecoveryError(STATE_CORRUPT, f"状态文件不可解析: {source.name}") from exc
    if not isinstance(parsed, dict):
        raise RecoveryError(STATE_CORRUPT, f"状态文件不是 JSON 对象: {source.name}")
    destination.write_bytes(raw)
    if _sha256_file(destination) != _sha256_bytes(raw):
        raise RecoveryError(RECOVERY_INCOMPLETE, f"状态文件复制校验失败: {source.name}")
    snapshot_at = _now_ms()
    if not epoch.accepts(started) or not epoch.accepts(snapshot_at):
        raise RecoveryError(EPOCH_MISMATCH, f"{source.name} 不在同一快照窗口内")
    return {
        "path": f"state/{source.name}",
        "size": len(raw),
        "sha256": _sha256_bytes(raw),
        "snapshot_at_ms": snapshot_at,
    }


def create_epoch_bundle(
    paths: RuntimePaths,
    output_dir: Path,
    *,
    environment: str,
    account_ref: str = "unknown",
    runtime_instance_id: str = "unknown",
    generation_id: str = "unknown",
    now_ms: int | None = None,
) -> Path:
    """创建同 epoch 恢复包（DB + JSON），中断不发布、已存在不覆盖。

    调用方必须保证交易服务已停机；本函数会在建包前复核单实例锁
    （:func:`check_writer_stopped`），并在复制前巡检状态 JSON
    （:func:`state_json_health`）。
    """
    data_dir = paths.data_dir
    db_source = paths.db_file
    if not db_source.is_file():
        raise RecoveryError(RECOVERY_INCOMPLETE, f"数据库不存在: {db_source}")
    check_writer_stopped(paths)
    health = state_json_health(data_dir)
    if health["corrupt"]:
        raise RecoveryError(STATE_CORRUPT,
                            "状态 JSON 损坏，拒绝用它建包: " + ", ".join(health["corrupt"]))
    missing = health["missing_required"]
    if missing:
        raise RecoveryError(RECOVERY_INCOMPLETE, "缺少关键状态文件: " + ", ".join(missing))

    target_root = Path(output_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    epoch = RecoveryEpoch(
        epoch_id=uuid.uuid4().hex,
        snapshot_started_ms=now_ms if now_ms is not None else _now_ms(),
        snapshot_finished_ms=0,
        environment=str(environment),
        profile=str(paths.profile or ""),
        account_ref=str(account_ref),
        runtime_instance_id=str(runtime_instance_id),
        generation_id=str(generation_id),
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    final = target_root / f"recovery_{stamp}"
    if final.exists():
        raise RecoveryError("BACKUP_TARGET_EXISTS", f"恢复包目标已存在，拒绝覆盖: {final}")
    stage = target_root / f".{final.name}.tmp-{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=False, exist_ok=False)
    (stage / "state").mkdir()
    try:
        db_target = stage / "agent_trader.sqlite"
        source_conn = sqlite3.connect(str(db_source))
        destination_conn = sqlite3.connect(str(db_target))
        try:
            source_conn.backup(destination_conn)
            check = destination_conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RecoveryError(RECOVERY_INCOMPLETE, "SQLite 恢复副本完整性校验失败")
            row = destination_conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()
            schema_version = int(row[0]) if row and row[0] is not None else None
        finally:
            destination_conn.close()
            source_conn.close()
        db_at = _now_ms()
        files: dict[str, Any] = {
            "database": {
                "path": db_target.name,
                "size": db_target.stat().st_size,
                "sha256": _sha256_file(db_target),
                "snapshot_at_ms": db_at,
                "schema_version": schema_version,
            }
        }
        # DB 快照与 JSON 复制必须落在同一个容差窗口内才叫"同 epoch"。
        epoch = RecoveryEpoch(**{**epoch.as_dict(),
                                 "snapshot_finished_ms": db_at,
                                 "db_schema_version": schema_version})
        for name in REQUIRED_STATE_FILES + OPTIONAL_STATE_FILES:
            source = data_dir / name
            if source.is_file():
                files[name] = _copy_state_json(source, stage / "state" / name, epoch)
        if not epoch.accepts(db_at):
            raise RecoveryError(EPOCH_MISMATCH, "数据库快照不在同一快照窗口内")
        manifest = {
            "bundle_schema": BUNDLE_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "writer_stopped": True,
            "epoch": epoch.as_dict(),
            "files": files,
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n").encode("utf-8")
        (stage / "manifest.json").write_bytes(manifest_bytes)
        (stage / "manifest.sha256").write_text(
            f"{_sha256_bytes(manifest_bytes)}  manifest.json\n",
            encoding="ascii", newline="\n")
        os.replace(stage, final)
        return final
    except Exception:
        if stage.is_dir() and stage.parent == target_root:
            shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_epoch_bundle(
    bundle_dir: Path, *, expected_environment: str | None = None,
) -> dict[str, Any]:
    """校验同 epoch 恢复包：哈希、白名单、schema、SQLite、epoch 窗口。"""
    bundle = Path(bundle_dir).resolve()
    if not bundle.is_dir() or bundle.is_symlink():
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包目录不存在或是符号链接")
    manifest_path = bundle / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if int(manifest.get("bundle_schema") or 0) != BUNDLE_SCHEMA:
        raise RecoveryError("BUNDLE_SCHEMA_UNSUPPORTED", "不支持的恢复包版本")
    expected_sha = (bundle / "manifest.sha256").read_text(encoding="ascii").split()[0]
    if expected_sha != _sha256_bytes(manifest_bytes):
        raise RecoveryError(RECOVERY_INCOMPLETE, "manifest 哈希不匹配")
    if manifest.get("writer_stopped") is not True:
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包未声明停写一致性")

    epoch = RecoveryEpoch(**_epoch_kwargs(manifest.get("epoch") or {}))
    files = manifest.get("files")
    if not isinstance(files, dict) or "database" not in files:
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包缺少数据库清单")
    missing = sorted(set(REQUIRED_STATE_FILES) - set(files))
    if missing:
        raise RecoveryError(RECOVERY_INCOMPLETE,
                            "恢复包缺少关键成员: " + ", ".join(missing))

    allowed = {"manifest.json", "manifest.sha256"}
    verified: dict[str, Path] = {}
    for key, metadata in files.items():
        if not isinstance(metadata, dict):
            raise RecoveryError(RECOVERY_INCOMPLETE, f"文件清单格式错误: {key}")
        relative = str(metadata.get("path") or "")
        member = (bundle / relative).resolve()
        if bundle not in member.parents and member != bundle:
            raise RecoveryError(RECOVERY_INCOMPLETE, f"恢复包路径越界: {relative}")
        if not member.is_file():
            raise RecoveryError(RECOVERY_INCOMPLETE, f"恢复包成员缺失: {relative}")
        if member.stat().st_size != int(metadata.get("size") or -1):
            raise RecoveryError(RECOVERY_INCOMPLETE, f"文件大小不匹配: {relative}")
        if _sha256_file(member) != str(metadata.get("sha256") or ""):
            raise RecoveryError(RECOVERY_INCOMPLETE, f"文件哈希不匹配: {relative}")
        snapshot_at = metadata.get("snapshot_at_ms")
        if not isinstance(snapshot_at, int) or not epoch.accepts(snapshot_at):
            raise RecoveryError(EPOCH_MISMATCH, f"{relative} 不在同一 epoch 窗口内")
        allowed.add(relative.replace("\\", "/"))
        verified[str(key)] = member
    actual = {path.relative_to(bundle).as_posix()
              for path in bundle.rglob("*") if path.is_file()}
    extras = sorted(actual - allowed)
    if extras:
        raise RecoveryError(RECOVERY_INCOMPLETE,
                            "恢复包包含白名单外文件: " + ", ".join(extras))

    connection = _open_immutable_readonly(verified["database"])
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RecoveryError(RECOVERY_INCOMPLETE, "恢复数据库 quick_check 失败")
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        actual_schema = int(row[0]) if row and row[0] is not None else None
    finally:
        connection.close()
    expected_schema = files["database"].get("schema_version")
    if expected_schema is not None and actual_schema != int(expected_schema):
        raise RecoveryError(RECOVERY_INCOMPLETE, "数据库迁移版本与清单不一致")
    if expected_environment and epoch.environment != expected_environment:
        raise RecoveryError("ENVIRONMENT_MISMATCH",
                            f"恢复包环境不匹配: {epoch.environment}")
    return {
        "bundle": str(bundle),
        "schema": BUNDLE_SCHEMA,
        "epoch": epoch.as_dict(),
        "environment": epoch.environment,
        "profile": epoch.profile,
        "account_ref": epoch.account_ref,
        "database_schema": actual_schema,
        "files": len(verified),
        "span_ms": epoch.snapshot_finished_ms - epoch.snapshot_started_ms,
    }


def _epoch_kwargs(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包缺少 epoch")
    return {
        "epoch_id": str(payload.get("epoch_id") or ""),
        "snapshot_started_ms": int(payload.get("snapshot_started_ms") or 0),
        "snapshot_finished_ms": int(payload.get("snapshot_finished_ms") or 0),
        "environment": str(payload.get("environment") or ""),
        "profile": str(payload.get("profile") or ""),
        "account_ref": str(payload.get("account_ref") or ""),
        "runtime_instance_id": str(payload.get("runtime_instance_id") or ""),
        "generation_id": str(payload.get("generation_id") or ""),
        "db_schema_version": payload.get("db_schema_version"),
        "tolerance_ms": int(payload.get("tolerance_ms") or DEFAULT_EPOCH_TOLERANCE_MS),
    }


def drill_epoch_bundle(
    bundle_dir: Path, target_dir: Path, *, execute: bool = False,
    require_independent_volume: bool = False,
    expected_environment: str | None = None,
) -> dict[str, Any]:
    """恢复演练：校验 → （可选）还原到全新隔离目录 → 复核 → 回执。

    绝不覆盖运行目录或既有文件；同卷演练显式标注不是灾难恢复（R21）。
    """
    started = time.perf_counter()
    verified = verify_epoch_bundle(bundle_dir, expected_environment=expected_environment)
    bundle = Path(bundle_dir).resolve()
    target = Path(target_dir).resolve()
    same_volume = bundle.anchor.casefold() == target.anchor.casefold()
    if require_independent_volume and same_volume:
        raise RecoveryError(SAME_VOLUME_NOT_DISASTER_RECOVERY,
                            "恢复目标不在独立卷；同盘恢复演练不能证明灾难恢复能力")
    if target.exists():
        raise RecoveryError("TARGET_EXISTS", f"恢复目标必须是不存在的新目录: {target}")
    if bundle == target or bundle in target.parents or target in bundle.parents:
        raise RecoveryError("TARGET_NESTED", "恢复目标不能与恢复包互相嵌套")
    notices = [SAME_VOLUME_NOT_DISASTER_RECOVERY] if same_volume else []
    if not execute:
        return {**verified, "status": "dry_run", "target": str(target),
                "same_volume": same_volume, "notices": notices,
                "reconcile_required": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.restore-{os.getpid()}"
    if stage.exists():
        raise RecoveryError("TARGET_EXISTS", f"恢复暂存目录已存在: {stage}")
    stage.mkdir(parents=False)
    try:
        (stage / "data").mkdir()
        (stage / "config").mkdir()
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        for key, metadata in manifest["files"].items():
            source = (bundle / str(metadata["path"])).resolve()
            destination = (stage / "data" / source.name) if key != "database" \
                else (stage / "data" / "agent_trader.sqlite")
            shutil.copy2(source, destination)
        check = _open_immutable_readonly(stage / "data" / "agent_trader.sqlite")
        try:
            if str(check.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                raise RecoveryError(RECOVERY_INCOMPLETE, "恢复后数据库 quick_check 失败")
        finally:
            check.close()
        receipt = {
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest_sha256": _sha256_file(bundle / "manifest.json"),
            "epoch_id": verified["epoch"]["epoch_id"],
            "environment": verified["environment"],
            "database_schema": verified["database_schema"],
            "same_volume": same_volume,
            "notices": notices,
            # 恢复完成 **不等于** 可以开仓：门禁仍在 RECONCILE_REQUIRED。
            "entry_gate": RECONCILE_REQUIRED_BEFORE_ENTRY,
        }
        _atomic_write_json(stage / "restore_receipt.json", receipt)
        os.replace(stage, target)
    except Exception:
        if stage.is_dir() and stage.parent == target.parent:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return {**verified, "status": "restored", "target": str(target),
            "same_volume": same_volume, "notices": notices,
            "reconcile_required": True,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3)}


def recovery_plan(paths: RuntimePaths, *, metadata_only: bool = False) -> dict[str, Any]:
    """恢复计划（只读）：证明"同盘备份 ≠ 灾难恢复"并给出所需步骤。

    ``metadata_only=True`` 时 **不读任何状态文件内容**（只做存在性判断），供
    对真实运行目录做只读盘点——真实 ``data/`` 的内容不属于可读范围。
    """
    data_dir = paths.data_dir
    if metadata_only:
        names = REQUIRED_STATE_FILES + OPTIONAL_STATE_FILES
        present = [name for name in names if (data_dir / name).is_file()]
        missing = [name for name in REQUIRED_STATE_FILES if name not in present]
        health = {
            "checked": list(names),
            "present": present,
            "missing_required": missing,
            "problems": [RECOVERY_INCOMPLETE] if missing else [],
            "new_entry_allowed": not missing,
            "content_inspected": False,
        }
    else:
        health = state_json_health(data_dir)
        health["content_inspected"] = True
    db_present = paths.db_file.is_file()
    return {
        "state_base": str(paths.state_base),
        "state_root": str(paths.state_root),
        "profile": paths.profile,
        "database": str(paths.db_file),
        "database_present": db_present,
        "backups_dir": str(paths.backups_dir),
        "recovery_bundles_dir": str(paths.recovery_bundles_dir),
        "bundles_dir_volume": paths.recovery_bundles_dir.anchor.casefold(),
        "state_volume": paths.state_base.anchor.casefold(),
        # 同盘：恢复包与状态根在同一卷 → 卷故障会同时丢两者。
        "same_volume_as_state": (paths.recovery_bundles_dir.anchor.casefold()
                                 == paths.state_base.anchor.casefold()),
        "disaster_recovery_requires_independent_volume": True,
        "state_health": health,
        "required_state_files": list(REQUIRED_STATE_FILES),
        "optional_state_files": list(OPTIONAL_STATE_FILES),
        "steps": [
            "1. 正常停机并确认单实例锁无存活 PID（check_writer_stopped）",
            "2. state_json_health：任一关键 JSON 损坏即停新仓，不得用坏状态建包",
            "3. create_epoch_bundle：DB+JSON 同一 epoch 窗口，暂存后原子发布",
            "4. 把恢复包复制到**独立卷**（同卷演练只证明流程，不证明灾难恢复，R21）",
            "5. drill_epoch_bundle(require_independent_volume=True) 还原到全新目录并 quick_check",
            "6. 启动后门禁 = RECONCILE_REQUIRED：先全量对账并提交结构化证据",
            "7. 对账证据通过后才允许新开仓（F14：不得恢复完就直接开仓）",
        ],
    }


# --------------------------------------------------------------------------
# 恢复后先对账再开仓（F14）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryGateDecision:
    allowed: bool
    reason_codes: tuple[str, ...]
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason_codes": list(self.reason_codes),
                "state": self.state}


@dataclass
class RecoveryGate:
    """恢复门禁：恢复完成后必须提交结构化对账证据才允许新开仓。

    证据必须是显式 Mapping（与 K05 ``resolve_incident`` 同口径，fail-closed）：

    .. code-block:: text

        {"observed_at_ms": int, "positions_verified": true,
         "open_orders_verified": true, "account_ref": "...",
         "runtime_instance_id": "..."}

    缺字段 → ``EVIDENCE_MISSING``；类型混淆/裸布尔 → ``EVIDENCE_TYPE_INVALID``；
    时间戳过旧或来自未来 → ``EVIDENCE_STALE``。**不伪造**：证据不足即保持阻断。
    """

    path: Path
    state: str = GATE_NOT_INSTALLED
    epoch_id: str = ""
    reconciled_at_ms: int | None = None
    account_ref: str = ""
    runtime_instance_id: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    evidence_ttl_ms: int = DEFAULT_EVIDENCE_TTL_MS

    @classmethod
    def load(cls, path: Path) -> "RecoveryGate":
        payload = None
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = None
        if not isinstance(payload, dict):
            return cls(path=Path(path))
        return cls(
            path=Path(path),
            state=str(payload.get("state") or GATE_NOT_INSTALLED),
            epoch_id=str(payload.get("epoch_id") or ""),
            reconciled_at_ms=payload.get("reconciled_at_ms"),
            account_ref=str(payload.get("account_ref") or ""),
            runtime_instance_id=str(payload.get("runtime_instance_id") or ""),
            evidence=payload.get("evidence") or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1, "state": self.state, "epoch_id": self.epoch_id,
            "reconciled_at_ms": self.reconciled_at_ms,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "evidence": self.evidence,
        }

    def save(self) -> None:
        _atomic_write_json(self.path, self.as_dict())

    def require_reconcile(self, *, epoch_id: str, account_ref: str = "",
                          runtime_instance_id: str = "") -> None:
        """恢复/接管后进入"必须先对账"状态（清空旧证据）。"""
        self.state = "RECONCILE_REQUIRED"
        self.epoch_id = epoch_id
        self.reconciled_at_ms = None
        self.account_ref = account_ref
        self.runtime_instance_id = runtime_instance_id
        self.evidence = {}
        self.save()

    def before_entry(self, *, now_ms: int | None = None) -> RecoveryGateDecision:
        """新开仓前的唯一判定入口。"""
        now = now_ms if now_ms is not None else _now_ms()
        if self.state == GATE_NOT_INSTALLED:
            # 从未恢复过：门禁尚未安装，不阻断（也不伪造"已对账"）。
            return RecoveryGateDecision(True, (GATE_NOT_INSTALLED,), self.state)
        if self.state != "CLEARED":
            return RecoveryGateDecision(
                False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
        if self.reconciled_at_ms is None:
            return RecoveryGateDecision(
                False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
        if now - int(self.reconciled_at_ms) > self.evidence_ttl_ms:
            return RecoveryGateDecision(False, (EVIDENCE_STALE,), self.state)
        return RecoveryGateDecision(True, (GATE_CLEARED,), self.state)

    def complete_reconcile(
        self, evidence: Any, *, now_ms: int | None = None,
        account_ref: str = "", runtime_instance_id: str = "",
        untrusted_clock: bool = False,
    ) -> RecoveryGateDecision:
        """提交对账证据；只有结构化且新鲜的证据才解除阻断。"""
        now = now_ms if now_ms is not None else _now_ms()
        if untrusted_clock:
            return RecoveryGateDecision(False, ("CLOCK_UNTRUSTED",), self.state)
        if not isinstance(evidence, Mapping):
            return RecoveryGateDecision(False, (EVIDENCE_MISSING,), self.state)
        if "observed_at_ms" not in evidence:
            return RecoveryGateDecision(False, (EVIDENCE_MISSING,), self.state)
        observed = evidence.get("observed_at_ms")
        if isinstance(observed, bool) or not isinstance(observed, int):
            return RecoveryGateDecision(False, (EVIDENCE_TYPE_INVALID,), self.state)
        for key in ("positions_verified", "open_orders_verified"):
            if key not in evidence:
                return RecoveryGateDecision(False, (EVIDENCE_MISSING,), self.state)
            if evidence.get(key) is not True:
                # 裸布尔之外的形状（字符串真值等）一律类型错误；False 即未完成。
                if not isinstance(evidence.get(key), bool):
                    return RecoveryGateDecision(False, (EVIDENCE_TYPE_INVALID,), self.state)
                return RecoveryGateDecision(False, (RECONCILE_REQUIRED_BEFORE_ENTRY,),
                                            self.state)
        if observed > now + 1000 or now - observed > self.evidence_ttl_ms:
            return RecoveryGateDecision(False, (EVIDENCE_STALE,), self.state)
        self.state = "CLEARED"
        self.reconciled_at_ms = now
        self.account_ref = account_ref or self.account_ref
        self.runtime_instance_id = runtime_instance_id or self.runtime_instance_id
        self.evidence = {
            "observed_at_ms": observed,
            "positions_verified": True,
            "open_orders_verified": True,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
        }
        self.save()
        return RecoveryGateDecision(True, (GATE_CLEARED,), self.state)
