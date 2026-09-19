"""十进制定点数量。

契约规定数量以十进制定点字符串传输，禁止用二进制浮点数承担守恒判断。
所有进入系统的数量都必须经过 parse_qty，输出统一走 qty_str。
"""

from decimal import Decimal, InvalidOperation

from app.errors import ValidationError

_ZERO = Decimal(0)


def parse_qty(value, field="qty"):
    """把外部输入解析为 Decimal；拒绝 float、布尔、非有限值。"""
    if value is None or isinstance(value, bool):
        raise ValidationError(f"{field} 缺失或类型非法: {value!r}")
    if isinstance(value, float):
        raise ValidationError(f"{field} 禁止使用二进制浮点数: {value!r}")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValidationError(f"{field} 不能为空字符串")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise ValidationError(f"{field} 不是合法的十进制数: {value!r}") from exc
    else:
        raise ValidationError(f"{field} 类型不支持: {type(value).__name__}")
    if not parsed.is_finite():
        raise ValidationError(f"{field} 必须为有限十进制数: {value!r}")
    return parsed


def require_positive(qty, field="qty"):
    if qty <= _ZERO:
        raise ValidationError(f"{field} 必须为正数，实际为 {qty_str(qty)}")
    return qty


def require_non_negative(qty, field="qty"):
    if qty < _ZERO:
        raise ValidationError(f"{field} 不能为负数，实际为 {qty_str(qty)}")
    return qty


def qty_str(value):
    """Decimal 的规范字符串：无指数、去掉多余尾零，便于确定性比较。"""
    if value == _ZERO:
        return "0"
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.normalize(), "f")
