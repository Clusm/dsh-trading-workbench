"""发布清单：为一次运行生成不含凭据的可复核版本指纹。"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml


SAFE_CONFIG_FILES = (
    "config/settings.yaml",
    "config/decision_policy.yaml",
    "config/agents.yaml",
    "config/risk_learning.yaml",
    "config/risk_profile.lock.json",
    "config/windows_service.json",
)
#: 实际部署依赖锁必须纳入指纹：只哈希范围约束文件会让“锁文件被改”不可见
#: （K01 ``hash_dependencies`` 的文件级落地；与 runtime_identity 同名单）。
DEPENDENCY_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-lock.txt",
    "requirements-dev-lock.txt",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
)
#: K01 策略卡来源文件：改卡即改身份，必须进入 ``strategy_hash``。
STRATEGY_CARD_FILES = ("knowledge/strategy_cards.json",)

#: 身份采集失败时的显式阻断原因码（宁可不可证明，也不静默生成残缺身份）。
IDENTITY_COLLECT_FAILED = "IDENTITY_COLLECT_FAILED"

#: ``dependency_comparison_status`` 取值。
DEPENDENCY_STATUS_MATCH = "MATCH"
DEPENDENCY_STATUS_MISMATCH = "MISMATCH"
DEPENDENCY_STATUS_UNAVAILABLE = "UNAVAILABLE"


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


def _direct_yaml(root: Path, relative: str) -> dict[str, Any]:
    """直读 YAML 配置；**不经过 ``config_loader``**，因此不会触发 ``load_dotenv``。

    缺失文件、非法 YAML 或非 Mapping 载荷一律返回空字典：身份采集是
    best-effort 证据，失败由 ``identity_blockers`` 表达，而不是让清单构建抛出。
    """
    target = root / relative
    if not target.is_file():
        return {}
    try:
        payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    # ---- U15 追加字段：全部带默认值，旧 ``asdict()`` 消费者用 ``.get(...)``
    # ---- 读取时不会因为缺键而失败（``write_manifest`` 只是多写几个键）。
    #: 代码工件哈希（与 ``content_hash`` 同源，供 K01 身份字段名直接消费）。
    artifact_hash: str = ""
    effective_config_hash: str = ""
    model_route_hash: str = ""
    runtime_instance_id: str = ""
    strategy_cards_hash: str = ""
    dependency_comparison_status: str = DEPENDENCY_STATUS_UNAVAILABLE
    git_dirty: bool = False
    identity_blockers: tuple[str, ...] = ()
    release_id_overridden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dependency_comparison_status(root: Path, identity_module: Any) -> str:
    """声明（``DEPENDENCY_FILES``）与实际必需锁文件的对照结论。

    ``MATCH`` = 身份模块要求的每一个部署锁文件都在本清单的声明集合里且确实存在，
    即 ``dependency_hash`` 真的覆盖了这些锁；``MISMATCH`` = 身份模块要求了清单
    未声明的锁文件（两份名单已漂移，锁文件改动会漏出指纹，指纹报告 #5 的复发
    形态）；``UNAVAILABLE`` = 必需锁文件缺失（身份不可证明）。
    """
    required = tuple(getattr(identity_module, "DEPENDENCY_LOCK_FILES", ()) or ())
    if not required:
        return DEPENDENCY_STATUS_UNAVAILABLE
    if any(str(name) not in DEPENDENCY_FILES for name in required):
        return DEPENDENCY_STATUS_MISMATCH
    if not all((root / str(name)).is_file() for name in required):
        return DEPENDENCY_STATUS_UNAVAILABLE
    return DEPENDENCY_STATUS_MATCH


def _resolved_runtime_instance_id(value: str | None) -> str:
    """归一化运行代：占位符 / 空白 / 非字符串 → ``""``（= 缺失）。

    单一事实源在 ``runtime_identity``（:func:`resolve_runtime_instance_id`）。
    该模块不可用时**保守返回空串** —— 宁可不证明，也不把哨兵写进清单
    （U15-FU1：``"unknown"`` 这类值读起来像运行代，实际不是）。
    """
    try:
        from core import runtime_identity as identity_module

        resolved = identity_module.resolve_runtime_instance_id(value)
    except Exception:  # noqa: BLE001 - 采集失败由 IDENTITY_COLLECT_FAILED 表达
        return ""
    return "" if resolved is None else resolved


def _identity_evidence(
    root: Path, *,
    environment: str,
    account_ref: str,
    runtime_instance_id: str | None,
) -> dict[str, Any]:
    """best-effort 采集 K01 身份证据；任何异常都降级为阻断原因码，绝不抛出。

    与 ``runtime_identity`` 的边界一致：不读 ``.env``、不读 ``data/``、不发网络
    请求。settings/agents 由本模块 **直读 YAML** 传入（``config_loader.load_settings``
    会 ``load_dotenv``，清单构建不得经过它），环境变量按 ``env={}`` 传入，因此
    ``environment_overrides`` 不会被 ``.env`` 内容污染。

    U15-FU1：``runtime_instance_id`` 先经 :func:`_resolved_runtime_instance_id`
    归一化 —— 占位符（``"unknown"`` 等）在这里就变成"**没有**运行代"，
    于是既不会被当成合法身份写进清单，也不会被偷偷回填成别的字符串。
    """
    resolved_instance = _resolved_runtime_instance_id(runtime_instance_id)
    fallback = {
        "artifact_hash": "",
        "effective_config_hash": "",
        "model_route_hash": "",
        "runtime_instance_id": resolved_instance,
        "strategy_cards_hash": "",
        "dependency_comparison_status": DEPENDENCY_STATUS_UNAVAILABLE,
        "git_dirty": False,
        "identity_blockers": (IDENTITY_COLLECT_FAILED,),
    }
    try:
        from core import runtime_identity as identity_module

        bundle = identity_module.collect_runtime_identity(
            root,
            environment=environment,
            # 空串 → None（"明确没有运行代"）：采集方据此登记
            # RUNTIME_GENERATION_UNPROVEN，而不是把空值当形状错误整份丢弃。
            runtime_instance_id=resolved_instance or None,
            execution_mode=environment,
            declared_account_ref=account_ref,
            env={},
            raw_settings=_direct_yaml(root, "config/settings.yaml"),
            raw_agents=_direct_yaml(root, "config/agents.yaml"),
        )
        identity = bundle.identity
        comparison = _dependency_comparison_status(root, identity_module)
        return {
            "artifact_hash": identity.artifact_hash,
            "effective_config_hash": identity.effective_config_hash,
            "model_route_hash": identity.model_route_hash,
            "runtime_instance_id": identity.runtime_instance_id,
            "strategy_cards_hash": identity.strategy_hash,
            "dependency_comparison_status": comparison,
            "git_dirty": bool(bundle.git_state.dirty),
            "identity_blockers": tuple(bundle.blockers),
        }
    except Exception:
        # 单点降级：身份不可证明时清单仍然可写，但阻断原因必须可见。
        return fallback


def build_release_manifest(
    project_root: Path, *, environment: str, account_ref: str | None = None,
    db_file: Path | None = None, egress_id: str | None = None,
    threshold_version: str = "v1", release_id: str | None = None,
    now: str | None = None, runtime_instance_id: str | None = None,
) -> ReleaseManifest:
    """生成当前代码/配置/依赖的确定性指纹，不读 ``.env`` 内容。

    ``runtime_instance_id`` 为 U15 追加的可选入参（默认从
    ``MTA_RUNTIME_INSTANCE_ID`` 取）：调用方（服务）可传入真实运行实例标识，
    使清单里的身份证据与进程一致。

    U15-FU1：**读不出真实运行代时不再记 ``"unknown"``**。缺省、占位符
    （``"unknown"``/``"n/a"``/…）与空白一律归一化为"缺失" —— 清单里的
    ``runtime_instance_id`` 留空，且 ``identity_blockers`` 带
    ``RUNTIME_GENERATION_UNPROVEN``。修复前这两处分别是 ``"unknown"`` 与空
    阻断列表：消费方（``bool(runtime_instance_id)`` 之类的判据）会把它读成
    "身份完整"。**绝不放宽**：这只是把"读起来像值的哨兵"降级为显式缺失。
    """
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
        *STRATEGY_CARD_FILES,
    ) if (root / name).is_file()]
    model_files = [root / name for name in (
        "config/agents.yaml", "config/settings.yaml",
    ) if (root / name).is_file()]
    strategy_hash = _normalised_file_hash(root, strategy_files)
    model_hash = _normalised_file_hash(root, model_files)
    strategy_cards_hash = _normalised_file_hash(
        root, [root / name for name in STRATEGY_CARD_FILES])
    schema_version = _schema_version(Path(db_file) if db_file else None)
    safe_account_ref = account_reference(env, account_ref)
    safe_egress_id = str(egress_id or "unknown").strip()[:160]
    git_commit = _git_commit(root)
    evidence = _identity_evidence(
        root,
        environment=env,
        account_ref=str(account_ref or ""),
        runtime_instance_id=runtime_instance_id or os.getenv("MTA_RUNTIME_INSTANCE_ID"),
    )
    # 种子纳入 git 提交 / 账户引用 / 策略卡哈希：三者任一变化都必须换发布号，
    # 否则“同一 release_id 对应两份不同代码或两套策略卡”会被掩盖。
    release_seed = "|".join((
        env, content_hash, dependency_hash, strategy_hash, model_hash,
        str(schema_version), str(threshold_version), safe_egress_id,
        str(git_commit), str(safe_account_ref), str(strategy_cards_hash),
    ))
    safe_release_id = str(release_id or f"rel-{_sha256_bytes(release_seed.encode('utf-8'))[:20]}")[:160]
    return ReleaseManifest(
        manifest_schema=1,
        release_id=safe_release_id,
        generated_at=now or _utc_now(),
        git_commit=git_commit,
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
        artifact_hash=content_hash,
        effective_config_hash=str(evidence.get("effective_config_hash") or ""),
        model_route_hash=str(evidence.get("model_route_hash") or ""),
        # U15-FU1 第二道闸：采集结果再归一化一次，任何降级分支都不会把
        # 哨兵漏进清单字段（缺失 = 空串，可被 `bool(...)` 类判据如实读成"无"）。
        runtime_instance_id=_resolved_runtime_instance_id(
            evidence.get("runtime_instance_id")),
        strategy_cards_hash=str(evidence.get("strategy_cards_hash") or strategy_cards_hash),
        dependency_comparison_status=str(
            evidence.get("dependency_comparison_status") or DEPENDENCY_STATUS_UNAVAILABLE),
        git_dirty=bool(evidence.get("git_dirty")),
        identity_blockers=tuple(evidence.get("identity_blockers") or ()),
        release_id_overridden=release_id is not None,
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
