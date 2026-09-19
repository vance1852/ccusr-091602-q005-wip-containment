"""生产谱系图。

设计要点：
- 事件按 event_id 去重（重复扫描幂等）；同 ID 不同内容记冲突并保留先到者。
- 图状态是「注册记录 + 事件集合」的纯函数：每次变更后按规范序
  (occurred_at, event_id) 全量重建，因此乱序到达不影响最终形态。
- 每条事件必须满足数量守恒：sum(inputs) == sum(outputs)，否则拒绝入图。
- 引用到却从未被创建的对象记为 phantom（谱系数据缺口）；创建冲突、
  超耗（消耗 > 产出）记为 conflict，供领料复核与关闭报告使用。
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal

from app.model import ACTIONS, LOCATIONS, SHIPPED_LOCATIONS, GenealogyEvent
from app.quantity import parse_qty, qty_str, require_positive

# 单个对象最多保留的传播路径条数（防止病态数据下路径爆炸），超出置 truncated 标记。
MAX_PATHS_PER_LOT = 200


@dataclass(frozen=True)
class Edge:
    """一次事件在某个输入对象与某个输出对象之间形成的有向边。"""

    event_id: str
    action: str
    from_lot: str
    to_lot: str
    in_qty: Decimal
    out_qty: Decimal


class LotState:
    __slots__ = (
        "lot_id",
        "material_batch",
        "location",
        "created_qty",
        "created_by",
        "created_at",
        "consumed_qty",
        "phantom",
        "conflict",
    )

    def __init__(self, lot_id, material_batch, location, created_qty, created_by, created_at, phantom=False):
        self.lot_id = lot_id
        self.material_batch = material_batch
        self.location = location
        self.created_qty = created_qty
        self.created_by = created_by  # None 表示登记根批次
        self.created_at = created_at
        self.consumed_qty = Decimal(0)
        self.phantom = phantom
        self.conflict = False


class GenealogyGraph:
    def __init__(self):
        self._registrations = {}  # lot_id -> dict
        self._events = {}         # event_id -> GenealogyEvent
        self._issued = defaultdict(lambda: Decimal(0))  # lot_id -> 已领数量（领料台账，不参与重建）
        self.rejected_events = []  # [{event_id, reason}] 摄入期拒绝记录（守恒/结构）
        self._ingest_conflicts = []  # 摄入期冲突（重复登记/同 ID 不同内容），追加式保留
        self.conflicts = []        # [str] 摄入期 + 重建期冲突的确定性视图
        self._lots = {}
        self._children = defaultdict(list)
        self._parents = defaultdict(list)

    # ------------------------------------------------------------------ 摄入

    def register_lot(self, lot_id, qty, location, material_batch=None, received_at=None):
        """登记根批次（采购入库等系统外来源）。重复登记同内容幂等。"""
        if not isinstance(lot_id, str) or not lot_id.strip():
            return {"status": "rejected", "reason": "lot_id 必须为非空字符串"}
        lot_id = lot_id.strip()
        if location not in LOCATIONS:
            return {"status": "rejected", "reason": f"location 非法: {location!r}"}
        try:
            qty = require_positive(parse_qty(qty, "qty"), "qty")
        except Exception as exc:  # 摄入入口不抛异常，返回状态
            return {"status": "rejected", "reason": str(exc)}
        record = {
            "lot_id": lot_id,
            "qty": qty,
            "location": location,
            "material_batch": material_batch.strip() if isinstance(material_batch, str) and material_batch.strip() else None,
            "received_at": received_at.strip() if isinstance(received_at, str) and received_at.strip() else None,
        }
        existing = self._registrations.get(lot_id)
        if existing is not None:
            if existing == record:
                return {"status": "duplicate", "reason": "重复登记，内容一致"}
            self._ingest_conflicts.append(f"lot {lot_id}: 登记冲突，保留先到记录")
            self._rebuild()
            return {"status": "conflict", "reason": "lot_id 已登记且内容不一致"}
        self._registrations[lot_id] = record
        self._rebuild()
        return {"status": "applied"}

    def add_event(self, event):
        """摄入一条谱系事件。返回状态字典：applied / duplicate / conflict / rejected。"""
        if not isinstance(event, GenealogyEvent):
            try:
                event = GenealogyEvent.parse(event)
            except Exception as exc:
                eid = event.get("event_id") if isinstance(event, dict) else None
                self.rejected_events.append({"event_id": eid, "reason": str(exc)})
                return {"status": "rejected", "reason": str(exc)}
        if event.action not in ACTIONS:
            self.rejected_events.append({"event_id": event.event_id, "reason": f"action 非法: {event.action}"})
            return {"status": "rejected", "reason": f"action 非法: {event.action}"}
        # 数量守恒：拆 / 合 / 装配 / 返工 / 发运，输入总量必须等于输出总量
        if event.total_in() != event.total_out():
            reason = (
                f"数量不守恒: 输入 {qty_str(event.total_in())} != 输出 {qty_str(event.total_out())}"
            )
            self.rejected_events.append({"event_id": event.event_id, "reason": reason})
            return {"status": "rejected", "reason": reason}
        existing = self._events.get(event.event_id)
        if existing is not None:
            if existing == event:
                return {"status": "duplicate", "reason": "重复扫描，内容一致"}
            self._ingest_conflicts.append(f"event {event.event_id}: 同 ID 不同内容，保留先到事件")
            self._rebuild()
            return {"status": "conflict", "reason": "event_id 已存在且内容不一致"}
        self._events[event.event_id] = event
        self._rebuild()
        return {"status": "applied"}

    def record_issue(self, lot_id, qty):
        """登记一笔领料发出（只影响可用量，不改变谱系结构）。"""
        self._issued[lot_id] += qty

    # ------------------------------------------------------------------ 重建

    def _rebuild(self):
        self._lots = {}
        self._children = defaultdict(list)
        self._parents = defaultdict(list)
        self.conflicts = list(self._ingest_conflicts)
        # 1. 根批次
        for lot_id in sorted(self._registrations):
            reg = self._registrations[lot_id]
            self._lots[lot_id] = LotState(
                lot_id=lot_id,
                material_batch=reg["material_batch"],
                location=reg["location"],
                created_qty=reg["qty"],
                created_by=None,
                created_at=reg["received_at"],
            )
        # 2. 事件按规范序回放
        for event in sorted(self._events.values(), key=lambda e: (e.occurred_at, e.event_id)):
            self._apply_event(event)
        # 3. 邻接表排序，保证遍历确定性
        for adj in (self._children, self._parents):
            for lot_id in adj:
                adj[lot_id].sort(key=lambda e: (e.event_id, e.from_lot, e.to_lot))

    def _apply_event(self, event):
        # 输入：累计消耗；缺失对象记 phantom；超耗记冲突
        first_location = None
        for port in event.inputs:
            lot = self._lots.get(port.lot_id)
            if lot is None:
                lot = LotState(port.lot_id, None, "warehouse", Decimal(0), None, None, phantom=True)
                self._lots[port.lot_id] = lot
                self.conflicts.append(f"lot {port.lot_id}: 被事件 {event.event_id} 消耗但从未创建（谱系缺口）")
            lot.consumed_qty += port.qty
            if lot.consumed_qty > lot.created_qty:
                lot.conflict = True
                self.conflicts.append(
                    f"lot {port.lot_id}: 超耗，累计消耗 {qty_str(lot.consumed_qty)} > 产出 {qty_str(lot.created_qty)}"
                )
            if first_location is None:
                first_location = lot.location
        # 输出落位：端口 > 事件 > 首输入位置 > warehouse；ship 默认 transit
        for port in event.outputs:
            location = port.location or event.output_location
            if location is None:
                location = "transit" if event.action == "ship" else (first_location or "warehouse")
            existing = self._lots.get(port.lot_id)
            if existing is None:
                self._lots[port.lot_id] = LotState(
                    lot_id=port.lot_id,
                    material_batch=event.material_batch,
                    location=location,
                    created_qty=port.qty,
                    created_by=event.event_id,
                    created_at=event.occurred_at,
                )
            elif existing.phantom:
                existing.phantom = False
                existing.material_batch = existing.material_batch or event.material_batch
                existing.location = location
                existing.created_qty = port.qty
                existing.created_by = event.event_id
                existing.created_at = event.occurred_at
            else:
                existing.conflict = True
                self.conflicts.append(
                    f"lot {port.lot_id}: 重复创建（事件 {event.event_id}），保留首次创建记录"
                )
        # 边：无论数量是否冲突，谱系关系都保留（围堵宁滥勿缺）
        for in_port in event.inputs:
            for out_port in event.outputs:
                edge = Edge(
                    event_id=event.event_id,
                    action=event.action,
                    from_lot=in_port.lot_id,
                    to_lot=out_port.lot_id,
                    in_qty=in_port.qty,
                    out_qty=out_port.qty,
                )
                self._children[in_port.lot_id].append(edge)
                self._parents[out_port.lot_id].append(edge)

    # ------------------------------------------------------------------ 查询

    def lot(self, lot_id):
        return self._lots.get(lot_id)

    def lots(self):
        return self._lots

    def known(self, lot_id):
        lot = self._lots.get(lot_id)
        return lot is not None and not lot.phantom

    def available(self, lot_id):
        """可用量 = 产出 - 谱系消耗 - 领料发出。数据冲突时可能为负，由调用方处置。"""
        lot = self._lots.get(lot_id)
        if lot is None:
            return Decimal(0)
        return lot.created_qty - lot.consumed_qty - self._issued.get(lot_id, Decimal(0))

    def issued(self, lot_id):
        return self._issued.get(lot_id, Decimal(0))

    def events(self):
        return sorted(self._events.values(), key=lambda e: (e.occurred_at, e.event_id))

    def phantom_lots(self):
        return sorted(lot_id for lot_id, lot in self._lots.items() if lot.phantom)

    # ------------------------------------------------------------------ 传播

    def descendants(self, seeds):
        """从种子对象前向可达的所有对象（含种子）。只沿产出方向传播。"""
        tainted = {s for s in seeds if s in self._lots}
        queue = deque(tainted)
        while queue:
            current = queue.popleft()
            for edge in self._children.get(current, ()):
                if edge.to_lot not in tainted:
                    tainted.add(edge.to_lot)
                    queue.append(edge.to_lot)
        return tainted

    def propagation_paths(self, seeds):
        """枚举每个受影响对象的全部传播路径（简单路径，防返工环）。

        返回 (paths, truncated)：paths[lot] = tuple(path, ...)，
        path 为 Edge 元组，种子对象的路径为空元组（表示源头）。
        """
        seeds = [s for s in sorted(seeds) if s in self._lots]
        paths = {s: [()] for s in seeds}
        truncated = set()
        for seed in seeds:
            stack = [(seed, ())]
            while stack:
                node, path = stack.pop()
                visited = {seed} | {e.to_lot for e in path}
                for edge in self._children.get(node, ()):
                    if edge.to_lot in visited:
                        continue  # 返工环：只走简单路径
                    new_path = path + (edge,)
                    bucket = paths.setdefault(edge.to_lot, [])
                    if len(bucket) >= MAX_PATHS_PER_LOT:
                        truncated.add(edge.to_lot)
                        continue
                    if new_path not in bucket:
                        bucket.append(new_path)
                    stack.append((edge.to_lot, new_path))
        frozen = {lot: tuple(ps) for lot, ps in paths.items()}
        return frozen, truncated
