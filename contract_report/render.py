"""Вывод отчёта: текстовая таблица, JSON, CSV. Расчёта здесь нет - только представление."""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal

from .calculator import ContractReport, Report
from .models import WorkState
from .money import format_money, money_str

_STATE_LABEL = {
    WorkState.DONE: "принята",
    WorkState.PLANNED: "запланирована",
    WorkState.CANCELLED: "отменена",
}


# --------------------------------------------------------------------------- таблица


def _table(headers: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells: list[str]) -> str:
        return "  ".join(
            c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(cells)
        ).rstrip()

    return [fmt(headers), "  ".join("-" * w for w in widths)] + [fmt(r) for r in rows]


def _contract_block(c: ContractReport) -> list[str]:
    k = c.contract
    out = [f"Договор {k.contract_id} - {k.contractor}, подписан {k.signed_at:%d.%m.%Y}"]
    rows = [
        [w.work_id, w.title, _STATE_LABEL[w.state], format_money(w.price),
         format_money(w.paid), format_money(w.paid_linked), format_money(w.remaining)]
        for w in c.works
    ]
    out += _table(
        ["Работа", "Название", "Статус", "Цена", "Выплачено", "в т.ч. по привязке", "Осталось"],
        rows, right={3, 4, 5, 6},
    )
    summary = [
        ("Сумма договора", format_money(k.amount)),
        ("Отменённые работы", "-" + format_money(c.cancelled_amount) if c.cancelled_amount else format_money(c.cancelled_amount)),
        ("К оплате с учётом отмен", format_money(c.payable)),
        ("Выплачено (за вычетом возвратов)", format_money(c.paid)),
        ("ОСТАЛОСЬ ОПЛАТИТЬ", format_money(c.remaining)),
        ("  из них: принято, но не оплачено", format_money(c.accepted_unpaid)),
        ("Аванс, не закреплённый за работами", format_money(c.unallocated)),
        ("В очереди на оплату (деньги не ушли)", format_money(c.queued)
         + (f"  [{', '.join(c.queued_payment_ids)}]" if c.queued_payment_ids else "")),
    ]
    if c.overpaid:
        summary.insert(5, ("ПЕРЕПЛАТА", format_money(c.overpaid)))
    label_w = max(len(s[0]) for s in summary)
    val_w = max(len(s[1].split("  [")[0]) for s in summary)
    out.append("")
    for label, value in summary:
        main, _, extra = value.partition("  [")
        out.append(f"  {label.ljust(label_w)}  {main.rjust(val_w)}" + (f"  [{extra}" if extra else ""))
    return out


def render_table(report: Report) -> str:
    lines = [f"Отчёт по договорам на {report.as_of:%d.%m.%Y}", ""]
    for c in report.contracts:
        lines += _contract_block(c) + [""]

    if len(report.contracts) > 1:
        lines += [
            "Всего по договорам:",
            f"  К оплате с учётом отмен: {format_money(report.total('payable'))}",
            f"  Выплачено:               {format_money(report.total('paid'))}",
            f"  Осталось оплатить:       {format_money(report.total('remaining'))}",
            f"  В очереди на оплату:     {format_money(report.total('queued'))}",
            "",
        ]
    if report.skipped_contracts:
        lines += [f"Не включены (подписаны после {report.as_of:%d.%m.%Y}): "
                  + ", ".join(report.skipped_contracts), ""]
    if report.warnings:
        lines.append("Предупреждения:")
        lines += [f"  [{w.contract_id}] {w.message}" for w in report.warnings]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- JSON


def _contract_dict(c: ContractReport) -> dict:
    return {
        "contract_id": c.contract.contract_id,
        "contractor": c.contract.contractor,
        "signed_at": c.contract.signed_at.isoformat(),
        "contract_amount": money_str(c.contract.amount),
        "cancelled_amount": money_str(c.cancelled_amount),
        "payable": money_str(c.payable),
        "paid": money_str(c.paid),
        "remaining": money_str(c.remaining),
        "overpaid": money_str(c.overpaid),
        "accepted_unpaid": money_str(c.accepted_unpaid),
        "unallocated": money_str(c.unallocated),
        "queued": money_str(c.queued),
        "queued_payment_ids": list(c.queued_payment_ids),
        "works": [
            {
                "work_id": w.work_id,
                "title": w.title,
                "state": w.state.value,
                "price": money_str(w.price),
                "paid": money_str(w.paid),
                "paid_linked": money_str(w.paid_linked),
                "remaining": money_str(w.remaining),
            }
            for w in c.works
        ],
    }


def render_json(report: Report) -> str:
    doc = {
        "as_of": report.as_of.isoformat(),
        "contracts": [_contract_dict(c) for c in report.contracts],
        "totals": {k: money_str(report.total(k)) for k in ("payable", "paid", "remaining", "queued")},
        "skipped_contracts": list(report.skipped_contracts),
        "warnings": [
            {"code": w.code, "contract_id": w.contract_id, "message": w.message}
            for w in report.warnings
        ],
    }
    # Суммы - строки, а не числа: JSON-парсеры по умолчанию читают числа как float.
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- CSV

CSV_COLUMNS = [
    "row_type", "contract_id", "work_id", "title", "state",
    "price", "paid", "remaining", "unallocated", "queued",
]


def render_csv(report: Report) -> str:
    """Плоская таблица для Excel: строки работ и по одной итоговой строке на договор.

    В итоговой строке price - сумма к оплате с учётом отмен (а не исходная сумма договора),
    чтобы колонки price/paid/remaining складывались одинаково для работ и для договора.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for c in report.contracts:
        for w in c.works:
            writer.writerow([
                "work", c.contract.contract_id, w.work_id, w.title, w.state.value,
                money_str(w.price), money_str(w.paid), money_str(w.remaining), "", "",
            ])
        writer.writerow([
            "contract_total", c.contract.contract_id, "", c.contract.contractor, "",
            money_str(c.payable), money_str(c.paid), money_str(c.remaining),
            money_str(c.unallocated), money_str(c.queued),
        ])
    return buf.getvalue()
