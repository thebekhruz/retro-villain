from datetime import date
from decimal import Decimal
import sqlite3

import pytest

from retro.modules.accountant.ledger import FinanceStore, LedgerError
from retro.modules.accountant.payroll import PayrollRow


WORKDAY = date(2026, 9, 15)
NEXT_DAY = date(2026, 9, 16)


def payroll_row(employee_id=1, amount='270000'):
    return PayrollRow(employee_id, 'Тест Сотрудник', 'официант', 'Обслуживание зала',
                      'late', None, Decimal(amount), Decimal(amount), False)


def test_confirmation_creates_debt_but_no_cash_expense_and_is_idempotent(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    assert store.confirm_payroll(WORKDAY, [payroll_row()], 'Финансы') is True
    assert store.confirm_payroll(WORKDAY, [payroll_row()], 'Финансы') is False

    summary = FinanceStore(tmp_path / 'finance.sqlite3').summary(WORKDAY)
    assert summary['accrued_on_day'] == Decimal('270000')
    assert summary['salary_debt'] == Decimal('270000')
    assert summary['cash_balance'] == Decimal(0)
    assert summary['salary_paid_on_day'] == Decimal(0)


def test_partial_payment_next_day_keeps_debt_and_requires_cash(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.confirm_payroll(WORKDAY, [payroll_row()], 'Финансы')
    accrual_id = store.accruals(WORKDAY)[0]['id']
    with pytest.raises(LedgerError, match='недостаточно'):
        store.pay_salary(accrual_id, NEXT_DAY, '100000')

    store.confirm_transfer(WORKDAY, NEXT_DAY, '200000')
    store.pay_salary(accrual_id, NEXT_DAY, '100000')
    summary = store.summary(NEXT_DAY)
    assert summary['salary_debt'] == Decimal('170000')
    assert summary['cash_balance'] == Decimal('100000')
    assert summary['salary_paid_on_day'] == Decimal('100000')
    assert summary['movements'][1]['type'] == 'salary_payment'
    assert 'Тест Сотрудник' in summary['movements'][1]['description']
    assert summary['movements'][1]['item_code'] == 'salary_staff'
    assert summary['salary_categories'] == {'ЗП персонал': '100000'}
    with pytest.raises(LedgerError, match='недостаточно'):
        store.pay_salary(accrual_id, NEXT_DAY, '170000')


def test_other_expense_and_shoh_procurement_are_separate_cash_outflows(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.add_opening(WORKDAY, '500000', 'Начальный остаток')
    store.add_expense(WORKDAY, 'ops_rent', 'Аренда', '100000')
    store.give_procurement(WORKDAY, 'Шох', 'Закуп продуктов', '150000')
    result = store.summary(WORKDAY)
    assert result['cash_balance'] == Decimal('250000')
    assert [x['type'] for x in result['movements']] == [
        'opening', 'other_expense', 'procurement_advance']


def test_daily_summary_shows_carried_balance_as_opening_income(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.daily_summary(WORKDAY, Decimal('500000'))
    store.daily_summary(NEXT_DAY, Decimal('200000'))
    result = store.daily_summary(NEXT_DAY, None)
    assert result['cash_flow']['opening_balance'] == '500000'
    assert result['movements'][0]['type'] == 'opening'
    assert result['movements'][0]['amount'] == '500000'


def test_other_receipt_increases_current_and_next_day_cash(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.daily_summary(WORKDAY, Decimal('100000'))
    store.add_income(WORKDAY, 'income_other', 'Возврат долга', '25000')
    store.daily_summary(NEXT_DAY, Decimal('50000'))
    today = store.daily_summary(WORKDAY, Decimal('100000'))
    tomorrow = store.daily_summary(NEXT_DAY, Decimal('50000'))
    assert today['cash_balance'] == Decimal('125000')
    assert today['cash_flow']['other_receipts'] == '25000'
    assert store.daily_summary(WORKDAY, Decimal('100000'), carry_history=False)['cash_balance'] == Decimal('125000')
    assert tomorrow['cash_flow']['opening_balance'] == '125000'
    assert tomorrow['cash_balance'] == Decimal('175000')


def test_transfer_and_unlinked_day_exception_cannot_be_repeated(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.confirm_transfer(WORKDAY, NEXT_DAY, '200000')
    with pytest.raises(LedgerError, match='подтверждена'):
        store.confirm_transfer(WORKDAY, NEXT_DAY, '200000')
    store.grant_exception(19, WORKDAY, 'Новый сотрудник', 'Руководитель финансов')
    assert store.exceptions_for_day(WORKDAY) == {19}
    with pytest.raises(LedgerError, match='уже использовано'):
        store.grant_exception(19, NEXT_DAY, 'Повторно', 'Руководитель финансов')


def test_payment_cannot_exceed_employee_debt_even_if_cash_is_available(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.confirm_payroll(WORKDAY, [payroll_row()], 'Финансы')
    store.confirm_transfer(WORKDAY, NEXT_DAY, '1000000')
    with pytest.raises(LedgerError, match='долг'):
        store.pay_salary(store.accruals(WORKDAY)[0]['id'], NEXT_DAY, '270001')


def test_backdated_expense_cannot_make_a_later_balance_negative(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.add_opening(WORKDAY, '200000', 'Остаток')
    store.add_expense(NEXT_DAY, 'admin_other', 'Выплата', '150000')
    with pytest.raises(LedgerError, match='последующ'):
        store.add_expense(WORKDAY, 'admin_other', 'Задним числом', '100000')
    assert store.summary(NEXT_DAY)['cash_balance'] == Decimal('50000')


def test_existing_finance_database_keeps_legacy_expenses_when_catalog_is_added(tmp_path):
    path = tmp_path / 'finance.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE accountant_movements ('
                           'id INTEGER PRIMARY KEY, day TEXT, kind TEXT, description TEXT, '
                           'amount TEXT, reference TEXT, created_at TEXT, UNIQUE(kind, reference))')
        connection.execute('INSERT INTO accountant_movements '
                           '(day, kind, description, amount, created_at) '
                           'VALUES (?, ?, ?, ?, ?)',
                           (WORKDAY.isoformat(), 'opening', 'Старый остаток', '500000',
                            '2026-09-15T10:00:00'))
    store = FinanceStore(path)
    store.add_expense(WORKDAY, 'admin_other', 'Новая запись', '100000')
    summary = store.summary(WORKDAY)
    assert summary['cash_balance'] == Decimal('400000')
    assert [item['item_code'] for item in summary['movements']] == [None, 'admin_other']
