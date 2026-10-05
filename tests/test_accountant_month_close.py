"""ТЗ 02.10: «На начало дня», сдача отчёта дня, закрытие месяца, баланс Шохруха."""
import os
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from io import BytesIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from retro.app import create_app
from retro.config import Settings
from retro.modules.accountant import closing
from retro.modules.accountant.ledger import FinanceStore, LedgerError

SEP_29, SEP_30, OCT_1 = date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1)
POSTGRES_URL = os.getenv('RETRO_TEST_POSTGRES_URL', '')
# На проде база — Postgres: те же сценарии гоняем на обоих, если он есть.
DATABASES = ['sqlite', pytest.param('postgres', marks=pytest.mark.skipif(
    not POSTGRES_URL, reason='RETRO_TEST_POSTGRES_URL не задан'))]


@pytest.fixture
def store(tmp_path):
    return FinanceStore(tmp_path / 'finance.sqlite3')


@contextmanager
def postgres_url():
    """Своя схема на тест: таблицы других тестов не мешают."""
    import psycopg
    from psycopg import sql
    schema = 'close_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query), options='-csearch_path=' + schema)
    try:
        yield urlunsplit(parts._replace(query=urlencode(query)))
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@contextmanager
def client(tmp_path, monkeypatch, today=OCT_1, database='sqlite'):
    monkeypatch.setattr('retro.modules.accountant.routes.today_tashkent', lambda: today)
    if database == 'postgres':
        with postgres_url() as url:
            app = create_app(Settings(database_url=url, data_dir=tmp_path, manual_handover_only=True))
            with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as test_client:
                yield test_client
        return
    app = create_app(Settings(manual_handover_only=True), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3')
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as test_client:
        yield test_client


# ── «На начало дня» ────────────────────────────────────────────────────────

def test_opening_is_yesterdays_closing_and_unconfirmed_cash_is_not_counted(store):
    """93 767 000 на 02.10 (ТЗ, п. 1): в остаток шли передачи кассира, которые
    бухгалтер не подтверждал. Теперь «ожидается» в остаток не входит."""
    store.record_handover(SEP_29, Decimal('1000'))
    store.set_cash_opening(SEP_29, '500', 'Пересчёт')
    store.record_handover(SEP_30, Decimal('7000'), source='cashier')
    sep_30 = store.daily_summary(SEP_30, None)
    assert sep_30['cash_flow']['opening_balance'] == '1500'
    assert sep_30['cash_flow']['handover_status'] == 'pending'
    assert sep_30['cash_balance'] == Decimal('1500')
    oct_1 = store.daily_summary(OCT_1, None)
    assert oct_1['cash_flow']['opening_balance'] == '1500'
    # Расшифровка «На начало дня»: вчерашний день целиком.
    previous = oct_1['opening_breakdown']['previous']
    assert (previous['day'], previous['opening'], previous['handover'], previous['handover_counted'],
            previous['closing']) == ('2026-09-30', '1500', '7000', '0', '1500')

    store.confirm_handover(SEP_30, '6500', Decimal('7000'), 'Лина')
    oct_1 = store.daily_summary(OCT_1, None)
    assert oct_1['cash_flow']['opening_balance'] == '8000'
    assert oct_1['opening_breakdown']['previous']['handover_status'] == 'confirmed'


def test_reconciliation_by_day_adds_up(store):
    store.record_handover(SEP_29, Decimal('1000'))
    store.set_cash_opening(SEP_29, '500', 'Пересчёт')
    store.add_expense(SEP_29, 'ops_rent', 'Аренда', '200')
    store.record_handover(SEP_30, Decimal('300'))
    days = store.reconciliation(SEP_29, SEP_30)
    assert [(d['opening'], d['handover_counted'], d['outflows'], d['closing']) for d in days] == [
        (Decimal('500'), Decimal('1000'), Decimal('200'), Decimal('1300')),
        (Decimal('1300'), Decimal('300'), Decimal('0'), Decimal('1600'))]


# ── Закрытие месяца ────────────────────────────────────────────────────────

def september(store):
    store.record_handover(SEP_29, Decimal('1000'))
    store.set_cash_opening(SEP_29, '500', 'Пересчёт')
    expense = store.add_expense(SEP_29, 'ops_rent', 'Аренда', '200')
    store.record_handover(SEP_30, Decimal('300'))
    return expense


def test_closed_month_is_read_only_and_its_end_starts_the_next_month(store):
    expense = september(store)
    preview = closing.month_preview(store, '2026-09', OCT_1)
    assert preview['can_close'] and preview['closing_balance'] == '1600'
    closed = closing.close_month(store, '2026-09', 'Лина', OCT_1)
    assert (closed['month'], closed['closed_by'], closed['closing_balance']) == ('2026-09', 'Лина', '1600')

    for write in (lambda: store.add_expense(SEP_30, 'ops_rent', 'Поздно', '1'),
                  lambda: store.record_handover(SEP_30, Decimal('999')),
                  lambda: store.confirm_handover(SEP_30, '1', None, 'Лина'),
                  lambda: store.delete_operation('movement', expense, SEP_29),
                  lambda: store.reserve_entry(SEP_29, 'dividends', 'opening', '0', 'Сейф')):
        with pytest.raises(LedgerError, match='Месяц закрыт'):
            write()
    # Конец 30.09 — начало 01.10, даже до первой передачи кассы за октябрь.
    assert store.daily_summary(OCT_1, None)['cash_flow']['opening_balance'] == '1600'
    assert store.daily_summary(OCT_1, None)['opening_breakdown']['anchor']['kind'] == 'closure:2026-09'
    store.record_handover(OCT_1, Decimal('100'))
    assert store.daily_summary(OCT_1, None)['cash_balance'] == Decimal('1700')

    with pytest.raises(LedgerError, match='уже закрыт'):
        closing.close_month(store, '2026-09', 'Лина', OCT_1)
    assert closing.closed_state(store, SEP_29)['month'] == '2026-09'
    assert closing.closed_state(store, OCT_1) is None
    # Снять закрытие можно только скриптом обслуживания.
    assert closing.reopen_month(store, '2026-09') is True
    store.add_expense(SEP_30, 'ops_rent', 'После снятия', '1')


def test_month_closes_only_on_its_last_day_and_shows_remarks(store):
    september(store)
    store.record_handover(SEP_30, Decimal('400'), source='cashier', replace_sources=('cashier', 'auto', 'accountant'))
    early = closing.month_preview(store, '2026-09', SEP_29)
    assert not early['can_close'] and 'последний день' in early['reason']
    with pytest.raises(LedgerError, match='последний день'):
        closing.close_month(store, '2026-09', 'Лина', SEP_29)
    preview = closing.month_preview(store, '2026-09', SEP_30)
    kinds = {item['kind']: item for item in preview['remarks']}
    assert kinds['pending']['days'] == ['2026-09-30']
    assert kinds['reports']['days'] == ['2026-09-29', '2026-09-30']
    # Замечания закрытие не запрещают — их показывают и просят подтвердить.
    assert preview['can_close'] and preview['closing_balance'] == '1300'


# ── Отчёт дня и экран ──────────────────────────────────────────────────────

@pytest.mark.parametrize('database', DATABASES)
def test_day_report_needs_confirmed_cashier_amount_and_reaches_the_founder(tmp_path, monkeypatch, database):
    with client(tmp_path, monkeypatch, database=database) as c:
        finance = c.app.state.accountant_finance
        finance.record_handover(SEP_30, Decimal('1000000'), source='cashier')
        refused = c.post('/api/accountant/day-report', json={'date': SEP_30.isoformat()})
        assert refused.status_code == 422 and 'Подтвердите' in refused.json()['detail']
        assert c.post('/api/accountant/handover/confirm', json={
            'date': SEP_30.isoformat(), 'amount': '1000000'}).status_code == 200
        sent = c.post('/api/accountant/day-report', json={
            'date': SEP_30.isoformat(), 'checks': [{'lvl': 'warn', 'text': 'Смена не выдана'}]})
        assert sent.status_code == 200, sent.text
        assert sent.json()['report']['flow']['closing'] == '1000000'
        day = c.get('/api/accountant/day', params={'date': SEP_30.isoformat()}).json()
        assert day['day_report']['changed'] is False
        assert day['month_close']['close_month'] == '2026-09'
        assert c.post('/api/accountant/expenses', json={
            'date': SEP_30.isoformat(), 'item_code': 'ops_rent', 'note': 'Аренда',
            'amount': '100000'}).status_code == 201
        day = c.get('/api/accountant/day', params={'date': SEP_30.isoformat()}).json()
        assert day['day_report']['changed'] is True
        feed = c.get('/api/founder/accountant-reports').json()
        assert feed['reports'][0]['day'] == '2026-09-30' and feed['reports'][0]['changed'] is True
        assert feed['reports'][0]['checks'][0]['text'] == 'Смена не выдана'


@pytest.mark.parametrize('database', DATABASES)
def test_month_close_through_the_api_locks_the_day_and_shows_the_label(tmp_path, monkeypatch, database):
    with client(tmp_path, monkeypatch, database=database) as c:
        finance = c.app.state.accountant_finance
        finance.record_handover(SEP_30, Decimal('1000000'))
        preview = c.get('/api/accountant/month-close', params={'month': '2026-09'}).json()
        assert preview['can_close'] and preview['closing_balance'] == '1000000'
        assert len(preview['days']) == 30
        done = c.post('/api/accountant/month-close', json={'month': '2026-09'})
        assert done.status_code == 201, done.text
        locked = c.post('/api/accountant/expenses', json={
            'date': SEP_30.isoformat(), 'item_code': 'ops_rent', 'note': 'Аренда', 'amount': '1'})
        assert locked.status_code == 409 and 'Месяц закрыт' in locked.json()['detail']
        day = c.get('/api/accountant/day', params={'date': SEP_30.isoformat()}).json()
        assert day['closed']['month'] == '2026-09' and day['closed']['closed_by']
        assert day['month_close']['close_month'] is None
        october = c.get('/api/accountant/day', params={'date': OCT_1.isoformat()}).json()
        assert october['ledger']['cash_flow']['opening_balance'] == '1000000'
        assert c.post('/api/accountant/month-close', json={'month': '2026-09'}).status_code == 409
        feed = c.get('/api/founder/accountant-reports').json()
        assert feed['closures'][0]['month'] == '2026-09'
        export = c.get('/api/accountant/reconciliation/export', params={'month': '2026-09'})
        assert export.status_code == 200
        sheet = load_workbook(BytesIO(export.content)).active
        assert sheet['A5'].value == '30.09' and sheet['L5'].value == 1000000


def test_archival_month_is_not_required_for_working_accounting(tmp_path, monkeypatch):
    with client(tmp_path, monkeypatch, today=date(2026, 10, 2)) as c:
        c.app.state.accountant_finance.record_handover(SEP_30, Decimal('1000'))
        day = c.get('/api/accountant/day', params={'date': '2026-10-02'}).json()
        assert day['month_close']['open_month'] is None
        archive = c.get('/api/accountant/day', params={'date': '2026-10-01'}).json()
        assert archive['month_close']['open_month'] == {'month': '2026-09', 'name': 'Сентябрь 2026',
                                                       'last_day': '2026-09-30'}


# ── Баланс Шохруха ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('database', DATABASES)
def test_shoh_balance_is_allocated_less_invoice_expenses(tmp_path, monkeypatch, database):
    with client(tmp_path, monkeypatch, database=database) as c:
        c.app.state.accountant_finance.record_handover(OCT_1, Decimal('5000000'))
        assert c.post('/api/accountant/procurement', json={
            'date': OCT_1.isoformat(), 'recipient': 'Шох', 'purpose': 'закуп за день',
            'amount': '1000000'}).status_code == 201
        expense = {'date': OCT_1.isoformat(), 'place': 'Алайский базар', 'amount': '300000',
                   'note': 'Расчёт по счёт-фактуре №12'}
        saved = c.post('/api/accountant/shoh/expenses', json=expense)
        assert saved.status_code == 201, saved.text
        assert c.post('/api/accountant/shoh/expenses', json=dict(expense, place='Чорсу')).status_code == 422
        assert c.post('/api/accountant/bazaars', json={'name': 'Чорсу'}).status_code == 201
        assert c.post('/api/accountant/bazaars', json={'name': 'чорсу'}).status_code == 409
        assert c.post('/api/accountant/shoh/expenses', json=dict(expense, place='Чорсу',
                                                                 amount='100000')).status_code == 201
        view = c.get('/api/accountant/shoh', params={'date': OCT_1.isoformat()}).json()
        assert (view['balance'], view['month_given'], view['month_spent']) == ('600000', '1000000', '400000')
        assert view['history'] is None  # история — только по кнопке «Месячный отчёт»
        assert 'Чорсу' in view['bazaars']
        # Касса уменьшилась только выдачей: расход Шоха второй раз её не трогает.
        assert view['cash_balance'] == '4000000'
        report = c.get('/api/accountant/shoh', params={'date': OCT_1.isoformat(), 'history': 1}).json()
        assert [row['kind'] for row in report['history']].count('expense') == 2
        assert c.delete(f"/api/accountant/shoh/expenses/{saved.json()['id']}",
                        params={'date': OCT_1.isoformat()}).status_code == 204
        assert c.get('/api/accountant/shoh', params={'date': OCT_1.isoformat()}).json()['balance'] == '900000'
        assert c.get('/api/accountant/day', params={'date': OCT_1.isoformat()}).json()['shoh_pocket']['pocket'] \
            == '900000'


def test_shokh_module_is_gone_entirely(tmp_path, monkeypatch):
    """ТЗ 02.10: Шох ничего не вносит — модуля «Закуп · Шох» нет ни в меню, ни на
    входе, ни в API. Его счёт ведёт бухгалтер на «Балансе Шохруха»."""
    monkeypatch.setattr('retro.modules.accountant.routes.today_tashkent', lambda: OCT_1)
    users = {'shokh': ('pw', 'shokh'), 'buh': ('pw', 'accountant')}
    app = create_app(Settings(manual_handover_only=True, dashboard_panel_users=users, dashboard_user='boss',
                              dashboard_password='pw'),
                     expense_db_path=tmp_path / 'cashier.sqlite3', accountant_db_path=tmp_path / 'accountant.sqlite3')
    with TestClient(app, base_url='http://dashboard.example.com', client=('203.0.113.10', 50000)) as c:
        refused = c.post('/api/session', json={'username': 'shokh', 'password': 'pw'})
        assert refused.status_code == 403 and 'Баланс Шохруха' in refused.json()['detail']
        assert c.post('/api/session', json={'username': 'boss', 'password': 'pw'}).status_code == 200
        assert 'shokh' not in [row['id'] for row in c.get('/api/config').json()['modules']]
        assert c.get('/shokh').status_code == 404
        assert c.get('/api/shokh/home').status_code == 404
        assert c.post('/api/shokh/trip').status_code == 404
        assert c.get('/accountant/shoh').status_code == 200
