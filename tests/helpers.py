"""Фабрики тестовых данных: чтобы в тестах было видно только то, что важно для случая."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from contract_report.models import (
    Contract, Dataset, Payment, PaymentStatus, Work, WorkStatus,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DATA = REPO_ROOT / "data"


def d(text: str) -> date:
    return date.fromisoformat(text)


def money(text: str) -> Decimal:
    return Decimal(text)


def contract(amount="1000.00", cid="C", signed="2026-01-01") -> Contract:
    return Contract(cid, "Подрядчик", money(amount), d(signed))


def work(wid, price, status="planned", done=None, cancelled=None, cid="C") -> Work:
    return Work(wid, cid, f"Работа {wid}", money(price), WorkStatus(status),
                d(done) if done else None, d(cancelled) if cancelled else None)


def payment(pid, amount, status="paid", work_id=None, created="2026-01-02",
            paid="2026-01-02", refunded=None, cid="C") -> Payment:
    st = PaymentStatus(status)
    if st is PaymentStatus.QUEUED:
        paid = None
    return Payment(pid, cid, work_id, "", money(amount), st, d(created),
                   d(paid) if paid else None, d(refunded) if refunded else None)


def dataset(contracts, works, payments) -> Dataset:
    return Dataset(tuple(contracts), tuple(works), tuple(payments))
