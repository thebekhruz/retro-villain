from datetime import date, timedelta
from decimal import Decimal

import pytest

from retro.modules.accountant.ledger import FinanceStore, LedgerError


DAY = date(2026, 9, 15)
NEXT = date(2026, 9, 16)


def test_dividend_transfer_debits_daily_cash_once_and_payout_only_debits_reserve(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    assert store.reserves(DAY)['dividends']['balance'] is None
    # Приход дня бухгалтер получил и записал сам: с ТЗ 02.10 в остаток входит
    # только полученное, а не расчёт.
    store.record_handover(DAY, Decimal('1000000'))
    store.record_handover(NEXT, Decimal('1000000'))
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
    store.record_handover(NEXT, Decimal('500000'))
    assert store.daily_summary(NEXT, Decimal('500000'))['cash_balance'] == Decimal('500000')
    with pytest.raises(LedgerError):
        store.reserve_entry(NEXT, 'usd', 'opening', '1', 'Повторный остаток')


def test_deleting_transfer_cannot_make_later_reserve_negative(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.record_handover(DAY, Decimal('1000'))
    store.reserve_entry(DAY, 'dividends', 'opening', '0', 'Начало')
    transfer = store.reserve_entry(
        DAY, 'dividends', 'transfer', '300', 'Сейф', cashier_amount=Decimal('1000'))
    store.reserve_entry(NEXT, 'dividends', 'withdrawal', '200', 'Выдача')

    with pytest.raises(LedgerError, match='последующ'):
        store.delete_operation('reserve_transfer', transfer, DAY)

    assert store.reserves(NEXT)['dividends']['balance'] == '100'


def test_reserves_require_initial_balance_do_not_guess_legacy_dividends(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.record_handover(DAY, Decimal('1000'))
    store.add_expense(DAY, 'distribution_dividends', 'в сейфе?', '100', cashier_amount=Decimal('1000'))
    assert store.reserves(DAY)['dividends']['balance'] is None
    with pytest.raises(LedgerError):
        store.reserve_entry(DAY, 'dividends', 'withdrawal', '50', 'Неизвестный остаток')
    with pytest.raises(LedgerError):
        store.reserve_entry(DAY, 'dividends', 'opening', '-1', 'Ошибочный остаток')


def test_monthly_plan_and_shoh_receipts_do_not_double_count_cash(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.record_handover(DAY, Decimal('1000000'))
    store.set_monthly_plan(DAY, '1000000', 'План выплат из кассы на сентябрь')
    store.add_expense(DAY, 'salary_monthly', 'Аванс', '200000', cashier_amount=Decimal('1000000'))
    store.add_expense(DAY, 'salary_staff', 'Смена', '100000', cashier_amount=Decimal('1000000'))
    store.add_expense(DAY, 'proc_shoh', 'Закуп', '300000', cashier_amount=Decimal('1000000'))
    store.reserve_entry(DAY, 'shoh', 'opening', '0', 'До начала учёта остаток ноль')
    store.reserve_entry(NEXT, 'shoh', 'withdrawal', '150000', 'Принята закупка по накладной')
    panels = store.reserves(NEXT)
    assert panels['monthly']['balance'] == '800000'
    assert panels['shoh']['balance'] == '150000'
    assert store.daily_summary(DAY, Decimal('1000000'))['cash_balance'] == Decimal('400000')
    assert store.reserves(date(2026, 10, 1))['monthly']['balance'] is None


def test_single_pass_negative_check_answers_exactly_as_the_per_day_recount():
    """Проверка остатка резерва за один проход вместо пересчёта на каждый день.

    Старая проверка перебирала все дни и для каждого складывала заново все
    строки не позже него — квадрат от числа дней, и так на каждой записи в
    резерв. Эталон здесь — та самая старая формулировка; ответ обязан
    совпадать на всех наборах, включая переход через начало учёта."""
    import random
    from retro.accounting_period import ACCOUNTING_START
    from retro.modules.accountant.reserves import _balance, first_negative_day

    def recount(rows):
        """Прежняя формулировка: минус хотя бы в один записанный день."""
        for cutoff in {row['day'] for row in rows}:
            balance = _balance([row for row in rows if row['day'] <= cutoff])
            if balance is not None and balance < 0:
                return True
        return False

    def entry(day, kind, amount):
        return dict(day=day.isoformat(), kind=kind, amount=str(amount))

    # Архив без начального остатка — остаток неизвестен, минуса в нём нет.
    archive = [entry(ACCOUNTING_START - timedelta(days=3), 'withdrawal', 500)]
    assert first_negative_day(archive) is None and not recount(archive)

    # Начало учёта обнуляет счёт: архивный плюс не покрывает рабочий минус.
    crossing = [entry(ACCOUNTING_START - timedelta(days=1), 'opening', 1000),
                entry(ACCOUNTING_START, 'opening', 0),
                entry(ACCOUNTING_START + timedelta(days=1), 'withdrawal', 10)]
    assert first_negative_day(crossing) == (ACCOUNTING_START + timedelta(days=1)).isoformat()
    assert recount(crossing)

    kinds = ('opening', 'transfer', 'deposit', 'withdrawal')
    random.seed(20261009)
    for _ in range(300):
        rows = []
        for _ in range(random.randint(0, 9)):
            day = ACCOUNTING_START + timedelta(days=random.randint(-4, 4))
            rows.append(entry(day, random.choice(kinds), random.randint(0, 300)))
        assert (first_negative_day(rows) is not None) == recount(rows), rows
