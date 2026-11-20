"""Расчёт отчёта на дату. Чистые функции: никакого ввода-вывода, никакого "сегодня".

Идея расчёта в трёх шагах (подробности и варианты - в DECISIONS.md):

1. "Откатываем" данные на дату отчёта. Выгрузка сделана позже, поэтому статусы в ней
   описывают будущее относительно даты отчёта. Работа, принятая 11.11, на 10.11 ещё не принята;
   платёж, ушедший 12.11, на 10.11 ещё не ушёл; возврат, случившийся 10.09, на 05.09 ещё не случился.
2. Платежи с привязкой идут в свою работу. Платежи без привязки образуют общий "пул"
   по договору, и он гасит принятые работы по порядку приёмки (FIFO).
3. Остаток пула, которому не хватило принятых работ, - это аванс. Он остаётся на уровне
   договора и в конкретные запланированные работы не раскладывается: мы не знаем, на что он пойдёт.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from .models import (
    Contract,
    Dataset,
    Payment,
    PaymentStatus,
    Work,
    WorkState,
    WorkStatus,
)
from .money import ZERO, format_money


# --------------------------------------------------------------------------- результат


@dataclass(frozen=True)
class WorkLine:
    work_id: str
    title: str
    state: WorkState
    price: Decimal            # цена из договора (у отменённой работы остаётся исходной)
    paid: Decimal             # сколько выплачено за эту работу всего
    paid_linked: Decimal      # из них - платежами, явно привязанными к работе
    remaining: Decimal        # сколько ещё нужно заплатить (у отменённой всегда 0)

    @property
    def paid_allocated(self) -> Decimal:
        """Часть выплаченного, отнесённая к работе расчётом (из платежей без привязки)."""
        return self.paid - self.paid_linked


@dataclass(frozen=True)
class ContractReport:
    contract: Contract
    works: tuple[WorkLine, ...]
    cancelled_amount: Decimal   # цены работ, отменённых к дате отчёта
    payable: Decimal            # сумма договора минус отменённое = сколько всего должны заплатить
    paid: Decimal               # выплачено за вычетом возвратов
    remaining: Decimal          # осталось оплатить по договору (не меньше 0)
    overpaid: Decimal           # переплата (не меньше 0)
    accepted_unpaid: Decimal    # принятые работы, за которые ещё не заплачено
    unallocated: Decimal        # аванс/деньги, не закреплённые ни за одной работой
    queued: Decimal             # в очереди на оплату, деньги ещё не ушли
    queued_payment_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReportWarning:
    code: str
    contract_id: str
    message: str


@dataclass(frozen=True)
class Report:
    as_of: date
    contracts: tuple[ContractReport, ...]
    warnings: tuple[ReportWarning, ...]
    skipped_contracts: tuple[str, ...] = field(default_factory=tuple)  # подписаны позже даты отчёта

    def total(self, attr: str) -> Decimal:
        return sum((getattr(c, attr) for c in self.contracts), ZERO)


# --------------------------------------------------------------------------- состояние "на дату"


def work_state(work: Work, as_of: date) -> WorkState:
    """Состояние работы на дату отчёта.

    Если дата события в данных отсутствует, событие считаем уже наступившим: статус
    в выгрузке - единственное, что мы знаем. Загрузчик такие строки не отвергает,
    а calculate() выдаёт предупреждение.
    """
    if work.status is WorkStatus.CANCELLED:
        if work.cancelled_at is None or work.cancelled_at <= as_of:
            return WorkState.CANCELLED
        return WorkState.PLANNED  # отменят позже - на дату отчёта работа ещё в силе
    if work.status is WorkStatus.DONE:
        if work.completed_at is None or work.completed_at <= as_of:
            return WorkState.DONE
        return WorkState.PLANNED  # примут позже даты отчёта
    return WorkState.PLANNED


def paid_net(payment: Payment, as_of: date) -> Decimal:
    """Сколько денег по платежу "сидит" у подрядчика на дату отчёта (0 или вся сумма).

    Возврат считается полным: в выгрузке нет поля "сумма возврата", а статус refunded
    описывает платёж целиком. Частичные возвраты это не покрывает (см. NOTES.md).
    """
    if payment.status is PaymentStatus.QUEUED:
        return ZERO
    if payment.paid_at is None or payment.paid_at > as_of:
        return ZERO  # деньги ещё не ушли
    if payment.status is PaymentStatus.REFUNDED and payment.refunded_at <= as_of:  # type: ignore[operator]
        return ZERO  # ушли и уже вернулись
    return payment.amount


def is_queued(payment: Payment, as_of: date) -> bool:
    """Платёж заведён, но к дате отчёта деньги ещё не ушли.

    Не смотрим на статус queued: платёж, который в выгрузке уже paid, на более раннюю
    дату мог стоять в очереди. Достаточно "заведён к дате и ещё не оплачен к дате".
    """
    return payment.created_at <= as_of and (payment.paid_at is None or payment.paid_at > as_of)


def _natural_key(text: str) -> list:
    """'W-9' < 'W-10': сравниваем числа как числа, а не как строки."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text)]


# --------------------------------------------------------------------------- расчёт по договору


def _calculate_contract(
    contract: Contract,
    works: list[Work],
    payments: list[Payment],
    as_of: date,
    warnings: list[ReportWarning],
) -> ContractReport:
    cid = contract.contract_id
    states = {w.work_id: work_state(w, as_of) for w in works}

    # Шаг 1. Разделяем деньги на привязанные к работам и общий пул.
    linked: dict[str, Decimal] = {}
    pool = ZERO
    paid_total = ZERO
    for p in payments:
        net = paid_net(p, as_of)
        if not net:
            continue
        paid_total += net
        if p.work_id is None:
            pool += net
        else:
            linked[p.work_id] = linked.get(p.work_id, ZERO) + net

    # Шаг 2. Привязанные платежи кладём в свои работы, но не больше цены работы.
    # Лишнее (переплата по работе, платёж по отменённой работе) не теряем,
    # а отправляем в пул: деньги ушли, и в сумме по договору они должны остаться.
    applied: dict[str, Decimal] = {}
    for w in works:
        direct = linked.get(w.work_id, ZERO)
        if states[w.work_id] is WorkState.CANCELLED:
            applied[w.work_id] = ZERO
            excess = direct
            if direct:
                warnings.append(ReportWarning(
                    "paid_for_cancelled_work", cid,
                    f"по отменённой работе {w.work_id} выплачено {format_money(direct)}: "
                    "деньги учтены как не закреплённые за работами; вероятно, нужен возврат или зачёт",
                ))
        else:
            applied[w.work_id] = min(direct, w.price)
            excess = direct - applied[w.work_id]
            if excess:
                warnings.append(ReportWarning(
                    "work_overpaid", cid,
                    f"по работе {w.work_id} привязанными платежами выплачено больше цены на "
                    f"{format_money(excess)}: излишек учтён как не закреплённый за работами",
                ))
        pool += excess

    # Шаг 3. Пул гасит принятые работы по порядку приёмки (FIFO), потом по номеру работы.
    # Порядок приёмки, а не дата платежа: бухгалтеру важно "за что рассчитались", и естественный
    # ответ - "за самые давние принятые работы". Платёж же с датой не связан с работой вообще.
    allocated: dict[str, Decimal] = {w.work_id: ZERO for w in works}
    done = sorted(
        (w for w in works if states[w.work_id] is WorkState.DONE),
        key=lambda w: (w.completed_at or date.min, _natural_key(w.work_id)),
    )
    for w in done:
        take = min(pool, w.price - applied[w.work_id])
        allocated[w.work_id] = take
        pool -= take

    # Строки отчёта в исходном порядке файла.
    lines = []
    for w in works:
        state = states[w.work_id]
        if state is WorkState.CANCELLED:
            paid, remaining = ZERO, ZERO
        else:
            paid = applied[w.work_id] + allocated[w.work_id]
            remaining = w.price - paid
        lines.append(WorkLine(
            work_id=w.work_id, title=w.title, state=state, price=w.price,
            paid=paid, paid_linked=applied[w.work_id], remaining=remaining,
        ))

    cancelled_amount = sum((w.price for w in works if states[w.work_id] is WorkState.CANCELLED), ZERO)
    payable = contract.amount - cancelled_amount
    queued_payments = [p for p in payments if is_queued(p, as_of)]

    # Инвариант, который держит отчёт честным: деньги не появляются и не пропадают.
    # sum(выплачено по работам) + нераспределённое == всего выплачено.
    assert sum((l.paid for l in lines), ZERO) + pool == paid_total

    return ContractReport(
        contract=contract,
        works=tuple(lines),
        cancelled_amount=cancelled_amount,
        payable=payable,
        paid=paid_total,
        remaining=max(payable - paid_total, ZERO),
        overpaid=max(paid_total - payable, ZERO),
        accepted_unpaid=sum((l.remaining for l in lines if l.state is WorkState.DONE), ZERO),
        unallocated=pool,
        queued=sum((p.amount for p in queued_payments), ZERO),
        queued_payment_ids=tuple(p.payment_id for p in queued_payments),
    )


# --------------------------------------------------------------------------- вход в расчёт


def calculate(dataset: Dataset, as_of: date, contract_id: Optional[str] = None) -> Report:
    """Строит отчёт на дату `as_of`. Если задан `contract_id` - только по этому договору."""
    warnings: list[ReportWarning] = []
    reports: list[ContractReport] = []
    skipped: list[str] = []

    for contract in dataset.contracts:
        if contract_id is not None and contract.contract_id != contract_id:
            continue
        if contract.signed_at > as_of:
            # На дату отчёта договора ещё не было; в отчёт он попасть не может.
            skipped.append(contract.contract_id)
            continue

        works = [w for w in dataset.works if w.contract_id == contract.contract_id]
        payments = [p for p in dataset.payments if p.contract_id == contract.contract_id]
        report = _calculate_contract(contract, works, payments, as_of, warnings)
        reports.append(report)
        _collect_warnings(contract, works, report, payments, as_of, warnings)

    return Report(as_of, tuple(reports), tuple(warnings), tuple(skipped))


def _collect_warnings(
    contract: Contract,
    works: list[Work],
    report: ContractReport,
    payments: list[Payment],
    as_of: date,
    warnings: list[ReportWarning],
) -> None:
    cid = contract.contract_id

    works_sum = sum((w.price for w in works), ZERO)
    if works_sum != contract.amount:
        warnings.append(ReportWarning(
            "works_sum_mismatch", cid,
            f"сумма работ ({format_money(works_sum)}) не равна сумме договора "
            f"({format_money(contract.amount)}); итоги считаются от суммы договора",
        ))

    for w in works:
        if w.status is WorkStatus.DONE and w.completed_at is None:
            warnings.append(ReportWarning("no_date", cid, f"работа {w.work_id} принята, но дата приёмки не указана: считаем принятой"))
        if w.status is WorkStatus.CANCELLED and w.cancelled_at is None:
            warnings.append(ReportWarning("no_date", cid, f"работа {w.work_id} отменена, но дата отмены не указана: считаем отменённой"))

    if report.overpaid:
        warnings.append(ReportWarning(
            "contract_overpaid", cid,
            f"выплачено больше, чем причитается по договору, на {format_money(report.overpaid)}",
        ))

    # "Заглядываем в будущее" данных: что будет, если учесть всё, что в выгрузке датировано
    # после даты отчёта. Так видно переплату, которая возникнет уже на следующий день
    # (платёж P-8 по D-2 от 12.11 при отчёте на 10.11), а не только ту, что есть сейчас.
    if as_of < date.max:
        later: list[ReportWarning] = []  # предупреждения "будущего расчёта" сами по себе не нужны
        future = _calculate_contract(contract, works, payments, date.max, later)
        if future.overpaid > report.overpaid:
            warnings.append(ReportWarning(
                "future_overpaid", cid,
                f"с учётом операций, датированных после {as_of:%d.%m.%Y}, по договору будет "
                f"переплата {format_money(future.overpaid)}",
            ))
