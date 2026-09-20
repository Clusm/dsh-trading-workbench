"""K01-K06 共享工具：规范化 JSON 哈希、Decimal 与敏感键名守卫。

本模块保持纯函数：不读取 ``.env``/``data``/运行状态，不构造网络请求。
哈希输入一律是调用方显式传入的有效值字典；含敏感键名（key/secret/token
等）的输入会被拒绝，防止把原始凭据或整个 ``.env`` 导出成指纹。
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator

SCHEMA_VERSION = "3.0"

SENSITIVE_KEY_MARKERS = (
    "key",
    "secret",
    "token",
    "password",
    "passwd",
    "passphrase",
    "private",
    "credential",
    "authorization",
    "cookie",
)


class SensitiveInputError(ValueError):
    """调用方试图把含敏感键名的字典送入哈希或身份计算。"""


class NumericInputError(ValueError):
    """数值不是有限 Decimal，或使用了被禁止的浮点直传。"""


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """稳定 JSON 文本：键排序、UTF-8、无多余空白（分隔符紧凑）。"""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_json_default,
    )


def canonical_hash(value: Any) -> str:
    """对规范化 JSON 取 sha256 十六进制摘要。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_sensitive_key(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def sensitive_paths(value: Any, *, prefix: str = "") -> list[str]:
    """递归列出含敏感键名的路径；只返回路径，不读取或返回值。"""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            name = str(key)
            path = f"{prefix}.{name}" if prefix else name
            if _is_sensitive_key(name):
                found.append(path)
            else:
                found.extend(sensitive_paths(value[key], prefix=path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(sensitive_paths(item, prefix=f"{prefix}[{index}]"))
    return found


def ensure_no_sensitive_fields(value: Any) -> None:
    """输入含敏感键名时立即抛错，并在报错里只暴露字段路径。"""
    paths = sensitive_paths(value)
    if paths:
        raise SensitiveInputError("sensitive keys rejected: " + ", ".join(paths))


def decimal_from_value(value: Any) -> Decimal:
    """把 str/int/Decimal/float 转为 Decimal；bool 与非法文本抛错。

    float 经 ``str()`` 转换以避免二进制尾差，并强制有限性检查。
    """
    if isinstance(value, bool):
        raise NumericInputError("bool is not a numeric value")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise NumericInputError(f"invalid decimal text: {value!r}") from exc
    elif isinstance(value, float):
        result = Decimal(str(value))
    else:
        raise NumericInputError(f"unsupported numeric type: {type(value).__name__}")
    if not result.is_finite():
        raise NumericInputError("non-finite decimal rejected at boundary")
    return result


def decimal_from_float(value: float, *, places: int | None = None) -> Decimal:
    """显式把已知来源的浮点转成 Decimal；默认经 ``str`` 避免二进制尾差。"""
    result = Decimal(str(value))
    if places is not None:
        result = result.quantize(Decimal(1).scaleb(-places))
    if not result.is_finite():
        raise NumericInputError("non-finite float rejected")
    return result


def _coerce_money(value: Any) -> Decimal:
    if isinstance(value, float):
        raise NumericInputError(
            "float money input rejected; use decimal_from_float explicitly"
        )
    return decimal_from_value(value)


MoneyDecimal = Annotated[Decimal, BeforeValidator(_coerce_money)]


def is_finite(value: Decimal) -> bool:
    return value.is_finite()


def decimal_text(value: Decimal) -> str:
    """十进制字符串：有限值去掉指数与尾零；NaN/Infinity 原样保留供校验拒绝。"""
    if not value.is_finite():
        return str(value)
    return format(value.normalize(), "f")


def require_finite(value: Decimal, *, ref: str = "value") -> Decimal:
    if not value.is_finite():
        raise NumericInputError(f"non-finite value at {ref}")
    return value


def utc_ms_now() -> int:
    """UTC 毫秒；仅供调用方显式取时钟，契约函数一律接收 now_ms。"""
    import time

    return int(time.time() * 1000)


def half_open_contains(point_ms: int, start_ms: int, end_ms: int) -> bool:
    """内部区间语义 [start, end)：左闭右开。"""
    return start_ms <= point_ms < end_ms


def half_open_problems(start_ms: int, end_ms: int) -> list[str]:
    problems: list[str] = []
    if end_ms <= start_ms:
        problems.append("INVALID_RANGE")
    return problems


def model_content_hash(
    model: BaseModel,
    *,
    exclude: Iterable[str] = ("content_hash",),
) -> str:
    """模型内容哈希：JSON 模式导出后剔除自引用字段再规范化。"""
    payload = model.model_dump(mode="json")
    for name in exclude:
        payload.pop(name, None)
    return canonical_hash(payload)
