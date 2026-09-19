"""谱系事件与对象台账模型。

事件是不可变值对象: 构造时完成规范化(同对象份量合并、按对象标识排序)
与守恒校验。相同内容的事件必然相等, 重复扫描因此可以安全去重;
乱序到达不影响最终图谱 —— 排序键为 (occurred_at, event_id)。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .errors import ConservationError, EventShapeError, QuantityError
from .quantity import ZERO, parse_quantity

ACTIONS = ("split", "merge", "assemble", "rework", "ship")
LOCATIONS = ("warehouse", "line", "transit", "customer")
IN_STOCK_LOCATIONS = ("warehouse", "line")
SHIPPED_LOCATIONS = ("transit", "customer")

# 动作 -> (输入对象数目, 输出对象数目); None 表示"至少一个"
_ACTION_SHAPES = {
    "split": (1, None),
    "merge": (None, 1),
    "assemble": (None, 1),
    "rework": (1, 1),
    "ship": (1, 1),
}


@dataclass(frozen=True)
class Portion:
    """一条带数量的对象引用。数量为正数 Decimal。"""

    object_id: str
    qty: Decimal

    def __init__(self, object_id, qty):
        if not isinstance(object_id, str) or not object_id:
            raise EventShapeError(f"对象标识必须为非空字符串: {object_id!r}")
        q = parse_quantity(qty, field=f"portion[{object_id}]")
        if q <= ZERO:
            raise QuantityError(f"portion[{object_id}]: 份量必须为正")
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "qty", q)


def _normalize_portions(raw, *, field):
    """接受 {对象: 数量} 或 [(对象, 数量)], 合并同对象份量并按对象标识排序。"""
    items = raw.items() if isinstance(raw, dict) else raw
    merged: dict[str, Decimal] = {}
    for entry in items:
        if isinstance(entry, Portion):
            oid, qty = entry.object_id, entry.qty
        else:
            oid, qty = entry
        if not isinstance(oid, str) or not oid:
            raise EventShapeError(f"{field}: 对象标识必须为非空字符串: {oid!r}")
        q = parse_quantity(qty, field=f"{field}[{oid}]")
        if q <= ZERO:
            raise QuantityError(f"{field}[{oid}]: 份量必须为正")
        merged[oid] = merged.get(oid, ZERO) + q
    if not merged:
        raise EventShapeError(f"{field}: 至少需要一个份量")
    return tuple(Portion(oid, merged[oid]) for oid in sorted(merged))


@dataclass(frozen=True)
class GenealogyEvent:
    """一条不可变谱系事件。

    occurred_at 使用 ISO8601 字符串; 事件排序键为 (occurred_at, event_id),
    与到达顺序无关, 保证乱序重放得到相同图谱。
    """

    event_id: str
    action: str
    inputs: tuple
    outputs: tuple
    occurred_at: str
    location: str
    equipment: str | None
    material_lot: str | None
    note: str

    def __init__(self, event_id, action, inputs, outputs, occurred_at, location,
                 equipment=None, material_lot=None, note=""):
        if not isinstance(event_id, str) or not event_id:
            raise EventShapeError("event_id 必须为非空字符串")
        if action not in _ACTION_SHAPES:
            raise EventShapeError(f"未知谱系动作: {action!r}; 支持 {ACTIONS}")
        if location not in LOCATIONS:
            raise EventShapeError(f"未知位置: {location!r}; 支持 {LOCATIONS}")
        if action == "ship" and location not in SHIPPED_LOCATIONS:
            raise EventShapeError("ship 事件的输出位置必须是 transit 或 customer")
        if not isinstance(occurred_at, str) or not occurred_at:
            raise EventShapeError("occurred_at 必须为 ISO8601 字符串")
        norm_in = _normalize_portions(inputs, field="inputs")
        norm_out = _normalize_portions(outputs, field="outputs")
        exp_in, exp_out = _ACTION_SHAPES[action]
        if exp_in is not None and len(norm_in) != exp_in:
            raise EventShapeError(
                f"{action} 要求恰好 {exp_in} 个输入对象, 实际 {len(norm_in)}")
        if exp_out is not None and len(norm_out) != exp_out:
            raise EventShapeError(
                f"{action} 要求恰好 {exp_out} 个输出对象, 实际 {len(norm_out)}")
        total_in = sum((p.qty for p in norm_in), ZERO)
        total_out = sum((p.qty for p in norm_out), ZERO)
        if total_in != total_out:
            raise ConservationError(
                f"事件 {event_id}({action}) 数量不守恒: "
                f"输入合计 {total_in} != 输出合计 {total_out}")
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "inputs", norm_in)
        object.__setattr__(self, "outputs", norm_out)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "location", location)
        object.__setattr__(self, "equipment", equipment)
        object.__setattr__(self, "material_lot", material_lot)
        object.__setattr__(self, "note", note)

    def sort_key(self):
        return (self.occurred_at, self.event_id)

    @property
    def total_in(self):
        return sum((p.qty for p in self.inputs), ZERO)

    @property
    def total_out(self):
        return sum((p.qty for p in self.outputs), ZERO)


@dataclass(frozen=True)
class ObjectRecord:
    """对象台账记录: 一批已登记的物料/在制品/成品。"""

    object_id: str
    registered_qty: Decimal
    location: str
    material_lot: str | None
    implicit: bool

    def __init__(self, object_id, registered_qty, location="warehouse",
                 material_lot=None, implicit=False):
        if not isinstance(object_id, str) or not object_id:
            raise EventShapeError(f"对象标识必须为非空字符串: {object_id!r}")
        if location not in LOCATIONS:
            raise EventShapeError(f"未知位置: {location!r}; 支持 {LOCATIONS}")
        object.__setattr__(self, "object_id", object_id)
        object.__setattr__(self, "registered_qty",
                           parse_quantity(registered_qty, field=f"register[{object_id}]"))
        object.__setattr__(self, "location", location)
        object.__setattr__(self, "material_lot", material_lot)
        object.__setattr__(self, "implicit", bool(implicit))
