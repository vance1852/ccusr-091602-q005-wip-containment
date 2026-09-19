"""检验、复验、偏差批准与部分放行：数量关联与职责分离。"""

import unittest

from app import (
    InsufficientBasisError,
    SeparationOfDutiesError,
    UnknownRecordError,
    ValidationError,
)

from scenario import APPROVER, INSPECTOR, INVESTIGATOR, build_service


class PartialReleaseTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_partial_release_against_sampling(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "pass", INSPECTOR)
        svc.release("C-1", "R-1", "L-B1", "60", INVESTIGATOR, inspection_ids=["I-1"])
        # 同一笔检验的剩余额度可继续支撑后续放行（部分释放）
        svc.release("C-1", "R-2", "L-B1", "40", INVESTIGATOR, inspection_ids=["I-1"])
        with self.assertRaises(InsufficientBasisError):
            svc.release("C-1", "R-3", "L-B1", "1", INVESTIGATOR, inspection_ids=["I-1"])

    def test_release_requires_basis(self):
        with self.assertRaises(InsufficientBasisError):
            self.svc.release("C-1", "R-1", "L-B1", "10", INVESTIGATOR)

    def test_release_cannot_exceed_inspected_qty(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "pass", INSPECTOR)
        with self.assertRaises(InsufficientBasisError):
            svc.release("C-1", "R-1", "L-B1", "150", INVESTIGATOR, inspection_ids=["I-1"])

    def test_release_cannot_exceed_held_qty(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "999", "pass", INSPECTOR)
        with self.assertRaises(ValidationError):
            svc.release("C-1", "R-1", "L-B1", "251", INVESTIGATOR, inspection_ids=["I-1"])

    def test_release_rejects_foreign_or_failed_basis(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "fail", INSPECTOR)
        svc.record_inspection("C-1", "I-2", "L-B2", "sampling", "100", "pass", INSPECTOR)
        with self.assertRaises(ValidationError):  # 不合格检验不能作为依据
            svc.release("C-1", "R-1", "L-B1", "10", INVESTIGATOR, inspection_ids=["I-1"])
        with self.assertRaises(ValidationError):  # 其他对象的检验不能挪用
            svc.release("C-1", "R-2", "L-B1", "10", INVESTIGATOR, inspection_ids=["I-2"])
        with self.assertRaises(UnknownRecordError):
            svc.release("C-1", "R-3", "L-B1", "10", INVESTIGATOR, inspection_ids=["I-NOPE"])

    def test_release_requires_impacted_lot(self):
        with self.assertRaises(ValidationError):
            self.svc.release("C-1", "R-1", "L-PACK", "10", INVESTIGATOR)

    def test_failed_release_is_atomic(self):
        """依据不足的放行不得部分消耗检验额度。"""
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "100", "pass", INSPECTOR)
        with self.assertRaises(InsufficientBasisError):
            svc.release("C-1", "R-1", "L-B1", "150", INVESTIGATOR, inspection_ids=["I-1"])
        svc.release("C-1", "R-2", "L-B1", "100", INVESTIGATOR, inspection_ids=["I-1"])


class DeviationTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_separation_of_duties(self):
        with self.assertRaises(SeparationOfDutiesError):
            self.svc.approve_deviation("C-1", "D-1", "L-M2R", "600", INVESTIGATOR, "特采")
        record = self.svc.approve_deviation("C-1", "D-2", "L-M2R", "600", APPROVER, "特采")
        self.assertEqual(record.approver, APPROVER)

    def test_deviation_qty_linked(self):
        svc = self.svc
        svc.approve_deviation("C-1", "D-1", "L-M2R", "200", APPROVER, "特采 200")
        svc.release("C-1", "R-1", "L-M2R", "200", INVESTIGATOR, deviation_ids=["D-1"])
        with self.assertRaises(InsufficientBasisError):
            svc.release("C-1", "R-2", "L-M2R", "1", INVESTIGATOR, deviation_ids=["D-1"])

    def test_mixed_basis(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-M2R", "sampling", "300", "pass", INSPECTOR)
        svc.approve_deviation("C-1", "D-1", "L-M2R", "300", APPROVER, "特采")
        svc.release("C-1", "R-1", "L-M2R", "600", INVESTIGATOR,
                    inspection_ids=["I-1"], deviation_ids=["D-1"])
        report = svc.closure_report("C-1")
        basis = report["released"][0]["basis"]
        self.assertEqual(
            sorted((b["type"], b["id"], b["qty"]) for b in basis),
            [("deviation", "D-1", "300"), ("inspection", "I-1", "300")],
        )


class PendingInspectionTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_pending_inspection_requires_review(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "250", "pending", INSPECTOR)
        decision = svc.request_material("REQ-P", "L-B1", "10", "worker")
        self.assertEqual(decision["decision"], "review_required")
        self.assertTrue(any("检验" in r for r in decision["reasons"]))

    def test_finalize_then_release(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "250", "pending", INSPECTOR)
        svc.finalize_inspection("C-1", "I-1", "pass", INSPECTOR)
        svc.release("C-1", "R-1", "L-B1", "250", INVESTIGATOR, inspection_ids=["I-1"])
        self.assertEqual(svc.request_material("REQ-F", "L-B1", "250", "worker")["decision"], "allow")

    def test_finalize_twice_rejected(self):
        svc = self.svc
        svc.record_inspection("C-1", "I-1", "L-B1", "sampling", "10", "pending", INSPECTOR)
        svc.finalize_inspection("C-1", "I-1", "fail", INSPECTOR)
        with self.assertRaises(ValidationError):
            svc.finalize_inspection("C-1", "I-1", "pass", INSPECTOR)

    def test_reinspection_requires_prior(self):
        svc = self.svc
        with self.assertRaises(ValidationError):
            svc.record_inspection("C-1", "I-1", "L-B1", "reinspection", "10", "pass", INSPECTOR)
        svc.record_inspection("C-1", "I-2", "L-B1", "sampling", "10", "fail", INSPECTOR)
        svc.record_inspection("C-1", "I-3", "L-B1", "reinspection", "10", "pass", INSPECTOR)


class ScrapTest(unittest.TestCase):
    def test_scrap_reduces_held(self):
        svc = build_service()
        svc.scrap("C-1", "L-M2R", "100", INVESTIGATOR, "AOI 复检不良报废")
        report = svc.closure_report("C-1")
        entry = next(e for e in report["held"] if e["lot_id"] == "L-M2R")
        self.assertEqual(entry["outstanding"], "500")
        self.assertEqual(entry["scrapped"], "100")
        with self.assertRaises(ValidationError):
            svc.scrap("C-1", "L-M2R", "501", INVESTIGATOR, "超额报废")


if __name__ == "__main__":
    unittest.main()
