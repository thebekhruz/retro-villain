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

START = date(2026, 10, 2)


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
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, manual_handover_only=True, shokh_module=True))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234)) as client:
        yield client


@pytest.mark.parametrize('path', [
    '/api/accountant/day?date=2026-09-30',
    '/api/accountant/day?date=2026-10-01',
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
    assert '02.10.2026' in response.json()['detail']


def test_write_export_and_path_dates_unavailable(client):
    for path in ['/api/accountant/cash-opening', '/api/accountant/day/export']:
        response = client.post(path, json={'date': '2026-09-30', 'amount': '1', 'note': 'old', 'checks': []})
        assert response.status_code == 422
    assert client.delete('/api/accountant/handover/2026-09-30').status_code == 422


def test_opening_is_visible_without_creating_income(client):
    assert client.get('/api/config').json()['accounting_start'] == '2026-10-02'
    finance = client.app.state.accountant_finance
    finance.record_handover(START, Decimal(0))
    summary = finance.daily_summary(START, Decimal(0))
    assert summary['cash_flow']['opening_balance'] == '3875000'
    assert summary['cash_balance'] == Decimal('3875000')
    assert shoh_view(finance, START)['start'] == '15639000'
    with closing(finance._open()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM accountant_movements').fetchone()[0] == 0


def test_confirmed_october_2_totals_and_carry_forward(database):
    finance = FinanceStore(database)
    # Old operations remain audit records but cannot affect the working balance.
    finance.record_handover(date(2026, 9, 30), Decimal('999999'))
    finance.set_cash_opening(date(2026, 9, 30), '123456', 'old')
    apply_october_opening(finance)
    finance.record_handover(START, Decimal('23240000'))
    finance.add_expense(START, 'admin_it', 'Расходы за 2 октября', '24567000')
    finance.give_procurement(START, 'Шох', 'Закуп', '548000', cashier_amount=Decimal('23240000'))
    finance.record_handover(date(2026, 10, 3), Decimal(0))
    assert finance.daily_summary(START, Decimal('23240000'))['cash_balance'] == Decimal('2000000')
    assert finance.daily_summary(date(2026, 10, 3), Decimal(0))['cash_flow']['opening_balance'] == '2000000'
    assert shoh_view(finance, START)['balance'] == '16187000'
    assert pocket_position(None, finance, START)['pocket'] == '16187000'
    apply_october_opening(finance)
    assert finance.daily_summary(START, Decimal('23240000'))['cash_balance'] == Decimal('2000000')
    with closing(finance._open()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM accountant_reserves WHERE account='shoh' AND kind='opening' AND day>=?", (START.isoformat(),)).fetchone()[0] == 1
        assert conn.execute('SELECT amount FROM accountant_cash_opening').fetchone()[0] == '123456'


def test_old_saved_report_and_pdf_are_hidden(client):
    store = client.app.state.director_store
    report_id = store.create({'period_start': '2026-09-20', 'period_end': '2026-09-30'}, {}, b'old', '2026-10-01')
    assert client.get('/api/director/reports').json()['reports'] == []
    assert client.get('/api/director/reports/' + report_id).status_code == 404
    assert client.get('/api/director/reports/' + report_id + '/pdf').status_code == 404


def test_salary_month_has_no_pre_accounting_days(client):
    response = client.get('/api/accountant/salary-day/month?month=2026-10')
    assert response.status_code == 200, response.text
    assert response.json()['days'][0] == '2026-10-02'


def test_default_director_period_is_clipped(monkeypatch):
    from retro.modules.director import routes
    monkeypatch.setattr(routes, 'today_tashkent', lambda: date(2026, 10, 7))
    assert routes.period_or_422(None, None, 30) == (START, date(2026, 10, 6))


def test_multipart_purchase_cannot_create_old_operation(client):
    response = client.post('/api/shokh/purchase', data={'date': '2026-09-30', 'point': 'test', 'item': 'test', 'quantity': '1', 'price': '1'}, files={'photo': ('test.jpg', b'fake', 'image/jpeg')})
    assert response.status_code == 422
    assert '02.10.2026' in response.json()['detail']


@pytest.mark.parametrize('old', ['2026-09-30T00:00:00', 1790726400])
def test_alternate_date_encodings_cannot_bypass_boundary(client, old):
    response = client.post('/api/accountant/day/export', json={'date': old, 'checks': []})
    assert response.status_code == 422
    assert '02.10.2026' in response.json()['detail']


def test_opening_migration_rolls_back_and_can_retry(database, monkeypatch):
    from retro.modules.accountant import opening_migration
    finance = FinanceStore(database)
    original = opening_migration.record_audit
    def fail(*args, **kwargs):
        raise RuntimeError('simulated failure')
    monkeypatch.setattr(opening_migration, 'record_audit', fail)
    with pytest.raises(RuntimeError):
        apply_october_opening(finance)
    assert finance.cash_opening() is None
    monkeypatch.setattr(opening_migration, 'record_audit', original)
    apply_october_opening(finance)
    assert finance.cash_opening()['amount'] == '3875000'


def test_two_workers_apply_opening_once(database):
    from concurrent.futures import ThreadPoolExecutor
    finance = FinanceStore(database)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: apply_october_opening(finance), range(2)))
    assert finance.cash_opening()['amount'] == '3875000'
    assert shoh_view(finance, START)['balance'] == '15639000'
