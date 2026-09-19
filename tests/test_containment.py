"""围堵：范围圈定、单调版本、召回单列、未定位数量、关闭视图。"""

import unittest

from app import OpenItemsError, QualityContainmentService

from scenario import (
    CLEAN_LOTS,
    EVENTS,
    IMPACTED_ALL,
    INSPECTOR,
    INVESTIGATOR,
    build_service,
)


def held_of(report, lot_id):
    for entry in report["held"]:
        if entry["lot_id"] == lot_id:
            return entry
    return None


class ScopeTest(unittest.TestCase):
    def test_batch_scope_freezes_reachable_only(self):
        svc = build_service()
        case = svc._case("C-1")
        self.assertEqual(set(case.impacted), IMPACTED_ALL)
        self.assertTrue(set(case.impacted).isdisjoint(CLEAN_LOTS))

    def test_equipment_scope(self):
        svc = build_service(scope={"type": "equipment", "equipment_id": "EQ-1"})
        case = svc._case("C-1")
        # EQ-1 的产出为 L-A、L-B（E1）与 L-B1、L-B2（E7），向下游传播
        self.assertEqual(
            set(case.impacted),
            {"L-A", "L-B", "L-B1", "L-B2", "L-M", "L-M1", "L-M2", "L-ASSY", "L-SHIP", "L-M2R"},
        )
        self.assertNotIn("L-RAW-1", case.impacted)  # 上游对象不被设备范围反向冻结

    def test_time_window_scope(self):
        svc = build_service(scope={
            "type": "time_window",
            "start": "2026-09-02T03:00:00Z",
            "end": "2026-09-02T05:00:00Z",
        })
        case = svc._case("C-1")
        self.assertEqual(set(case.impacted), {"L-M1", "L-M2", "L-ASSY", "L-SHIP", "L-M2R"})


class VersionMonotonicityTest(unittest.TestCase):
    def test_late_event_expands_only(self):
        late_e7 = EVENTS[6]
        svc = build_service(events=[e for e in EVENTS if e["event_id"] != "E7"])
        case = svc._case("C-1")
        self.assertEqual(len(case.versions), 1)
        v1 = set(case.versions[0].impacted)
        self.assertIn("L-B", v1)
        self.assertNotIn("L-B1", v1)

        result = svc.record_event(late_e7)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(case.versions), 2)
        v2 = set(case.versions[1].impacted)
        self.assertTrue(v1 <= v2, "已记录的影响范围不得消失")
        self.assertTrue({"L-B1", "L-B2"} <= v2)
        # v1 的路径证据在 v2 中仍然保留
        for lot_id, paths in case.versions[0].paths.items():
            self.assertTrue(set(paths) <= set(case.versions[1].paths[lot_id]))

    def test_late_registration_expands(self):
        svc = build_service()
        case = svc._case("C-1")
        before = len(case.versions)
        svc.register_lot("L-RAW-9", "80", "warehouse", material_batch="B-1")
        self.assertEqual(len(case.versions), before + 1)
        self.assertIn("L-RAW-9", case.impacted)

    def test_irrelevant_event_no_new_version(self):
        svc = build_service()
        case = svc._case("C-1")
        before = len(case.versions)
        svc.register_lot("L-OTHER", "10", "warehouse", material_batch="B-9")
        svc.record_event({
            "event_id": "E-OTHER", "action": "split", "occurred_at": "2026-09-03T01:00:00Z",
            "inputs": [{"lot_id": "L-OTHER", "qty": "10"}],
            "outputs": [{"lot_id": "L-OTHER-1", "qty": "10"}],
        })
        self.assertEqual(len(case.versions), before)  # 无关产线不触发围堵扩展
        self.assertNotIn("L-OTHER-1", case.impacted)


class RecallTest(unittest.TestCase):
    def test_shipped_listed_as_recall_not_held(self):
        svc = build_service()
        report = svc.closure_report("C-1")
        recall_ids = {e["lot_id"] for e in report["recalls"]}
        held_ids = {e["lot_id"] for e in report["held"]}
        self.assertEqual(recall_ids, {"L-SHIP"})
        self.assertNotIn("L-SHIP", held_ids)
        self.assertEqual(report["recalls"][0]["outstanding"], "400")
        self.assertEqual(report["recalls"][0]["location"], "customer")

    def test_recall_request_blocked(self):
        svc = build_service()
        decision = svc.request_material("REQ-SHIP", "L-SHIP", "10", "worker")
        self.assertEqual(decision["decision"], "block")
        self.assertTrue(any("召回" in r for r in decision["reasons"]))


class UnlocatedTest(unittest.TestCase):
    def test_unlocated_quantity(self):
        svc = build_service(scope={"type": "material_batch", "material_batch": "B-1", "declared_qty": "1200"})
        report = svc.closure_report("C-1")
        self.assertEqual(report["unlocated_qty"], "200")

    def test_unlocated_zero_when_fully_traced(self):
        svc = build_service()
        self.assertEqual(svc.closure_report("C-1")["unlocated_qty"], "0")

    def test_unlocated_none_without_declared(self):
        svc = build_service(scope={"type": "material_batch", "material_batch": "B-1"})
        self.assertIsNone(svc.closure_report("C-1")["unlocated_qty"])


class ClosureReportTest(unittest.TestCase):
    def test_report_contains_paths_basis_recalls(self):
        svc = build_service()
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "pass", INSPECTOR)
        svc.release("C-1", "R-1", "L-B1", "100", INVESTIGATOR, inspection_ids=["I-1"])
        report = svc.closure_report("C-1")

        # 每条传播路径：L-ASSY 的完整证据链
        assy_paths = report["paths"]["L-ASSY"]
        self.assertEqual(len(assy_paths), 1)
        self.assertEqual([h["event_id"] for h in assy_paths[0]], ["E1", "E2", "E3", "E4"])
        self.assertEqual(report["paths"]["L-RAW-1"], [[]])  # 源头对象路径为空

        # 已释放依据关联到具体检验与数量
        self.assertEqual(len(report["released"]), 1)
        self.assertEqual(report["released"][0]["qty"], "100")
        self.assertEqual(
            report["released"][0]["basis"],
            [{"type": "inspection", "id": "I-1", "qty": "100"}],
        )

        # 召回清单与未定位数量
        self.assertEqual([e["lot_id"] for e in report["recalls"]], ["L-SHIP"])
        self.assertEqual(report["unlocated_qty"], "0")

    def test_close_blocked_by_open_items(self):
        svc = build_service()
        with self.assertRaises(OpenItemsError) as ctx:
            svc.close_case("C-1", "quality-manager")
        message = str(ctx.exception)
        self.assertIn("冻结", message)
        self.assertIn("召回", message)

    def test_close_with_acknowledgement_releases_holds(self):
        svc = build_service()
        report = svc.close_case("C-1", "quality-manager", acknowledge_open_items=True)
        self.assertEqual(report["status"], "closed")
        self.assertTrue(report["acknowledged_open_items"])
        # 事件关闭后，原冻结对象不再被该事件拦截
        decision = svc.request_material("REQ-AFTER", "L-B1", "10", "worker")
        self.assertEqual(decision["decision"], "allow")

    def test_issued_before_impact_is_reported(self):
        """围堵前已发出的数量单列展示，不伪装成在库。"""
        svc = QualityContainmentService()
        svc.register_lot("L-Q", "100", "warehouse", material_batch="B-9")
        decision = svc.request_material("REQ-Q", "L-Q", "30", "worker")
        self.assertEqual(decision["decision"], "allow")
        svc.open_case("C-Q", INVESTIGATOR, {
            "type": "material_batch", "material_batch": "B-9", "declared_qty": "100",
        })
        report = svc.closure_report("C-Q")
        self.assertEqual(report["issued_from_impacted"], [{"lot_id": "L-Q", "issued": "30"}])
        self.assertEqual(held_of(report, "L-Q")["outstanding"], "70")


if __name__ == "__main__":
    unittest.main()
