import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from contract_report.loader import DataError, load_dataset, parse_date
from contract_report.money import format_money, money_str, parse_money

from helpers import SAMPLE_DATA


class MoneyTests(unittest.TestCase):
    def test_parse_is_exact_decimal(self):
        self.assertEqual(parse_money("300000.00"), Decimal("300000.00"))
        self.assertEqual(parse_money("15000"), Decimal("15000.00"))
        self.assertIsInstance(parse_money("0.1"), Decimal)

    def test_no_float_drift(self):
        # Классика: 0.1 + 0.2 != 0.3 для float. Для Decimal-денег равенство должно быть точным.
        self.assertEqual(parse_money("0.10") + parse_money("0.20"), parse_money("0.30"))

    def test_rejects_garbage(self):
        for bad in ["", "abc", "1e3", "NaN", "Infinity", "-5", "+5", "1,5", "100.005", "0", "0.00"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_money(bad)

    def test_format(self):
        self.assertEqual(format_money(Decimal("1234567.5")), "1 234 567,50")
        self.assertEqual(format_money(Decimal("0")), "0,00")
        self.assertEqual(money_str(Decimal("5000")), "5000.00")


class DateTests(unittest.TestCase):
    def test_strict_iso(self):
        self.assertEqual(parse_date("2026-11-10").day, 10)
        for bad in ["20261110", "10.11.2026", "2026-13-01", "2026-2-3x"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_date(bad)


class LoaderTests(unittest.TestCase):
    def _write(self, tmp, contracts=None, works=None, payments=None):
        files = {
            "contracts.csv": contracts or "contract_id,contractor,amount,signed_at\nC1,Иванов,1000.00,2026-01-01\n",
            "works.csv": works or "work_id,contract_id,title,price,status,completed_at,cancelled_at\n"
                                  "W1,C1,Работа,1000.00,planned,,\n",
            "payments.csv": payments or "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
                                        "P1,C1,,Аванс,500.00,paid,2026-01-02,2026-01-03,\n",
        }
        for name, text in files.items():
            (Path(tmp) / name).write_text(text, encoding="utf-8")
        return Path(tmp)

    def test_loads_sample_data(self):
        ds = load_dataset(SAMPLE_DATA)
        self.assertEqual((len(ds.contracts), len(ds.works), len(ds.payments)), (2, 9, 8))
        # Пустое work_id означает "платёж без привязки", а не пустую строку.
        self.assertIsNone(next(p for p in ds.payments if p.payment_id == "P-1").work_id)

    def test_bom_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, contracts="﻿contract_id,contractor,amount,signed_at\nC1,Иванов,1000.00,2026-01-01\n")
            self.assertEqual(len(load_dataset(Path(tmp)).contracts), 1)

    def test_all_errors_reported_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                works="work_id,contract_id,title,price,status,completed_at,cancelled_at\n"
                      "W1,C1,Работа,abc,planned,,\n"
                      "W2,NOPE,Работа,10.00,weird,,\n",
            )
            with self.assertRaises(DataError) as ctx:
                load_dataset(Path(tmp))
            msg = str(ctx.exception)
            self.assertIn("works.csv:2", msg)
            self.assertIn("works.csv:3", msg)
            self.assertIn("price", msg)
            self.assertIn("status", msg)

    def test_paid_payment_requires_paid_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, payments="payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
                                      "P1,C1,,Аванс,500.00,paid,2026-01-02,,\n")
            with self.assertRaisesRegex(DataError, "без даты оплаты"):
                load_dataset(Path(tmp))

    def test_payment_linked_to_work_of_another_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp,
                contracts="contract_id,contractor,amount,signed_at\nC1,А,1000.00,2026-01-01\nC2,Б,1000.00,2026-01-01\n",
                payments="payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
                         "P1,C2,W1,x,500.00,paid,2026-01-02,2026-01-03,\n",
            )
            with self.assertRaisesRegex(DataError, "относится к договору C1"):
                load_dataset(Path(tmp))

    def test_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, contracts="contract_id,contractor,amount,signed_at\n"
                                       "C1,А,1000.00,2026-01-01\nC1,Б,1000.00,2026-01-01\n")
            with self.assertRaisesRegex(DataError, "повторяется contract_id"):
                load_dataset(Path(tmp))

    def test_missing_file_and_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(DataError, "не найден файл"):
                load_dataset(Path(tmp))
            self._write(tmp, contracts="contract_id,contractor\nC1,А\n")
            with self.assertRaisesRegex(DataError, "нет обязательных колонок"):
                load_dataset(Path(tmp))


if __name__ == "__main__":
    unittest.main()
