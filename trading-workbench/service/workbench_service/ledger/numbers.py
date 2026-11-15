"""Decimal 文本工具：账本与风控统一用十进制字符串，避免 float 尾差。

设计依据：docs/03 §6.3（数量/金额一律十进制字符串）、§10（账本列 TEXT）。
本模块不依赖任何第三方库，可被 ledger / risk / execution 复用。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP

__all__ = [
    "to_decimal",
    "dec_text",
    "floor_to_step",
    "ceil_to_step",
    "format_with_step",
    "safe_ratio",
]


def to_decimal(value: object, default: Decimal | None = None) -> Decimal | None:
    """把 int/float/str/Decimal 转成 Decimal；非法值返回 default（不抛异常）。"""

    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value if value.is_finite() else default
    try:
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return default
            parsed = Decimal(str(value))
        elif isinstance(value, bool):  # bool 是 int 子类，显式拒绝
            return default
        else:
            parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return default
    return parsed if parsed.is_finite() else default


def dec_text(value: object, default: str = "0") -> str:
    """十进制文本（无科学计数法、无多余尾零）；非法值返回 ``default``。"""

    parsed = to_decimal(value)
    if parsed is None:
        return default
    if parsed == 0:
        return "0"
    text = format(parsed.normalize(), "f")
    return text


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向下取整到 step 的整数倍（下单数量必须向下截断，避免 -2022）。"""

    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向上取整到 step 的整数倍（用于止盈价方向安全取整）。"""

    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def _decimals_for_step(step: Decimal) -> int:
    exponent = step.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < 0:
        return -exponent
    return 0


def format_with_step(value: Decimal, step: Decimal) -> str:
    """按 step 的小数位格式化，规避科学计数法与 1e-05 之类的文本。"""

    decimals = _decimals_for_step(step)
    quant = Decimal(1).scaleb(-decimals)
    return format(value.quantize(quant), f".{decimals}f")


def safe_ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator
