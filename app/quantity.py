"""十进制定点数量。

数量在接口边界以字符串传输, 内部以 Decimal 计算;
禁止二进制浮点承担守恒判断 —— float 输入一律拒绝。
"""

from decimal import Decimal, InvalidOperation

from .errors import QuantityError

ZERO = Decimal("0")


def parse_quantity(value, *, field="quantity"):
    """把外部输入解析为非负 Decimal。

    接受 str / int / Decimal; 拒绝 float 与布尔, 拒绝 NaN/无穷/负数。
    """
    if isinstance(value, bool):
        raise QuantityError(f"{field}: 布尔值不是合法数量: {value!r}")
    if isinstance(value, float):
        raise QuantityError(f"{field}: 禁止使用二进制浮点表示数量: {value!r}")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise QuantityError(f"{field}: 空字符串不是合法数量")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise QuantityError(f"{field}: 无法解析的数量字符串: {value!r}") from exc
    else:
        raise QuantityError(f"{field}: 不支持的数量类型 {type(value).__name__}")
    if parsed.is_nan() or parsed.is_infinite():
        raise QuantityError(f"{field}: 数量必须有限: {value!r}")
    if parsed < ZERO:
        raise QuantityError(f"{field}: 数量不得为负: {value!r}")
    return parsed


def qty_str(value):
    """把数量规范为定点小数字符串, 用于日志、报告与快照对比。"""
    return format(value.normalize(), "f")
