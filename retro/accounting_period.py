"""Граница рабочего учёта Retro; более ранние даты остаются архивом.

Дата относится к хозяйственному дню в Ташкенте, а не ко времени создания
записи: расход за 13 октября можно внести позднее. Архивные отчёты целиком
до границы сохраняют прежний расчёт; период, включающий рабочий учёт,
начинается не раньше ACCOUNTING_START.
"""
from datetime import date, timedelta

ACCOUNTING_START = date(2026, 10, 13)
# Зарплатная ведомость по людям — со смены 14 октября (первая выдача 15-го).
# Смены 12–13 октября оплачены общими строками «ЗП персонал» 13 и 14 октября.
SALARY_SHIFT_START = date(2026, 10, 14)
SALARY_ENTRY_START = SALARY_SHIFT_START + timedelta(days=1)


def period_start(through: date) -> date:
    return ACCOUNTING_START if through >= ACCOUNTING_START else date.min


def salary_period_start(through: date) -> date:
    return SALARY_SHIFT_START if through >= ACCOUNTING_START else date.min


def accounting_range_start(first: date, last: date) -> date:
    return max(first, period_start(last))


def salary_entry_range_start(first: date, last: date) -> date:
    return max(first, SALARY_ENTRY_START) if last >= ACCOUNTING_START else first


def cash_opening_table(day: date) -> str:
    return ('accountant_working_cash_opening' if day >= ACCOUNTING_START
            else 'accountant_cash_opening')
