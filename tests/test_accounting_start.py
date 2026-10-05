"""Реальный переход 01 → 02 → 03 октября, включая старую заполненную БД."""
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from retro.accounting_period import ACCOUNTING_START as START
from retro.app import create_app
from retro.config import Settings
from retro.db import Database
from retro.modules.accountant.ledger import CashBook, FinanceStore, LedgerError
from retro.modules.accountant.payroll import PayrollRow
from retro.modules.accountant import closing as month_closing, shoh_balance
from retro.modules.cashier.archive import CashierArchive
from retro.modules.cashier.expenses import ExpenseStore
from retro.modules.cashier.service import TZ
from retro.modules.cashier.till import give_shokh
from retro.modules.founder.export import month_workbook
from retro.modules.founder import overview
from retro.modules.shokh.store import ShokhStore, ShokhError, pocket_position

OLD = date(2026, 10, 1)
NEXT = date(2026, 10, 3)


def initialized(tmp_path, *, check_mode=False, database=None):
    store = FinanceStore(database or tmp_path / 'finance.sqlite3', allow_negative_cash=check_mode)
    store.record_handover(OLD, Decimal('9000'))
    store.set_cash_opening(OLD, '8000', 'Архивный остаток')
    store.add_expense(OLD, 'admin_it', 'Архивный расход', '1000')
    store.record_handover(START, Decimal('1000'))
    store.set_cash_opening(START, '200', 'Пересчёт на 2 октября')
    return store


def test_cash_starts_on_october_2_and_backdated_expense_updates_later_days(tmp_path):
    store = initialized(tmp_path)
    store.record_handover(NEXT, Decimal('500'))
    store.add_expense(START, 'admin_it', 'Внесено позднее за 2 октября', '100')
    first = store.daily_summary(START, Decimal('1000'))
    following = store.daily_summary(NEXT, Decimal('500'))
    assert first['cash_flow']['opening_balance'] == '200'
    assert first['cash_balance'] == Decimal('1100')
    assert following['cash_flow']['opening_balance'] == '1100'
    assert following['cash_balance'] == Decimal('1600')
    assert following['cash_flow']['first_day'] == '2026-10-02'
    assert store.daily_summary(OLD, Decimal('9000'))['cash_balance'] == Decimal('16000')
    assert store.cash_opening(OLD)['amount'] == '8000'
    assert FinanceStore(store.path).cash_opening()['amount'] == '200'
    with pytest.raises(LedgerError, match='уже'):
        store.set_cash_opening(START, '200', 'Повтор')
    with pytest.raises(LedgerError, match='02.10.2026'):
        store.set_cash_opening(NEXT, '200', 'Неверная дата')


@pytest.mark.parametrize('check_mode', [False, True])
def test_unconfirmed_opening_and_missing_days_are_not_silently_zero(tmp_path, check_mode):
    store = FinanceStore(tmp_path / 'finance.sqlite3', allow_negative_cash=check_mode)
    store.record_handover(OLD, Decimal('9000'))
    store.set_cash_opening(OLD, '8000', 'Архив')
    store.record_handover(START, Decimal('1000'))
    assert store.daily_summary(START, Decimal('1000'))['cash_balance'] is None
    with pytest.raises(LedgerError, match='начальный остаток'):
        store.add_expense(START, 'admin_it', 'Расход', '1')
    store.set_cash_opening(START, '0', 'Подтверждённый нулевой остаток')
    later = NEXT + timedelta(days=1)
    store.record_handover(later, Decimal('500'))
    summary = store.daily_summary(later, Decimal('500'))
    assert summary['cash_balance'] is None
    assert summary['cash_flow']['missing_day'] == NEXT.isoformat()
    with pytest.raises(LedgerError):
        store.add_expense(later, 'admin_it', 'Расход с пропуском', '1')


def test_existing_october_2_opening_is_adopted_without_changing_old_table(tmp_path):
    path = tmp_path / 'finance.sqlite3'
    store = FinanceStore(path)
    with closing(store._open()) as c, c:
        c.execute('INSERT INTO accountant_cash_opening VALUES (1,?,?,?,?)',
                  (START.isoformat(), '123.45', 'Уже подтверждено', '2026-10-02T10:00:00'))
    store = FinanceStore(path)
    assert store.cash_opening()['amount'] == '123.45'
    with closing(store._open()) as c:
        assert c.execute('SELECT amount FROM accountant_cash_opening').fetchone() == ('123.45',)


def test_period_totals_and_monthly_payroll_exclude_october_1(tmp_path):
    store = initialized(tmp_path)
    store.add_expense(START, 'admin_it', 'Новый расход', '100')
    store.add_supplier_transfer(OLD, 'Поставщик', 'Товар', 'RETRO', '700')
    store.add_supplier_transfer(START, 'Поставщик', 'Товар', 'RETRO', '50')
    assert store.expense_totals_between(OLD, NEXT)['total'] == Decimal('150')
    assert {r['day'] for r in store.cash_flows_between(OLD, NEXT)} == {START.isoformat()}
    assert len(store.supplier_transfers(OLD, NEXT)) == 1
    expenses = ExpenseStore(tmp_path / 'cashier.sqlite3')
    expenses.add(OLD, 'Архив', '900')
    expenses.add(START, 'Расход', '10')
    assert expenses.total_between(OLD, NEXT) == Decimal('10')
    assert expenses.total_between(OLD, OLD) == Decimal('900')
    row = PayrollRow(1, 'Сотрудник', 'Официант', 'Зал', 'on_time', None,
                     Decimal('100'), Decimal('100'), False)
    store.confirm_payroll(OLD, [row], 'Бухгалтер')
    old_id = store.accruals(OLD)[0]['id']
    store.confirm_payroll(START, [row], 'Бухгалтер')
    assert [r['work_day'] for r in store.accruals(NEXT)] == [START.isoformat()]
    cells = store.payroll_month(OLD, NEXT)['shift'][0]['cells']
    assert list(cells) == [START.isoformat()]
    with pytest.raises(LedgerError, match='архив'):
        store.pay_salary(old_id, NEXT, '100')


def test_reserves_debts_and_shokh_do_not_carry_archival_money(tmp_path):
    store = initialized(tmp_path)
    old_debt = store.record_debt(OLD, 'admin_it', 'Старый долг', '500', '0')
    store.record_debt(START, 'admin_it', 'Новый долг', '50', '0')
    assert store.debt_summary(NEXT)['manual_debt_total'] == '50'
    with pytest.raises(LedgerError, match='архив'):
        store.pay_debt(old_debt, NEXT, '1')
    store.reserve_entry(OLD, 'shoh', 'opening', '9000', 'Архив')
    give_shokh(store, OLD, '1000')
    assert store.reserves(START)['shoh']['balance'] == '0'
    store.reserve_entry(START, 'shoh', 'opening', '200', 'Пересчёт')
    give_shokh(store, START, '100')
    shokh = ShokhStore(store.path)
    def buy(day, price):
        return shokh.add_purchase(day, datetime.combine(day, datetime.min.time(), TZ),
                                  point='RETRO', item='Товар', unit='кг', quantity='1', price=price)
    old = buy(OLD, '1000')
    buy(START, '50')
    # В действующей панели расходы Шоха вносит бухгалтер; старые записи
    # с телефона не должны списывать деньги второй раз.
    shoh_balance.add_expense(store, START, shoh_balance.bazaars(store)[0], '50')
    assert Decimal(pocket_position(shokh, store, NEXT)['pocket']) == Decimal('250')
    month = shoh_balance.shoh_view(store, NEXT, history=True)
    assert all(row['day'] >= START.isoformat() for row in month['history'])
    assert month['month_spent'] == '50'
    with pytest.raises(ShokhError, match='архив'):
        shokh.accept_with_finance(old['id'], START, datetime(2026, 10, 2, 12, tzinfo=TZ), store)
    assert shokh.purchase(old['id'])['accepted_at'] is None
    assert store.reserves(OLD)['shoh']['balance'] == '10000'


def test_api_allows_expenses_for_october_2_after_an_old_opening(tmp_path, monkeypatch):
    monkeypatch.setattr('retro.modules.accountant.routes.today_tashkent', lambda: date(2026, 10, 5))
    settings = Settings(manual_handover_only=True, data_dir=tmp_path)
    app = create_app(settings, expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'finance.sqlite3')
    finance = app.state.accountant_finance
    finance.record_handover(OLD, Decimal('9000'))
    finance.set_cash_opening(OLD, '8000', 'Архив')
    finance.record_handover(START, Decimal('1000'))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        assert client.post('/api/accountant/cash-opening', json={
            'date': START.isoformat(), 'amount': '200', 'note': 'Пересчёт'}).status_code == 201
        response = client.post('/api/accountant/expenses', json={
            'date': START.isoformat(), 'item_code': 'admin_it', 'note': 'Расход за 2 октября',
            'amount': '100'})
        assert response.status_code == 201, response.text
        ledger = client.get('/api/accountant/day', params={'date': START.isoformat()}).json()['ledger']
        assert ledger['accounting_start'] == START.isoformat()
        assert ledger['archived'] is False
        assert ledger['cash_balance'] == '1100'
        month = client.get('/api/accountant/payroll/month', params={'month': '2026-10'})
        assert month.status_code == 200, month.text
        assert month.json()['first'] == START.isoformat()


def test_export_starts_on_october_2(tmp_path):
    finance = initialized(tmp_path)
    state = SimpleNamespace(accountant_finance=finance, settings=Settings(manual_handover_only=True),
                            shokh=ShokhStore(finance.path))
    book = load_workbook(BytesIO(month_workbook(state, OLD, START, None, 'offline')))
    assert book['По дням']['A5'].value.date() == START
    assert book['По дням']['L5'].value == 1200


@pytest.mark.parametrize('installed', [date(2026, 9, 20), date(2026, 11, 5)])
def test_archive_backfills_from_launch_even_when_installed_later(tmp_path, installed):
    archive = CashierArchive(tmp_path / 'cashier.sqlite3', Settings())
    list(archive.due_days(datetime.combine(installed, datetime.min.time(), TZ)))
    now = datetime(2026, 11, 5, 8, tzinfo=TZ)
    due = list(archive.due_days(now))
    assert min(due) == START
    assert max(due) == now.date() - timedelta(days=1)
    assert set(due) == {START + timedelta(days=n) for n in range((now.date() - START).days)}


def test_cash_book_can_read_both_periods_in_one_pass(tmp_path):
    store = initialized(tmp_path)
    with closing(store._open()) as c:
        book = CashBook(c, START)
        assert book.position(OLD)[1] == Decimal('16000')
        assert book.position(START)[1] == Decimal('1200')


def test_founder_month_fact_and_first_week_plan_start_on_october_2():
    history = {day.isoformat(): {'retro': {'revenue': Decimal(amount)}}
               for day, amount in ((OLD, '9999'), (START, '100'), (NEXT, '200'))}
    forecast = {weekday: {'revenue': '0'} for weekday in range(7)}
    assert overview.month_forecast(history, forecast, NEXT)['fact'] == '300.00'
    plan = overview.dividend_week(START, Decimal('300'), {})
    assert plan['start'] == START.isoformat()
    assert len(plan['days']) == 3
    assert plan['pace'] == '100.00' and plan['due'] == '0.00'
    assert plan['days_left'] == 3


def test_archival_month_closure_cannot_become_the_october_opening(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    september = date(2026, 9, 30)
    store.record_handover(september, Decimal('9000'))
    store.set_cash_opening(september, '5000', 'Архив')
    month_closing.close_month(store, '2026-09', 'Бухгалтер', OLD)
    store.record_handover(OLD, Decimal('100'))
    store.record_debt(OLD, 'admin_it', 'Архивный долг', '999', '0')
    store.record_handover(START, Decimal('1000'))
    unknown = store.daily_summary(START, None)
    assert unknown['cash_balance'] is None
    assert unknown['opening_breakdown']['previous'] is None
    store.set_cash_opening(START, '200', 'Новый период')
    for n in range(1, 30):
        store.record_handover(START + timedelta(days=n), Decimal('0'))
    preview = month_closing.month_preview(store, '2026-10', date(2026, 11, 1))
    assert preview['first'] == START.isoformat()
    assert preview['opening'] == '200'
    assert preview['closing_balance'] == '1200'
    assert preview['debts']['expenses'] == '0'
    assert preview['includes_earlier'] is False
    assert len(preview['days']) == 30
    month_closing.close_month(store, '2026-10', 'Бухгалтер', date(2026, 11, 1))
    store.record_handover(date(2026, 11, 1), Decimal('50'))
    november = store.daily_summary(date(2026, 11, 1), None)
    assert november['cash_flow']['opening_balance'] == '1200'
    assert november['cash_balance'] == Decimal('1250')


def test_postgres_migration_and_working_opening_are_idempotent(tmp_path):
    url = os.getenv('RETRO_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
    import psycopg
    from psycopg import sql
    schema = 'accounting_start_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query), options='-csearch_path=' + schema)
            database = Database(urlunsplit(parts._replace(query=urlencode(query))))
            store = initialized(tmp_path, database=database)
            store.add_expense(START, 'admin_it', 'Расход', '100')
            reopened = FinanceStore(database)
            assert reopened.daily_summary(START, Decimal('1000'))['cash_balance'] == Decimal('1100')
            assert reopened.cash_opening(OLD)['amount'] == '8000'
            with pytest.raises(LedgerError, match='уже'):
                reopened.set_cash_opening(START, '999', 'Повтор')
            assert reopened.cash_opening()['amount'] == '200'
            # Путь обновления уже заполненной прежней версии с якорем на 2 октября.
            with closing(database.connect()) as c, c:
                c.execute('DELETE FROM accountant_working_cash_opening')
                c.execute('UPDATE accountant_cash_opening SET day=?, amount=? WHERE id=1',
                          (START.isoformat(), '123.45'))
            assert FinanceStore(database).cash_opening()['amount'] == '123.45'
            assert FinanceStore(database).cash_opening()['amount'] == '123.45'
        finally:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
