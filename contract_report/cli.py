"""Командная строка. Единственное место, где есть ввод-вывод и знание о "сегодня"."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Sequence, TextIO

from .calculator import calculate
from .loader import DataError, load_dataset
from .render import render_csv, render_json, render_table

EXIT_OK = 0
EXIT_USAGE = 2   # совпадает с кодом argparse при неверных аргументах
EXIT_DATA = 3

_RENDERERS = {"table": render_table, "json": render_json, "csv": render_csv}


def _report_date(raw: str) -> date:
    """Принимаем и ДД.ММ.ГГГГ (как пишет бухгалтер), и ГГГГ-ММ-ДД (как в данных)."""
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"не похоже на дату: {raw!r} (ожидается ДД.ММ.ГГГГ или ГГГГ-ММ-ДД)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="contract_report",
        description="Отчёт по договорам: по каждой работе - цена, выплачено, осталось оплатить; итоги по договору.",
    )
    p.add_argument("-d", "--date", type=_report_date, default=None, metavar="ДАТА",
                   help="дата отчёта, ДД.ММ.ГГГГ или ГГГГ-ММ-ДД (по умолчанию - сегодня)")
    p.add_argument("--data-dir", type=Path, default=Path("data"), metavar="КАТАЛОГ",
                   help="каталог с contracts.csv, works.csv, payments.csv (по умолчанию ./data)")
    p.add_argument("-f", "--format", choices=sorted(_RENDERERS), default="table",
                   help="формат вывода (по умолчанию table)")
    p.add_argument("-c", "--contract", metavar="ID", help="показать только один договор")
    p.add_argument("-o", "--output", type=Path, metavar="ФАЙЛ",
                   help="записать отчёт в файл (UTF-8) вместо вывода на экран")
    return p


def main(argv: Optional[Sequence[str]] = None, stdout: Optional[TextIO] = None,
         stderr: Optional[TextIO] = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)  # при ошибке аргументов argparse сам завершает процесс с кодом 2

    as_of = args.date or date.today()

    try:
        dataset = load_dataset(args.data_dir)
    except DataError as exc:
        print(f"Ошибка: {exc}", file=stderr)
        return EXIT_DATA

    if args.contract and args.contract not in {c.contract_id for c in dataset.contracts}:
        print(f"Ошибка: договор {args.contract!r} не найден в {args.data_dir / 'contracts.csv'}", file=stderr)
        return EXIT_USAGE

    report = calculate(dataset, as_of, args.contract)
    text = _RENDERERS[args.format](report)

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"Отчёт записан в {args.output}", file=stderr)
    else:
        stdout.write(text)

    # Для CSV и JSON предупреждения внутри отчёта неудобны (в CSV их вообще негде хранить),
    # поэтому в таблице они уже есть в тексте, а в остальных форматах дублируем в stderr.
    if args.format == "csv":
        for w in report.warnings:
            print(f"Предупреждение [{w.contract_id}]: {w.message}", file=stderr)
    return EXIT_OK
