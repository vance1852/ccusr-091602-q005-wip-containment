"""关闭前视图与关闭条件: 未定位数量、传播路径、放行依据、召回清单。"""

import unittest

from app.errors import CloseBlockedError, IncidentStateError
from scenario import build_service


class CloseTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()
        self.svc.open_incident("INC-1", investigator="INV-1", equipment="EQ-7",
                               reported_qty={"M1": "1000"})

    def _dispose_all(self):
        # LOT-A 400 检验放行; FG-E2 400 = 检验 200 + 偏差 200; SHP-F 召回确认
        self.svc.record_inspection("INC-1", "INSP-A", "LOT-A", sampled_qty="40",
                                   covered_qty="400", result="pass", inspector="QA-1")
        self.svc.release("INC-1", "REL-A", "LOT-A", qty="400",
                         basis_kind="inspection", basis_id="INSP-A", released_by="MGR-1")
        self.svc.record_inspection("INC-1", "INSP-E", "FG-E2", sampled_qty="40",
                                   covered_qty="200", result="pass", inspector="QA-1")
        self.svc.release("INC-1", "REL-E1", "FG-E2", qty="200",
                         basis_kind="inspection", basis_id="INSP-E", released_by="MGR-1")
        self.svc.request_deviation("INC-1", "DEV-E", "FG-E2", qty="200",
                                   reason="末检加严合格", requested_by="ENG-1")
        self.svc.decide_deviation("INC-1", "DEV-E", approver="MGR-1")
        self.svc.release("INC-1", "REL-E2", "FG-E2", qty="200",
                         basis_kind="deviation", basis_id="DEV-E", released_by="MGR-1")
        self.svc.confirm_recall("INC-1", "SHP-F", confirmed_by="CS-1", note="客户已隔离")

    def test_pre_close_report_contents(self):
        self._dispose_all()
        rep = self.svc.pre_close_report("INC-1")
        self.assertEqual(rep.unlocated, ())  # 通报 1000, 谱系定位 1000
        self.assertEqual(rep.recalls, ("SHP-F",))
        self.assertEqual(rep.recalls_confirmed, ("SHP-F",))
        path_map = dict(rep.paths)
        self.assertEqual([s.object_id for s in path_map["SHP-F"]],
                         ["LOT-A", "WIP-B", "WIP-D", "FG-E", "FG-E1", "SHP-F"])
        self.assertEqual(len(rep.releases), 3)  # REL-A, REL-E1, REL-E2
        bases = {r[0]: r[3] for r in rep.releases}
        self.assertEqual(bases["REL-E2"], "deviation:DEV-E")
        self.assertEqual(bases["REL-A"], "inspection:INSP-A")
        disp = dict(rep.dispositions)
        self.assertEqual(disp["SHP-F"], "recalled")
        self.assertEqual(disp["LOT-A"], "released")
        self.assertEqual(rep.close_blockers, ())

    def test_close_blocked_until_all_disposed(self):
        with self.assertRaises(CloseBlockedError) as ctx:
            self.svc.close_incident("INC-1", manager="QM-1")
        msg = str(ctx.exception)
        self.assertIn("LOT-A", msg)      # 冻结量未处置
        self.assertIn("FG-E2", msg)
        self.assertIn("SHP-F", msg)      # 召回未确认
        self._dispose_all()
        out = self.svc.close_incident("INC-1", manager="QM-1")
        self.assertEqual(out["status"], "closed")
        with self.assertRaises(IncidentStateError):  # 关闭后不能再放行
            self.svc.release("INC-1", "REL-Z", "LOT-A", qty="1",
                             basis_kind="inspection", basis_id="INSP-A",
                             released_by="MGR-1")

    def test_unlocated_quantity_blocks_close(self):
        svc = build_service()
        svc.open_incident("INC-2", investigator="INV-1", equipment="EQ-7",
                          reported_qty={"M1": "1200"})  # 通报 1200, 谱系只见 1000
        rep = svc.pre_close_report("INC-2")
        self.assertEqual(dict(rep.unlocated), {"M1": "200"})
        with self.assertRaises(CloseBlockedError) as ctx:
            svc.close_incident("INC-2", manager="QM-1")
        self.assertIn("未定位", str(ctx.exception))

    def test_pending_deviation_blocks_close(self):
        self._dispose_all()
        self.svc.request_deviation("INC-1", "DEV-P", "FG-E2", qty="1",
                                   reason="遗留待批", requested_by="ENG-1")
        with self.assertRaises(CloseBlockedError) as ctx:
            self.svc.close_incident("INC-1", manager="QM-1")
        self.assertIn("DEV-P", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
