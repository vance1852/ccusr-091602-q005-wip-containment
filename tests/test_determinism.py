"""确定性：乱序谱系、重复扫描、部分放行、并发领料 → 相同围堵边界。"""

import random
import threading
import unittest

from scenario import (
    APPROVER,
    EVENTS,
    INSPECTOR,
    INVESTIGATOR,
    REGISTRATIONS,
    SCOPE_B1,
    build_service,
)
from app import QualityContainmentService


def full_workflow(svc):
    """在谱系加载完成后开立事件并施加固定的检验 / 放行 / 领料序列。"""
    svc.open_case("C-1", INVESTIGATOR, SCOPE_B1)
    apply_dispositions(svc)
    return svc


def apply_dispositions(svc):
    svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "pass", INSPECTOR)
    svc.record_inspection("C-1", "I-2", "L-M2R", "sampling", "300", "fail", INSPECTOR)
    svc.approve_deviation("C-1", "D-1", "L-M2R", "300", APPROVER, "特采")
    svc.release("C-1", "R-1", "L-B1", "100", INVESTIGATOR, inspection_ids=["I-1"])
    svc.release("C-1", "R-2", "L-M2R", "300", INVESTIGATOR, deviation_ids=["D-1"])
    svc.request_material("REQ-1", "L-B1", "60", "worker-a")
    svc.request_material("REQ-2", "L-B1", "100", "worker-b")   # 超出剩余放行额度 → block
    svc.request_material("REQ-3", "L-PACK", "50", "worker-c")  # 干净对象 → allow
    svc.request_material("REQ-4", "L-SHIP", "10", "worker-d")  # 召回对象 → block
    return svc


class ShuffledArrivalTest(unittest.TestCase):
    def test_same_boundary_regardless_of_order(self):
        reference = full_workflow(build_service(open_case=False))
        expected = reference.boundary_digest("C-1")

        for seed in range(10):
            rng = random.Random(seed)
            events = list(EVENTS)
            rng.shuffle(events)
            # 交织重复扫描（重复扫描不得改变结果）
            interleaved = []
            for event in events:
                interleaved.append(event)
                if rng.random() < 0.4:
                    interleaved.append(dict(event))
            svc = build_service(events=interleaved, open_case=False)
            full_workflow(svc)
            self.assertEqual(
                svc.boundary_digest("C-1"), expected, f"arrival order seed={seed} 边界不一致"
            )

    def test_events_and_registrations_interleaved(self):
        reference = full_workflow(build_service(open_case=False))
        expected = reference.boundary_digest("C-1")
        for seed in range(5):
            items = [("event", e) for e in EVENTS] + [("reg", r) for r in REGISTRATIONS]
            random.Random(100 + seed).shuffle(items)
            svc = QualityContainmentService()
            for kind, payload in items:
                if kind == "event":
                    svc.record_event(payload)
                else:
                    svc.register_lot(**payload)
            full_workflow(svc)
            self.assertEqual(svc.boundary_digest("C-1"), expected)


class LateEventConvergenceTest(unittest.TestCase):
    def test_late_events_converge_to_same_boundary(self):
        """无论 E7 早到还是晚到，最终边界一致。"""
        early = full_workflow(build_service(open_case=False))

        late = build_service(events=[e for e in EVENTS if e["event_id"] != "E7"], open_case=False)
        late.open_case("C-1", INVESTIGATOR, SCOPE_B1)
        self.assertNotIn("L-B1", late._case("C-1").impacted)
        late.record_event(EVENTS[6])  # 迟到事件只能扩展围堵版本
        self.assertIn("L-B1", late._case("C-1").impacted)
        apply_dispositions(late)
        self.assertEqual(late.boundary_digest("C-1"), early.boundary_digest("C-1"))


class ConcurrentLoadTest(unittest.TestCase):
    def test_concurrent_requests_match_sequential_reference(self):
        """并发领料与串行领料得到相同的最终边界。"""
        sequential = full_workflow(build_service(open_case=False))
        for i in range(10):
            sequential.request_material(f"REQ-S-{i}", "L-B1", "4", "worker")
        expected = sequential.boundary_digest("C-1")

        concurrent = full_workflow(build_service(open_case=False))
        barrier = threading.Barrier(10)

        def worker(i):
            barrier.wait()
            concurrent.request_material(f"REQ-S-{i}", "L-B1", "4", "worker")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(concurrent.boundary_digest("C-1"), expected)


if __name__ == "__main__":
    unittest.main()
