"""围堵事件聚合: 圈定条件、版本化影响范围、检验/偏差/放行/报废记录。"""

from __future__ import annotations

from dataclasses import dataclass

from .quantity import ZERO


@dataclass(frozen=True)
class ScopeCriteria:
    """调查员圈定条件: 设备 / 物料批次 / 时间窗, 多条件取并集。"""

    equipment: str | None = None
    material_lot: str | None = None
    time_window: tuple | None = None  # (start, end) ISO8601, 闭区间

    def __post_init__(self):
        if self.equipment is None and self.material_lot is None and self.time_window is None:
            raise ValueError("圈定条件不能为空: 至少给出设备、物料批次或时间窗之一")
        if self.time_window is not None:
            try:
                start, end = self.time_window
            except (TypeError, ValueError) as exc:
                raise ValueError(f"时间窗必须为 (start, end): {self.time_window!r}") from exc
            if not (isinstance(start, str) and isinstance(end, str)) or start > end:
                raise ValueError(f"非法时间窗: {self.time_window!r}")

    def matches_event(self, event) -> bool:
        if self.equipment is not None and event.equipment == self.equipment:
            return True
        if self.material_lot is not None and event.material_lot == self.material_lot:
            return True
        if self.time_window is not None:
            start, end = self.time_window
            if start <= event.occurred_at <= end:
                return True
        return False


@dataclass
class ImpactVersion:
    """围堵范围的一个版本。

    版本只增不改: 对象集合(影响范围)单调扩展;
    holds / recalls 是边界对象按当前图谱位置的分类视图 ——
    已发运对象单列召回, 不伪装成在库冻结。
    """

    version: int
    log_seq: int
    impact: object  # graph.Impact
    holds: frozenset    # 在库/在制冻结对象(有在库量)
    recalls: frozenset  # 在途/客户召回对象


@dataclass
class Inspection:
    """一次检验: 取样数量与结论覆盖数量分开记录, 复验指向原检验。"""

    inspection_id: str
    object_id: str
    sampled_qty: object  # Decimal
    covered_qty: object  # Decimal, 结论可覆盖的数量(放行额度来源)
    result: str          # "pass" / "fail"
    inspector: str
    ref_inspection: str | None = None

    @property
    def is_reinspection(self):
        return self.ref_inspection is not None


@dataclass
class Deviation:
    """偏差申请: 关联具体数量, 批准人不得为调查员或申请人本人。"""

    deviation_id: str
    object_id: str
    qty: object
    reason: str
    requested_by: str
    status: str = "pending"  # pending / approved / rejected
    decided_by: str | None = None


@dataclass
class Release:
    """一次部分放行: 数量必须落在检验覆盖量或已批准偏差量的额度内。"""

    release_id: str
    object_id: str
    qty: object
    basis_kind: str  # "inspection" / "deviation"
    basis_id: str
    released_by: str
    log_seq: int


class Incident:
    """一个质量围堵事件(聚合根, 由 ContainmentService 在锁内操作)。"""

    def __init__(self, incident_id, investigator, criteria, reported_qty, created_seq):
        self.incident_id = incident_id
        self.investigator = investigator
        self.criteria = criteria
        self.reported_qty = dict(reported_qty)  # 通报的可疑总量: 对象或批次 -> Decimal
        self.created_seq = created_seq
        self.status = "open"
        self.closed_by = None
        self.versions = []
        self.inspections = {}
        self.deviations = {}
        self.releases = {}
        self.scrapped = {}             # object_id -> 报废数量
        self.release_used = {}         # object_id -> 已领用消耗的放行额度
        self.basis_used = {}           # basis_id -> 已占用的放行依据额度
        self.recall_confirmations = {} # object_id -> 召回确认记录

    @property
    def current(self):
        return self.versions[-1] if self.versions else None

    def contains(self, object_id):
        cur = self.current
        return cur is not None and object_id in cur.impact.objects

    def boundary_objects(self):
        cur = self.current
        return cur.impact.objects if cur else frozenset()

    def released_qty(self, object_id):
        return sum((r.qty for r in self.releases.values() if r.object_id == object_id), ZERO)

    def scrapped_qty(self, object_id):
        return self.scrapped.get(object_id, ZERO)

    def pending_deviations(self, object_id=None):
        return [d for d in self.deviations.values()
                if d.status == "pending" and (object_id is None or d.object_id == object_id)]
