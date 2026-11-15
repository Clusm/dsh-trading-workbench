"""``md-evidence/1`` JSON Schema 加载与最小 draft-07 校验器。

schema 直接取自 ``docs/04-行情与指标.md``（冻结副本 ``evidence_schema.json``），
避免实现与文档漂移；校验器只实现该 schema 实际用到的关键字：
``type/const/enum/pattern/minimum/maximum/exclusiveMinimum/minItems/maxItems/
uniqueItems/minProperties/maxLength/required/properties/additionalProperties/
items/oneOf/$ref``。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

_SCHEMA_PATH = Path(__file__).with_name("evidence_schema.json")

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
}


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(payload: Any, schema: Mapping[str, Any] | None = None) -> list[str]:
    """返回错误列表；空列表表示通过。"""

    root = schema or load_schema()
    errors: list[str] = []
    _validate(root, payload, "$", root, errors)
    return errors


def _resolve(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _validate(
    node: Mapping[str, Any],
    value: Any,
    path: str,
    root: Mapping[str, Any],
    errors: list[str],
) -> None:
    if "$ref" in node:
        _validate(_resolve(root, str(node["$ref"])), value, path, root, errors)
        return

    if "type" in node:
        expected = node["type"]
        options = expected if isinstance(expected, list) else [expected]
        if not any(_TYPE_CHECKS.get(str(option), lambda _value: False)(value) for option in options):
            errors.append(f"{path}: expected type {expected}, got {type(value).__name__}")
            return

    if "const" in node and value != node["const"]:
        errors.append(f"{path}: expected const {node['const']!r}, got {value!r}")

    if "enum" in node and value not in node["enum"]:
        errors.append(f"{path}: {value!r} not in enum {node['enum']}")

    if isinstance(value, str):
        pattern = node.get("pattern")
        if pattern and not re.search(str(pattern), value):
            errors.append(f"{path}: {value!r} does not match pattern {pattern!r}")
        max_length = node.get("maxLength")
        if max_length is not None and len(value) > int(max_length):
            errors.append(f"{path}: string longer than {max_length}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{path}: {value} < minimum {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            errors.append(f"{path}: {value} > maximum {node['maximum']}")
        if "exclusiveMinimum" in node and value <= node["exclusiveMinimum"]:
            errors.append(f"{path}: {value} <= exclusiveMinimum {node['exclusiveMinimum']}")

    if isinstance(value, list):
        if "minItems" in node and len(value) < int(node["minItems"]):
            errors.append(f"{path}: fewer than {node['minItems']} items")
        if "maxItems" in node and len(value) > int(node["maxItems"]):
            errors.append(f"{path}: more than {node['maxItems']} items")
        if node.get("uniqueItems"):
            seen: list[str] = []
            for item in value:
                marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if marker in seen:
                    errors.append(f"{path}: duplicate item {item!r}")
                    break
                seen.append(marker)
        item_schema = node.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate(item_schema, item, f"{path}[{index}]", root, errors)

    if isinstance(value, dict):
        if "minProperties" in node and len(value) < int(node["minProperties"]):
            errors.append(f"{path}: fewer than {node['minProperties']} properties")
        required = node.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{path}: missing required property {key!r}")
        properties = node.get("properties") or {}
        for key, child in properties.items():
            if key in value:
                _validate(child, value[key], f"{path}.{key}", root, errors)
        additional = node.get("additionalProperties", True)
        for key in value:
            if key in properties:
                continue
            if additional is False:
                errors.append(f"{path}: unexpected property {key!r}")
            elif isinstance(additional, Mapping):
                _validate(additional, value[key], f"{path}.{key}", root, errors)

    if "oneOf" in node:
        matched = 0
        branch_errors: list[str] = []
        for branch in node["oneOf"]:
            scratch: list[str] = []
            _validate(branch, value, path, root, scratch)
            if not scratch:
                matched += 1
            else:
                branch_errors.extend(scratch)
        if matched != 1:
            errors.append(f"{path}: matched {matched} oneOf branches (expected 1)")
