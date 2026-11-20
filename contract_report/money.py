"""Деньги: строгий разбор и форматирование.

Все суммы в проекте - Decimal с двумя знаками. float не используется нигде:
ни при чтении, ни при расчёте, ни при выводе (JSON тоже отдаёт суммы строками).
"""
from __future__ import annotations

import re
from decimal import Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

# Намеренно строже, чем Decimal(): тот принимает "1e3", "NaN", "Infinity", "+5".
# Больше двух знаков после точки тоже отвергаем, а не округляем молча:
# "100.005" в выгрузке - это ошибка данных, а не повод угадывать округление.
_MONEY_RE = re.compile(r"\d+(\.\d{1,2})?")


def parse_money(raw: str) -> Decimal:
    """Разбирает положительную сумму вида '300000.00' или '15000'."""
    text = raw.strip()
    if not _MONEY_RE.fullmatch(text):
        raise ValueError(
            f"некорректная сумма {raw!r} (ожидается число вида 1234.56, "
            "не больше двух знаков после точки)"
        )
    value = Decimal(text).quantize(CENT)
    if value <= 0:
        raise ValueError(f"сумма должна быть больше нуля, получено {raw!r}")
    return value


def money_str(value: Decimal) -> str:
    """Машинный формат: '300000.00' (для JSON и CSV)."""
    return f"{value.quantize(CENT):.2f}"


def format_money(value: Decimal) -> str:
    """Читаемый формат для людей: '300 000,00'."""
    text = f"{value.quantize(CENT):,.2f}"
    # Сначала запятые-разделители тысяч -> пробел, потом точка -> запятая.
    # Порядок важен: обратный даст путаницу.
    return text.replace(",", " ").replace(".", ",")
