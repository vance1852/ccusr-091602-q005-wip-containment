"""谱系事件模型与契约常量。

动作、处置、位置枚举以 domain_contract.json 为唯一权威来源。
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple

from app.errors import ValidationError
from app.quantity import parse_qty, require_positive

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "domain_contract.json"


def _load_contract():
    try:
        return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {
            "genealogy_actions": ["split", "merge", "assemble", "rework", "ship"],
            "dispositions": ["held", "review_required", "released", "recalled", "scrapped"],
            "locations": ["warehouse", "line", "transit", "customer"],
        }


CONTRACT = _load_contract()
ACTIONS = tuple(CONTRACT["genealogy_actions"])
DISPOSITIONS = tuple(CONTRACT["dispositions"])
LOCATIONS = tuple(CONTRACT["locations"])

# 已发运位置：进入这些位置的对象不再属于在库冻结，只能单列召回。
SHIPPED_LOCATIONS = tuple(loc for loc in ("transit", "customer") if loc in LOCATIONS)

# 领料结论
ALLOW = "allow"
BLOCK = "block"
REVIEW_REQUIRED = "review_required"
DECISIONS = (ALLOW, BLOCK, REVIEW_REQUIRED)

# 检验类型与结果
INSPECTION_KINDS = ("sampling", "reinspection")
INSPECTION_RESULTS = ("pass", "fail", "pending")


@dataclass(frozen=True)
class EventPort:
    """事件的一端数量：某对象 + 数量 + 可选落位。"""

    lot_id: str
    qty: Decimal
    location: Optional[str] = None

    @staticmethod
    def parse(raw, field):
        if not isinstance(raw, dict):
            raise ValidationError(f"{field} 必须为对象: {raw!r}")
        lot_id = raw.get("lot_id")
        if not isinstance(lot_id, str) or not lot_id.strip():
            raise ValidationError(f"{field}.lot_id 必须为非空字符串")
        qty = require_positive(parse_qty(raw.get("qty"), f"{field}.qty"), f"{field}.qty")
        location = raw.get("location")
        if location is not None and location not in LOCATIONS:
            raise ValidationError(f"{field}.location 非法: {location!r}")
        return EventPort(lot_id.strip(), qty, location)


@dataclass(frozen=True)
class GenealogyEvent:
    """一条带数量的谱系关系：拆分 / 合并 / 装配 / 返工 / 发运。"""

    event_id: str
    action: str
    inputs: Tuple[EventPort, ...]
    outputs: Tuple[EventPort, ...]
    occurred_at: str
    equipment_id: Optional[str] = None
    material_batch: Optional[str] = None
    output_location: Optional[str] = None

    @staticmethod
    def parse(raw):
        if not isinstance(raw, dict):
            raise ValidationError(f"事件必须为对象: {raw!r}")
        event_id = raw.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValidationError("event_id 必须为非空字符串")
        action = raw.get("action")
        if action not in ACTIONS:
            raise ValidationError(f"action 非法: {action!r}，允许值 {ACTIONS}")
        occurred_at = raw.get("occurred_at")
        if not isinstance(occurred_at, str) or not occurred_at.strip():
            raise ValidationError("occurred_at 必须为 ISO-8601 字符串")

        inputs = tuple(EventPort.parse(p, f"inputs[{i}]") for i, p in enumerate(raw.get("inputs") or []))
        outputs = tuple(EventPort.parse(p, f"outputs[{i}]") for i, p in enumerate(raw.get("outputs") or []))
        if not inputs:
            raise ValidationError("事件至少需要一个输入")
        if not outputs:
            raise ValidationError("事件至少需要一个输出")

        output_location = raw.get("output_location")
        if output_location is not None and output_location not in LOCATIONS:
            raise ValidationError(f"output_location 非法: {output_location!r}")
        if action == "ship":
            resolved = output_location or next((p.location for p in outputs if p.location), None)
            if resolved is not None and resolved not in SHIPPED_LOCATIONS:
                raise ValidationError(f"ship 事件的输出位置必须属于 {SHIPPED_LOCATIONS}")

        equipment_id = raw.get("equipment_id")
        material_batch = raw.get("material_batch")
        return GenealogyEvent(
            event_id=event_id.strip(),
            action=action,
            inputs=inputs,
            outputs=outputs,
            occurred_at=occurred_at.strip(),
            equipment_id=equipment_id.strip() if isinstance(equipment_id, str) and equipment_id.strip() else None,
            material_batch=material_batch.strip() if isinstance(material_batch, str) and material_batch.strip() else None,
            output_location=output_location,
        )

    def total_in(self):
        return sum((p.qty for p in self.inputs), Decimal(0))

    def total_out(self):
        return sum((p.qty for p in self.outputs), Decimal(0))
