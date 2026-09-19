"""谱系图推导: 在库量、位置、违规检测与下游传播。"""

import unittest

from app.graph import GenealogyGraph, Impact
from app.model import GenealogyEvent, ObjectRecord

T = "2026-09-01T"


def events():
    return [
        GenealogyEvent("E1", "split", {"A": "60"}, {"B": "60"},
                       T + "08:00:00", "line", equipment="EQ-7"),
        GenealogyEvent("E2", "merge", {"B": "60", "C": "40"}, {"D": "100"},
                       T + "08:30:00", "line"),
        GenealogyEvent("E3", "ship", {"D": "100"}, {"S": "100"},
                       T + "09:00:00", "customer"),
    ]


class GraphTest(unittest.TestCase):
    def setUp(self):
        self.objects = {"A": ObjectRecord("A", "100"), "C": ObjectRecord("C", "40")}

    def test_on_hand_and_location(self):
        g = GenealogyGraph(self.objects, events())
        self.assertEqual(str(g.on_hand("A")), "40")
        self.assertEqual(str(g.on_hand("B")), "0")
        self.assertEqual(str(g.on_hand("D")), "0")
        self.assertEqual(str(g.on_hand("S")), "100")
        self.assertEqual(g.location_of("S"), "customer")
        self.assertTrue(g.is_shipped("S"))
        self.assertEqual(g.location_of("A"), "warehouse")

    def test_arrival_order_irrelevant(self):
        g1 = GenealogyGraph(self.objects, events())
        g2 = GenealogyGraph(self.objects, list(reversed(events())))
        for oid in ("A", "B", "C", "D", "S"):
            self.assertEqual(g1.on_hand(oid), g2.on_hand(oid), msg=oid)
            self.assertEqual(g1.location_of(oid), g2.location_of(oid), msg=oid)

    def test_overdraft_recorded_as_violation(self):
        bad = [GenealogyEvent("E9", "split", {"A": "999"}, {"Z": "999"},
                              T + "10:00:00", "line")]
        g = GenealogyGraph(self.objects, bad)
        self.assertTrue(any("透支" in v for v in g.violations))

    def test_unregistered_object_recorded_as_violation(self):
        ev = [GenealogyEvent("E10", "split", {"GHOST": "5"}, {"Z": "5"},
                             T + "10:00:00", "line")]
        g = GenealogyGraph({}, ev)
        self.assertTrue(any("GHOST" in v for v in g.violations))

    def test_downstream_impact_and_path(self):
        g = GenealogyGraph(self.objects, events())
        impact = g.downstream_impact({"A"})
        self.assertEqual(impact.objects, frozenset({"A", "B", "D", "S"}))
        self.assertNotIn("C", impact.objects)  # 干净批次不被卷入
        path = impact.path_to("S")
        self.assertEqual([s.object_id for s in path], ["A", "B", "D", "S"])
        self.assertIsNone(path[0].via_event)
        self.assertEqual(path[1].via_event, "E1")

    def test_impact_union_keeps_earliest_evidence(self):
        g = GenealogyGraph(self.objects, events())
        i1 = g.downstream_impact({"A"})
        i2 = g.downstream_impact({"C"})
        merged = Impact.union(i1, i2)
        self.assertEqual(merged.objects, frozenset({"A", "B", "C", "D", "S"}))
        self.assertEqual(merged.introduced_by["B"], ("E1", "A"))  # 先记录的证据优先


if __name__ == "__main__":
    unittest.main()
