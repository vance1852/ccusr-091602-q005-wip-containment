"""领料判定：允许 / 阻止 / 待复核，幂等与并发守恒。"""

import threading
import unittest

from app import ValidationError

from scenario import INSPECTOR, INVESTIGATOR, build_service


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_block_held_lot(self):
        decision = self.svc.request_material("REQ-1", "L-B1", "10", "worker", station="ST-1")
        self.assertEqual(decision["decision"], "block")
        self.assertEqual(decision["case_ids"], ["C-1"])
        self.assertTrue(any("冻结" in r for r in decision["reasons"]))

    def test_allow_clean_lot(self):
        decision = self.svc.request_material("REQ-2", "L-PACK", "100", "worker")
        self.assertEqual(decision["decision"], "allow")
        self.assertTrue(any("未命中" in r for r in decision["reasons"]))

    def test_block_insufficient_stock(self):
        decision = self.svc.request_material("REQ-3", "L-PACK", "1000", "worker")
        self.assertEqual(decision["decision"], "block")
        self.assertTrue(any("不足" in r for r in decision["reasons"]))

    def test_review_unknown_lot(self):
        decision = self.svc.request_material("REQ-4", "L-NOPE", "1", "worker")
        self.assertEqual(decision["decision"], "review_required")
        self.assertTrue(any("无法核实来源" in r for r in decision["reasons"]))

    def test_review_clean_but_shipped(self):
        decision = self.svc.request_material("REQ-5", "L-PSHIP", "10", "worker")
        self.assertEqual(decision["decision"], "review_required")
        self.assertTrue(any("已发运" in r for r in decision["reasons"]))

    def test_issue_consumes_available(self):
        self.assertEqual(self.svc.request_material("REQ-6", "L-PACK", "250", "worker")["decision"], "allow")
        # L-PACK 300 - 已发运 50 - 已领 250 = 0
        self.assertEqual(self.svc.request_material("REQ-7", "L-PACK", "1", "worker")["decision"], "block")

    def test_request_id_idempotent(self):
        first = self.svc.request_material("REQ-8", "L-PACK", "10", "worker")
        retry = self.svc.request_material("REQ-8", "L-PACK", "10", "worker")
        self.assertEqual(first, retry)
        with self.assertRaises(ValidationError):
            self.svc.request_material("REQ-8", "L-PACK", "20", "worker")

    def test_released_lot_allows_up_to_released_qty(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "pass", INSPECTOR)
        svc.release("C-1", "R-1", "L-B1", "100", INVESTIGATOR, inspection_ids=["I-1"])
        allowed = svc.request_material("REQ-9", "L-B1", "100", "worker")
        self.assertEqual(allowed["decision"], "allow")
        self.assertEqual(allowed["basis_release_ids"], ["R-1"])
        blocked = svc.request_material("REQ-10", "L-B1", "1", "worker")
        self.assertEqual(blocked["decision"], "block")


class ConcurrencyTest(unittest.TestCase):
    @staticmethod
    def _run_concurrent_pool(svc, threads=20, each="10"):
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "pass", INSPECTOR)
        svc.release("C-1", "R-1", "L-B1", "100", INVESTIGATOR, inspection_ids=["I-1"])
        results = []
        barrier = threading.Barrier(threads)

        def worker(i):
            barrier.wait()
            results.append(svc.request_material(f"REQ-C-{i}", "L-B1", each, f"worker-{i}"))

        pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        return results

    def test_concurrent_requests_conserve_released_pool(self):
        svc = build_service()
        impacted_before = set(svc._case("C-1").impacted)
        results = self._run_concurrent_pool(svc)

        allowed = [r for r in results if r["decision"] == "allow"]
        blocked = [r for r in results if r["decision"] == "block"]
        # 释放额度 100，每笔 10：恰好 10 笔放行，绝不超过
        self.assertEqual(len(allowed), 10)
        self.assertEqual(len(blocked), 10)
        # 围堵边界（受影响集合）不受并发领料影响
        self.assertEqual(set(svc._case("C-1").impacted), impacted_before)

    def test_concurrent_workload_deterministic_boundary(self):
        """同样的并发负载跑两次，最终围堵边界逐字节一致。"""
        svc1, svc2 = build_service(), build_service()
        self._run_concurrent_pool(svc1)
        self._run_concurrent_pool(svc2)
        self.assertEqual(svc1.boundary_digest("C-1"), svc2.boundary_digest("C-1"))

    def test_concurrent_duplicate_request_id_single_decision(self):
        svc = build_service()
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "10", "pass", INSPECTOR)
        svc.release("C-1", "R-1", "L-B1", "10", INVESTIGATOR, inspection_ids=["I-1"])

        results = []
        barrier = threading.Barrier(10)

        def worker():
            barrier.wait()
            results.append(svc.request_material("REQ-DUP", "L-B1", "10", "worker"))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 同一 request_id 并发重试：全部得到同一结论，且只消耗一笔额度
        self.assertEqual({r["decision"] for r in results}, {"allow"})
        self.assertEqual({r["seq"] for r in results}, {results[0]["seq"]})
        followup = svc.request_material("REQ-AFTER", "L-B1", "1", "worker")
        self.assertEqual(followup["decision"], "block")


if __name__ == "__main__":
    unittest.main()
