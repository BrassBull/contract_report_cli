"""Чтение и валидация входных CSV.

Принцип: структурные ошибки (битая сумма, неизвестный статус, ссылка на
несуществующий договор, платёж `paid` без даты оплаты) останавливают программу -
деньги нельзя считать по данным, которым нельзя верить. Все найденные ошибки
собираются и показываются разом, чтобы не чинить выгрузку по одной строке.

Подозрительные, но однозначно читаемые данные (например, сумма работ не равна
сумме договора) здесь не проверяются - это предупреждения расчёта.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, TypeVar

from .models import (
    Contract,
    Dataset,
    Payment,
    PaymentStatus,
    Work,
    WorkStatus,
)
from .money import parse_money

T = TypeVar("T")


class DataError(Exception):
    """Входные данные нельзя использовать. Сообщение - готовый текст для человека."""


def parse_date(raw: str) -> date:
    """Строго YYYY-MM-DD. date.fromisoformat в новых Python принимает и '20261110'."""
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"некорректная дата {raw!r} (ожидается ГГГГ-ММ-ДД)") from None


class _Row:
    """Обёртка над строкой CSV: достаёт поля и запоминает, где мы, для сообщений об ошибках."""

    def __init__(self, raw: dict[str, str], where: str) -> None:
        self._raw = {k: (v or "").strip() for k, v in raw.items() if k is not None}
        self.where = where

    def text(self, col: str) -> str:
        value = self._raw.get(col, "")
        if not value:
            raise ValueError(f"пустое обязательное поле {col!r}")
        return value

    def opt_text(self, col: str) -> Optional[str]:
        return self._raw.get(col) or None

    def get(self, col: str, parser: Callable[[str], T]) -> T:
        try:
            return parser(self.text(col))
        except ValueError as exc:
            raise ValueError(f"{col}: {exc}") from None

    def opt(self, col: str, parser: Callable[[str], T]) -> Optional[T]:
        value = self._raw.get(col)
        if not value:
            return None
        try:
            return parser(value)
        except ValueError as exc:
            raise ValueError(f"{col}: {exc}") from None


def _enum(cls: type) -> Callable[[str], object]:
    def parse(raw: str):
        try:
            return cls(raw)
        except ValueError:
            allowed = ", ".join(m.value for m in cls)  # type: ignore[attr-defined]
            raise ValueError(f"неизвестное значение {raw!r} (допустимо: {allowed})") from None

    return parse


def _read(path: Path, required: list[str], errors: list[str]) -> list[_Row]:
    if not path.is_file():
        raise DataError(f"не найден файл {path}")
    # utf-8-sig: Excel любит добавлять BOM, и первая колонка тогда называлась бы '﻿contract_id'.
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise DataError(f"{path.name}: нет обязательных колонок: {', '.join(missing)}")
        rows = []
        for raw in reader:
            # line_num у DictReader - номер строки в файле с учётом заголовка и многострочных полей.
            rows.append(_Row(raw, f"{path.name}:{reader.line_num}"))
        return rows


def _collect(
    rows: list[_Row],
    build: Callable[[_Row], T],
    id_of: Callable[[T], str],
    id_name: str,
    errors: list[str],
) -> list[T]:
    result: list[T] = []
    seen: dict[str, str] = {}
    for row in rows:
        try:
            item = build(row)
        except ValueError as exc:
            errors.append(f"{row.where}: {exc}")
            continue
        key = id_of(item)
        if key in seen:
            errors.append(f"{row.where}: повторяется {id_name} {key!r} (первый раз - {seen[key]})")
            continue
        seen[key] = row.where
        result.append(item)
    return result


def _build_contract(r: _Row) -> Contract:
    return Contract(
        contract_id=r.text("contract_id"),
        contractor=r.text("contractor"),
        amount=r.get("amount", parse_money),
        signed_at=r.get("signed_at", parse_date),
    )


def _build_work(r: _Row) -> Work:
    return Work(
        work_id=r.text("work_id"),
        contract_id=r.text("contract_id"),
        title=r.text("title"),
        price=r.get("price", parse_money),
        status=r.get("status", _enum(WorkStatus)),
        completed_at=r.opt("completed_at", parse_date),
        cancelled_at=r.opt("cancelled_at", parse_date),
    )


def _build_payment(r: _Row) -> Payment:
    p = Payment(
        payment_id=r.text("payment_id"),
        contract_id=r.text("contract_id"),
        work_id=r.opt_text("work_id"),
        purpose=r.opt_text("purpose") or "",
        amount=r.get("amount", parse_money),
        status=r.get("status", _enum(PaymentStatus)),
        created_at=r.get("created_at", parse_date),
        paid_at=r.opt("paid_at", parse_date),
        refunded_at=r.opt("refunded_at", parse_date),
    )
    # Согласованность статуса и дат. Расчёт на дату целиком держится на этих полях,
    # поэтому "paid без paid_at" - не мелочь, а невозможность понять, ушли ли деньги к дате отчёта.
    if p.status is PaymentStatus.QUEUED and p.paid_at:
        raise ValueError("платёж queued, но указана дата оплаты paid_at")
    if p.status in (PaymentStatus.PAID, PaymentStatus.REFUNDED) and not p.paid_at:
        raise ValueError(f"платёж {p.status.value} без даты оплаты paid_at")
    if p.status is PaymentStatus.REFUNDED:
        if not p.refunded_at:
            raise ValueError("платёж refunded без даты возврата refunded_at")
        if p.refunded_at < p.paid_at:  # type: ignore[operator]
            raise ValueError("дата возврата раньше даты оплаты")
    elif p.refunded_at:
        raise ValueError(f"указана дата возврата, но статус {p.status.value}")
    return p


def load_dataset(data_dir: Path) -> Dataset:
    """Читает contracts.csv, works.csv, payments.csv из каталога и проверяет ссылки между ними."""
    errors: list[str] = []

    contracts = _collect(
        _read(data_dir / "contracts.csv", ["contract_id", "contractor", "amount", "signed_at"], errors),
        _build_contract, lambda c: c.contract_id, "contract_id", errors,
    )
    works = _collect(
        _read(
            data_dir / "works.csv",
            ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"],
            errors,
        ),
        _build_work, lambda w: w.work_id, "work_id", errors,
    )
    payments = _collect(
        _read(
            data_dir / "payments.csv",
            ["payment_id", "contract_id", "work_id", "purpose", "amount", "status",
             "created_at", "paid_at", "refunded_at"],
            errors,
        ),
        _build_payment, lambda p: p.payment_id, "payment_id", errors,
    )

    contract_ids = {c.contract_id for c in contracts}
    work_contract = {w.work_id: w.contract_id for w in works}
    for w in works:
        if w.contract_id not in contract_ids:
            errors.append(f"работа {w.work_id}: договор {w.contract_id!r} не найден в contracts.csv")
    for p in payments:
        if p.contract_id not in contract_ids:
            errors.append(f"платёж {p.payment_id}: договор {p.contract_id!r} не найден в contracts.csv")
        if p.work_id is not None:
            if p.work_id not in work_contract:
                errors.append(f"платёж {p.payment_id}: работа {p.work_id!r} не найдена в works.csv")
            elif work_contract[p.work_id] != p.contract_id:
                # Платёж по договору А, привязанный к работе договора Б, - почти наверняка опечатка,
                # и молча выбрать "правильный" договор мы не можем.
                errors.append(
                    f"платёж {p.payment_id}: работа {p.work_id} относится к договору "
                    f"{work_contract[p.work_id]}, а платёж - к {p.contract_id}"
                )

    if errors:
        raise DataError("Ошибки во входных данных:\n  " + "\n  ".join(errors))
    return Dataset(tuple(contracts), tuple(works), tuple(payments))
