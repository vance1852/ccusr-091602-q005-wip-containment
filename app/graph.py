"""谱系图: 由对象台账与事件集合推导的确定性视图。

相同 (台账, 事件集合) 必然推导出相同图谱 —— 与事件到达顺序无关。
图只描述结构推导(在库量、位置、下游传播), 不持有任何可变业务状态。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import SHIPPED_LOCATIONS, ObjectRecord
from .quantity import ZERO


@dataclass(frozen=True)
class PathStep:
    """传播路径上的一步: 该对象经由哪条事件、从哪个上游对象被卷入。

    种子的 via_event / from_object 为 None。
    """

    object_id: str
    via_event: str | None
    from_object: str | None


@dataclass
class Impact:
    """一次下游传播的结论: 受影响对象、受影响事件与引入证据。"""

    objects: frozenset
    events: frozenset
    introduced_by: dict  # object_id -> (event_id | None, source_object_id | None)

    def path_to(self, object_id):
        """沿引入证据回溯到种子, 返回从种子到目标的 PathStep 序列。"""
        if object_id not in self.objects:
            return None
        steps = []
        current = object_id
        seen = set()
        while current is not None and current not in seen:
            seen.add(current)
            event_id, source = self.introduced_by.get(current, (None, None))
            steps.append(PathStep(current, event_id, source))
            current = source
        steps.reverse()
        return tuple(steps)

    @staticmethod
    def union(left, right):
        """单调合并: 已记录的影响永不消失, 路径证据以先记录者为准。"""
        introduced = dict(left.introduced_by)
        for oid, evidence in right.introduced_by.items():
            introduced.setdefault(oid, evidence)
        return Impact(left.objects | right.objects,
                      left.events | right.events,
                      introduced)


class GenealogyGraph:
    """对象台账 + 事件日志的推导视图。"""

    def __init__(self, objects, events):
        self.events = tuple(sorted(events, key=lambda e: e.sort_key()))
        self.objects = {}
        self.violations = []
        referenced = set()
        for event in self.events:
            for portion in event.inputs + event.outputs:
                referenced.add(portion.object_id)
        for oid in sorted(referenced):
            rec = objects.get(oid)
            if rec is None:
                rec = ObjectRecord(oid, ZERO, "warehouse", None, implicit=True)
                self.violations.append(f"对象 {oid} 未登记, 由事件引用隐式补登")
            self.objects[oid] = rec
        for oid, rec in objects.items():
            self.objects.setdefault(oid, rec)
        self._produced = {}
        self._consumed = {}
        self._location = {}
        for event in self.events:
            for portion in event.inputs:
                self._consumed[portion.object_id] = \
                    self._consumed.get(portion.object_id, ZERO) + portion.qty
            for portion in event.outputs:
                self._produced[portion.object_id] = \
                    self._produced.get(portion.object_id, ZERO) + portion.qty
                self._location[portion.object_id] = event.location
        for oid, rec in self.objects.items():
            self._location.setdefault(oid, rec.location)
        for oid in sorted(self.objects):
            if self.on_hand(oid) < ZERO:
                self.violations.append(f"对象 {oid} 库存透支: 在库 {self.on_hand(oid)}")

    def produced(self, object_id):
        return self._produced.get(object_id, ZERO)

    def consumed(self, object_id):
        return self._consumed.get(object_id, ZERO)

    def on_hand(self, object_id):
        rec = self.objects.get(object_id)
        if rec is None:
            return ZERO
        return rec.registered_qty + self.produced(object_id) - self.consumed(object_id)

    def location_of(self, object_id):
        return self._location.get(object_id)

    def is_shipped(self, object_id):
        return self._location.get(object_id) in SHIPPED_LOCATIONS

    def downstream_impact(self, seeds):
        """从种子对象沿事件输出方向传播到不动点。

        事件按 (occurred_at, event_id) 排序扫描。第一遍计算可达集合;
        第二遍为"本身是受影响事件输出"的种子补全事件证据, 使路径证据
        能沿物料流回溯到最上游的根, 而不是停在中间种子上。
        """
        introduced = {}
        for seed in sorted(seeds):
            if seed in self.objects:
                introduced.setdefault(seed, (None, None))
        affected_events = set()
        changed = True
        while changed:
            changed = False
            for event in self.events:
                source = None
                for portion in event.inputs:
                    if portion.object_id in introduced:
                        source = portion.object_id
                        break
                if source is None:
                    continue
                affected_events.add(event.event_id)
                for portion in event.outputs:
                    if portion.object_id not in introduced:
                        introduced[portion.object_id] = (event.event_id, source)
                        changed = True
        for event in self.events:
            if event.event_id not in affected_events:
                continue
            source = None
            for portion in event.inputs:
                if portion.object_id in introduced:
                    source = portion.object_id
                    break
            for portion in event.outputs:
                oid = portion.object_id
                if oid != source and introduced.get(oid) == (None, None):
                    introduced[oid] = (event.event_id, source)
        return Impact(frozenset(introduced), frozenset(affected_events), introduced)
