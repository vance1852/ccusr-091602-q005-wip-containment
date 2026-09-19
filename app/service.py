"""质量围堵与放行服务门面。

职责：
- 接收谱系事件 / 根批次登记，维护图谱；新事件到达后自动扩展所有未结事件的围堵版本。
- 提供领料结论（allow / block / review_required），结论必须可解释。
- 检验、复验、偏差批准、部分放行、报废的事务入口。
- 关闭前的完整视图：尚未定位数量、每条传播路径、已释放依据、召回清单。

线程安全：所有写操作与领料判定都在同一把锁内完成，
并发领料不会重复消耗同一笔已释放额度或库存。
"""

import hashlib
import json
import threading
from decimal import Decimal

from app.containment import ContainmentCase, Scope, boundary_snapshot
from app.errors import OpenItemsError, UnknownCaseError, ValidationError
from app.graph import GenealogyGraph
from app.model import ALLOW, BLOCK, REVIEW_REQUIRED, SHIPPED_LOCATIONS
from app.quantity import parse_qty, qty_str, require_positive


class QualityContainmentService:
    def __init__(self):
        self._graph = GenealogyGraph()
        self._cases = {}
        self._requests = {}                 # request_id -> 决策记录（幂等）
        self._issued_against_release = {}   # lot_id -> 已领走的放行额度
        self._lock = threading.RLock()
        self._seq = 0

    def _next_seq(self):
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------ 谱系摄入

    def register_lot(self, lot_id, qty, location, material_batch=None, received_at=None):
        with self._lock:
            result = self._graph.register_lot(lot_id, qty, location, material_batch, received_at)
            if result["status"] == "applied":
                self._refresh_open_cases(f"补登根批次 {lot_id}")
            return result

    def record_event(self, event):
        """摄入谱系事件；applied 时自动扩展所有未结事件的围堵版本。"""
        with self._lock:
            result = self._graph.add_event(event)
            if result["status"] == "applied":
                event_id = event.get("event_id") if isinstance(event, dict) else event.event_id
                self._refresh_open_cases(f"补到谱系事件 {event_id}")
            return result

    def _refresh_open_cases(self, reason):
        for case in self._cases.values():
            if case.status == "open":
                case.refresh(self._graph, reason)

    # ------------------------------------------------------------------ 围堵事件

    def open_case(self, case_id, investigator, scope):
        """调查员圈定范围开立围堵事件，立即计算首个围堵版本。"""
        with self._lock:
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValidationError("case_id 必须为非空字符串")
            case_id = case_id.strip()
            if case_id in self._cases:
                raise ValidationError(f"case_id 已存在: {case_id}")
            if not isinstance(investigator, str) or not investigator.strip():
                raise ValidationError("调查员必须为非空字符串")
            case = ContainmentCase(case_id, investigator.strip(), Scope.parse(scope))
            case.refresh(self._graph, f"开立事件，圈定{case.scope.describe()}")
            self._cases[case_id] = case
            return case

    def refresh_case(self, case_id, reason="手动重算"):
        with self._lock:
            return self._case(case_id).refresh(self._graph, reason)

    def _case(self, case_id):
        case = self._cases.get(case_id)
        if case is None:
            raise UnknownCaseError(f"围堵事件不存在: {case_id}")
        return case

    # ------------------------------------------------------------------ 领料判定

    def request_material(self, request_id, lot_id, qty, requester, station=None):
        """仓库 / 工位领料请求。返回可解释的 allow / block / review_required 结论。

        结论与消耗在同一临界区内完成：并发请求不会超发。
        相同 request_id 重试返回首次结论（幂等）。
        """
        qty = require_positive(parse_qty(qty, "qty"), "qty")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValidationError("request_id 必须为非空字符串")
        if not isinstance(lot_id, str) or not lot_id.strip():
            raise ValidationError("lot_id 必须为非空字符串")
        if not isinstance(requester, str) or not requester.strip():
            raise ValidationError("requester 必须为非空字符串")
        request_id = request_id.strip()
        lot_id = lot_id.strip()
        with self._lock:
            previous = self._requests.get(request_id)
            if previous is not None:
                if previous["lot_id"] == lot_id and previous["qty"] == qty_str(qty):
                    return dict(previous)
                raise ValidationError(f"request_id 冲突: {request_id} 已用于其他请求")

            decision, plan = self._evaluate_request(lot_id, qty)
            record = {
                "request_id": request_id,
                "lot_id": lot_id,
                "qty": qty_str(qty),
                "requester": requester.strip(),
                "station": station,
                "decision": decision["decision"],
                "reasons": decision["reasons"],
                "case_ids": decision["case_ids"],
                "seq": self._next_seq(),
            }
            self._requests[request_id] = record
            if decision["decision"] == ALLOW:
                self._graph.record_issue(lot_id, qty)
                if plan.get("against_release"):
                    used = self._issued_against_release.get(lot_id, Decimal(0))
                    self._issued_against_release[lot_id] = used + qty
                    record["basis_release_ids"] = plan["basis_release_ids"]
            return dict(record)

    def _evaluate_request(self, lot_id, qty):
        """领料判定纯逻辑：返回 (结论, 执行计划)。不修改任何状态。"""
        lot = self._graph.lot(lot_id)
        if lot is None or lot.phantom:
            return {
                "decision": REVIEW_REQUIRED,
                "reasons": [f"对象 {lot_id} 在谱系中不存在或缺少创建事件，无法核实来源，需人工复核"],
                "case_ids": [],
            }, {}
        if lot.conflict:
            return {
                "decision": REVIEW_REQUIRED,
                "reasons": [f"对象 {lot_id} 的谱系数据存在冲突（重复创建或超耗），需人工复核"],
                "case_ids": [],
            }, {}

        open_cases = [c for c in self._cases.values() if c.status == "open" and lot_id in c.impacted]

        if not open_cases:
            if lot.location in SHIPPED_LOCATIONS:
                return {
                    "decision": REVIEW_REQUIRED,
                    "reasons": [f"对象 {lot_id} 已发运（{lot.location}），不在库内，请核实单据"],
                    "case_ids": [],
                }, {}
            available = self._graph.available(lot_id)
            if available >= qty:
                return {
                    "decision": ALLOW,
                    "reasons": [f"对象 {lot_id} 未命中任何未结围堵事件，可用 {qty_str(available)}"],
                    "case_ids": [],
                }, {}
            return {
                "decision": BLOCK,
                "reasons": [f"可用数量不足：可用 {qty_str(available)}，申请 {qty_str(qty)}"],
                "case_ids": [],
            }, {}

        case_ids = sorted(c.case_id for c in open_cases)
        if lot.location in SHIPPED_LOCATIONS:
            return {
                "decision": BLOCK,
                "reasons": [
                    f"对象 {lot_id} 已发运并处于召回状态（{lot.location}），"
                    f"命中围堵事件 {', '.join(case_ids)}，禁止发料，请走召回流程"
                ],
                "case_ids": case_ids,
            }, {}

        pending = [c.case_id for c in open_cases if c.has_pending_inspection(lot_id)]
        if pending:
            return {
                "decision": REVIEW_REQUIRED,
                "reasons": [
                    f"对象 {lot_id} 存在待出结论的检验（事件 {', '.join(sorted(pending))}），需等待复核"
                ],
                "case_ids": case_ids,
            }, {}

        released_total = sum((c.released_qty(lot_id) for c in open_cases), Decimal(0))
        issued = self._issued_against_release.get(lot_id, Decimal(0))
        released_pool = released_total - issued
        if released_pool < 0:
            released_pool = Decimal(0)
        pool = min(released_pool, self._graph.available(lot_id))
        if pool >= qty:
            basis = sorted(
                r.release_id for c in open_cases for r in c.releases.values() if r.lot_id == lot_id
            )
            return {
                "decision": ALLOW,
                "reasons": [
                    f"对象 {lot_id} 已放行额度充足（剩余可领 {qty_str(pool)}），"
                    f"放行依据 {', '.join(basis)}"
                ],
                "case_ids": case_ids,
            }, {"against_release": True, "basis_release_ids": basis}

        scrapped_total = sum((c.scrapped_qty(lot_id) for c in open_cases), Decimal(0))
        held = self._graph.available(lot_id) - released_total - scrapped_total
        if held < 0:
            held = Decimal(0)
        return {
            "decision": BLOCK,
            "reasons": [
                f"对象 {lot_id} 处于围堵冻结中（事件 {', '.join(case_ids)}），"
                f"在冻 {qty_str(held)}，已放行剩余可领 {qty_str(pool)}，申请 {qty_str(qty)} 被阻止"
            ],
            "case_ids": case_ids,
        }, {}

    # ------------------------------------------------------------------ 检验 / 偏差 / 放行 / 报废

    def record_inspection(self, case_id, inspection_id, lot_id, kind, qty, result, inspector):
        with self._lock:
            return self._case(case_id).add_inspection(inspection_id, lot_id, kind, qty, result, inspector)

    def finalize_inspection(self, case_id, inspection_id, result, by):
        with self._lock:
            return self._case(case_id).finalize_inspection(inspection_id, result, by)

    def approve_deviation(self, case_id, deviation_id, lot_id, qty, approver, reason):
        with self._lock:
            return self._case(case_id).add_deviation(deviation_id, lot_id, qty, approver, reason)

    def release(self, case_id, release_id, lot_id, qty, by, inspection_ids=(), deviation_ids=()):
        with self._lock:
            return self._case(case_id).release(
                release_id, lot_id, qty, by, self._graph, inspection_ids, deviation_ids
            )

    def scrap(self, case_id, lot_id, qty, by, reason):
        with self._lock:
            return self._case(case_id).scrap(lot_id, qty, by, reason, self._graph)

    # ------------------------------------------------------------------ 召回与关闭

    def recall_list(self, case_id):
        """已发运对象单列召回，绝不混入在库冻结。"""
        with self._lock:
            case = self._case(case_id)
            return boundary_snapshot(case, self._graph)["recalls"]

    def closure_report(self, case_id):
        """关闭前视图：尚未定位数量、每条传播路径、已释放依据、召回清单。"""
        with self._lock:
            case = self._case(case_id)
            snapshot = boundary_snapshot(case, self._graph)
            version = case.versions[-1] if case.versions else None
            released = []
            for record in sorted(case.releases.values(), key=lambda r: r.seq):
                released.append({
                    "release_id": record.release_id,
                    "lot_id": record.lot_id,
                    "qty": qty_str(record.qty),
                    "by": record.by,
                    "basis": [
                        {"type": b["type"], "id": b["id"], "qty": qty_str(b["qty"])}
                        for b in record.basis
                    ],
                })
            paths = {}
            if version is not None:
                for lot_id in sorted(version.paths):
                    paths[lot_id] = [self._format_path(p) for p in version.paths[lot_id]]
            unlocated = case.unlocated_qty()
            issued_from_impacted = [
                {"lot_id": lot_id, "issued": qty_str(self._graph.issued(lot_id))}
                for lot_id in sorted(case.impacted)
                if self._graph.issued(lot_id) > 0
            ]
            return {
                "case_id": case.case_id,
                "status": case.status,
                "investigator": case.investigator,
                "scope": case.scope.describe(),
                "version_count": len(case.versions),
                "impacted_lots": sorted(case.impacted),
                "held": snapshot["held"],
                "recalls": snapshot["recalls"],
                "released": released,
                "scrapped": [
                    {"lot_id": s.lot_id, "qty": qty_str(s.qty), "by": s.by, "reason": s.reason}
                    for s in case.scraps
                ],
                "unlocated_qty": qty_str(unlocated) if unlocated is not None else None,
                "paths": paths,
                "issued_from_impacted": issued_from_impacted,
                "data_gaps": {
                    "phantom_lots": self._graph.phantom_lots(),
                    "conflicts": sorted(set(self._graph.conflicts)),
                    "rejected_events": list(self._graph.rejected_events),
                },
                "pending_inspections": sorted(
                    r.inspection_id for r in case.inspections.values() if r.result == "pending"
                ),
            }

    def close_case(self, case_id, by, acknowledge_open_items=False):
        """关闭事件。存在未结事项时须显式确认，确认内容随关闭报告留存。"""
        with self._lock:
            case = self._case(case_id)
            report = self.closure_report(case_id)
            open_items = []
            held_total = sum(Decimal(e["outstanding"]) for e in report["held"])
            if held_total > 0:
                open_items.append(f"仍在冻结的数量合计 {qty_str(held_total)}")
            recall_total = sum(Decimal(e["outstanding"]) for e in report["recalls"])
            if recall_total > 0:
                open_items.append(f"未决召回数量合计 {qty_str(recall_total)}")
            if report["unlocated_qty"] is not None and Decimal(report["unlocated_qty"]) > 0:
                open_items.append(f"尚未定位数量 {report['unlocated_qty']}")
            if report["pending_inspections"]:
                open_items.append(f"待结论检验 {len(report['pending_inspections'])} 项")
            if open_items and not acknowledge_open_items:
                raise OpenItemsError(
                    f"事件 {case_id} 存在未结事项，关闭需确认: {'；'.join(open_items)}",
                    open_items,
                )
            case.status = "closed"
            case.closed_by = by
            report["status"] = "closed"
            report["closed_by"] = by
            report["acknowledged_open_items"] = open_items
            return report

    # ------------------------------------------------------------------ 确定性边界

    def canonical_boundary(self, case_id):
        """围堵边界的规范 JSON（不含时间戳等易变字段），用于确定性比对。"""
        with self._lock:
            case = self._case(case_id)
            report = self.closure_report(case_id)
            canon = {
                "impacted": report["impacted_lots"],
                "held": report["held"],
                "recalls": report["recalls"],
                "released": [
                    {"release_id": r["release_id"], "lot_id": r["lot_id"], "qty": r["qty"], "basis": r["basis"]}
                    for r in report["released"]
                ],
                "scrapped": report["scrapped"],
                "unlocated_qty": report["unlocated_qty"],
                "paths": report["paths"],
                "data_gaps": {
                    "phantom_lots": report["data_gaps"]["phantom_lots"],
                    "conflicts": report["data_gaps"]["conflicts"],
                },
            }
            return json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def boundary_digest(self, case_id):
        return hashlib.sha256(self.canonical_boundary(case_id).encode("utf-8")).hexdigest()

    @staticmethod
    def _format_path(path):
        """路径证据格式化：源头为 []，否则为逐跳事件链。"""
        return [
            {
                "event_id": edge.event_id,
                "action": edge.action,
                "from": edge.from_lot,
                "to": edge.to_lot,
                "in_qty": qty_str(edge.in_qty),
                "out_qty": qty_str(edge.out_qty),
            }
            for edge in path
        ]
