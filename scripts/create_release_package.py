"""创建并自检"不含运行状态与凭据"的可复核发布 ZIP 包。

发布包是**代码部署工件**，不是恢复包：不包含 ``data/``、``.env``、日志、虚拟环境、
缓存、密钥、运行状态或未跟踪的临时诊断脚本。目标文件已存在时直接失败，避免静默
覆盖已有发布工件。

两种包类型（``--kind``）：

``runtime``（默认）
    可部署运行工件：启动/托管入口、健康守望、实盘预检、发布门禁报告、迁移与恢复
    工具、策略卡与全部有效配置、运行所需源码与模板、发布包自检入口。**不含**测试、
    CI 配置、冒烟/诊断/维护脚本。
``source``
    在 ``runtime`` 之上补齐 ``tests/``、``.github/`` 与其余脚本（冒烟、门禁、诊断、
    维护），用于复现完整仓库行为与重建环境。

三类硬约束（任何一条不满足即构建失败，绝不静默放行）：

1. **不漏包**：仓库中每个已跟踪文件都必须被显式归类（入包 / 排除并写明理由），
   未归类文件即失败；``src/`` 与 ``scripts/`` **不按后缀过滤**，模板、``PROVENANCE.md``、
   ``.bat`` 等非 ``.py`` 运行资产同样纳入。
2. **凭据与状态不入包**：路径规则（``data/``、``.env``、日志、SQLite、密钥、缓存、
   虚拟环境）与内容规则（复用仓库唯一凭据扫描器 ``scripts/scan_tracked_secrets.py``）
   双重拦截。
3. **解压后可自检**：``--verify`` 在解压目录内离线运行——校验哈希清单、未登记文件、
   必需运行资产、相对文档链接、Python 语法与 SCM 配置语义，全部通过才返回 ``0``。
   ``--self-check`` 连"构建→解压→自检"一起跑，是发布前的最低证据。

运行（项目根目录）：

    .venv\\Scripts\\python.exe scripts\\create_release_package.py ^
      --manifest data\\release_manifest.json --kind runtime --out dist\\mta.zip
    .venv\\Scripts\\python.exe scripts\\create_release_package.py --self-check --kind runtime
    .venv\\Scripts\\python.exe scripts\\create_release_package.py --verify <解压目录>
"""
from __future__ import annotations

#: 自检可能在**只读或已封存的发布目录**里运行：写入 ``__pycache__`` 会让
#: ``package_manifest.json`` 的"未登记文件"校验在第二次运行时自我污染（同一目录
#: 第一次自检通过、第二次失败）。这里在导入任何被扫描模块**之前**关掉字节码写入，
#: 子进程（读取包内声明清单/SCM 语义）另行通过 ``PYTHONDONTWRITEBYTECODE=1`` 约束。
import sys as _sys

_sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
SRC_DIRNAME = "src"
MANIFEST_MEMBER = "package_manifest.json"
RELEASE_MANIFEST_MEMBER = "release_manifest.json"
PACKAGE_SCHEMA = 2

KIND_RUNTIME = "runtime"
KIND_SOURCE = "source"
KINDS = (KIND_RUNTIME, KIND_SOURCE)

#: 排除清单里给操作者看的说明（``excluded`` 字段保持既有字符串形态）。
EXCLUDED_SUMMARY = [
    ".env", ".env.local", ".env.production", "data/", "logs/", ".venv/", "cache/",
    "dist/", "*.log", "*.sqlite", "*.pem", "*.key", "docs/dispatch/reviews/",
    "docs/dispatch/patches/", "untracked diagnostics",
]

#: 目录名出现在路径**任意一层**即排除（缓存/版本库/依赖，任何深度都不该发布）。
EXCLUDED_DIR_NAMES: dict[str, str] = {
    ".venv": "本机虚拟环境，由部署方按 requirements-lock 重建",
    "venv": "本机虚拟环境，由部署方重建",
    "__pycache__": "可再生的字节码缓存",
    ".git": "版本库对象，不是部署工件",
    ".ruff_cache": "静态检查缓存",
    ".pytest_cache": "测试缓存",
    ".mypy_cache": "类型检查缓存",
    "node_modules": "前端依赖，不参与运行时",
}
#: 只在**项目根第一层**按名字排除的目录。
#:
#: 必须限定第一层：``data`` 同样是**合法源码目录名**（``src/data/`` 是数据接入层）。
#: 旧实现把 ``data`` 当成任意层排除，结果把 ``src/data/*.py``（限流、传输、行情查询）
#: 与 ``src/dashboard/templates/*.html`` 一起静默丢出发布包——见
#: ``docs/dispatch/patches/U10a`` 的对照证据。任何"按目录名排除"的新规则都必须
#: 明确写清作用层数，否则会重演这一类漏包。
EXCLUDED_TOPLEVEL_DIR_NAMES: dict[str, str] = {
    "data": "运行时状态/账本/备份，禁止进入代码包",
    "logs": "运行日志目录",
    "cache": "运行缓存目录",
    "dist": "既有发布工件输出目录",
    "build": "构建产物目录",
    "htmlcov": "覆盖率报告",
}
#: 文件名精确匹配即排除（真实凭据文件）。
EXCLUDED_FILE_NAMES: dict[str, str] = {
    ".env": "真实凭据文件，只允许 .env.example 进包",
    ".env.local": "真实凭据文件",
    ".env.production": "真实凭据文件",
    ".env.bak": "凭据备份",
}
#: 后缀匹配即排除（日志/数据库/密钥/工件）。
EXCLUDED_SUFFIXES: dict[str, str] = {
    ".log": "运行日志",
    ".sqlite": "运行账本数据库",
    ".sqlite-wal": "运行账本 WAL",
    ".sqlite-shm": "运行账本 WAL 索引",
    ".db": "运行数据库",
    ".pem": "私钥/证书",
    ".key": "私钥",
    ".p12": "密钥库",
    ".pfx": "密钥库",
    ".pyc": "字节码",
    ".zip": "既有发布/恢复工件",
    ".bak": "备份文件",
}
#: 目录前缀排除（评审证据与流程台账：不是交付物，且刻意引用凭据模式）。
#:
#: 注意：机器可读编排台账 ``docs/无人值守升级任务清单_2026-09-19.json`` **不在此列**，
#: 因此它随 ``docs/`` 进包。原因是包内多篇文档（``docs/contracts/README.md``、
#: ``docs/多Agent执行分派手册_2026-09-20.md``、执行规格等）以 markdown 相对链接引用它，
#: 把它排除会让构建期 ``doc_links_resolve`` 报断链——而其中一篇在**不得改动的**
#: ``docs/contracts/`` 内。它同时命中仓库扫描器的 KI04 范围豁免，因此包内自检**如实列出**
#: 它是免内容扫描成员（``credentials_absent.detail.repo_exempt_files``），不是静默跳过；
#: ``scripts/smoke_u10a_release_package.py`` 对该"如实列出"有断言。要把它移出发布包，
#: 必须由 I 同批改掉上述 4 篇文档的链接，属跨任务动作。
EXCLUDED_PATH_PREFIXES: dict[str, str] = {
    "docs/dispatch/reviews/": "评审证据归档，属流程记录而非交付物（且刻意引用凭据模式）",
    "docs/dispatch/patches/": "待应用补丁文本，未合入内容不得随部署工件分发",
    "docs/dispatch/RESUME.md": "编排状态记录，只由 I 维护",
    "docs/dispatch/w0_runtime_inventory.md": "W0 只读盘点快照，属流程记录",
    "docs/dispatch/交接说明_2026-09-21.md": "会话交接记录，属流程记录",
}

#: 运行/部署必需的非脚本资产（与仓库声明清单交叉核对，见 REQUIRED_CONFIGS）。
REQUIRED_TOP_LEVEL: dict[str, str] = {
    "README.md": "项目边界与快速启动",
    "CONTRIBUTING.md": "维护与安全边界",
    ".env.example": "脱敏有效配置模板（部署方据此创建 .env）",
    ".gitignore": "部署目录自带运行时目录忽略规则",
    "pyproject.toml": "静态检查与工具配置",
    "requirements.txt": "依赖声明（允许升级范围）",
    "requirements-lock.txt": "部署依赖锁（可复现构建）",
    "requirements-dev.txt": "开发依赖声明",
    "requirements-dev-lock.txt": "开发依赖锁（CI 依赖）",
}
#: 必须与 ``src/core/release_manifest.py:SAFE_CONFIG_FILES`` 一致（构建期交叉核对）。
REQUIRED_CONFIGS: tuple[str, ...] = (
    "config/settings.yaml",
    "config/decision_policy.yaml",
    "config/agents.yaml",
    "config/risk_learning.yaml",
    "config/risk_profile.lock.json",
    "config/windows_service.json",
)
#: 必须覆盖 ``src/core/release_manifest.py:STRATEGY_CARD_FILES``（构建期交叉核对）。
REQUIRED_KNOWLEDGE: tuple[str, ...] = (
    "knowledge/strategy_cards.json",
    "knowledge/backup_strategy_cards.json",
)
#: 运行锚点源码：缺任一个都不能算"可运行发布包"。
REQUIRED_RUNTIME_SOURCES: tuple[str, ...] = (
    "src/core/config_loader.py",
    "src/core/release_manifest.py",
    "src/core/runtime_paths.py",
    "src/core/windows_service.py",
    "src/dashboard/app.py",
)
#: 必须被 ``release_manifest.DEPENDENCY_FILES`` 覆盖（构建期交叉核对）。
REQUIRED_DEPENDENCY_FILES: tuple[str, ...] = (
    "requirements.txt",
    "requirements-lock.txt",
    "requirements-dev.txt",
    "requirements-dev-lock.txt",
    "pyproject.toml",
)

#: 运行/部署必需脚本（显式清单；**先于**前缀规则判定，优先级最高）。
RUNTIME_SCRIPTS: dict[str, str] = {
    "scripts/run_web.py": "统一前台/SCM 启动入口",
    "scripts/windows_service_host.py": "SCM 宿主入口（WindowsServiceConfig.scm_bin_path 指向它）",
    "scripts/start_web.bat": "Windows 诊断/隔离启动批处理（手册 §2 登记）",
    "scripts/validate_windows_service_plan.py": "SCM 安装前只读校验（手册 §2）",
    "scripts/health_watch.py": "独立只读健康守望（手册 §10）",
    "scripts/preflight_live_account.py": "实盘只读预检（文档索引·脚本入口）",
    "scripts/report_release_readiness.py": "严格发布门禁报告（手册 §3）",
    "scripts/report_demo_acceptance.py": "Demo 观察窗口只读汇总（手册 §3）",
    "scripts/create_recovery_bundle.py": "停写一致性恢复包（手册 §5）",
    "scripts/restore_recovery_bundle.py": "只写新目录的恢复工具（手册 §5）",
    "scripts/ops/verify_recovery_plan.py": "恢复计划核验（U13 ops 入口）",
    "scripts/ops/verify_runtime_paths.py": "RuntimePaths 核验（U13 ops 入口）",
    "scripts/verify_profile_lock.py": "风险档锁核验（手册 §1/§2 常态判据）",
    "scripts/verify_dependency_lock.py": "部署依赖锁核验（代码维护规范 §9）",
    "scripts/smoke_db_migrations.py": "迁移版本与向前兼容校验（迁移工具）",
    "scripts/create_release_package.py": "发布包生成与解压自检（本任务）",
    "scripts/scan_tracked_secrets.py": "发布包内容凭据扫描依赖（自检必需）",
}
#: 明确的 source-only 顶层文件（非运行必需）。
SOURCE_ONLY_TOP_LEVEL: dict[str, str] = {
    ".editorconfig": "编辑器格式约定，非运行必需",
}
#: source-only 的目录前缀。
SOURCE_ONLY_PATH_PREFIXES: dict[str, str] = {
    "tests/": "测试与独立验收夹具，保留在 source 包",
    ".github/": "CI 配置，保留在 source 包",
}
#: ``scripts/`` 下按名称前缀判定为 source-only 的维护工具（不在 RUNTIME_SCRIPTS 内）。
SOURCE_ONLY_SCRIPT_PREFIXES: dict[str, str] = {
    "smoke_": "隔离冒烟，开发/验收期工具",
    "test_": "脚本级自检，开发期工具",
    "check_": "质量门禁（依赖 ruff 等开发依赖）",
    "probe_": "显式在线查询工具，不进入默认 CI",
    "verify_": "显式在线验证工具（verify_*_live / 供应商协议检查）",
    "diag_": "本地诊断工具",
    "cleanup_": "有副作用的一次性维护工具",
    "repair_": "有副作用的一次性修复工具",
    "benchmark_": "性能基线测量，非运行必需",
    "export_": "事故夹具导出工具，开发期使用",
    "run_offline": "离线回归统一入口，开发/CI 期使用",
    "run_phase": "阶段脚手架，历史入口",
    "run_review": "复盘任务手工入口",
    "live_check": "在线只读查询工具",
    "start_web_detached": "过渡脱离启动方式（手册 §9 标注为过渡，不等同无人值守托管）",
    "run_web_service": "仅隔离演练的旧服务包装器（手册 §2 明示不得与 SCM 并行）",
    "_": "本地临时脚本，按约定不入包",
}

#: 未分类文件 → 构建失败（防"漏包"的兜底闸门）。
_CLASS_EXCLUDED = "excluded"
_CLASS_RUNTIME = "runtime"
_CLASS_SOURCE_ONLY = "source-only"

#: 运行资产根目录：这里的每个已跟踪文件都**不得**被路径排除规则误伤。
RUNTIME_ASSET_ROOTS = ("src/", "config/", "knowledge/")


class ReleasePackageError(ValueError):
    """发布包构建/自检的 fail-closed 错误。"""


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _subprocess_env() -> dict[str, str]:
    """子进程环境：禁止写字节码，避免自检污染被测目录。"""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _tracked_files(root: Path) -> set[str]:
    """返回 Git 索引中的相对路径。

    必须用 ``-z`` 输出：默认输出会对非 ASCII 路径做 C 风格转义并加引号
    （``"docs/\\346\\226\\207..."``）；按行取值会得到转义后的伪路径，它们
    **命中不了任何分类规则**，于是构建以"存在未归类文件"**硬失败**（fail-closed，
    不是静默少文件）。

    更正（U10a r2，独立复核反证）：旧打包器丢中文名文档的根因是 ``SAFE_DOCS``
    白名单只有 5 篇，与 ``-z`` 无关——给旧实现补 ``-z`` 前后成员集合为 0 差异。
    ``-z`` 是**新设计**（全量归类）下的必需项，缺了会让构建失败。
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, check=False, capture_output=True,
            env=_subprocess_env(),
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {
        item.decode("utf-8") for item in result.stdout.split(b"\0") if item
    }


def _filesystem_inventory(root: Path) -> set[str]:
    """无 Git 时的兜底盘点（只用于夹具/无版本库目录）。"""
    files: set[str] = set()
    for base in ("src", "scripts", "config", "docs", "knowledge", "tests", ".github"):
        directory = root / base
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if any(part in EXCLUDED_DIR_NAMES for part in Path(relative).parts):
                continue
            files.add(relative)
    for path in root.iterdir():
        if path.is_file():
            files.add(path.name)
    return files


def _excluded_reason(relative: str) -> str | None:
    """路径规则排除判定；命中即返回可读理由。"""
    parts = Path(relative).parts
    for part in parts:
        if part in EXCLUDED_DIR_NAMES:
            return EXCLUDED_DIR_NAMES[part]
    if parts and len(parts) > 1 and parts[0] in EXCLUDED_TOPLEVEL_DIR_NAMES:
        return EXCLUDED_TOPLEVEL_DIR_NAMES[parts[0]]
    name = parts[-1] if parts else relative
    if name in EXCLUDED_FILE_NAMES:
        return EXCLUDED_FILE_NAMES[name]
    lowered = name.lower()
    for suffix, reason in EXCLUDED_SUFFIXES.items():
        if lowered.endswith(suffix):
            return reason
    for prefix, reason in EXCLUDED_PATH_PREFIXES.items():
        if relative == prefix or relative.startswith(prefix):
            return reason
    return None


def _source_only_script_reason(relative: str) -> str | None:
    """返回 ``scripts/`` 文件的 **显式** source-only 理由；未登记时返回 ``None``。

    这里刻意**不再**提供"默认 source-only"的兜底：``scripts/`` 下新增文件若既不
    在 ``RUNTIME_SCRIPTS``、也不匹配任何 ``SOURCE_ONLY_SCRIPT_PREFIXES``，即属
    "未归类"，构建会硬失败并要求作者显式登记（维护规范 §11.1）。否则新加的启动
    入口会被**静默降级**成 source-only，runtime 包悄悄缺文件——这正是 U10a 防护的
    失效模式。
    """
    name = Path(relative).name
    for prefix, reason in SOURCE_ONLY_SCRIPT_PREFIXES.items():
        if name.startswith(prefix):
            return reason
    return None


def _classify(relative: str) -> tuple[str, str]:
    """把单个相对路径归类为 ``(class, reason)``。

    判定顺序（前面的优先级高）：排除 → 运行必需脚本 → source-only 前缀 →
    运行资产目录 → 顶层必需文件 → ``scripts/`` 显式前缀 → 未分类。

    ``scripts/`` 分支**只认显式登记**：既不在 ``RUNTIME_SCRIPTS`` 又无前缀理由的
    脚本一律落到"未归类"（构建失败），不允许静默归类。
    """
    reason = _excluded_reason(relative)
    if reason is not None:
        return _CLASS_EXCLUDED, reason
    if relative in RUNTIME_SCRIPTS:
        return _CLASS_RUNTIME, RUNTIME_SCRIPTS[relative]
    if relative in SOURCE_ONLY_TOP_LEVEL:
        return _CLASS_SOURCE_ONLY, SOURCE_ONLY_TOP_LEVEL[relative]
    for prefix, prefix_reason in SOURCE_ONLY_PATH_PREFIXES.items():
        if relative.startswith(prefix):
            return _CLASS_SOURCE_ONLY, prefix_reason
    if relative in REQUIRED_TOP_LEVEL:
        return _CLASS_RUNTIME, REQUIRED_TOP_LEVEL[relative]
    if relative.startswith(("src/", "config/", "knowledge/")):
        return _CLASS_RUNTIME, "运行资产（源码/模板/配置/策略卡）"
    if relative.startswith("docs/"):
        return _CLASS_RUNTIME, "文档（部署与运维需要，且相对链接必须可达）"
    if relative.startswith("scripts/"):
        reason = _source_only_script_reason(relative)
        if reason is not None:
            return _CLASS_SOURCE_ONLY, reason
        return "unclassified", (
            "scripts/ 下未显式登记的脚本（禁止静默归类：请在 RUNTIME_SCRIPTS 登记为"
            "运行入口，或按 SOURCE_ONLY_SCRIPT_PREFIXES 补一条前缀理由）"
        )
    return "unclassified", "未归类（构建失败：必须显式登记入包或排除理由）"


def _inventory(root: Path, tracked: set[str]) -> dict[str, Any]:
    """对仓库盘点逐文件归类；返回清单、缺口与统计。"""
    classes: dict[str, dict[str, str]] = {}
    for relative in sorted(tracked):
        klass, reason = _classify(relative)
        classes[relative] = {"class": klass, "reason": reason}
    unclassified = sorted(k for k, v in classes.items() if v["class"] == "unclassified")
    return {"classes": classes, "unclassified": unclassified}


def packaged_files(inventory: dict[str, Any], *, kind: str) -> list[str]:
    """按包类型展开入包清单（``source`` = ``runtime`` + source-only）。"""
    wanted = {_CLASS_RUNTIME} if kind == KIND_RUNTIME else {_CLASS_RUNTIME, _CLASS_SOURCE_ONLY}
    return sorted(
        relative for relative, info in inventory["classes"].items()
        if info["class"] in wanted
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleasePackageError(f"release manifest 无法读取: {path}") from exc
    if not isinstance(value, dict) or not value.get("release_id"):
        raise ReleasePackageError("release manifest 必须是含 release_id 的 JSON 对象")
    serialized = json.dumps(value, ensure_ascii=False).lower()
    if any(token in serialized for token in ("api_secret", "private_key", "authorization")):
        raise ReleasePackageError("release manifest 包含疑似凭据字段")
    return value


def _secret_allowances() -> dict[tuple[str, int, str], str]:
    """复用仓库唯一凭据扫描器的已登记误报表（避免出现第二套豁免口径）。"""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        from scan_tracked_secrets import _REGISTERED_FALSE_POSITIVES  # noqa: PLC0415
    except Exception:  # pragma: no cover - 扫描器缺失时由调用方 fail-closed
        raise ReleasePackageError(
            "找不到 scripts/scan_tracked_secrets.py：凭据扫描无法证明，拒绝生成/放行发布包"
        ) from None
    return {
        key: allowance.fingerprint
        for key, allowance in _REGISTERED_FALSE_POSITIVES.items()
    }


def _repo_scan_exemptions() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """复用仓库扫描器的**范围豁免**（KI04）：前缀与精确文件。"""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from scan_tracked_secrets import _EXEMPT_FILES, _EXEMPT_PREFIXES  # noqa: PLC0415

    return tuple(_EXEMPT_PREFIXES), tuple(_EXEMPT_FILES)


def _scan_secrets(relative: str, text: str) -> tuple[list[tuple[int, str, str]], bool]:
    """用仓库扫描器返回 ``([(行号, 规则, 指纹)], 是否命中范围豁免)``。

    口径与仓库门禁完全一致：① 已登记误报按 (路径, 行号, 规则) + 行指纹剔除；
    ② KI04 的流程记录范围豁免（``docs/dispatch/reviews/**`` 与台账 JSON）不参与
    扫描。豁免**不是**本模块自定的放宽——它来自仓库唯一扫描器的登记表，改动它
    必须同时改扫描器与台账 KI04，否则这里会立刻如实报红。
    """
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    from scan_tracked_secrets import find_secret_hits  # noqa: PLC0415

    prefixes, exact = _repo_scan_exemptions()
    if relative in exact or relative.startswith(prefixes):
        return [], True
    allowances = _secret_allowances()
    suffix = Path(relative).suffix
    hits: list[tuple[int, str, str]] = []
    for finding in find_secret_hits(text, suffix=suffix):
        registered = allowances.get((relative, finding.line, finding.rule))
        if registered is not None and registered == finding.fingerprint:
            continue
        hits.append((finding.line, finding.rule, finding.fingerprint))
    return hits, False


def discover_inventory(root: Path) -> dict[str, Any]:
    """盘点仓库：优先 Git 索引，无 Git 时退回文件系统扫描。"""
    tracked = _tracked_files(root)
    source = "git-index" if tracked else "filesystem-scan"
    if not tracked:
        tracked = _filesystem_inventory(root)
    inventory = _inventory(root, tracked)
    inventory["source"] = source
    inventory["total"] = len(tracked)
    return inventory


# --------------------------------------------------------------------------
# 必需运行资产与跨清单核对
# --------------------------------------------------------------------------
_CROSS_CHECK_PROGRAM = (
    "import json,sys;"
    "sys.path.insert(0, sys.argv[1]);"
    "from core import release_manifest as rm;"
    "print(json.dumps({"
    "'configs': list(rm.SAFE_CONFIG_FILES),"
    "'deps': list(rm.DEPENDENCY_FILES),"
    "'cards': list(rm.STRATEGY_CARD_FILES)}))"
)


def _declared_runtime_lists(package_root: Path) -> dict[str, list[str]] | None:
    """在**隔离子进程**里读取包内 ``src/core/release_manifest.py`` 的声明清单。

    交叉核对必须使用包内那份代码，而不是构建机的另一份：发布包自己的"运行必需
    文件"口径才是被部署的口径。
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CROSS_CHECK_PROGRAM, str(package_root / SRC_DIRNAME)],
            cwd=package_root, check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_subprocess_env(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return payload if isinstance(payload, dict) else None


def required_runtime_assets(
    package_root: Path, *, enforce_declared: bool | None = None,
) -> tuple[list[str], list[str]]:
    """返回 ``(必需相对路径, 问题列表)``。

    必需集合 = 显式清单 ∪ 包内 ``release_manifest`` 声明清单；两者不一致即问题
    （名单漂移会让"锁文件/策略卡改动"漏出发布身份）。

    ``enforce_declared`` 默认按"树里是否存在 ``src/core/release_manifest.py``"
    决定：真实仓库/真实发布包必然存在，因此交叉核对必然执行；只有不含该模块的
    合成夹具才跳过（跳过时不会静默——构建报告里仍会列出必需集合）。
    """
    problems: list[str] = []
    required: list[str] = list(
        REQUIRED_TOP_LEVEL | {rel: "" for rel in REQUIRED_CONFIGS}
        | {rel: "" for rel in REQUIRED_KNOWLEDGE}
        | {rel: "" for rel in REQUIRED_DEPENDENCY_FILES}
        | {rel: "" for rel in REQUIRED_RUNTIME_SOURCES}
        | {rel: "" for rel in RUNTIME_SCRIPTS}
    )
    if enforce_declared is None:
        enforce_declared = (Path(package_root) / "src" / "core" / "release_manifest.py").is_file()
    declared = _declared_runtime_lists(package_root) if enforce_declared else None
    if declared is None:
        if enforce_declared:
            problems.append("RUNTIME_LISTS_UNPROVEN: 无法在包内解析 src/core/release_manifest.py")
        return sorted(set(required)), problems
    configs = [str(item) for item in declared.get("configs") or []]
    if set(configs) != set(REQUIRED_CONFIGS):
        problems.append(
            f"CONFIG_LIST_DRIFT: 包内 SAFE_CONFIG_FILES={sorted(configs)} "
            f"与打包清单 {sorted(REQUIRED_CONFIGS)} 不一致"
        )
    cards = {str(item) for item in declared.get("cards") or []}
    missing_cards = cards - set(REQUIRED_KNOWLEDGE)
    if missing_cards:
        problems.append(f"CARD_LIST_DRIFT: 包内策略卡来源 {sorted(missing_cards)} 未纳入必需清单")
    deps = {str(item) for item in declared.get("deps") or []}
    uncovered = set(REQUIRED_DEPENDENCY_FILES) - deps
    if uncovered:
        problems.append(f"DEPENDENCY_LIST_DRIFT: 必需依赖文件 {sorted(uncovered)} 不在包内声明集合中")
    required.extend(sorted(cards))
    return sorted(set(required)), problems


# --------------------------------------------------------------------------
# 构建
# --------------------------------------------------------------------------
def _package_manifest(
    root: Path, files: list[str], release_manifest: dict[str, Any], *,
    kind: str, inventory: dict[str, Any],
) -> dict[str, Any]:
    rows = [{"path": relative, "sha256": _sha256(root / relative)} for relative in files]
    counts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    excluded_rows: list[dict[str, str]] = []
    for relative, info in sorted(inventory["classes"].items()):
        counts[info["class"]] = counts.get(info["class"], 0) + 1
        if info["class"] == _CLASS_EXCLUDED:
            reasons[info["reason"]] = reasons.get(info["reason"], 0) + 1
            excluded_rows.append({"path": relative, "reason": info["reason"]})
    return {
        "package_schema": PACKAGE_SCHEMA,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "release_id": str(release_manifest["release_id"]),
        "release_manifest_sha256": hashlib.sha256(
            json.dumps(release_manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "files": rows,
        "inventory": {
            "source": inventory["source"],
            "tracked_total": inventory["total"],
            "included": len(files),
            "excluded": counts.get(_CLASS_EXCLUDED, 0),
            "runtime_assets": counts.get(_CLASS_RUNTIME, 0),
            "source_only": counts.get(_CLASS_SOURCE_ONLY, 0),
            "unclassified": 0,
            "excluded_by_reason": dict(sorted(reasons.items())),
        },
        "excluded": EXCLUDED_SUMMARY,
        "excluded_files": excluded_rows,
    }


def _write_archive(
    root: Path, output: Path, files: list[str], release_manifest: dict[str, Any],
    package_meta: dict[str, Any],
) -> None:
    package_root = output.parent
    package_root.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", suffix=".tmp", dir=package_root, delete=False,
    )
    handle.close()
    temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative in files:
                archive.write(root / relative, arcname=relative)
            archive.writestr(
                RELEASE_MANIFEST_MEMBER,
                json.dumps(release_manifest, ensure_ascii=False, indent=2) + "\n",
            )
            archive.writestr(
                MANIFEST_MEMBER,
                json.dumps(package_meta, ensure_ascii=False, indent=2) + "\n",
            )
        os.replace(temporary, output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_release_package(
    project_root: Path, output_path: Path, *, manifest_path: Path,
    kind: str = KIND_RUNTIME,
) -> dict[str, Any]:
    """原子生成发布包；目标存在、归类有缺口、manifest 越界或文件不安全时失败。"""
    if kind not in KINDS:
        raise ReleasePackageError(f"kind 只允许 {KINDS}，收到 {kind!r}")
    root = Path(project_root).resolve()
    output = Path(output_path).resolve()
    manifest_file = Path(manifest_path).resolve()
    if output.exists():
        raise FileExistsError(f"发布包已存在，不覆盖: {output}")
    try:
        manifest_file.relative_to(root)
    except ValueError as exc:
        raise ReleasePackageError("manifest 必须位于项目根目录内") from exc
    release_manifest = _load_manifest(manifest_file)
    inventory = discover_inventory(root)
    if inventory["unclassified"]:
        raise ReleasePackageError(
            "存在未归类文件（禁止漏包，也未排除）：" + ", ".join(inventory["unclassified"][:10])
        )
    misfiled = sorted(
        relative for relative, info in inventory["classes"].items()
        if relative.startswith(RUNTIME_ASSET_ROOTS) and info["class"] == _CLASS_EXCLUDED
    )
    if misfiled:
        raise ReleasePackageError(
            "运行资产被路径排除规则误伤（禁止漏包）：" + ", ".join(misfiled[:10])
        )
    files = packaged_files(inventory, kind=kind)
    if not files:
        raise ReleasePackageError("没有可打包的安全文件")

    required, problems = required_runtime_assets(root)
    # 登记项与仓库脱节必须在**出包路径**就硬失败：早期版本写作
    # ``... and (root / relative).exists()``，把不存在的必需项直接跳过，于是
    # ``scripts/run_web.py`` 被重命名后 ``--out`` 仍能**静默出包**（包内没有任何
    # 启动器）。现在与 ``verify_release_package`` 的判定对齐：真实仓库里必需路径不
    # 存在即失败，且"存在但没进包"与"根本不存在"分别给出可读原因。
    #
    # 唯一的例外是**合成夹具**（无 Git 索引、也没有 ``src/core/release_manifest.py``
    # 的临时目录，例如 ``scripts/smoke_release_package.py`` 自建的 partial tree）：
    # 它没有"真实仓库"语义，天然只带一小撮文件，沿用旧的"存在才要求入包"口径。
    real_repo = inventory["source"] == "git-index" or (
        root / SRC_DIRNAME / "core" / "release_manifest.py"
    ).is_file()
    if real_repo:
        absent = sorted(relative for relative in required
                        if not (root / relative).is_file())
        if absent:
            raise ReleasePackageError(
                f"运行必需资产在仓库中不存在（登记项与实际仓库脱节，禁止出包）: {absent[:10]}"
            )
        missing = sorted(set(required) - set(files))
    else:
        missing = sorted(relative for relative in required
                         if relative not in set(files) and (root / relative).exists())
    if missing:
        raise ReleasePackageError(f"运行必需资产未入包: {missing[:10]}")
    if problems:
        raise ReleasePackageError("；".join(problems))

    unsafe: list[str] = []
    for relative in files:
        path = root / relative
        if path.is_symlink():
            raise ReleasePackageError(f"发布文件不得是符号链接: {relative}")
        if not path.is_file():
            raise ReleasePackageError(f"发布文件不存在: {relative}")
        if _excluded_reason(relative) is not None:
            unsafe.append(relative)
    if unsafe:
        raise ReleasePackageError(f"发布文件清单包含禁止路径: {unsafe[:3]}")

    package_meta = _package_manifest(
        root, files, release_manifest, kind=kind, inventory=inventory,
    )
    _write_archive(root, output, files, release_manifest, package_meta)
    package_meta["package_sha256"] = _sha256(output)
    package_meta["output"] = str(output)
    return package_meta


# --------------------------------------------------------------------------
# 自检（可独立在解压目录运行）
# --------------------------------------------------------------------------
_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_LINE_SUFFIX_RE = re.compile(r":\d+(?::\d+)?$")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "#", "//")


def _normalize_link(origin: str, target: str) -> str | None:
    """把 markdown 相对链接解析为包内相对路径；外部/锚点返回 ``None``。"""
    target = target.strip().strip("<>").split("#", 1)[0]
    if not target or target.startswith(_EXTERNAL_PREFIXES) or target.startswith("/"):
        return None
    target = _LINE_SUFFIX_RE.sub("", target)
    parts: list[str] = []
    for part in (Path(origin).parent / target).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts) if parts else None


def _check_links(members: set[str], texts: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for relative, text in sorted(texts.items()):
        if not relative.lower().endswith(".md"):
            continue
        for raw in _LINK_RE.findall(text):
            target = _normalize_link(relative, raw)
            if target is None:
                continue
            if target not in members:
                problems.append(f"BROKEN_LINK: {relative} -> {raw}")
    return problems


def _check_syntax(package_root: Path, files: list[str]) -> list[str]:
    problems: list[str] = []
    for relative in files:
        if not relative.endswith(".py"):
            continue
        path = package_root / relative
        try:
            source = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"UNREADABLE_PY: {relative} ({type(exc).__name__})")
            continue
        try:
            compile(source, relative, "exec")
        except SyntaxError as exc:
            problems.append(f"SYNTAX_ERROR: {relative}:{exc.lineno}")
    return problems


def _check_windows_service_config(package_root: Path) -> list[str]:
    """在隔离子进程里用**包内** ``core.windows_service`` 解析 SCM 配置语义。"""
    config = package_root / "config" / "windows_service.json"
    if not config.is_file():
        return ["WINDOWS_SERVICE_CONFIG_MISSING: config/windows_service.json"]
    program = (
        "import json,sys;"
        "sys.path.insert(0, sys.argv[1]);"
        "from pathlib import Path;"
        "from core.windows_service import load_windows_service_config as load;"
        "cfg = load(Path(sys.argv[2]), project_root=Path(sys.argv[3]), require_files=False);"
        "print(json.dumps({'ok': True, 'host': cfg.host, 'port': cfg.port}))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(package_root / SRC_DIRNAME),
             str(config), str(package_root)],
            cwd=package_root, check=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=_subprocess_env(),
        )
    except OSError as exc:
        return [f"WINDOWS_SERVICE_CONFIG_UNVERIFIED: {type(exc).__name__}"]
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()
        return [f"WINDOWS_SERVICE_CONFIG_INVALID: {tail[-1] if tail else 'unknown'}"]
    return []


def _iter_members(package_root: Path, files: list[str]) -> Iterable[tuple[str, bytes]]:
    for relative in files:
        yield relative, (package_root / relative).read_bytes()


def verify_release_package(package_root: Path) -> dict[str, Any]:
    """自检解压后的发布包；返回结构化报告，``ok=False`` 时列出全部问题。"""
    root = Path(package_root).resolve()
    problems: list[str] = []
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            problems.append(f"{name}: {detail}")

    manifest_file = root / MANIFEST_MEMBER
    meta: dict[str, Any] = {}
    if not manifest_file.is_file():
        record("package_manifest_present", False, f"缺少 {MANIFEST_MEMBER}")
    else:
        try:
            meta = json.loads(manifest_file.read_text(encoding="utf-8"))
            record("package_manifest_present", True,
                   {"schema": meta.get("package_schema"), "kind": meta.get("kind")})
        except (OSError, json.JSONDecodeError) as exc:
            record("package_manifest_present", False, f"{type(exc).__name__}")

    release_file = root / RELEASE_MANIFEST_MEMBER
    if not release_file.is_file():
        record("release_manifest_present", False, f"缺少 {RELEASE_MANIFEST_MEMBER}")
    else:
        record("release_manifest_present", True)

    # 两次发布之间只换清单不换代码（或反之）会让"这份工件对应哪个 release_id"
    # 变成两套说法；绑定两者的哈希与 release_id 是发布身份的最低要求（F13）。
    embedded: dict[str, Any] = {}
    if release_file.is_file():
        try:
            embedded = json.loads(release_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            record("release_identity_matches", False, f"{type(exc).__name__}")
            embedded = None  # type: ignore[assignment]
    if embedded:
        expected_hash = hashlib.sha256(
            json.dumps(embedded, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        same_id = str(meta.get("release_id") or "") == str(embedded.get("release_id") or "")
        same_hash = str(meta.get("release_manifest_sha256") or "") == expected_hash
        record("release_identity_matches", same_id and same_hash,
               {"release_id_equal": same_id, "manifest_sha256_equal": same_hash})

    rows = meta.get("files") if isinstance(meta.get("files"), list) else []
    listed = {str(row.get("path")) for row in rows if isinstance(row, dict)}

    on_disk: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file():
            on_disk.add(path.relative_to(root).as_posix())
    ignored = {MANIFEST_MEMBER, RELEASE_MANIFEST_MEMBER}
    unlisted = sorted(on_disk - listed - ignored)
    record("no_unlisted_files", not unlisted,
           {"unlisted": unlisted[:10], "count": len(unlisted)} if unlisted else
           {"count": 0})

    bad_hash: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        relative = str(row.get("path"))
        path = root / relative
        if not path.is_file():
            bad_hash.append(f"{relative}(missing)")
        elif _sha256(path) != str(row.get("sha256")):
            bad_hash.append(f"{relative}(sha256)")
    record("file_hashes_match", not bad_hash,
           {"mismatched": bad_hash[:10], "count": len(bad_hash)} if bad_hash else
           {"count": len(rows)})

    # --- 内容规则：路径 + 凭据 ---------------
    # 路径规则判定遍历**盘上全部文件**（``on_disk``），而不只是清单里列到的文件：
    # 早期版本用 ``listed & on_disk``，于是"盘上有、清单无"的禁止路径（例如手工放进
    # 解压目录的 ``server.key``）不会由本项报出，只能靠 ``no_unlisted_files``/
    # ``runtime_state_absent`` 兜住——检查名与语义不符（P3-5）。
    forbidden = sorted(
        f"{relative}({reason})"
        for relative in (on_disk - ignored)
        if (reason := _excluded_reason(relative)) is not None
    )
    texts: dict[str, str] = {}
    secret_hits: list[str] = []
    exempted: list[str] = []
    scannable_suffixes = (
        ".md", ".json", ".yaml", ".yml", ".txt", ".py", ".bat", ".cfg", ".ini",
        ".toml", ".example", ".sh", ".ps1",
    )
    for relative, raw in _iter_members(root, sorted(listed & on_disk)):
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        texts[relative] = text
        is_text_asset = (
            relative.lower().endswith(scannable_suffixes)
            or Path(relative).name.startswith(".env")
        )
        if not is_text_asset:
            continue
        hits, was_exempt = _scan_secrets(relative, text)
        if was_exempt:
            exempted.append(relative)
            continue
        for line, rule, fingerprint in hits:
            secret_hits.append(f"{relative}:{line} rule={rule} fp={fingerprint}")
    record("forbidden_paths_absent", not forbidden,
           {"hits": forbidden[:10]} if forbidden else {"count": 0})
    record("credentials_absent", not secret_hits,
           {"hits": secret_hits[:10], "count": len(secret_hits)} if secret_hits else
           {"scanned": len(texts), "repo_exempt_files": sorted(exempted)})
    state_files = sorted(
        name for name in on_disk
        if name.endswith((".log", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".pem", ".key"))
    )
    record("runtime_state_absent", not state_files, {"hits": state_files[:10]})
    record("dotenv_absent", not (root / ".env").exists())

    # --- 必需运行资产（与包内声明清单交叉核对） ---------------
    required, cross_problems = required_runtime_assets(root, enforce_declared=True)
    missing = [relative for relative in required if not (root / relative).is_file()]
    record("required_assets_present", not missing and not cross_problems,
           {"missing": missing[:10], "cross_check": cross_problems} if (missing or cross_problems)
           else {"required": len(required)})

    # --- 链接 / 语法 / SCM 语义 ---------------
    link_problems = _check_links(listed, texts)
    record("doc_links_resolve", not link_problems,
           {"broken": link_problems[:10], "count": len(link_problems)} if link_problems
           else {"checked_docs": sum(1 for name in texts if name.lower().endswith(".md"))})

    syntax_problems = _check_syntax(root, sorted(listed))
    record("python_syntax_ok", not syntax_problems,
           {"problems": syntax_problems[:10]} if syntax_problems else
           {"compiled": sum(1 for name in listed if name.endswith(".py"))})

    service_problems = _check_windows_service_config(root)
    record("windows_service_config_valid", not service_problems,
           {"problems": service_problems} if service_problems else {"ok": True})

    return {
        "verify_schema": 1,
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "package_root": str(root),
        "kind": meta.get("kind"),
        "release_id": meta.get("release_id"),
        "file_count": len(listed),
        "checks": checks,
        "problems": problems,
        "ok": not problems,
    }


def extract_archive(archive: Path, target: Path) -> Path:
    """安全解压（拒绝路径穿越与绝对路径），返回解压根目录。"""
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as handle:
        for member in handle.namelist():
            normalized = Path(member.replace("\\", "/"))
            if normalized.is_absolute() or ".." in normalized.parts:
                raise ReleasePackageError(f"压缩包包含越界成员: {member}")
        handle.extractall(target)
    return target


def self_check(
    project_root: Path, *, manifest_path: Path, kind: str = KIND_RUNTIME,
    workdir: Path | None = None,
) -> dict[str, Any]:
    """构建 → 解压到临时目录 → 自检，返回合并报告（发布前最低证据）。"""
    root = Path(project_root).resolve()
    holder = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="mta-selfcheck-"))
    holder.mkdir(parents=True, exist_ok=True)
    archive = holder / f"mta-{kind}.zip"
    if archive.exists():
        archive.unlink()
    meta = build_release_package(
        root, archive, manifest_path=manifest_path, kind=kind,
    )
    extracted = extract_archive(archive, holder / "extracted")
    report = verify_release_package(extracted)
    report["package"] = {
        "kind": meta["kind"],
        "output": meta["output"],
        "package_sha256": meta["package_sha256"],
        "inventory": meta["inventory"],
    }
    return report


# --------------------------------------------------------------------------
# 兼容入口（既有 scripts/smoke_release_package.py 依赖该签名）
# --------------------------------------------------------------------------
def create_release_package(
    project_root: Path, output_path: Path, *, manifest_path: Path,
    kind: str = KIND_RUNTIME,
) -> dict[str, Any]:
    """``build_release_package`` 的兼容别名（默认 runtime 包，语义与旧版一致）。"""
    return build_release_package(
        project_root, output_path, manifest_path=manifest_path, kind=kind,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="创建并自检不含状态/凭据的代码发布包")
    parser.add_argument("--project-root", type=Path, default=ROOT,
                        help="要打包的代码根（默认本脚本所在仓库；manifest 必须位于其内）")
    parser.add_argument("--manifest", type=Path,
                        default=None, help="默认 <project-root>/data/release_manifest.json")
    parser.add_argument("--out", type=Path, help="输出 ZIP；必须不存在")
    parser.add_argument("--kind", choices=KINDS, default=KIND_RUNTIME,
                        help="runtime=可部署运行工件（默认）；source=含测试与维护工具")
    parser.add_argument("--verify", type=Path,
                        help="自检一个**已解压**的发布包目录，不构建")
    parser.add_argument("--self-check", action="store_true",
                        help="构建到临时目录并解压自检，不写 dist/")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    manifest_path = args.manifest or (project_root / "data" / "release_manifest.json")
    try:
        if args.verify is not None:
            report = verify_release_package(args.verify)
        elif args.self_check:
            report = self_check(
                project_root, manifest_path=manifest_path, kind=args.kind,
            )
        elif args.out is not None:
            report = build_release_package(
                project_root, args.out, manifest_path=manifest_path, kind=args.kind,
            )
        else:
            parser.error("必须给出 --out、--verify 或 --self-check 之一")
            return 2
    except (OSError, ReleasePackageError, zipfile.BadZipFile) as exc:
        print(f"RELEASE PACKAGE FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
