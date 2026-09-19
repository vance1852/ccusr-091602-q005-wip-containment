"""谱系事件模型: 守恒、形状、规范化与值相等性。"""

import unittest
from decimal import Decimal

from app.errors import ConservationError, EventShapeError, QuantityError
from app.model import GenealogyEvent, Portion

T = "2026-09-01T08:00:00"


class GenealogyEventTest(unittest.TestCase):
    def test_split_conservation_ok(self):
        ev = GenealogyEvent("E1", "split", {"A": "100"}, {"B": "60", "C": "40"}, T, "line")
        self.assertEqual([p.object_id for p in ev.outputs], ["B", "C"])

    def test_split_imbalance_rejected(self):
        with self.assertRaises(ConservationError):
            GenealogyEvent("E1", "split", {"A": "100"}, {"B": "60", "C": "41"}, T, "line")

    def test_merge_decimal_conservation(self):
        ev = GenealogyEvent("E2", "merge", {"A": "1.5", "B": "2.25"}, {"C": "3.75"}, T, "line")
        self.assertEqual(ev.inputs[1].qty, Decimal("2.25"))

    def test_shape_rules(self):
        with self.assertRaises(EventShapeError):  # split 只能一个输入
            GenealogyEvent("E3", "split", {"A": "1", "B": "1"}, {"C": "2"}, T, "line")
        with self.assertRaises(EventShapeError):  # merge 只能一个输出
            GenealogyEvent("E4", "merge", {"A": "1", "B": "1"}, {"C": "1", "D": "1"}, T, "line")
        with self.assertRaises(EventShapeError):  # ship 必须发往 transit/customer
            GenealogyEvent("E5", "ship", {"A": "1"}, {"B": "1"}, T, "warehouse")

    def test_duplicate_portions_merged_and_sorted(self):
        ev = GenealogyEvent("E6", "merge", [("B", "1"), ("A", "1"), ("B", "2")],
                            {"C": "4"}, T, "line")
        self.assertEqual([(p.object_id, str(p.qty)) for p in ev.inputs],
                         [("A", "1"), ("B", "3")])

    def test_same_content_equal_so_duplicate_scan_dedupes(self):
        a = GenealogyEvent("E7", "split", {"A": "2"}, {"B": "2"}, T, "line")
        b = GenealogyEvent("E7", "split", {"A": "2"}, {"B": "2"}, T, "line")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))

    def test_unknown_action_rejected(self):
        with self.assertRaises(EventShapeError):
            GenealogyEvent("E8", "teleport", {"A": "1"}, {"B": "1"}, T, "line")

    def test_portion_must_be_positive(self):
        with self.assertRaises(QuantityError):
            Portion("A", "0")


if __name__ == "__main__":
    unittest.main()
