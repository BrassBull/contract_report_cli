"""Доменные модели: то, что лежит во входных CSV, без какой-либо логики расчёта."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional


class WorkStatus(str, enum.Enum):
    """Статус работы *на момент выгрузки* (не на дату отчёта)."""

    PLANNED = "planned"
    DONE = "done"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    """Статус платежа *на момент выгрузки* (не на дату отчёта)."""

    PAID = "paid"
    QUEUED = "queued"
    REFUNDED = "refunded"


class WorkState(str, enum.Enum):
    """Состояние работы *на дату отчёта*. Вычисляется, в данных не хранится."""

    PLANNED = "planned"
    DONE = "done"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: WorkStatus
    completed_at: Optional[date]
    cancelled_at: Optional[date]


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: Optional[str]  # None - платёж по договору без привязки к работе
    purpose: str
    amount: Decimal
    status: PaymentStatus
    created_at: date
    paid_at: Optional[date]
    refunded_at: Optional[date]


@dataclass(frozen=True)
class Dataset:
    contracts: tuple[Contract, ...]
    works: tuple[Work, ...]
    payments: tuple[Payment, ...]
