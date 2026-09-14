"""L03 发布检查：源码中的 Binance 端点必须存在显式成本登记。"""
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.binance_request_policy import registered_endpoints  # noqa: E402


def main() -> None:
    registered = set(registered_endpoints())
    missing: set[tuple[str, str]] = set()
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "binance_request_policy.py":
            continue
        text = path.read_text(encoding="utf-8")
        # 明确的 _signed(method, path) 调用按真实方法检查。
        for match in re.finditer(
                r"\._signed\(\s*['\"]([A-Z]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
                text):
            method, endpoint = match.groups()
            if (method, endpoint.split("?", 1)[0]) not in registered:
                missing.add((method, endpoint))
        # 公共 request_json/_get_json 的第二个参数是 GET；不要把“用途映射”或
        # _request_purpose 中的路径常量误当成 GET 调用。
        for match in re.finditer(
                r"\b(?:request_json|_get_json)\(\s*[^,\n]+,\s*['\"](/(?:fapi|futures/data)/[^'\"]+)['\"]",
                text):
            endpoint = match.group(1).split("?", 1)[0]
            if ("GET", endpoint) not in registered:
                missing.add(("GET", endpoint))
    assert not missing, f"未登记 Binance 端点: {sorted(missing)}"
    print(f"PASS request policy coverage: endpoints={len(registered)}")


if __name__ == "__main__":
    main()
