"""Public boundary and real confirmed opening, with no legacy fixture overrides."""
from contextlib import closing
from datetime import date
from decimal import Decimal
import os
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from uuid import uuid4

from retro.db import Database

import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.modules.accountant.ledger import FinanceStore
from retro.modules.accountant.opening_migration import apply_october_opening
from retro.modules.accountant.shoh_balance import shoh_view
from retro.modules.shokh.store import pocket_position

START = date(2026, 10, 6)


@pytest.fixture(params=['sqlite', 'postgres'])
def database(request, tmp_path):
    if request.param == 'sqlite':
        yield tmp_path / 'finance.sqlite3'
        return
    url = os.getenv('RETRO_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
    import psycopg
    from psycopg import sql
    schema = 'october_cutover_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query), options='-csearch_path=' + schema)
            yield Database(urlunsplit(parts._replace(query=urlencode(query))))
        finally:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@pytest.fixture
def client(tmp_path, database):
    app = create_app(Settings(data_dir=tmp_path, manual_handover_only=True, shokh_module=True,
                              database_url=database.url if isinstance(database, Database) else ''))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        yield client


@pytest.mark.parametrize('path', [
    '/api/accountant/day?date=2026-09-30',
    '/api/accountant/day?date=2026-10-01',
    '/api/accountant/day?date=2026-10-04',
    '/api/accountant/day?date=2026-10-05',
    '/api/founder/day?date=2026-10-05',
    '/api/accountant/shoh?date=2026-10-05',
    '/api/accountant/salary-day/month?month=2026-09&basis=shift',
    '/api/accountant/payroll/month?month=2026-09',
    '/api/accountant/salary-day/month?month=2026-09',
    '/api/accountant/reconciliation/export?month=2026-09',
    '/api/accountant/shoh?date=2026-09-30',
    '/api/founder/export/month?month=2026-09',
    '/api/founder/day?date=2026-09-30',
    '/api/director/report?start=2026-09-30&end=2026-10-06',
    '/api/cashier/day?date=2026-09-30',
    '/api/shokh/home?date=2026-09-30',
])
def test_old_dates_unavailable(client, path):
    response = client.get(path)
    assert response.status_code == 422, response.text
    assert '06.10.2026' in response.json()['detail']


def test_write_export_and_path_dates_unavailable(client):
    for path in ['/api/accountant/cash-opening', '/api/accountant/day/export']:
        response = client.post(path, json={'date': '2026-09-30', 'amount': '1', 'note': 'old', 'checks': []})
        assert response.status_code == 422
    assert client.delete('/api/accountant/handover/2026-09-30').status_code == 422


def test_opening_is_visible_without_creating_income(client):
    assert client.get('/api/config').json()['accounting_start'] == '2026-10-06'
    finance = client.app.state.accountant_finance
    finance.record_handover(START, Decimal(0))
    summary = finance.daily_summary(START, Decimal(0))
    assert summary['cash_flow']['opening_balance'] == '0'
    assert summary['cash_balance'] == Decimal('0')
    assert shoh_view(finance, START)['start'] == '0'
    with closing(finance._open()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM accountant_movements').fetchone()[0] == 0



def test_old_saved_report_and_pdf_are_hidden(client):
    store = client.app.state.director_store
    report_id = store.create({'period_start': '2026-09-20', 'period_end': '2026-09-30'}, {}, b'old', '2026-10-01')
    assert client.get('/api/director/reports').json()['reports'] == []
    assert client.get('/api/director/reports/' + report_id).status_code == 404
    assert client.get('/api/director/reports/' + report_id + '/pdf').status_code == 404


def test_salary_month_has_no_pre_accounting_days(client):
    response = client.get('/api/accountant/salary-day/month?month=2026-10')
    assert response.status_code == 200, response.text
    assert response.json()['days'][0] == '2026-10-06'


def test_default_director_period_is_clipped(monkeypatch):
    from retro.modules.director import routes
    monkeypatch.setattr(routes, 'today_tashkent', lambda: date(2026, 10, 7))
    assert routes.period_or_422(None, None, 30) == (START, date(2026, 10, 6))


def test_multipart_purchase_cannot_create_old_operation(client):
    response = client.post('/api/shokh/purchase', data={'date': '2026-09-30', 'point': 'test', 'item': 'test', 'quantity': '1', 'price': '1'}, files={'photo': ('test.jpg', b'fake', 'image/jpeg')})
    assert response.status_code == 422
    assert '06.10.2026' in response.json()['detail']


@pytest.mark.parametrize('old', ['2026-09-30T00:00:00', 1790726400])
def test_alternate_date_encodings_cannot_bypass_boundary(client, old):
    response = client.post('/api/accountant/day/export', json={'date': old, 'checks': []})
    assert response.status_code == 422
    assert '06.10.2026' in response.json()['detail']



def old_install(database, old_migration='confirmed_october_opening_2026_v2'):
    """An installed Oct 2 database, including rows on both sides of Oct 6."""
    finance = FinanceStore(database)
    old_day = '2026-10-05' if old_migration == 'confirmed_october_5_zero_opening_2026_v1' else '2026-10-02'
    with closing(finance._open()) as conn, conn:
        conn.execute('INSERT INTO accountant_working_cash_opening VALUES (1,?,?,?,?)',
                     (old_day, '2000000', 'Old opening', '2026-10-07'))
        conn.execute('CREATE TABLE IF NOT EXISTS accountant_data_migrations (name TEXT PRIMARY KEY, applied INTEGER NOT NULL)')
        conn.execute('INSERT INTO accountant_data_migrations VALUES (?,1)', (old_migration,))
        for account in ('dividends', 'usd', 'shoh'):
            conn.execute("INSERT INTO accountant_reserves(day,account,kind,amount,note,created_at) VALUES (?,?,'opening','9000','Archive','2026-10-02')", (old_day, account))
        for day in ('2026-10-05', '2026-10-06', '2026-10-07'):
            conn.execute("INSERT INTO accountant_movements(day,kind,description,amount,item_code,created_at) VALUES (?,'other_expense','Existing','100','admin_it',?)", (day, day))
    for day in (date(2026, 10, 5), START, START.replace(day=7)):
        finance.record_handover(day, Decimal('1000'))
    return finance


def snapshot(finance):
    with closing(finance._open()) as conn:
        return {table: conn.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() for table in (
            'accountant_movements', 'accountant_handover_days', 'accountant_cash_opening',
            'accountant_accruals', 'accountant_salary_payments')}


@pytest.mark.parametrize('old_migration', ['confirmed_october_opening_2026_v1', 'confirmed_october_opening_2026_v2', 'confirmed_october_5_zero_opening_2026_v1'])
def test_cutover_preserves_operations_and_ignores_old_openings(database, old_migration):
    finance = old_install(database, old_migration)
    before = snapshot(finance)
    apply_october_opening(finance)
    apply_october_opening(finance)
    assert snapshot(finance) == before
    assert finance.cash_opening()['day'] == '2026-10-06'
    assert finance.cash_opening()['amount'] == '0'
    assert finance.daily_summary(START, None)['cash_flow']['opening_balance'] == '0'
    assert finance.daily_summary(START, None)['cash_balance'] == Decimal('900')
    assert finance.daily_summary(START.replace(day=7), None)['cash_balance'] == Decimal('1800')
    assert {name: row['balance'] for name, row in finance.reserves(START).items() if name != 'monthly'} == dict(dividends='0', usd='0', shoh='0')
    from retro.modules.accountant.audit import audit_entries
    old_day = '2026-10-05' if old_migration == 'confirmed_october_5_zero_opening_2026_v1' else '2026-10-02'
    with closing(finance._open()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accountant_reserves WHERE kind='opening' AND day=?", (old_day,)).fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM accountant_reserves WHERE kind='opening' AND day='2026-10-06'").fetchone()[0] == 3
        audit = audit_entries(conn, entity_type='cash_opening')
    assert audit[-1]['before']['day'] == old_day
    assert audit[-1]['before']['amount'] == '2000000'
    assert audit[-1]['after']['amount'] == '0'


def test_opening_migration_rolls_back_and_can_retry(database, monkeypatch):
    from retro.modules.accountant import opening_migration
    finance = old_install(database)
    original = opening_migration.record_audit
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        original(*args, **kwargs)
        if len(calls) == 3:
            raise RuntimeError('simulated failure')
    monkeypatch.setattr(opening_migration, 'record_audit', fail)
    with pytest.raises(RuntimeError):
        apply_october_opening(finance)
    assert finance.cash_opening()['amount'] == '2000000'
    with closing(finance._open()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accountant_reserves WHERE day='2026-10-06'").fetchone()[0] == 0
    monkeypatch.setattr(opening_migration, 'record_audit', original)
    apply_october_opening(finance)
    assert finance.cash_opening()['amount'] == '0'


def test_two_workers_apply_opening_once(database):
    from concurrent.futures import ThreadPoolExecutor
    finance = old_install(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: apply_october_opening(finance), range(2)))
    assert finance.cash_opening()['amount'] == '0'
    with closing(finance._open()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accountant_reserves WHERE kind='opening' AND day='2026-10-06'").fetchone()[0] == 3


def test_new_install_starts_all_reserves_at_zero(client):
    finance = client.app.state.accountant_finance
    assert all(finance.reserves(START)[account]['balance'] == '0' for account in ('shoh', 'usd', 'dividends'))
    assert finance.cash_opening()['amount'] == '0'


def test_restarting_does_not_reset_new_period_operations(database):
    finance = FinanceStore(database)
    apply_october_opening(finance)
    finance.record_handover(START, Decimal('1000'))
    finance.add_expense(START, 'admin_it', 'New expense', '100')
    finance.reserve_entry(START, 'dividends', 'transfer', '200', 'Safe', cashier_amount=Decimal('1000'))
    apply_october_opening(FinanceStore(database))
    assert finance.daily_summary(START, None)['cash_balance'] == Decimal('700')
    assert finance.reserves(START)['dividends']['balance'] == '200'


def test_shift_five_pays_on_six_and_earlier_shifts_are_blocked(client, monkeypatch):
    for module in ('routes', 'salary_day', 'extra_payouts'):
        monkeypatch.setattr(f'retro.modules.accountant.{module}.today_tashkent', lambda: date(2026, 10, 10))
    person = client.app.state.accountant_roster.add(name='Тестовый Сотрудник', role='Официант', rate='100', group_name='Обслуживание зала')
    finance = client.app.state.accountant_finance
    for number in range(5, 9):
        finance.record_handover(date(2026, 10, number), Decimal('1000'))
    sheet = client.get('/api/accountant/salary-day/month?month=2026-10&basis=shift').json()
    assert sheet['first'] == sheet['days'][0] == sheet['shift_start'] == '2026-10-05'
    for shift in range(5, 8):
        body = dict(work_day=f'2026-10-{shift:02d}', employee_id=person.id, amount='100', expected_amount='0')
        response = client.put('/api/accountant/salary-day/shift-cell', json=body)
        assert response.status_code == 200, response.text
        assert response.json()['date'] == f'2026-10-{shift+1:02d}'
        assert client.put('/api/accountant/salary-day/shift-cell', json=body).json()['changed'] is False
    assert finance.daily_summary(START, None)['salary_paid_on_day'] == Decimal('100')
    for paid in range(6, 9):
        assert finance.daily_summary(date(2026, 10, paid), None)['salary_paid_on_day'] == Decimal('100')
    assert client.put('/api/accountant/salary-day/shift-cell', json=dict(body, work_day='2026-10-04')).status_code == 422
    assert client.put('/api/accountant/salary-day/cell', json=dict(date='2026-10-05', employee_id=person.id, amount='100', expected_amount='0')).status_code == 422
    assert client.post('/api/accountant/salary-day/extra', json=dict(body, work_day='2026-10-04', paid_day='2026-10-06', note='Old')).status_code == 422


def test_only_salary_and_cashier_source_shift_may_precede_accounting():
    from fastapi import HTTPException
    from retro.accounting_access import check_dates
    check_dates([('date', '2026-10-06'), ('cashier_date', '2026-10-05')])
    check_dates([('work_day', '2026-10-05')], salary=True)
    with pytest.raises(HTTPException):
        check_dates([('work_day', '2026-10-05')])
    with pytest.raises(HTTPException):
        check_dates([('paid_day', '2026-10-05')], salary=True)
    for field, day in [('cashier_date', '2026-10-04'), ('work_day', '2026-10-04')]:
        with pytest.raises(HTTPException) as error:
            check_dates([(field, day)])
        assert error.value.status_code == 422


def test_upgrade_from_five_preserves_payout_six_and_excludes_money_five(database, monkeypatch):
    from retro.modules.accountant.roster import RosterStore
    from retro.modules.accountant.closing import _debts_at
    from retro.modules.accountant.salary_day_export import salary_day_workbook
    from io import BytesIO
    from openpyxl import load_workbook
    monkeypatch.setattr('retro.modules.accountant.salary_day.today_tashkent', lambda: date(2026, 10, 10))
    finance = old_install(database, 'confirmed_october_5_zero_opening_2026_v1')
    person = RosterStore(database).add(name='Проверка переноса', role='Повар', rate='310', group_name='Кухня')
    with closing(finance._open()) as conn, conn:
        for work, paid, amount in [('2026-10-04', '2026-10-05', '900'), ('2026-10-05', '2026-10-06', '310')]:
            accrual = conn.execute('INSERT INTO accountant_accruals(work_day,employee_id,employee_name,group_name,attendance_status,rate,amount) VALUES (?,?,?,?,?,?,?)',
                                  (work, person.id, person.name, person.group_name, 'manual_salary', amount, amount)).lastrowid
            conn.execute('INSERT INTO accountant_salary_payments(accrual_id,paid_day,amount,created_at) VALUES (?,?,?,?)', (accrual, paid, amount, paid))
    before = snapshot(finance)
    apply_october_opening(finance)
    apply_october_opening(FinanceStore(database))
    assert snapshot(finance) == before
    summary = finance.daily_summary(START, None)
    assert summary['cash_flow']['first_day'] == '2026-10-06'
    assert summary['cash_flow']['opening_balance'] == '0'
    assert summary['salary_paid_on_day'] == Decimal('310')
    assert summary['cash_balance'] == Decimal('590')  # 1000 received - 100 expense - 310 salary.
    assert [item['work_day'] for item in summary['accruals']] == ['2026-10-05']
    assert summary['salary_debt'] == 0
    with closing(finance._open()) as conn:
        assert _debts_at(conn, START)['salary'] == '0'
    sheet = finance.salary_day_month(START, date(2026, 10, 31))
    cell = next(p for p in sheet['people'] if p['id'] == person.id)['cells']['2026-10-06']
    assert cell['amount'] == '310' and cell['work_day'] == '2026-10-05' and cell['editable']
    assert sheet['entry_start'] == '2026-10-06' and sheet['shift_start'] == '2026-10-05'
    sheet.update(month='2026-10', first='2026-10-06', last='2026-10-31')
    workbook = load_workbook(BytesIO(salary_day_workbook(sheet)))
    text = [str(c.value) for ws in workbook for row in ws for c in row if c.value is not None]
    assert any('Выплата 06.10' in value and '05.10' in value for value in text)
    assert not any('Выплата 05.10' in value for value in text)
    changed = finance.set_salary_day_cell(START, person.id, '300', '310')
    assert changed['work_day'] == '2026-10-05'
    assert finance.daily_summary(START, None)['cash_balance'] == Decimal('600')


def test_first_payment_day_allows_extra_for_previous_shift_only(client, monkeypatch):
    for module in ('routes', 'salary_day', 'extra_payouts'):
        monkeypatch.setattr(f'retro.modules.accountant.{module}.today_tashkent', lambda: date(2026, 10, 10))
    person = client.app.state.accountant_roster.add(name='Доплата тест', role='Повар', rate='310', group_name='Кухня')
    client.app.state.accountant_finance.record_handover(START, Decimal('1000'))
    body = dict(employee_id=person.id, work_day='2026-10-05', paid_day='2026-10-06', amount='40', note='Доплата за смену')
    response = client.post('/api/accountant/salary-day/extra', json=body)
    assert response.status_code == 201, response.text
    assert client.post('/api/accountant/salary-day/extra', json=dict(body, paid_day='2026-10-05')).status_code == 422
    assert client.post('/api/accountant/salary-day/extra', json=dict(body, work_day='2026-10-04')).status_code == 422
    data = client.get('/api/accountant/day?date=2026-10-06').json()
    assert data['ledger']['cash_balance'] == '960'
    assert data['ledger']['day_flow']['salary'] == '40'
    sheet = client.get('/api/accountant/salary-day/month?month=2026-10').json()
    assert sheet['first'] == sheet['days'][0] == sheet['entry_start'] == '2026-10-06'
    assert sheet['shift_start'] == '2026-10-05'
