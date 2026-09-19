"""领料判定: 允许/阻止/待复核的可解释结论与并发安全。"""

import threading
import unittest

from app.errors import RequestConflictError
from app.service import ALLOW, BLOCK, REVIEW
from scenario import build_service


class IssueDecisionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7")

    def _release_fg_e2(self, qty):
        self.svc.record_inspection("INC-1", "INSP-1", "FG-E2", sampled_qty="40",
                                   covered_qty=qty, result="pass", inspector="QA-1")
        self.svc.release("INC-1", "REL-1", "FG-E2", qty=qty,
                         basis_kind="inspection", basis_id="INSP-1", released_by="MGR-1")

    def test_allow_outside_boundary(self):
        d = self.svc.decide_issue("LOT-X", "50", requester="WH-1")
        self.assertEqual(d.verdict, ALLOW)
        self.assertTrue(any("不在任何围堵边界" in r for r in d.reasons))

    def test_block_inside_boundary_with_path_evidence(self):
        d = self.svc.decide_issue("LOT-A", "100")
        self.assertEqual(d.verdict, BLOCK)
        self.assertTrue(any("INC-1" in r for r in d.reasons))
        self.assertTrue(d.paths)
        iid, path = d.paths[0]
        self.assertEqual(iid, "INC-1")
        self.assertEqual(path[0].object_id, "LOT-A")

    def test_block_shipped_object_points_to_recall(self):
        d = self.svc.decide_issue("SHP-F", "10")
        self.assertEqual(d.verdict, BLOCK)
        self.assertTrue(any("召回" in r for r in d.reasons))

    def test_block_unknown_object(self):
        self.assertEqual(self.svc.decide_issue("NOPE", "1").verdict, BLOCK)

    def test_block_insufficient_qty(self):
        d = self.svc.decide_issue("LOT-X", "999")
        self.assertEqual(d.verdict, BLOCK)
        self.assertTrue(any("不足" in r for r in d.reasons))

    def test_partial_release_gives_allow_then_review(self):
        self._release_fg_e2("200")
        self.assertEqual(self.svc.decide_issue("FG-E2", "150").verdict, ALLOW)
        d = self.svc.decide_issue("FG-E2", "300")
        self.assertEqual(d.verdict, REVIEW)
        self.assertTrue(any("放行额度" in r for r in d.reasons))
        committed = self.svc.commit_issue("REQ-1", "FG-E2", "150", requester="LINE-3")
        self.assertTrue(committed.allowed)
        # 放行池剩 50: 50 可领, 51 待复核
        self.assertEqual(self.svc.decide_issue("FG-E2", "50").verdict, ALLOW)
        self.assertEqual(self.svc.decide_issue("FG-E2", "51").verdict, REVIEW)

    def test_review_when_deviation_pending(self):
        self.svc.request_deviation("INC-1", "DEV-1", "LOT-A", qty="100",
                                   reason="客户急单待评估", requested_by="ENG-1")
        d = self.svc.decide_issue("LOT-A", "50")
        self.assertEqual(d.verdict, REVIEW)
        self.assertTrue(any("偏差" in r for r in d.reasons))

    def test_commit_idempotent_replay(self):
        self._release_fg_e2("200")
        d1 = self.svc.commit_issue("REQ-9", "FG-E2", "100", requester="L")
        d2 = self.svc.commit_issue("REQ-9", "FG-E2", "100", requester="L")
        self.assertTrue(d1.allowed and d2.allowed)
        self.assertEqual(self.svc.object_state("FG-E2")["issued"], "100")  # 只扣一次
        with self.assertRaises(RequestConflictError):
            self.svc.commit_issue("REQ-9", "FG-E2", "50", requester="L")

    def test_concurrent_commits_never_overissue(self):
        self._release_fg_e2("200")
        results = []

        def worker(i):
            results.append(self.svc.commit_issue(f"REQ-{i}", "FG-E2", "30",
                                                 requester=f"W-{i}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        allowed = [d for d in results if d.allowed]
        self.assertEqual(len(allowed), 6)  # 200 // 30, 锁内原子判定
        self.assertEqual(self.svc.object_state("FG-E2")["issued"], "180")
        # 并发领料不改变围堵边界
        b = self.svc.containment_boundary("INC-1")
        self.assertIn("FG-E2", b["objects"])
        self.assertIn("FG-E2", b["holds"])


if __name__ == "__main__":
    unittest.main()
