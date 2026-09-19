"""检验、复验、偏差与部分放行: 数量关联与职责分离。"""

import unittest

from app.errors import DomainError, ReleaseError, SeparationOfDutiesError
from scenario import build_service


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7")

    def _pass_inspection(self, iid="INSP-1", covered="200"):
        return self.svc.record_inspection("INC-1", iid, "FG-E2", sampled_qty="40",
                                          covered_qty=covered, result="pass",
                                          inspector="QA-1")

    def test_partial_release_bounded_by_inspection_coverage(self):
        self._pass_inspection(covered="200")
        self.svc.release("INC-1", "REL-1", "FG-E2", qty="120",
                         basis_kind="inspection", basis_id="INSP-1", released_by="MGR-1")
        self.svc.release("INC-1", "REL-2", "FG-E2", qty="80",
                         basis_kind="inspection", basis_id="INSP-1", released_by="MGR-1")
        with self.assertRaises(ReleaseError):  # 检验覆盖额度已用完
            self.svc.release("INC-1", "REL-3", "FG-E2", qty="1",
                             basis_kind="inspection", basis_id="INSP-1", released_by="MGR-1")

    def test_release_cannot_exceed_held_qty(self):
        self._pass_inspection(iid="INSP-9", covered="999")
        with self.assertRaises(ReleaseError):
            self.svc.release("INC-1", "REL-9", "FG-E2", qty="401",
                             basis_kind="inspection", basis_id="INSP-9", released_by="MGR-1")

    def test_failed_inspection_cannot_release(self):
        self.svc.record_inspection("INC-1", "INSP-F", "FG-E2", sampled_qty="40",
                                   covered_qty="400", result="fail", inspector="QA-1")
        with self.assertRaises(ReleaseError):
            self.svc.release("INC-1", "REL-F", "FG-E2", qty="10",
                             basis_kind="inspection", basis_id="INSP-F", released_by="MGR-1")

    def test_reinspection_chain(self):
        self.svc.record_inspection("INC-1", "INSP-1", "FG-E2", sampled_qty="40",
                                   covered_qty="400", result="fail", inspector="QA-1")
        self.svc.record_inspection("INC-1", "INSP-2", "FG-E2", sampled_qty="80",
                                   covered_qty="150", result="pass", inspector="QA-2",
                                   ref_inspection="INSP-1")
        self.svc.release("INC-1", "REL-1", "FG-E2", qty="150",
                         basis_kind="inspection", basis_id="INSP-2", released_by="MGR-1")
        self.assertEqual(self.svc.decide_issue("FG-E2", "150").verdict, "allow")

    def test_deviation_flow_and_separation_of_duties(self):
        self.svc.request_deviation("INC-1", "DEV-1", "LOT-A", qty="100",
                                   reason="风险评估可接受", requested_by="ENG-1")
        with self.assertRaises(SeparationOfDutiesError):  # 调查员不能批准
            self.svc.decide_deviation("INC-1", "DEV-1", approver="INV-1")
        with self.assertRaises(SeparationOfDutiesError):  # 申请人不能批准
            self.svc.decide_deviation("INC-1", "DEV-1", approver="ENG-1")
        self.svc.decide_deviation("INC-1", "DEV-1", approver="MGR-1")
        self.svc.release("INC-1", "REL-D", "LOT-A", qty="100",
                         basis_kind="deviation", basis_id="DEV-1", released_by="MGR-1")
        self.assertEqual(self.svc.decide_issue("LOT-A", "100").verdict, "allow")

    def test_unapproved_deviation_cannot_release(self):
        self.svc.request_deviation("INC-1", "DEV-2", "LOT-A", qty="50",
                                   reason="待评估", requested_by="ENG-1")
        with self.assertRaises(ReleaseError):
            self.svc.release("INC-1", "REL-P", "LOT-A", qty="50",
                             basis_kind="deviation", basis_id="DEV-2", released_by="MGR-1")

    def test_release_separation_of_duties(self):
        self._pass_inspection()
        with self.assertRaises(SeparationOfDutiesError):
            self.svc.release("INC-1", "REL-X", "FG-E2", qty="10",
                             basis_kind="inspection", basis_id="INSP-1",
                             released_by="INV-1")  # 调查员本人

    def test_inspection_outside_boundary_rejected(self):
        with self.assertRaises(DomainError):
            self.svc.record_inspection("INC-1", "INSP-Z", "LOT-X", sampled_qty="1",
                                       covered_qty="1", result="pass", inspector="QA-1")


if __name__ == "__main__":
    unittest.main()
