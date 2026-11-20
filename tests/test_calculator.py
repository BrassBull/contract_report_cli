import unittest
from datetime import date
from decimal import Decimal

from contract_report.calculator import calculate, is_queued, paid_net, work_state
from contract_report.loader import load_dataset
from contract_report.models import WorkState

from helpers import SAMPLE_DATA, contract, d, dataset, money, payment, work


def D(x):
    return Decimal(x)


class AsOfStateTests(unittest.TestCase):
    """Откат данных на дату отчёта: статусы в выгрузке описывают будущее."""

    def test_work_accepted_after_date_is_still_planned(self):
        w = work("W", "100", "done", done="2026-11-11")
        self.assertIs(work_state(w, d("2026-11-10")), WorkState.PLANNED)
        self.assertIs(work_state(w, d("2026-11-11")), WorkState.DONE)  # граница включается

    def test_work_cancelled_after_date_is_still_alive(self):
        w = work("W", "100", "cancelled", cancelled="2026-10-12")
        self.assertIs(work_state(w, d("2026-10-11")), WorkState.PLANNED)
        self.assertIs(work_state(w, d("2026-10-12")), WorkState.CANCELLED)

    def test_refund_is_visible_only_after_it_happened(self):
        p = payment("P", "50", "refunded", paid="2026-09-02", refunded="2026-09-10")
        self.assertEqual(paid_net(p, d("2026-09-01")), 0)    # ещё не ушёл
        self.assertEqual(paid_net(p, d("2026-09-05")), 50)   # ушёл, не вернулся
        self.assertEqual(paid_net(p, d("2026-09-10")), 0)    # вернулся

    def test_queued_never_counts_as_paid(self):
        p = payment("P", "50", "queued", created="2026-11-01")
        self.assertEqual(paid_net(p, d("2099-01-01")), 0)
        self.assertFalse(is_queued(p, d("2026-10-31")))  # ещё не заведён
        self.assertTrue(is_queued(p, d("2026-11-01")))

    def test_paid_payment_was_in_queue_before_its_paid_date(self):
        p = payment("P", "50", "paid", created="2026-11-01", paid="2026-11-12")
        self.assertTrue(is_queued(p, d("2026-11-10")))
        self.assertFalse(is_queued(p, d("2026-11-12")))


class AllocationTests(unittest.TestCase):
    def _one(self, works, payments, as_of="2026-12-31", amount="1000.00"):
        rep = calculate(dataset([contract(amount)], works, payments), d(as_of))
        return rep.contracts[0], rep

    def test_unlinked_payments_cover_accepted_works_oldest_first(self):
        # Принята сначала W2, потом W1: FIFO идёт по порядку приёмки, а не по номеру.
        c, _ = self._one(
            [work("W1", "100", "done", done="2026-02-01"), work("W2", "100", "done", done="2026-01-15"),
             work("W3", "100", "done", done="2026-03-01")] + [],
            [payment("P1", "150")],
            amount="300.00",
        )
        by = {l.work_id: l for l in c.works}
        self.assertEqual(by["W2"].paid, D("100"))
        self.assertEqual(by["W1"].paid, D("50"))
        self.assertEqual(by["W3"].paid, D("0"))
        self.assertEqual(by["W1"].remaining, D("50"))
        self.assertEqual(c.accepted_unpaid, D("150"))

    def test_natural_order_of_work_ids_on_same_date(self):
        c, _ = self._one(
            [work("W-10", "100", "done", done="2026-02-01"), work("W-9", "100", "done", done="2026-02-01")],
            [payment("P1", "100")], amount="200.00",
        )
        by = {l.work_id: l for l in c.works}
        self.assertEqual(by["W-9"].paid, D("100"))   # не "W-10" < "W-9" как у строк
        self.assertEqual(by["W-10"].paid, D("0"))

    def test_advance_beyond_accepted_works_stays_unallocated(self):
        c, _ = self._one(
            [work("W1", "100", "done", done="2026-02-01"), work("W2", "100")],
            [payment("P1", "150")], amount="200.00",
        )
        by = {l.work_id: l for l in c.works}
        self.assertEqual(by["W2"].paid, D("0"))           # в запланированную работу не раскладываем
        self.assertEqual(c.unallocated, D("50"))
        self.assertEqual(c.remaining, D("50"))            # 200 - 150
        self.assertEqual(c.accepted_unpaid, D("0"))

    def test_linked_payment_goes_to_its_work_first(self):
        c, _ = self._one(
            [work("W1", "100", "done", done="2026-02-02"), work("W2", "100", "done", done="2026-02-01")],
            [payment("P1", "30", work_id="W2"), payment("P2", "100")], amount="200.00",
        )
        by = {l.work_id: l for l in c.works}
        self.assertEqual((by["W2"].paid_linked, by["W2"].paid_allocated), (D("30"), D("70")))
        self.assertEqual(by["W1"].paid, D("30"))
        self.assertEqual(c.remaining, D("70"))

    def test_money_is_conserved(self):
        """Выплачено по работам + нераспределённое = выплачено по договору, при любой каше в данных."""
        c, _ = self._one(
            [work("W1", "100", "done", done="2026-02-01"), work("W2", "100", "cancelled", cancelled="2026-02-05"),
             work("W3", "100")],
            [payment("P1", "130"), payment("P2", "40", work_id="W2"), payment("P3", "500", work_id="W3")],
            amount="300.00",
        )
        self.assertEqual(sum((l.paid for l in c.works), D("0")) + c.unallocated, c.paid)

    def test_cancelled_work_reduces_payable_and_has_zero_remaining(self):
        c, _ = self._one(
            [work("W1", "100", "done", done="2026-02-01"), work("W2", "100", "cancelled", cancelled="2026-02-05")],
            [payment("P1", "100")], amount="200.00",
        )
        self.assertEqual(c.cancelled_amount, D("100"))
        self.assertEqual(c.payable, D("100"))
        self.assertEqual(c.remaining, D("0"))
        self.assertEqual({l.work_id: l.remaining for l in c.works}["W2"], D("0"))

    def test_payment_for_cancelled_work_is_kept_and_flagged(self):
        c, rep = self._one(
            [work("W1", "100", "cancelled", cancelled="2026-02-05"), work("W2", "100")],
            [payment("P1", "40", work_id="W1")], amount="200.00",
        )
        self.assertEqual(c.paid, D("40"))
        self.assertEqual(c.unallocated, D("40"))
        self.assertIn("paid_for_cancelled_work", [w.code for w in rep.warnings])

    def test_overpaid_linked_work_excess_goes_to_pool(self):
        c, rep = self._one(
            [work("W1", "100", "done", done="2026-02-01"), work("W2", "100", "done", done="2026-02-02")],
            [payment("P1", "130", work_id="W1")], amount="200.00",
        )
        by = {l.work_id: l for l in c.works}
        self.assertEqual(by["W1"].paid, D("100"))
        self.assertEqual(by["W2"].paid, D("30"))  # излишек покрыл следующую принятую работу
        self.assertIn("work_overpaid", [w.code for w in rep.warnings])

    def test_decimal_cents_stay_exact(self):
        c, _ = self._one(
            [work("W1", "0.10", "done", done="2026-02-01"), work("W2", "0.20", "done", done="2026-02-02")],
            [payment("P1", "0.10"), payment("P2", "0.10")], amount="0.30",
        )
        self.assertEqual(c.paid, D("0.20"))
        self.assertEqual(c.remaining, D("0.10"))

    def test_contract_overpaid(self):
        c, rep = self._one([work("W1", "100", "done", done="2026-02-01")],
                           [payment("P1", "130")], amount="100.00")
        self.assertEqual((c.remaining, c.overpaid), (D("0"), D("30")))
        self.assertIn("contract_overpaid", [w.code for w in rep.warnings])

    def test_works_sum_mismatch_warns(self):
        _, rep = self._one([work("W1", "100")], [], amount="150.00")
        self.assertIn("works_sum_mismatch", [w.code for w in rep.warnings])

    def test_contract_signed_after_date_is_skipped(self):
        rep = calculate(dataset([contract(signed="2026-06-01")], [], []), d("2026-05-31"))
        self.assertEqual((rep.contracts, rep.skipped_contracts), ((), ("C",)))


class SampleDataTests(unittest.TestCase):
    """Эталонные числа на данных из задания. Посчитаны вручную, см. DECISIONS.md."""

    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(SAMPLE_DATA)

    def contract(self, cid, as_of):
        rep = calculate(self.ds, d(as_of))
        return next(c for c in rep.contracts if c.contract.contract_id == cid), rep

    def test_d1_on_2026_11_10(self):
        c, _ = self.contract("D-1", "2026-11-10")
        self.assertEqual(c.payable, D("500000.00"))      # 600 000 минус отменённая W-13
        self.assertEqual(c.paid, D("315000.00"))         # 300 000 + 15 000; ошибочные 50 000 возвращены 10.09
        self.assertEqual(c.remaining, D("185000.00"))
        self.assertEqual(c.accepted_unpaid, D("0.00"))
        self.assertEqual(c.unallocated, D("15000.00"))
        self.assertEqual(c.queued, D("150000.00"))       # P-4 в очереди, деньги не ушли
        by = {l.work_id: l for l in c.works}
        self.assertEqual([by[w].remaining for w in ("W-11", "W-12", "W-13", "W-14")], [D("0")] * 4)
        self.assertEqual((by["W-15"].remaining, by["W-16"].remaining), (D("100000.00"), D("100000.00")))
        self.assertEqual(by["W-14"].paid_linked, D("15000.00"))

    def test_d2_on_2026_11_10(self):
        c, rep = self.contract("D-2", "2026-11-10")
        by = {l.work_id: l for l in c.works}
        self.assertIs(by["W-23"].state, WorkState.PLANNED)   # принята 11.11 - позже даты отчёта
        self.assertEqual(c.paid, D("75000.00"))              # P-8 (12.11) ещё не ушёл
        self.assertEqual(c.remaining, D("15000.00"))
        self.assertEqual(c.overpaid, D("0"))
        self.assertIn("future_overpaid", [w.code for w in rep.warnings])

    def test_d2_after_p8_is_overpaid(self):
        c, _ = self.contract("D-2", "2026-11-12")
        self.assertEqual((c.paid, c.remaining, c.overpaid), (D("95000.00"), D("0"), D("5000.00")))

    def test_refund_timeline_on_d1(self):
        self.assertEqual(self.contract("D-1", "2026-09-01")[0].paid, D("300000.00"))
        self.assertEqual(self.contract("D-1", "2026-09-05")[0].paid, D("350000.00"))  # ошибочный платёж ещё у подрядчика
        self.assertEqual(self.contract("D-1", "2026-09-10")[0].paid, D("300000.00"))

    def test_contract_not_yet_signed(self):
        rep = calculate(self.ds, d("2026-09-01"))
        self.assertEqual([c.contract.contract_id for c in rep.contracts], ["D-1"])
        self.assertEqual(rep.skipped_contracts, ("D-2",))


if __name__ == "__main__":
    unittest.main()
