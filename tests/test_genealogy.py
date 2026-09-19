"""谱系图：守恒、去重、乱序、环安全。"""

import random
import unittest

from app import QualityContainmentService

from scenario import EVENTS, REGISTRATIONS, build_service


class ConservationTest(unittest.TestCase):
    def test_conservation_enforced(self):
        svc = QualityContainmentService()
        svc.register_lot("L-X", "100", "warehouse", material_batch="B-X")
        bad = {
            "event_id": "E-BAD", "action": "split", "occurred_at": "2026-09-02T01:00:00Z",
            "inputs": [{"lot_id": "L-X", "qty": "100"}],
            "outputs": [{"lot_id": "L-X1", "qty": "60"}, {"lot_id": "L-X2", "qty": "30"}],
        }
        result = svc.record_event(bad)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("不守恒", result["reason"])
        # 图未被污染
        self.assertIsNone(svc._graph.lot("L-X1"))
        self.assertEqual(str(svc._graph.available("L-X")), "100")
        self.assertEqual(len(svc._graph.rejected_events), 1)

    def test_float_quantity_rejected(self):
        svc = QualityContainmentService()
        svc.register_lot("L-X", "100", "warehouse")
        result = svc.record_event({
            "event_id": "E-F", "action": "split", "occurred_at": "2026-09-02T01:00:00Z",
            "inputs": [{"lot_id": "L-X", "qty": 100.0}],
            "outputs": [{"lot_id": "L-X1", "qty": 100.0}],
        })
        self.assertEqual(result["status"], "rejected")
        self.assertIn("浮点", result["reason"])

    def test_decimal_string_conservation(self):
        svc = QualityContainmentService()
        svc.register_lot("L-X", "0.3", "warehouse")
        result = svc.record_event({
            "event_id": "E-D", "action": "split", "occurred_at": "2026-09-02T01:00:00Z",
            "inputs": [{"lot_id": "L-X", "qty": "0.3"}],
            "outputs": [{"lot_id": "L-X1", "qty": "0.1"}, {"lot_id": "L-X2", "qty": "0.2"}],
        })
        self.assertEqual(result["status"], "applied")


class DedupTest(unittest.TestCase):
    def test_duplicate_event_idempotent(self):
        svc = QualityContainmentService()
        for reg in REGISTRATIONS:
            svc.register_lot(**reg)
        first = svc.record_event(EVENTS[0])
        self.assertEqual(first["status"], "applied")
        again = svc.record_event(dict(EVENTS[0]))
        self.assertEqual(again["status"], "duplicate")
        # 重复扫描不改变图
        self.assertEqual(str(svc._graph.available("L-A")), "600")
        self.assertEqual(str(svc._graph.available("L-B")), "400")

    def test_conflicting_event_id_rejected(self):
        svc = QualityContainmentService()
        for reg in REGISTRATIONS:
            svc.register_lot(**reg)
        svc.record_event(EVENTS[0])
        changed = dict(EVENTS[0])
        changed["outputs"] = [{"lot_id": "L-A", "qty": "1000", "location": "line"}]
        result = svc.record_event(changed)
        self.assertEqual(result["status"], "conflict")
        # 保留先到事件
        self.assertEqual(str(svc._graph.available("L-B")), "400")

    def test_duplicate_registration_idempotent(self):
        svc = QualityContainmentService()
        self.assertEqual(svc.register_lot(**REGISTRATIONS[0])["status"], "applied")
        self.assertEqual(svc.register_lot(**REGISTRATIONS[0])["status"], "duplicate")
        conflict = dict(REGISTRATIONS[0], qty="999")
        self.assertEqual(svc.register_lot(**conflict)["status"], "conflict")
        self.assertEqual(str(svc._graph.available("L-RAW-1")), "1000")


class OutOfOrderTest(unittest.TestCase):
    def test_shuffled_arrival_same_graph(self):
        reference = build_service(open_case=False)
        for seed in range(8):
            events = list(EVENTS)
            random.Random(seed).shuffle(events)
            svc = build_service(events=events, open_case=False)
            for lot_id in ("L-RAW-1", "L-A", "L-B", "L-M", "L-ASSY", "L-SHIP", "L-B1", "L-PACK"):
                self.assertEqual(
                    svc._graph.available(lot_id),
                    reference._graph.available(lot_id),
                    f"seed={seed} lot={lot_id}",
                )
            self.assertEqual(svc._graph.phantom_lots(), [])
            self.assertEqual(svc._graph.conflicts, [])

    def test_events_before_registrations(self):
        """事件先于根批次登记到达，最终图形态不变。"""
        svc = QualityContainmentService()
        for event in EVENTS:
            self.assertEqual(svc.record_event(event)["status"], "applied")
        for reg in REGISTRATIONS:
            svc.register_lot(**reg)
        reference = build_service(open_case=False)
        for lot_id in ("L-RAW-1", "L-B1", "L-SHIP"):
            self.assertEqual(svc._graph.available(lot_id), reference._graph.available(lot_id))
        self.assertEqual(svc._graph.phantom_lots(), [])


class RobustnessTest(unittest.TestCase):
    def test_rework_cycle_terminates(self):
        """返工数据形成环时，传播与路径枚举必须终止且结果有限。"""
        svc = QualityContainmentService()
        svc.register_lot("L-X", "100", "line", material_batch="B-X")
        svc.record_event({
            "event_id": "E-C1", "action": "rework", "occurred_at": "2026-09-02T01:00:00Z",
            "inputs": [{"lot_id": "L-X", "qty": "100"}],
            "outputs": [{"lot_id": "L-Y", "qty": "100", "location": "line"}],
        })
        svc.record_event({
            "event_id": "E-C2", "action": "rework", "occurred_at": "2026-09-02T02:00:00Z",
            "inputs": [{"lot_id": "L-Y", "qty": "100"}],
            "outputs": [{"lot_id": "L-X", "qty": "100", "location": "line"}],
        })
        # L-X 被重复创建 → 冲突被标记
        self.assertTrue(svc._graph.lot("L-X").conflict)
        case = svc.open_case("C-CYCLE", "inv", {
            "type": "time_window", "start": "2026-09-02T00:00:00Z", "end": "2026-09-03T00:00:00Z",
        })
        self.assertEqual(set(case.impacted), {"L-X", "L-Y"})

    def test_over_consumption_flagged(self):
        svc = QualityContainmentService()
        svc.register_lot("L-Z", "50", "warehouse")
        svc.record_event({
            "event_id": "E-OC", "action": "split", "occurred_at": "2026-09-02T01:00:00Z",
            "inputs": [{"lot_id": "L-Z", "qty": "60"}],
            "outputs": [{"lot_id": "L-Z1", "qty": "60"}],
        })
        self.assertTrue(svc._graph.lot("L-Z").conflict)
        decision = svc.request_material("REQ-OC", "L-Z", "1", "worker")
        self.assertEqual(decision["decision"], "review_required")


if __name__ == "__main__":
    unittest.main()
