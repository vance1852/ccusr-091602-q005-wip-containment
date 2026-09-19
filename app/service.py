"""质量围堵与放行服务。

确定性承诺
----------
* 图谱与围堵边界是 (对象台账, 事件集合) 的纯函数, 与事件到达顺序无关;
* 重复扫描按事件标识幂等去重, 同标识不同内容立即拒绝;
* 围堵版本只增不减: 迟到事件只能扩展已记录的影响范围, 不能让其消失;
* 数量一律十进制定点, 拆合守恒在事件入口强制校验;
* 并发领料在内部锁下原子判定并扣减, 不会超发;
* 调查员与批准人职责分离, 由服务层强制。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import (CloseBlockedError, DomainError, EventConflictError,
                     IncidentStateError, QuantityError, ReleaseError,
                     RequestConflictError, SeparationOfDutiesError,
                     UnknownIncidentError, UnknownObjectError)
from .graph import GenealogyGraph, Impact, PathStep  # noqa: F401  (PathStep 供报告类型注解)
from .incident import (Deviation, ImpactVersion, Incident, Inspection,
                       Release, ScopeCriteria)
from .model import (IN_STOCK_LOCATIONS, SHIPPED_LOCATIONS, GenealogyEvent,
                    ObjectRecord)
from .quantity import ZERO, parse_quantity, qty_str

ALLOW = "allow"
BLOCK = "block"
REVIEW = "review_required"

DISPOSITIONS = ("held", "review_required", "released", "recalled", "scrapped")


@dataclass(frozen=True)
class IssueDecision:
    """领料判定结论: allow / block / review_required, 附可解释理由与路径证据。"""

    verdict: str
    object_id: str
    qty: object
    reasons: tuple
    incidents: tuple = ()
    paths: tuple = ()          # ((incident_id, (PathStep, ...)), ...)
    available: object | None = None

    @property
    def allowed(self):
        return self.verdict == ALLOW


@dataclass(frozen=True)
class CloseReport:
    """关闭前视图: 未定位数量、传播路径、放行依据、召回清单与关闭阻碍。"""

    incident_id: str
    status: str
    version: int
    holds: tuple
    recalls: tuple
    recalls_confirmed: tuple
    unlocated: tuple    # ((通报键, 未定位数量字符串)), 已排序
    paths: tuple        # ((object_id, (PathStep, ...)), ...)
    releases: tuple     # ((release_id, object_id, qty_str, basis), ...)
    dispositions: tuple  # ((object_id, disposition), ...)
    violations: tuple
    close_blockers: tuple


class ContainmentService:
    """质量围堵与放行服务门面。所有公开方法线程安全。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._objects = {}
        self._events = {}
        self._log_seq = 0
        self._incidents = {}
        self._issued = {}     # object_id -> 累计领料量
        self._scrapped = {}   # object_id -> 累计报废量(跨围堵汇总)
        self._issue_log = []

    # ---------------- 台账 ----------------

    def register_object(self, object_id, qty, *, location="warehouse", material_lot=None):
        """登记一批物料/在制品/成品。同内容重复登记幂等, 不同内容拒绝。"""
        with self._lock:
            rec = ObjectRecord(object_id, parse_quantity(qty, field=f"register[{object_id}]"),
                               location, material_lot)
            existing = self._objects.get(object_id)
            if existing is not None:
                if (existing.registered_qty == rec.registered_qty
                        and existing.location == rec.location
                        and existing.material_lot == rec.material_lot):
                    return existing
                raise DomainError(f"对象 {object_id} 已登记且内容不一致")
            self._objects[object_id] = rec
            return rec

    # ---------------- 谱系事件 ----------------

    def ingest_event(self, event):
        """登记一条谱系事件。重复扫描(同标识同内容)幂等; 同标识不同内容拒绝。

        新事件到达后自动重算所有未关闭围堵的范围版本(只扩不缩)。
        """
        with self._lock:
            if isinstance(event, dict):
                event = GenealogyEvent(**event)
            existing = self._events.get(event.event_id)
            if existing is not None:
                if existing == event:
                    return {"event_id": event.event_id, "duplicate": True, "new_versions": []}
                raise EventConflictError(
                    f"事件 {event.event_id} 与已登记内容冲突; 重复扫描必须内容一致")
            self._events[event.event_id] = event
            self._log_seq += 1
            return {"event_id": event.event_id, "duplicate": False,
                    "new_versions": self._refresh_open_incidents()}

    def ingest_events(self, events):
        return [self.ingest_event(event) for event in events]

    @property
    def log_seq(self):
        return self._log_seq

    # ---------------- 围堵 ----------------

    def open_incident(self, incident_id, *, investigator, equipment=None,
                      material_lot=None, time_window=None, reported_qty=None):
        """调查员圈定设备/物料批次/时间窗, 立即计算第一版围堵范围。"""
        with self._lock:
            if incident_id in self._incidents:
                raise DomainError(f"围堵事件已存在: {incident_id}")
            criteria = ScopeCriteria(equipment, material_lot, time_window)
            reported = {str(k): parse_quantity(v, field=f"reported_qty[{k}]")
                        for k, v in (reported_qty or {}).items()}
            incident = Incident(incident_id, investigator, criteria, reported, self._log_seq)
            self._incidents[incident_id] = incident
            self._refresh_incident(incident)
            return incident

    def containment_boundary(self, incident_id):
        """当前围堵边界: 冻结清单、召回清单、影响对象/事件与传播路径证据。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            cur = incident.current
            return {
                "incident_id": incident_id,
                "status": incident.status,
                "version": cur.version if cur else 0,
                "versions": [v.version for v in incident.versions],
                "objects": sorted(cur.impact.objects) if cur else [],
                "events": sorted(cur.impact.events) if cur else [],
                "holds": sorted(cur.holds) if cur else [],
                "recalls": sorted(cur.recalls) if cur else [],
                "paths": ({oid: cur.impact.path_to(oid) for oid in sorted(cur.impact.objects)}
                          if cur else {}),
            }

    def _refresh_open_incidents(self):
        refreshed = []
        for iid in sorted(self._incidents):
            incident = self._incidents[iid]
            if incident.status != "open":
                continue
            version = self._refresh_incident(incident)
            if version is not None:
                refreshed.append((iid, version.version))
        return refreshed

    def _refresh_incident(self, incident):
        graph = self._graph()
        seeds = self._seeds_for(incident.criteria, graph)
        impact = graph.downstream_impact(seeds)
        prev = incident.current
        if prev is not None:
            # 单调承诺: 已记录的影响范围只能扩展, 不能消失
            impact = Impact.union(prev.impact, impact)
        recalls = frozenset(oid for oid in impact.objects if graph.is_shipped(oid))
        holds = frozenset(
            oid for oid in impact.objects
            if graph.location_of(oid) in IN_STOCK_LOCATIONS and graph.on_hand(oid) > ZERO)
        if (prev is not None
                and prev.impact.objects == impact.objects
                and prev.impact.events == impact.events
                and prev.holds == holds and prev.recalls == recalls):
            return None
        version = ImpactVersion(len(incident.versions) + 1, self._log_seq,
                                impact, holds, recalls)
        incident.versions.append(version)
        return version

    def _seeds_for(self, criteria, graph):
        seeds = set()
        for event in graph.events:
            if criteria.matches_event(event):
                for portion in event.inputs + event.outputs:
                    seeds.add(portion.object_id)
        if criteria.material_lot is not None:
            for oid, rec in graph.objects.items():
                if rec.material_lot == criteria.material_lot:
                    seeds.add(oid)
        return seeds

    # ---------------- 领料判定 ----------------

    def decide_issue(self, object_id, qty, *, requester=""):
        """纯查询判定: 允许 / 阻止 / 待复核, 附可解释理由。"""
        with self._lock:
            return self._decide_issue(object_id, self._positive(qty, "issue qty"))

    def commit_issue(self, request_id, object_id, qty, *, requester=""):
        """原子判定并扣减。同 request_id 重放返回原结论(幂等), 不会重复扣减。"""
        with self._lock:
            qty = self._positive(qty, "issue qty")
            for rec in self._issue_log:
                if rec["request_id"] == request_id:
                    if rec["object_id"] == object_id and rec["qty"] == qty:
                        return rec["decision"]
                    raise RequestConflictError(
                        f"领料请求 {request_id} 与已记录内容冲突")
            decision = self._decide_issue(object_id, qty)
            if decision.allowed:
                self._issued[object_id] = self._issued.get(object_id, ZERO) + qty
                for iid in decision.incidents:
                    incident = self._incidents[iid]
                    incident.release_used[object_id] = \
                        incident.release_used.get(object_id, ZERO) + qty
            self._issue_log.append({
                "request_id": request_id, "object_id": object_id,
                "qty": qty, "requester": requester, "decision": decision,
            })
            return decision

    def _decide_issue(self, object_id, qty):
        graph = self._graph()
        if object_id not in graph.objects:
            return IssueDecision(BLOCK, object_id, qty, (f"对象 {object_id} 未登记",))
        location = graph.location_of(object_id)
        if location in SHIPPED_LOCATIONS:
            return IssueDecision(
                BLOCK, object_id, qty,
                (f"对象已发运(位置 {location}), 属于召回对象而非在库, 不能领料",),
                available=ZERO)
        on_hand = graph.on_hand(object_id)
        issued = self._issued.get(object_id, ZERO)
        scrapped = self._scrapped.get(object_id, ZERO)
        available = on_hand - issued - scrapped
        if available < qty:
            return IssueDecision(
                BLOCK, object_id, qty,
                (f"可用数量不足: 在库 {qty_str(on_hand)}, 已发 {qty_str(issued)}, "
                 f"已报废 {qty_str(scrapped)}, 请求 {qty_str(qty)}",),
                available=available)
        containers = [inc for _, inc in sorted(self._incidents.items())
                      if inc.status == "open" and inc.contains(object_id)]
        if not containers:
            return IssueDecision(ALLOW, object_id, qty,
                                 ("对象不在任何围堵边界内",), available=available)
        hard_blocks, reviews = [], []
        for inc in containers:
            pool = inc.released_qty(object_id) - inc.release_used.get(object_id, ZERO)
            if pool >= qty:
                continue  # 该围堵的放行额度覆盖
            if pool > ZERO:
                reviews.append(
                    f"围堵 {inc.incident_id}: 剩余放行额度 {qty_str(pool)} 小于请求 "
                    f"{qty_str(qty)}, 请拆分请求或申请偏差")
            elif inc.pending_deviations(object_id):
                reviews.append(f"围堵 {inc.incident_id}: 存在待批准的偏差申请")
            else:
                hard_blocks.append(inc)
        if hard_blocks:
            reasons, paths = [], []
            for inc in hard_blocks:
                reasons.append(f"对象处于围堵 {inc.incident_id} 的冻结范围且未放行")
                path = inc.current.impact.path_to(object_id)
                if path:
                    paths.append((inc.incident_id, path))
                    reasons.append("传播路径: " + " -> ".join(
                        step.object_id + (f"(经 {step.via_event})" if step.via_event else "(种子)")
                        for step in path))
            return IssueDecision(BLOCK, object_id, qty, tuple(reasons),
                                 incidents=tuple(i.incident_id for i in hard_blocks),
                                 paths=tuple(paths), available=available)
        if reviews:
            return IssueDecision(REVIEW, object_id, qty, tuple(reviews),
                                 incidents=tuple(i.incident_id for i in containers),
                                 available=available)
        refs = []
        for inc in containers:
            for rel in sorted(inc.releases.values(), key=lambda r: r.release_id):
                if rel.object_id == object_id:
                    refs.append(f"{inc.incident_id}/{rel.release_id}"
                                f"(依据 {rel.basis_kind}:{rel.basis_id})")
        return IssueDecision(ALLOW, object_id, qty,
                             ("对象处于围堵但放行额度覆盖: " + "; ".join(refs),),
                             incidents=tuple(i.incident_id for i in containers),
                             available=available)

    # ---------------- 检验与放行 ----------------

    def record_inspection(self, incident_id, inspection_id, object_id, *,
                          sampled_qty, covered_qty, result, inspector, ref_inspection=None):
        """记录检验: 取样数量与结论覆盖数量分开; ref_inspection 标记复验。"""
        with self._lock:
            incident = self._require_open(incident_id)
            if inspection_id in incident.inspections:
                raise DomainError(f"检验记录已存在: {inspection_id}")
            self._require_in_boundary(incident, object_id)
            sampled = self._positive(sampled_qty, "sampled_qty")
            covered = self._positive(covered_qty, "covered_qty")
            if result not in ("pass", "fail"):
                raise DomainError(f"未知检验结论: {result!r}; 支持 pass/fail")
            if ref_inspection is not None:
                ref = incident.inspections.get(ref_inspection)
                if ref is None:
                    raise DomainError(f"复验引用的原检验不存在: {ref_inspection}")
                if ref.object_id != object_id:
                    raise DomainError("复验必须针对同一对象")
            insp = Inspection(inspection_id, object_id, sampled, covered, result,
                              inspector, ref_inspection)
            incident.inspections[inspection_id] = insp
            return insp

    def request_deviation(self, incident_id, deviation_id, object_id, *,
                          qty, reason, requested_by):
        """申请偏差放行, 关联具体数量。"""
        with self._lock:
            incident = self._require_open(incident_id)
            if deviation_id in incident.deviations:
                raise DomainError(f"偏差申请已存在: {deviation_id}")
            self._require_in_boundary(incident, object_id)
            dev = Deviation(deviation_id, object_id, self._positive(qty, "deviation qty"),
                            reason, requested_by)
            incident.deviations[deviation_id] = dev
            return dev

    def decide_deviation(self, incident_id, deviation_id, *, approver, approve=True):
        """批准或拒绝偏差。批准人不得为调查员, 也不得为申请人本人。"""
        with self._lock:
            incident = self._require_open(incident_id)
            dev = incident.deviations.get(deviation_id)
            if dev is None:
                raise DomainError(f"偏差申请不存在: {deviation_id}")
            if dev.status != "pending":
                raise IncidentStateError(f"偏差 {deviation_id} 已处理: {dev.status}")
            if approver == incident.investigator:
                raise SeparationOfDutiesError("批准人与调查员不得为同一人")
            if approver == dev.requested_by:
                raise SeparationOfDutiesError("批准人与偏差申请人不得为同一人")
            dev.status = "approved" if approve else "rejected"
            dev.decided_by = approver
            return dev

    def release(self, incident_id, release_id, object_id, *,
                qty, basis_kind, basis_id, released_by):
        """部分放行: 数量必须落在检验覆盖量或已批准偏差量的剩余额度内。"""
        with self._lock:
            incident = self._require_open(incident_id)
            if release_id in incident.releases:
                raise DomainError(f"放行记录已存在: {release_id}")
            if released_by == incident.investigator:
                raise SeparationOfDutiesError("放行批准人与调查员不得为同一人")
            self._require_in_boundary(incident, object_id)
            qty = self._positive(qty, "release qty")
            capacity = self._basis_capacity(incident, basis_kind, basis_id, object_id)
            used = incident.basis_used.get(basis_id, ZERO)
            if used + qty > capacity:
                raise ReleaseError(
                    f"放行依据 {basis_id} 额度不足: 剩余 {qty_str(capacity - used)}, "
                    f"请求 {qty_str(qty)}")
            graph = self._graph()
            held = graph.on_hand(object_id) - incident.scrapped_qty(object_id)
            if incident.released_qty(object_id) + qty > held:
                raise ReleaseError(f"放行总量将超过冻结在库量 {qty_str(held)}")
            incident.basis_used[basis_id] = used + qty
            rel = Release(release_id, object_id, qty, basis_kind, basis_id,
                          released_by, self._log_seq)
            incident.releases[release_id] = rel
            return rel

    def _basis_capacity(self, incident, basis_kind, basis_id, object_id):
        if basis_kind == "inspection":
            insp = incident.inspections.get(basis_id)
            if insp is None:
                raise ReleaseError(f"放行依据不存在: 检验 {basis_id}")
            if insp.object_id != object_id:
                raise ReleaseError(f"检验 {basis_id} 不属于对象 {object_id}")
            if insp.result != "pass":
                raise ReleaseError(f"检验 {basis_id} 结论为不合格, 不能作为放行依据")
            return insp.covered_qty
        if basis_kind == "deviation":
            dev = incident.deviations.get(basis_id)
            if dev is None:
                raise ReleaseError(f"放行依据不存在: 偏差 {basis_id}")
            if dev.object_id != object_id:
                raise ReleaseError(f"偏差 {basis_id} 不属于对象 {object_id}")
            if dev.status != "approved":
                raise ReleaseError(f"偏差 {basis_id} 未获批准(状态 {dev.status})")
            return dev.qty
        raise ReleaseError(f"未知放行依据类型: {basis_kind!r}")

    def scrap(self, incident_id, object_id, *, qty, approved_by):
        """报废冻结中的数量, 从在库物理移除。"""
        with self._lock:
            incident = self._require_open(incident_id)
            if approved_by == incident.investigator:
                raise SeparationOfDutiesError("报废批准人与调查员不得为同一人")
            self._require_in_boundary(incident, object_id)
            qty = self._positive(qty, "scrap qty")
            graph = self._graph()
            physical = (graph.on_hand(object_id) - self._issued.get(object_id, ZERO)
                        - self._scrapped.get(object_id, ZERO))
            if qty > physical:
                raise ReleaseError(f"报废数量超过物理在库 {qty_str(physical)}")
            incident.scrapped[object_id] = incident.scrapped_qty(object_id) + qty
            self._scrapped[object_id] = self._scrapped.get(object_id, ZERO) + qty
            return {"object_id": object_id, "scrapped": qty_str(incident.scrapped[object_id])}

    # ---------------- 召回与关闭 ----------------

    def confirm_recall(self, incident_id, object_id, *, confirmed_by, note=""):
        """确认一个已发运对象已被召回(客户/在途隔离)。"""
        with self._lock:
            incident = self._require_open(incident_id)
            cur = incident.current
            if cur is None or object_id not in cur.recalls:
                raise DomainError(f"对象 {object_id} 不在围堵 {incident_id} 的召回清单")
            incident.recall_confirmations[object_id] = {"by": confirmed_by, "note": note}
            return incident.recall_confirmations[object_id]

    def pre_close_report(self, incident_id):
        """关闭前视图: 未定位数量、每条传播路径、已释放依据、召回清单。"""
        with self._lock:
            incident = self._require_incident(incident_id)
            graph = self._graph()
            cur = incident.current
            objects = sorted(cur.impact.objects) if cur else []
            unlocated = []
            for key in sorted(incident.reported_qty):
                reported = incident.reported_qty[key]
                located = self._located_qty(key, incident, graph)
                if reported > located:
                    unlocated.append((key, qty_str(reported - located)))
            paths = tuple((oid, cur.impact.path_to(oid)) for oid in objects)
            releases = tuple(
                (rel.release_id, rel.object_id, qty_str(rel.qty),
                 f"{rel.basis_kind}:{rel.basis_id}")
                for rel in sorted(incident.releases.values(), key=lambda r: r.release_id))
            dispositions = tuple((oid, self._disposition_of(incident, oid, graph))
                                 for oid in objects)
            return CloseReport(
                incident_id=incident_id,
                status=incident.status,
                version=cur.version if cur else 0,
                holds=tuple(sorted(cur.holds)) if cur else (),
                recalls=tuple(sorted(cur.recalls)) if cur else (),
                recalls_confirmed=tuple(sorted(incident.recall_confirmations)),
                unlocated=tuple(unlocated),
                paths=paths,
                releases=releases,
                dispositions=dispositions,
                violations=tuple(graph.violations),
                close_blockers=tuple(self._close_blockers(incident, graph)),
            )

    def close_incident(self, incident_id, *, manager):
        """关闭围堵。条件: 无未定位数量、无待批偏差、召回全部确认、
        边界内在库量全部已放行或报废。"""
        with self._lock:
            incident = self._require_open(incident_id)
            blockers = self._close_blockers(incident, self._graph())
            if blockers:
                raise CloseBlockedError(
                    f"围堵 {incident_id} 不满足关闭条件: " + "; ".join(blockers))
            incident.status = "closed"
            incident.closed_by = manager
            return {"incident_id": incident_id, "status": "closed", "closed_by": manager}

    def _close_blockers(self, incident, graph):
        blockers = []
        cur = incident.current
        if cur is None:
            return ["围堵尚未计算任何版本"]
        for key in sorted(incident.reported_qty):
            reported = incident.reported_qty[key]
            located = self._located_qty(key, incident, graph)
            if reported > located:
                blockers.append(f"通报 {key} 尚有 {qty_str(reported - located)} 未定位")
        for dev in sorted(incident.deviations.values(), key=lambda d: d.deviation_id):
            if dev.status == "pending":
                blockers.append(f"偏差 {dev.deviation_id} 仍待批准")
        for oid in sorted(cur.recalls):
            if oid not in incident.recall_confirmations:
                blockers.append(f"召回对象 {oid} 尚未确认")
        for oid in sorted(cur.impact.objects):
            if graph.is_shipped(oid):
                continue
            residual = (graph.on_hand(oid) - incident.released_qty(oid)
                        - incident.scrapped_qty(oid))
            if residual > ZERO:
                blockers.append(f"对象 {oid} 尚有 {qty_str(residual)} 冻结量未处置")
        return blockers

    def _located_qty(self, key, incident, graph):
        cur = incident.current
        if cur is None:
            return ZERO
        if key in graph.objects:
            return graph.objects[key].registered_qty if key in cur.impact.objects else ZERO
        return sum((rec.registered_qty for oid, rec in graph.objects.items()
                    if rec.material_lot == key and oid in cur.impact.objects), ZERO)

    def _disposition_of(self, incident, object_id, graph):
        if graph.is_shipped(object_id):
            return "recalled"
        on_hand = graph.on_hand(object_id)
        released = incident.released_qty(object_id)
        scrapped = incident.scrapped_qty(object_id)
        if on_hand > ZERO and scrapped >= on_hand:
            return "scrapped"
        if on_hand > ZERO and released + scrapped >= on_hand:
            return "released"
        if incident.pending_deviations(object_id):
            return "review_required"
        return "held"

    # ---------------- 查询 ----------------

    def object_state(self, object_id):
        with self._lock:
            graph = self._graph()
            if object_id not in graph.objects:
                raise UnknownObjectError(f"对象不存在: {object_id}")
            issued = self._issued.get(object_id, ZERO)
            scrapped = self._scrapped.get(object_id, ZERO)
            return {
                "object_id": object_id,
                "location": graph.location_of(object_id),
                "on_hand": qty_str(graph.on_hand(object_id)),
                "issued": qty_str(issued),
                "scrapped": qty_str(scrapped),
                "available": qty_str(graph.on_hand(object_id) - issued - scrapped),
                "incidents": [iid for iid, inc in sorted(self._incidents.items())
                              if inc.status == "open" and inc.contains(object_id)],
            }

    def snapshot(self):
        """确定性状态导出: 相同输入历史必得相同快照。"""
        with self._lock:
            graph = self._graph()
            return {
                "log_seq": self._log_seq,
                "objects": {oid: qty_str(graph.on_hand(oid))
                            for oid in sorted(graph.objects)},
                "locations": {oid: graph.location_of(oid)
                              for oid in sorted(graph.objects)},
                "issued": {oid: qty_str(q) for oid, q in sorted(self._issued.items())},
                "scrapped": {oid: qty_str(q) for oid, q in sorted(self._scrapped.items())},
                "incidents": {
                    iid: {
                        "status": inc.status,
                        "versions": [v.version for v in inc.versions],
                        "objects": sorted(inc.current.impact.objects) if inc.current else [],
                        "holds": sorted(inc.current.holds) if inc.current else [],
                        "recalls": sorted(inc.current.recalls) if inc.current else [],
                        "released": {oid: qty_str(inc.released_qty(oid))
                                     for oid in sorted(inc.boundary_objects())},
                    }
                    for iid, inc in sorted(self._incidents.items())
                },
            }

    # ---------------- 内部 ----------------

    def _graph(self):
        return GenealogyGraph(self._objects, self._events.values())

    def _require_incident(self, incident_id):
        incident = self._incidents.get(incident_id)
        if incident is None:
            raise UnknownIncidentError(f"围堵事件不存在: {incident_id}")
        return incident

    def _require_open(self, incident_id):
        incident = self._require_incident(incident_id)
        if incident.status != "open":
            raise IncidentStateError(f"围堵 {incident_id} 已关闭")
        return incident

    def _require_in_boundary(self, incident, object_id):
        if not incident.contains(object_id):
            raise DomainError(f"对象 {object_id} 不在围堵 {incident.incident_id} 的边界内")

    @staticmethod
    def _positive(qty, field):
        parsed = parse_quantity(qty, field=field)
        if parsed <= ZERO:
            raise QuantityError(f"{field}: 数量必须为正")
        return parsed
