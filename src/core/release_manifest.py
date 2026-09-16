"""发布清单：为一次运行生成不含凭据的可复核版本指纹。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SAFE_CONFIG_FILES = (
    "config/settings.yaml",
    "config/decision_policy.yaml",
    "config/agents.yaml",
    "config/risk_learning.yaml",
    "config/risk_profile.lock.json",
    "config/windows_service.json",
)
DEPENDENCY_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalised_file_hash(root: Path, paths: Iterable[Path]) -> str:
    """按相对路径和文件内容计算集合指纹，避免平台路径差异。"""
    rows: list[str] = []
    for path in sorted({Path(item).resolve() for item in paths}, key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        rows.append(f"{relative}\0{_sha256_file(path)}")
    return _sha256_bytes("\n".join(rows).encode("utf-8"))


def _safe_source_files(root: Path) -> list[Path]:
    files = [path for path in (root / "src").rglob("*.py") if "__pycache__" not in path.parts]
    # 脚本是运行入口的一部分，但只纳入 Git 已跟踪脚本，避免临时诊断文件
    # 无意间改变生产发布指纹。
    try:
        result = subprocess.run(
            ["git", "ls-files", "scripts/*.py"], cwd=root, check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        files.extend(root / line.strip() for line in result.stdout.splitlines() if line.strip())
    except OSError:
        files.extend(path for path in (root / "scripts").glob("*.py")
                     if "diag_" not in path.name and "probe_" not in path.name)
    return files


def _git_commit(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return "unknown"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unknown"


def _schema_version(db_file: Path | None) -> int:
    if db_file is None or not db_file.exists():
        return 0
    resolved = db_file.resolve()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0) if row else 0
    except (OSError, sqlite3.Error, ValueError):
        return 0
    finally:
        if connection is not None:
            connection.close()


def account_reference(environment: str, account_ref: str | None) -> str:
    """保留已脱敏账户引用，否则只保存不可逆短哈希。"""
    env = str(environment or "unknown").lower()[:32]
    value = str(account_ref or "").strip()
    if value and value.startswith(f"{env}:") and len(value) <= 160:
        return value
    if not value:
        return f"{env}:unknown"
    return f"{env}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class ReleaseManifest:
    """发布报告需要的安全元数据；严禁在这里保存原始密钥或请求头。"""

    manifest_schema: int
    release_id: str
    generated_at: str
    git_commit: str
    content_hash: str
    dependency_hash: str
    config_hashes: dict[str, str]
    strategy_config_hash: str
    model_config_hash: str
    schema_version: int
    environment: str
    account_ref: str
    egress_id: str
    threshold_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_release_manifest(
    project_root: Path, *, environment: str, account_ref: str | None = None,
    db_file: Path | None = None, egress_id: str | None = None,
    threshold_version: str = "v1", release_id: str | None = None,
    now: str | None = None,
) -> ReleaseManifest:
    """生成当前代码/配置/依赖的确定性指纹，不读 ``.env`` 内容。"""
    root = Path(project_root).resolve()
    env = str(environment or "unknown").lower()[:32]
    config_hashes = {
        relative: _sha256_file(root / relative)
        for relative in SAFE_CONFIG_FILES
        if (root / relative).is_file()
    }
    content_hash = _normalised_file_hash(root, _safe_source_files(root))
    dependency_paths = [root / relative for relative in DEPENDENCY_FILES]
    dependency_hash = _normalised_file_hash(root, dependency_paths)
    strategy_files = [root / name for name in (
        "config/decision_policy.yaml", "config/risk_learning.yaml",
    ) if (root / name).is_file()]
    model_files = [root / name for name in (
        "config/agents.yaml", "config/settings.yaml",
    ) if (root / name).is_file()]
    strategy_hash = _normalised_file_hash(root, strategy_files)
    model_hash = _normalised_file_hash(root, model_files)
    schema_version = _schema_version(Path(db_file) if db_file else None)
    safe_account_ref = account_reference(env, account_ref)
    safe_egress_id = str(egress_id or "unknown").strip()[:160]
    release_seed = "|".join((
        env, content_hash, dependency_hash, strategy_hash, model_hash,
        str(schema_version), str(threshold_version), safe_egress_id,
    ))
    safe_release_id = str(release_id or f"rel-{_sha256_bytes(release_seed.encode('utf-8'))[:20]}")[:160]
    return ReleaseManifest(
        manifest_schema=1,
        release_id=safe_release_id,
        generated_at=now or _utc_now(),
        git_commit=_git_commit(root),
        content_hash=content_hash,
        dependency_hash=dependency_hash,
        config_hashes=config_hashes,
        strategy_config_hash=strategy_hash,
        model_config_hash=model_hash,
        schema_version=schema_version,
        environment=env,
        account_ref=safe_account_ref,
        egress_id=safe_egress_id,
        threshold_version=str(threshold_version)[:64],
    )


def write_manifest(path: Path, manifest: ReleaseManifest) -> None:
    """原子写出清单；调用方应把目标放在 data/ 或发布工件目录。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    temporary.replace(target)
