"""确定性: 乱序谱系、重复扫描、部分放行与并发领料下的围堵边界稳定性。"""

import random
import threading
import unittest

from app.model import GenealogyEvent
from app.service import ContainmentService
from scenario import base_events, build_service, register_objects


def shuffled_service(order, duplicates=False):
    svc = ContainmentService()
    register_objects(svc)
    events = [base_events()[i] for i in order]
    if duplicates:
        events = events + events  # 每条事件重复扫描一次
    svc.ingest_events(events)
    svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7",
                      reported_qty={"M1": "1000"})
    return svc


class DeterminismTest(unittest.TestCase):
    def test_out_of_order_and_duplicate_scans_same_boundary(self):
        n = len(base_events())
        reference = shuffled_service(list(range(n))).containment_boundary("INC-1")
        for seed in (1, 7, 42, 2026):
            order = list(range(n))
            random.Random(seed).shuffle(order)
            svc = shuffled_service(order, duplicates=True)
            b = svc.containment_boundary("INC-1")
            self.assertEqual(b["objects"], reference["objects"], msg=f"seed={seed}")
            self.assertEqual(b["holds"], reference["holds"], msg=f"seed={seed}")
            self.assertEqual(b["recalls"], reference["recalls"], msg=f"seed={seed}")
            self.assertEqual(b["events"], reference["events"], msg=f"seed={seed}")
            self.assertEqual(b["version"], reference["version"], msg=f"seed={seed}")

    def test_same_history_same_snapshot(self):
        s1 = build_service()
        s2 = build_service()
        s1.open_incident("INC-1", investigator="INV-1", equipment="EQ-7")
        s2.open_incident("INC-1", investigator="INV-1", equipment="EQ-7")
        self.assertEqual(s1.snapshot(), s2.snapshot())

    def test_late_events_only_expand_scope(self):
        svc = build_service()
        svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7")
        history = [set(svc.containment_boundary("INC-1")["objects"])]
        late = [
            GenealogyEvent("EV-7", "split", {"LOT-A": "400"}, {"WIP-G": "400"},
                           "2026-09-01T11:00:00", "line", equipment="EQ-8"),
            GenealogyEvent("EV-8", "split", {"WIP-G": "200"}, {"WIP-H": "200"},
                           "2026-09-01T11:30:00", "line", equipment="EQ-8"),
        ]
        for ev in late:
            svc.ingest_event(ev)
            history.append(set(svc.containment_boundary("INC-1")["objects"]))
        self.assertTrue(history[0] <= history[1] <= history[2])  # 单调扩展
        final = svc.containment_boundary("INC-1")
        self.assertIn("WIP-H", final["objects"])
        self.assertIn("LOT-A", final["objects"])  # 已记录范围不消失
        self.assertEqual(final["versions"], [1, 2, 3])

    def test_partial_release_and_concurrent_issues_keep_boundary(self):
        svc = build_service()
        svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7")
        svc.record_inspection("INC-1", "INSP-1", "FG-E2", sampled_qty="40",
                              covered_qty="200", result="pass", inspector="QA-1")
        svc.release("INC-1", "REL-1", "FG-E2", qty="200",
                    basis_kind="inspection", basis_id="INSP-1", released_by="MGR-1")
        before = svc.containment_boundary("INC-1")

        def worker(i):
            svc.commit_issue(f"REQ-{i}", "FG-E2", "25", requester=f"W-{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        after = svc.containment_boundary("INC-1")
        self.assertEqual(before["objects"], after["objects"])
        self.assertEqual(before["holds"], after["holds"])
        self.assertEqual(before["recalls"], after["recalls"])
        # 放行 200, 每笔 25: 恰好 8 笔成功, 不超发
        self.assertEqual(svc.object_state("FG-E2")["issued"], "200")


if __name__ == "__main__":
    unittest.main()
