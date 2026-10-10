"""Граница рабочего учёта Retro; более ранние даты остаются архивом.

Дата относится к хозяйственному дню в Ташкенте, а не ко времени создания
записи: расход за 5 октября можно внести позднее. Архивные отчёты целиком
до границы сохраняют прежний расчёт; период, включающий рабочий учёт,
начинается не раньше ACCOUNTING_START.
"""
from datetime import date

ACCOUNTING_START = date(2026, 10, 5)


def period_start(through: date) -> date:
    return ACCOUNTING_START if through >= ACCOUNTING_START else date.min


def accounting_range_start(first: date, last: date) -> date:
    return max(first, period_start(last))


def cash_opening_table(day: date) -> str:
    return ('accountant_working_cash_opening' if day >= ACCOUNTING_START
            else 'accountant_cash_opening')
