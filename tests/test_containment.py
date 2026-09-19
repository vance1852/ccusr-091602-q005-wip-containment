"""围堵范围: 圈定、传播、版本单调扩展、冻结与召回分离。"""

import unittest

from app.errors import EventConflictError
from app.model import GenealogyEvent
from scenario import base_events, build_service


class ContainmentTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7",
                               reported_qty={"M1": "1000"})

    def test_boundary_covers_all_reachable_objects(self):
        b = self.svc.containment_boundary("INC-1")
        self.assertEqual(b["objects"],
                         ["FG-E", "FG-E1", "FG-E2", "LOT-A", "SHP-F", "WIP-B", "WIP-D"])
        self.assertEqual(b["version"], 1)

    def test_holds_and_recalls_are_separated(self):
        b = self.svc.containment_boundary("INC-1")
        self.assertEqual(b["holds"], ["FG-E2", "LOT-A"])
        self.assertEqual(b["recalls"], ["SHP-F"])
        # 已发运对象单列召回, 不伪装成在库冻结
        self.assertNotIn("SHP-F", b["holds"])

    def test_unrelated_line_untouched(self):
        b = self.svc.containment_boundary("INC-1")
        self.assertNotIn("LOT-C", b["objects"])  # 干净批次, 合批点不在 EQ-7
        self.assertNotIn("LOT-X", b["objects"])  # 无关产线
        self.assertTrue(self.svc.decide_issue("LOT-X", "50").allowed)

    def test_path_evidence_saved(self):
        b = self.svc.containment_boundary("INC-1")
        path = b["paths"]["SHP-F"]
        self.assertEqual([s.object_id for s in path],
                         ["LOT-A", "WIP-B", "WIP-D", "FG-E", "FG-E1", "SHP-F"])
        self.assertEqual([s.via_event for s in path],
                         [None, "EV-1", "EV-2", "EV-3", "EV-4", "EV-6"])

    def test_late_event_expands_version_monotonically(self):
        v1 = self.svc.containment_boundary("INC-1")
        late = GenealogyEvent("EV-7", "split", {"LOT-A": "400"}, {"WIP-G": "400"},
                              "2026-09-01T11:00:00", "line", equipment="EQ-8")
        res = self.svc.ingest_event(late)
        self.assertEqual(res["new_versions"], [("INC-1", 2)])
        v2 = self.svc.containment_boundary("INC-1")
        self.assertEqual(v2["versions"], [1, 2])
        self.assertIn("WIP-G", v2["objects"])
        self.assertIn("WIP-G", v2["holds"])
        # 单调: 已记录的影响范围不消失, 即使 LOT-A 在库量已耗尽
        self.assertTrue(set(v1["objects"]) <= set(v2["objects"]))
        self.assertIn("LOT-A", v2["objects"])

    def test_scope_by_material_lot(self):
        svc = build_service()
        svc.open_incident("INC-M", investigator="INV-1", material_lot="M1")
        b = svc.containment_boundary("INC-M")
        self.assertIn("LOT-A", b["objects"])
        self.assertIn("SHP-F", b["objects"])
        self.assertNotIn("LOT-C", b["objects"])

    def test_scope_by_time_window(self):
        svc = build_service()
        svc.open_incident("INC-T", investigator="INV-1",
                          time_window=("2026-09-01T08:00:00", "2026-09-01T08:59:59"))
        b = svc.containment_boundary("INC-T")
        # 窗内事件 EV-1/EV-2 涉及的对象及其下游全部卷入
        self.assertIn("SHP-F", b["objects"])
        self.assertIn("LOT-C", b["objects"])
        self.assertNotIn("LOT-X", b["objects"])

    def test_duplicate_scan_idempotent_and_conflict_rejected(self):
        svc = build_service()
        svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7")
        before = svc.containment_boundary("INC-1")
        res = svc.ingest_event(base_events()[0])  # 重复扫描
        self.assertTrue(res["duplicate"])
        self.assertEqual(res["new_versions"], [])
        after = svc.containment_boundary("INC-1")
        self.assertEqual(before["objects"], after["objects"])
        self.assertEqual(before["version"], after["version"])
        conflict = GenealogyEvent("EV-1", "split", {"LOT-A": "600"},
                                  {"WIP-B": "599", "WIP-Z": "1"},
                                  "2026-09-01T08:00:00", "line", equipment="EQ-7")
        with self.assertRaises(EventConflictError):
            svc.ingest_event(conflict)


if __name__ == "__main__":
    unittest.main()
