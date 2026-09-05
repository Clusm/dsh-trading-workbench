"""验证风险档与USD-M合约交易范围未被升级代码意外修改。"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "config" / "risk_profile.lock.json"


def _get(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def _canonical_hash(values: dict[str, Any]) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def collect() -> dict[str, Any]:
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8")) or {}
    risk = yaml.safe_load((ROOT / "config" / "risk_learning.yaml").read_text(encoding="utf-8")) or {}
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    actual: dict[str, Any] = {}
    for key in lock["expected"]:
        if key.startswith("risk."):
            actual[key] = _get(risk, key.removeprefix("risk."))
        elif key.startswith("settings."):
            actual[key] = _get(settings, key.removeprefix("settings."))
        else:
            raise KeyError(f"锁文件包含未知命名空间: {key}")
    return actual


def main() -> int:
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    expected = lock.get("expected") or {}
    expected_hash = _canonical_hash(expected)
    failures: list[str] = []
    if lock.get("sha256") != expected_hash:
        failures.append(
            f"锁文件自身哈希不匹配: recorded={lock.get('sha256')} computed={expected_hash}"
        )

    try:
        actual = collect()
    except (KeyError, TypeError, ValueError) as exc:
        failures.append(f"读取风险档失败: {exc}")
        actual = {}

    for key, wanted in expected.items():
        got = actual.get(key, "<MISSING>")
        if got != wanted:
            failures.append(f"{key}: expected={wanted!r}, actual={got!r}")

    symbols = actual.get("settings.market_universe.core_symbols") or []
    if len(symbols) != len(set(symbols)):
        failures.append("core_symbols 存在重复项")
    invalid = [s for s in symbols if not re.fullmatch(r"[0-9A-Z]+USDT", str(s))]
    if invalid:
        failures.append(f"core_symbols 包含非USDT格式: {invalid}")

    if failures:
        print("PROFILE LOCK FAILED")
        for item in failures:
            print(f"  - {item}")
        return 1

    print(f"PROFILE LOCK OK sha256={expected_hash}")
    print(f"  locked_fields={len(expected)} symbols={len(symbols)} quote=USDT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
