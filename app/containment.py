"""围堵事件（case）：范围圈定、单调扩展的版本、检验 / 偏差 / 放行 / 报废记录。

不变式：
- 围堵版本只能扩展：新版本的受影响对象集合 ⊇ 旧版本，路径证据只增不减。
- 受影响集合一旦记录永不消失；处置（放行 / 报废 / 召回）只改变对象的状态桶。
- 放行数量必须逐笔关联到检验合格量或偏差批准量，且累计不得超过其额度。
"""

from dataclasses import dataclass
from decimal import Decimal

from app.errors import (
    CaseClosedError,
    InsufficientBasisError,
    SeparationOfDutiesError,
    UnknownRecordError,
    ValidationError,
)
from app.model import INSPECTION_KINDS, INSPECTION_RESULTS, SHIPPED_LOCATIONS
from app.quantity import parse_qty, qty_str, require_positive

SCOPE_TYPES = ("equipment", "material_batch", "time_window")


@dataclass(frozen=True)
class Scope:
    """调查员圈定的围堵范围：设备 / 物料批次 / 时间窗。"""

    kind: str
    equipment_id: str = None
    material_batch: str = None
    declared_qty: Decimal = None  # 疑似总量（用于计算尚未定位数量）
    start: str = None
    end: str = None

    @staticmethod
    def parse(raw):
        if not isinstance(raw, dict):
            raise ValidationError(f"scope 必须为对象: {raw!r}")
        kind = raw.get("type")
        if kind not in SCOPE_TYPES:
            raise ValidationError(f"scope.type 非法: {kind!r}，允许值 {SCOPE_TYPES}")
        declared = raw.get("declared_qty")
        declared = parse_qty(declared, "scope.declared_qty") if declared is not None else None
        if declared is not None and declared < 0:
            raise ValidationError("scope.declared_qty 不能为负")
        if kind == "equipment":
            equipment_id = raw.get("equipment_id")
            if not isinstance(equipment_id, str) or not equipment_id.strip():
                raise ValidationError("equipment 范围必须给出 equipment_id")
            return Scope(kind=kind, equipment_id=equipment_id.strip(), declared_qty=declared)
        if kind == "material_batch":
            batch = raw.get("material_batch")
            if not isinstance(batch, str) or not batch.strip():
                raise ValidationError("material_batch 范围必须给出 material_batch")
            return Scope(kind=kind, material_batch=batch.strip(), declared_qty=declared)
        start, end = raw.get("start"), raw.get("end")
        if not isinstance(start, str) or not start or not isinstance(end, str) or not end:
            raise ValidationError("time_window 范围必须给出 start 与 end（ISO-8601 字符串）")
        if start > end:
            raise ValidationError("time_window 范围 start 不能晚于 end")
        return Scope(kind=kind, start=start, end=end, declared_qty=declared)

    def describe(self):
        if self.kind == "equipment":
            return f"设备 {self.equipment_id}"
        if self.kind == "material_batch":
            return f"物料批次 {self.material_batch}"
        return f"时间窗 [{self.start}, {self.end}]"


@dataclass
class InspectionRecord:
    inspection_id: str
    lot_id: str
    kind: str          # sampling | reinspection
    qty: Decimal
    result: str        # pass | fail | pending
    inspector: str
    seq: int
    consumed: Decimal = Decimal(0)  # 已被放行引用的数量
    finalized_by: str = None

    @property
    def remaining(self):
        return self.qty - self.consumed


@dataclass
class DeviationRecord:
    deviation_id: str
    lot_id: str
    qty: Decimal
    approver: str
    reason: str
    seq: int
    consumed: Decimal = Decimal(0)

    @property
    def remaining(self):
        return self.qty - self.consumed


@dataclass
class ReleaseRecord:
    release_id: str
    lot_id: str
    qty: Decimal
    by: str
    basis: tuple  # ({"type": "inspection"|"deviation", "id": str, "qty": Decimal}, ...)
    seq: int


@dataclass
class ScrapRecord:
    lot_id: str
    qty: Decimal
    by: str
    reason: str
    seq: int


@dataclass
class Version:
    """一次围堵计算的快照。impacted 为对象 ID 有序元组；paths 为路径证据。"""

    seq: int
    reason: str
    impacted: tuple
    paths: dict          # lot_id -> tuple[path, ...]
    located_qty: Decimal
    truncated: tuple = ()


def select_seeds(scope, graph):
    """按范围从谱系图中选出种子对象。"""
    seeds = set()
    if scope.kind == "equipment":
        for event in graph.events():
            if event.equipment_id == scope.equipment_id:
                seeds.update(p.lot_id for p in event.outputs)
    elif scope.kind == "material_batch":
        for lot_id, lot in graph.lots().items():
            if lot.material_batch == scope.material_batch:
                seeds.add(lot_id)
    else:  # time_window
        for event in graph.events():
            if scope.start <= event.occurred_at <= scope.end:
                seeds.update(p.lot_id for p in event.outputs)
    return seeds


def select_located_qty(scope, graph, seeds):
    """已定位数量：种子对象的产出量之和（用于与疑似总量比对）。"""
    total = Decimal(0)
    for lot_id in seeds:
        lot = graph.lot(lot_id)
        if lot is not None:
            total += lot.created_qty
    return total


class ContainmentCase:
    def __init__(self, case_id, investigator, scope):
        self.case_id = case_id
        self.investigator = investigator
        self.scope = scope
        self.status = "open"
        self.closed_by = None
        self.versions = []
        self.inspections = {}
        self.deviations = {}
        self.releases = {}
        self.scraps = []
        self._seq = 0

    def _next_seq(self):
        self._seq += 1
        return self._seq

    # -------------------------------------------------------------- 版本

    @property
    def impacted(self):
        """当前受影响对象集合（单调不减）。"""
        if not self.versions:
            return frozenset()
        return frozenset(self.versions[-1].impacted)

    def refresh(self, graph, reason):
        """重算围堵边界并与历史版本取并集。返回新版本或 None（无扩展）。"""
        self._require_open()
        seeds = select_seeds(self.scope, graph)
        impacted = graph.descendants(seeds)
        paths, truncated = graph.propagation_paths(seeds)
        located = select_located_qty(self.scope, graph, seeds)

        prev = self.versions[-1] if self.versions else None
        if prev is not None:
            merged = set(prev.impacted) | impacted
            merged_paths = {lot: list(ps) for lot, ps in prev.paths.items()}
            for lot, new_paths in paths.items():
                bucket = merged_paths.setdefault(lot, [])
                for p in new_paths:
                    if p not in bucket:
                        bucket.append(p)
            impacted = merged
            paths = {lot: tuple(ps) for lot, ps in merged_paths.items()}
            located = max(located, prev.located_qty)

        if prev is not None and set(prev.impacted) == set(impacted) and prev.paths.keys() == paths.keys():
            same = all(set(prev.paths.get(lot, ())) == set(paths.get(lot, ())) for lot in paths)
            if same:
                return None  # 无扩展，不产生新版本
        version = Version(
            seq=len(self.versions) + 1,
            reason=reason,
            impacted=tuple(sorted(impacted)),
            paths=paths,
            located_qty=located,
            truncated=tuple(sorted(truncated)),
        )
        self.versions.append(version)
        return version

    # -------------------------------------------------------------- 检验 / 偏差 / 放行 / 报废

    def add_inspection(self, inspection_id, lot_id, kind, qty, result, inspector):
        self._require_open()
        self._require_impacted(lot_id)
        if not isinstance(inspection_id, str) or not inspection_id.strip():
            raise ValidationError("inspection_id 必须为非空字符串")
        inspection_id = inspection_id.strip()
        if inspection_id in self.inspections:
            raise ValidationError(f"inspection_id 已存在: {inspection_id}")
        if kind not in INSPECTION_KINDS:
            raise ValidationError(f"检验类型非法: {kind!r}，允许值 {INSPECTION_KINDS}")
        if result not in INSPECTION_RESULTS:
            raise ValidationError(f"检验结论非法: {result!r}，允许值 {INSPECTION_RESULTS}")
        if not isinstance(inspector, str) or not inspector.strip():
            raise ValidationError("检验人必须为非空字符串")
        qty = require_positive(parse_qty(qty, "qty"), "qty")
        if kind == "reinspection" and not any(
            r.lot_id == lot_id for r in self.inspections.values()
        ):
            raise ValidationError("复验必须发生在该对象已有检验记录之后")
        record = InspectionRecord(
            inspection_id=inspection_id,
            lot_id=lot_id,
            kind=kind,
            qty=qty,
            result=result,
            inspector=inspector.strip(),
            seq=self._next_seq(),
        )
        self.inspections[inspection_id] = record
        return record

    def finalize_inspection(self, inspection_id, result, by):
        """把待结论（pending）的检验落定为 pass / fail。"""
        self._require_open()
        record = self.inspections.get(inspection_id)
        if record is None:
            raise UnknownRecordError(f"检验记录不存在: {inspection_id}")
        if record.result != "pending":
            raise ValidationError(f"检验 {inspection_id} 已有结论 {record.result}，不可重复落定")
        if result not in ("pass", "fail"):
            raise ValidationError("落定结论必须为 pass 或 fail")
        if not isinstance(by, str) or not by.strip():
            raise ValidationError("落单人必须为非空字符串")
        record.result = result
        record.finalized_by = by.strip()
        return record

    def add_deviation(self, deviation_id, lot_id, qty, approver, reason):
        """偏差批准：批准人不得与调查员为同一人（职责分离）。"""
        self._require_open()
        self._require_impacted(lot_id)
        if not isinstance(deviation_id, str) or not deviation_id.strip():
            raise ValidationError("deviation_id 必须为非空字符串")
        deviation_id = deviation_id.strip()
        if deviation_id in self.deviations:
            raise ValidationError(f"deviation_id 已存在: {deviation_id}")
        if not isinstance(approver, str) or not approver.strip():
            raise ValidationError("批准人必须为非空字符串")
        approver = approver.strip()
        if approver == self.investigator:
            raise SeparationOfDutiesError(
                f"批准人 {approver} 与调查员为同一人，违反职责分离"
            )
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("偏差批准必须给出理由")
        qty = require_positive(parse_qty(qty, "qty"), "qty")
        record = DeviationRecord(
            deviation_id=deviation_id,
            lot_id=lot_id,
            qty=qty,
            approver=approver,
            reason=reason.strip(),
            seq=self._next_seq(),
        )
        self.deviations[deviation_id] = record
        return record

    def release(self, release_id, lot_id, qty, by, graph, inspection_ids=(), deviation_ids=()):
        """部分放行：数量必须逐笔落到检验合格量 / 偏差批准量上。"""
        self._require_open()
        self._require_impacted(lot_id)
        if not isinstance(release_id, str) or not release_id.strip():
            raise ValidationError("release_id 必须为非空字符串")
        release_id = release_id.strip()
        if release_id in self.releases:
            raise ValidationError(f"release_id 已存在: {release_id}")
        if not isinstance(by, str) or not by.strip():
            raise ValidationError("放行操作人必须为非空字符串")
        qty = require_positive(parse_qty(qty, "qty"), "qty")

        held = self.held_qty(lot_id, graph)
        if qty > held:
            raise ValidationError(
                f"放行数量 {qty_str(qty)} 超过在冻数量 {qty_str(held)}"
            )

        # 第一阶段：只试算分配，不做任何修改
        plan = []
        remaining = qty
        for iid in inspection_ids:
            record = self.inspections.get(iid)
            if record is None:
                raise UnknownRecordError(f"检验记录不存在: {iid}")
            if record.lot_id != lot_id:
                raise ValidationError(f"检验 {iid} 不属于对象 {lot_id}")
            if record.result != "pass":
                raise ValidationError(f"检验 {iid} 结论为 {record.result}，不能作为放行依据")
            take = min(record.remaining, remaining)
            if take > 0:
                plan.append(("inspection", record, take))
                remaining -= take
        for did in deviation_ids:
            record = self.deviations.get(did)
            if record is None:
                raise UnknownRecordError(f"偏差批准不存在: {did}")
            if record.lot_id != lot_id:
                raise ValidationError(f"偏差 {did} 不属于对象 {lot_id}")
            take = min(record.remaining, remaining)
            if take > 0:
                plan.append(("deviation", record, take))
                remaining -= take
        if remaining > 0:
            raise InsufficientBasisError(
                f"放行依据不足：还差 {qty_str(remaining)}，"
                f"检验合格与偏差批准的可用额度不足以覆盖放行数量"
            )

        # 第二阶段：应用分配
        basis = []
        for kind, record, take in plan:
            record.consumed += take
            basis.append({"type": kind, "id": record.inspection_id if kind == "inspection" else record.deviation_id, "qty": take})
        record = ReleaseRecord(
            release_id=release_id,
            lot_id=lot_id,
            qty=qty,
            by=by.strip(),
            basis=tuple(basis),
            seq=self._next_seq(),
        )
        self.releases[release_id] = record
        return record

    def scrap(self, lot_id, qty, by, reason, graph):
        self._require_open()
        self._require_impacted(lot_id)
        if not isinstance(by, str) or not by.strip():
            raise ValidationError("报废操作人必须为非空字符串")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("报废必须给出理由")
        qty = require_positive(parse_qty(qty, "qty"), "qty")
        held = self.held_qty(lot_id, graph)
        if qty > held:
            raise ValidationError(f"报废数量 {qty_str(qty)} 超过在冻数量 {qty_str(held)}")
        record = ScrapRecord(lot_id=lot_id, qty=qty, by=by.strip(), reason=reason.strip(), seq=self._next_seq())
        self.scraps.append(record)
        return record

    # -------------------------------------------------------------- 派生数量

    def released_qty(self, lot_id):
        return sum((r.qty for r in self.releases.values() if r.lot_id == lot_id), Decimal(0))

    def scrapped_qty(self, lot_id):
        return sum((s.qty for s in self.scraps if s.lot_id == lot_id), Decimal(0))

    def held_qty(self, lot_id, graph):
        """在冻数量 = 可用量 - 已放行 - 已报废（不为负）。"""
        available = graph.available(lot_id)
        held = available - self.released_qty(lot_id) - self.scrapped_qty(lot_id)
        return max(held, Decimal(0))

    def has_pending_inspection(self, lot_id):
        return any(r.lot_id == lot_id and r.result == "pending" for r in self.inspections.values())

    def unlocated_qty(self):
        """尚未定位数量 = 疑似总量 - 已定位数量（未提供疑似总量时为 None）。"""
        if self.scope.declared_qty is None or not self.versions:
            return None
        unlocated = self.scope.declared_qty - self.versions[-1].located_qty
        return max(unlocated, Decimal(0))

    def _require_open(self):
        if self.status != "open":
            raise CaseClosedError(f"事件 {self.case_id} 已关闭，禁止写入")

    def _require_impacted(self, lot_id):
        if lot_id not in self.impacted:
            raise ValidationError(f"对象 {lot_id} 不在事件 {self.case_id} 的受影响范围内")


def boundary_snapshot(case, graph):
    """当前围堵边界的派生视图：在冻 / 召回 / 已放行 / 已报废分桶。"""
    held, recalls = [], []
    for lot_id in sorted(case.impacted):
        lot = graph.lot(lot_id)
        if lot is None:
            continue
        available = graph.available(lot_id)
        if available < 0:
            available = Decimal(0)
        released = case.released_qty(lot_id)
        scrapped = case.scrapped_qty(lot_id)
        outstanding = available - released - scrapped
        if outstanding < 0:
            outstanding = Decimal(0)
        entry = {
            "lot_id": lot_id,
            "location": lot.location,
            "available": qty_str(available),
            "released": qty_str(released),
            "scrapped": qty_str(scrapped),
            "outstanding": qty_str(outstanding),
        }
        if lot.location in SHIPPED_LOCATIONS:
            entry["disposition"] = "recalled"
            recalls.append(entry)
        else:
            if outstanding > 0:
                entry["disposition"] = "held"
            elif released > 0:
                entry["disposition"] = "released"
            elif scrapped > 0:
                entry["disposition"] = "scrapped"
            else:
                entry["disposition"] = "held"  # 数量已全部流入下游的中间节点
            held.append(entry)
    return {"held": held, "recalls": recalls}
