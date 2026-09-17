from datetime import date
from decimal import Decimal

import pytest

from retro.modules.accountant.ledger import FinanceStore, LedgerError


DAY = date(2026, 9, 15)
NEXT = date(2026, 9, 16)


def test_dividend_transfer_debits_daily_cash_once_and_payout_only_debits_reserve(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    assert store.reserves(DAY)['dividends']['balance'] is None
    store.reserve_entry(DAY, 'dividends', 'opening', '0', 'Начало учёта')
    store.reserve_entry(DAY, 'dividends', 'transfer', '300000', 'В сейф', cashier_amount=Decimal('1000000'))
    assert store.daily_summary(DAY, Decimal('1000000'))['cash_balance'] == Decimal('700000')
    store.reserve_entry(NEXT, 'dividends', 'withdrawal', '100000', 'Выдано собственнику')
    assert store.reserves(DAY)['dividends']['balance'] == '300000'
    assert store.reserves(NEXT)['dividends']['balance'] == '200000'
    assert store.daily_summary(NEXT, Decimal('1000000'))['cash_balance'] == Decimal('1700000')
    with pytest.raises(LedgerError):
        store.reserve_entry(NEXT, 'dividends', 'withdrawal', '200001', 'Лишняя выдача')
    with pytest.raises(LedgerError):
        store.add_expense(DAY, 'admin_other', 'Лишний расход', '700001', cashier_amount=Decimal('1000000'))


def test_usd_are_actual_currency_and_backdating_cannot_break_later_balance(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.reserve_entry(DAY, 'usd', 'opening', '100.50', 'Пересчитано в кассе')
    store.reserve_entry(NEXT, 'usd', 'withdrawal', '90', 'Выдано')
    with pytest.raises(LedgerError):
        store.reserve_entry(DAY, 'usd', 'withdrawal', '20', 'Задним числом')
    assert store.reserves(DAY)['usd']['balance'] == '100.50'
    assert store.reserves(NEXT)['usd']['balance'] == '10.50'
    assert store.daily_summary(NEXT, Decimal('500000'))['cash_balance'] == Decimal('500000')
    with pytest.raises(LedgerError):
        store.reserve_entry(NEXT, 'usd', 'opening', '1', 'Повторный остаток')


def test_reserves_require_initial_balance_do_not_guess_legacy_dividends(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.add_expense(DAY, 'distribution_dividends', 'в сейфе?', '100', cashier_amount=Decimal('1000'))
    assert store.reserves(DAY)['dividends']['balance'] is None
    with pytest.raises(LedgerError):
        store.reserve_entry(DAY, 'dividends', 'withdrawal', '50', 'Неизвестный остаток')
    with pytest.raises(LedgerError):
        store.reserve_entry(DAY, 'dividends', 'opening', '-1', 'Ошибочный остаток')


def test_monthly_plan_and_shoh_receipts_do_not_double_count_cash(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.set_monthly_plan(DAY, '1000000', 'План выплат из кассы на сентябрь')
    store.add_expense(DAY, 'salary_fazilova', 'Аванс', '200000', cashier_amount=Decimal('1000000'))
    store.add_expense(DAY, 'salary_staff', 'Смена', '100000', cashier_amount=Decimal('1000000'))
    store.add_expense(DAY, 'proc_shoh', 'Закуп', '300000', cashier_amount=Decimal('1000000'))
    store.reserve_entry(DAY, 'shoh', 'opening', '0', 'До начала учёта остаток ноль')
    store.reserve_entry(NEXT, 'shoh', 'withdrawal', '150000', 'Принята закупка по накладной')
    panels = store.reserves(NEXT)
    assert panels['monthly']['balance'] == '800000'
    assert panels['shoh']['balance'] == '150000'
    assert store.daily_summary(DAY, Decimal('1000000'))['cash_balance'] == Decimal('400000')
    assert store.reserves(date(2026, 10, 1))['monthly']['balance'] is None
