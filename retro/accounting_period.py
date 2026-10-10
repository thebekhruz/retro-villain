"""Граница рабочего учёта Retro; более ранние даты остаются архивом.

Дата относится к хозяйственному дню в Ташкенте, а не ко времени создания
записи: расход за 6 октября можно внести позднее. Архивные отчёты целиком
до границы сохраняют прежний расчёт; период, включающий рабочий учёт,
начинается не раньше ACCOUNTING_START.
"""
from datetime import date, timedelta

ACCOUNTING_START = date(2026, 10, 6)
# Первая выдача 6 октября относится к предыдущей смене.
SALARY_SHIFT_START = ACCOUNTING_START - timedelta(days=1)


def period_start(through: date) -> date:
    return ACCOUNTING_START if through >= ACCOUNTING_START else date.min


def salary_period_start(through: date) -> date:
    return SALARY_SHIFT_START if through >= ACCOUNTING_START else date.min


def accounting_range_start(first: date, last: date) -> date:
    return max(first, period_start(last))


def cash_opening_table(day: date) -> str:
    return ('accountant_working_cash_opening' if day >= ACCOUNTING_START
            else 'accountant_cash_opening')
