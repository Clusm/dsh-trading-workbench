"""离线验证精确依赖锁、传递闭包和当前虚拟环境。"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
def _meaningful(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _exact_lock(path: Path) -> dict[str, str]:
    locked: dict[str, str] = {}
    environment = default_environment()
    for line in _meaningful(path):
        requirement = Requirement(line)
        specs = list(requirement.specifier)
        if len(specs) != 1 or specs[0].operator != "==" or requirement.url:
            raise ValueError(f"锁文件必须全部使用 name==version: {line}")
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        name, version = canonicalize_name(requirement.name), specs[0].version
        if name in locked:
            raise ValueError(f"当前环境命中重复依赖锁: {name}")
        locked[name] = version
    return locked


def _root_requirements(path: Path) -> list[Requirement]:
    roots: list[Requirement] = []
    for line in _meaningful(path):
        if line.startswith("-"):
            continue
        roots.append(Requirement(line))
    return roots


def _installed_distribution(name: str) -> metadata.Distribution:
    try:
        return metadata.distribution(name)
    except metadata.PackageNotFoundError as exc:
        raise ValueError(f"当前环境缺少依赖: {name}") from exc


def _resolved_closure(roots: list[Requirement]) -> set[str]:
    """按当前平台 marker 计算闭包，并保留 direct requirement 选中的 extras。"""
    environment = default_environment()
    pending = [(requirement.name, frozenset(requirement.extras)) for requirement in roots]
    visited: set[tuple[str, frozenset[str]]] = set()
    closure: set[str] = set()
    while pending:
        name, extras = pending.pop()
        key = canonicalize_name(name)
        state = (key, extras)
        if state in visited:
            continue
        visited.add(state)
        distribution = _installed_distribution(name)
        closure.add(key)
        active_extras = extras or frozenset({""})
        for raw in distribution.requires or []:
            child = Requirement(raw)
            applies = child.marker is None or any(
                child.marker.evaluate({**environment, "extra": extra})
                for extra in active_extras)
            if applies:
                pending.append((child.name, frozenset(child.extras)))
    return closure


def main() -> int:
    lock_path = ROOT / "requirements-lock.txt"
    runtime_lock = _exact_lock(lock_path)
    roots = _root_requirements(ROOT / "requirements.txt")
    closure = _resolved_closure(roots)
    missing = sorted(closure - set(runtime_lock))
    surplus = sorted(set(runtime_lock) - closure)
    if missing or surplus:
        raise ValueError(f"依赖闭包不一致 missing={missing} surplus={surplus}")
    for requirement in roots:
        locked_version = runtime_lock[canonicalize_name(requirement.name)]
        if Version(locked_version) not in requirement.specifier:
            raise ValueError(f"锁定版本超出直接依赖范围: {requirement.name}=={locked_version}")
    mismatches = []
    for name, expected in runtime_lock.items():
        actual = _installed_distribution(name).version
        if Version(actual) != Version(expected):
            mismatches.append(f"{name}: expected={expected} actual={actual}")
    dev_lines = _meaningful(ROOT / "requirements-dev-lock.txt")
    if dev_lines.count("-r requirements-lock.txt") != 1:
        raise ValueError("开发锁必须且只能引用一次运行锁")
    dev_exact = [line for line in dev_lines if not line.startswith("-r ")]
    if dev_exact != [f"ruff=={metadata.version('ruff')}"]:
        raise ValueError("开发锁与当前 Ruff 版本不一致")
    if mismatches:
        raise ValueError("当前环境与依赖锁不一致: " + "; ".join(mismatches))
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True, cwd=ROOT)
    digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    print(f"DEPENDENCY LOCK OK packages={len(runtime_lock)} sha256={digest}")
    print(f"  python={sys.version_info.major}.{sys.version_info.minor} platform={sys.platform}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
