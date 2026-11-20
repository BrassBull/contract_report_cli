import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from contract_report.cli import main

from helpers import SAMPLE_DATA


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--data-dir", str(SAMPLE_DATA), *argv], stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_table_default(self):
        code, out, _ = run("--date", "10.11.2026")
        self.assertEqual(code, 0)
        self.assertIn("Отчёт по договорам на 10.11.2026", out)
        self.assertIn("185 000,00", out)

    def test_both_date_formats_agree(self):
        self.assertEqual(run("-d", "10.11.2026")[1], run("-d", "2026-11-10")[1])

    def test_json_has_exact_strings(self):
        code, out, _ = run("-d", "10.11.2026", "-f", "json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        d1 = next(c for c in doc["contracts"] if c["contract_id"] == "D-1")
        self.assertEqual(d1["remaining"], "185000.00")
        self.assertEqual(d1["queued_payment_ids"], ["P-4"])
        self.assertEqual(doc["totals"]["paid"], "390000.00")
        self.assertEqual(doc["warnings"][0]["code"], "future_overpaid")

    def test_csv_rows(self):
        code, out, err = run("-d", "10.11.2026", "-f", "csv")
        self.assertEqual(code, 0)
        rows = list(csv.DictReader(io.StringIO(out)))
        self.assertEqual(len(rows), 9 + 2)  # 9 работ + 2 итога
        total = next(r for r in rows if r["row_type"] == "contract_total" and r["contract_id"] == "D-2")
        self.assertEqual((total["paid"], total["remaining"]), ("75000.00", "15000.00"))
        self.assertIn("Предупреждение", err)  # в CSV предупреждениям негде жить - идут в stderr

    def test_single_contract_filter(self):
        _, out, _ = run("-d", "10.11.2026", "-c", "D-2")
        self.assertIn("Договор D-2", out)
        self.assertNotIn("Договор D-1", out)

    def test_unknown_contract(self):
        code, _, err = run("-d", "10.11.2026", "-c", "D-99")
        self.assertEqual(code, 2)
        self.assertIn("D-99", err)

    def test_bad_date_exits_with_usage_error(self):
        with self.assertRaises(SystemExit) as ctx, contextlib_stderr():
            run("--date", "вчера")
        self.assertEqual(ctx.exception.code, 2)

    def test_data_error_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            code = main(["--data-dir", tmp, "-d", "10.11.2026"], stdout=out, stderr=err)
        self.assertEqual(code, 3)
        self.assertIn("не найден файл", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.csv"
            code, out, _ = run("-d", "10.11.2026", "-f", "csv", "-o", str(target))
            self.assertEqual(code, 0)
            self.assertEqual(out, "")
            self.assertTrue(target.read_text(encoding="utf-8").startswith("row_type,"))


class contextlib_stderr:
    """argparse печатает ошибку в настоящий stderr - глушим её, чтобы не засорять вывод тестов."""

    def __enter__(self):
        import contextlib
        self._cm = contextlib.redirect_stderr(io.StringIO())
        return self._cm.__enter__()

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


if __name__ == "__main__":
    unittest.main()
