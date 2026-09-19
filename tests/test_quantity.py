"""定点数量解析: 守恒判断的根基。"""

import unittest
from decimal import Decimal

from app.errors import QuantityError
from app.quantity import parse_quantity, qty_str


class ParseQuantityTest(unittest.TestCase):
    def test_accepts_decimal_string(self):
        self.assertEqual(parse_quantity("12.500"), Decimal("12.500"))

    def test_accepts_int_and_decimal(self):
        self.assertEqual(parse_quantity(7), Decimal(7))
        self.assertEqual(parse_quantity(Decimal("3.25")), Decimal("3.25"))

    def test_rejects_binary_float(self):
        with self.assertRaises(QuantityError):
            parse_quantity(0.1)

    def test_rejects_negative_nan_inf(self):
        for bad in ("-1", "NaN", "Infinity", "-Infinity"):
            with self.assertRaises(QuantityError, msg=bad):
                parse_quantity(bad)

    def test_rejects_garbage(self):
        for bad in ("", "abc", None, True, [1]):
            with self.assertRaises(QuantityError, msg=repr(bad)):
                parse_quantity(bad)

    def test_qty_str_normalizes(self):
        self.assertEqual(qty_str(Decimal("2.50")), "2.5")
        self.assertEqual(qty_str(Decimal("2E+2")), "200")
        self.assertEqual(qty_str(Decimal("0")), "0")


if __name__ == "__main__":
    unittest.main()
