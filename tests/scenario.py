"""共享测试场景：AOI 漏检通报后的疑似批次谱系。

谱系结构（数量均守恒）：

    L-RAW-1 (B-1, 1000, 疑似批次)
      └─E1 split─→ L-A 600 (line)          L-B 400 (warehouse)
    L-RAW-2 (B-2, 500, 干净批次)             └─E7 split─→ L-B1 250, L-B2 150
      └─E2 merge(+L-A)→ L-M 1100 (line)
                          └─E3 split─→ L-M1 500          L-M2 600
    L-COMP (B-3, 200)                        │             └─E6 rework─→ L-M2R 600
      └─E4 assemble(+L-M1)→ L-ASSY 700 ──────┘
                             └─E5 ship─→ L-SHIP 400 (customer)
    L-PACK (B-4, 300, 干净且在库)
      └─E8 ship─→ L-PSHIP 50 (customer)

围堵圈定批次 B-1 后，受影响对象应覆盖 L-RAW-1 的全部下游，
而 L-RAW-2、L-COMP、L-PACK 等无关对象不得被误停。
"""

from app import QualityContainmentService

INVESTIGATOR = "investigator-wang"
APPROVER = "manager-li"
INSPECTOR = "inspector-zhao"

REGISTRATIONS = [
    {"lot_id": "L-RAW-1", "qty": "1000", "location": "warehouse", "material_batch": "B-1", "received_at": "2026-09-01T00:00:00Z"},
    {"lot_id": "L-RAW-2", "qty": "500", "location": "warehouse", "material_batch": "B-2", "received_at": "2026-09-01T00:00:00Z"},
    {"lot_id": "L-COMP", "qty": "200", "location": "warehouse", "material_batch": "B-3", "received_at": "2026-09-01T00:00:00Z"},
    {"lot_id": "L-PACK", "qty": "300", "location": "warehouse", "material_batch": "B-4", "received_at": "2026-09-01T00:00:00Z"},
]

EVENTS = [
    {"event_id": "E1", "action": "split", "occurred_at": "2026-09-02T01:00:00Z", "equipment_id": "EQ-1",
     "inputs": [{"lot_id": "L-RAW-1", "qty": "1000"}],
     "outputs": [{"lot_id": "L-A", "qty": "600", "location": "line"},
                 {"lot_id": "L-B", "qty": "400", "location": "warehouse"}]},
    {"event_id": "E2", "action": "merge", "occurred_at": "2026-09-02T02:00:00Z", "equipment_id": "EQ-2",
     "inputs": [{"lot_id": "L-A", "qty": "600"}, {"lot_id": "L-RAW-2", "qty": "500"}],
     "outputs": [{"lot_id": "L-M", "qty": "1100", "location": "line"}]},
    {"event_id": "E3", "action": "split", "occurred_at": "2026-09-02T03:00:00Z", "equipment_id": "EQ-3",
     "inputs": [{"lot_id": "L-M", "qty": "1100"}],
     "outputs": [{"lot_id": "L-M1", "qty": "500", "location": "line"},
                 {"lot_id": "L-M2", "qty": "600", "location": "line"}]},
    {"event_id": "E4", "action": "assemble", "occurred_at": "2026-09-02T04:00:00Z", "equipment_id": "EQ-4",
     "inputs": [{"lot_id": "L-M1", "qty": "500"}, {"lot_id": "L-COMP", "qty": "200"}],
     "outputs": [{"lot_id": "L-ASSY", "qty": "700", "location": "line"}]},
    {"event_id": "E5", "action": "ship", "occurred_at": "2026-09-02T05:00:00Z",
     "inputs": [{"lot_id": "L-ASSY", "qty": "400"}],
     "outputs": [{"lot_id": "L-SHIP", "qty": "400", "location": "customer"}]},
    {"event_id": "E6", "action": "rework", "occurred_at": "2026-09-02T06:00:00Z", "equipment_id": "EQ-5",
     "inputs": [{"lot_id": "L-M2", "qty": "600"}],
     "outputs": [{"lot_id": "L-M2R", "qty": "600", "location": "line"}]},
    {"event_id": "E7", "action": "split", "occurred_at": "2026-09-02T07:00:00Z", "equipment_id": "EQ-1",
     "inputs": [{"lot_id": "L-B", "qty": "400"}],
     "outputs": [{"lot_id": "L-B1", "qty": "250", "location": "warehouse"},
                 {"lot_id": "L-B2", "qty": "150", "location": "warehouse"}]},
    {"event_id": "E8", "action": "ship", "occurred_at": "2026-09-02T08:00:00Z",
     "inputs": [{"lot_id": "L-PACK", "qty": "50"}],
     "outputs": [{"lot_id": "L-PSHIP", "qty": "50", "location": "customer"}]},
]

# 批次 B-1 的全部可达对象（含 L-RAW-1 自身）
IMPACTED_ALL = {
    "L-RAW-1", "L-A", "L-B", "L-M", "L-M1", "L-M2", "L-ASSY", "L-SHIP", "L-M2R", "L-B1", "L-B2",
}
# 不受影响的干净对象
CLEAN_LOTS = {"L-RAW-2", "L-COMP", "L-PACK", "L-PSHIP"}

SCOPE_B1 = {"type": "material_batch", "material_batch": "B-1", "declared_qty": "1000"}


def build_service(events=None, registrations=None, open_case=True, scope=None):
    """按给定顺序加载谱系并（可选）开立围堵事件。"""
    svc = QualityContainmentService()
    for reg in registrations if registrations is not None else REGISTRATIONS:
        result = svc.register_lot(**reg)
        assert result["status"] == "applied", result
    for event in events if events is not None else EVENTS:
        result = svc.record_event(event)
        assert result["status"] in ("applied", "duplicate"), result
    if open_case:
        svc.open_case("C-1", INVESTIGATOR, scope or SCOPE_B1)
    return svc
