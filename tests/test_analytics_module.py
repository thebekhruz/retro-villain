"""Модуль «Аналитика» (T-524): выручка за 7 дней против двух предыдущих семидневок.

Панель общая для директора и учредителя (SHARED_PANELS), остальным закрыта.
История продаж — в базе панели (sales_days); iiko в тестах не нужен."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from retro.app import create_app, role_panels
from retro.config import Settings
from retro.modules.analytics.service import SPAN_DAYS, build_report, compare, span, windows
from retro.modules.cashier.service import TZ, today_tashkent

USERS = {'director': ('secret', 'director'), 'founder': ('secret', 'founder'), 'cashier': ('secret', 'cashier'),
         'karina': ('secret', 'manager'), 'buh': ('secret', 'accountant'), 'boss': ('secret', 'admin')}
TODAY = today_tashkent()
YESTERDAY = TODAY - timedelta(days=1)


def seed(store, end, revenue_of, *, closed=lambda day: True):
    """Дни трёх окон по `end` с выручкой `revenue_of(offset)`; None — день не загружен."""
    first, _ = span(end)
    for offset in range(SPAN_DAYS):
        day = first + timedelta(days=offset)
        amount = revenue_of(offset)
        if amount is None:
            continue
        store.save_day(day, [], [], fetched_at=datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=23),
                       revision='r1', closed=closed(day), revenue=Decimal(amount), iiko_revenue=Decimal(amount),
                       status='ok', reconciliation={})


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(dashboard_panel_users=USERS, data_dir=tmp_path),
                      expense_db_path=tmp_path / 'cashier.sqlite3', accountant_db_path=tmp_path / 'accountant.sqlite3',
                      director_db_path=tmp_path / 'director.sqlite3', founder_db_path=tmp_path / 'founder.sqlite3')


@pytest.fixture
def client(app):
    with TestClient(app, base_url='http://dashboard.example.com', client=('203.0.113.10', 50000)) as http:
        yield http


def modules(client, user):
    return [row['id'] for row in client.get('/api/config', auth=(user, 'secret')).json()['modules']]


# ── Доступ ────────────────────────────────────────────────────────────────

def test_analytics_is_a_shared_panel_of_director_and_founder():
    assert role_panels('director') == {'director', 'analytics'}
    assert role_panels('founder') == {'founder', 'analytics'}
    assert role_panels('cashier') == {'cashier'}
    assert role_panels(None) == frozenset()


def test_menu_shows_analytics_to_director_founder_and_admin_only(client):
    assert modules(client, 'director') == ['director', 'analytics']
    assert modules(client, 'founder') == ['founder', 'analytics']
    assert modules(client, 'cashier') == ['cashier']
    assert modules(client, 'karina') == ['manager']
    assert modules(client, 'buh') == ['accountant']
    assert 'analytics' in modules(client, 'boss')
    row = next(row for row in client.get('/api/config', auth=('founder', 'secret')).json()['modules']
               if row['id'] == 'analytics')
    assert row == {'id': 'analytics', 'name': 'Аналитика', 'path': '/analytics', 'available': True}


@pytest.mark.parametrize('user, status', [('director', 200), ('founder', 200), ('boss', 200),
                                          ('cashier', 403), ('karina', 403), ('buh', 403)])
def test_page_api_and_static_follow_the_panel(client, user, status):
    assert client.get('/analytics', auth=(user, 'secret')).status_code == status
    assert client.get('/api/analytics/revenue/weeks', auth=(user, 'secret')).status_code == status
    for name in ('analytics.html', 'analytics.js', 'analytics-logic.js', 'analytics.css'):
        assert client.get('/static/' + name, auth=(user, 'secret')).status_code == status, name


def test_anonymous_is_sent_to_login(client):
    answer = client.get('/analytics', follow_redirects=False)
    assert answer.status_code == 303 and answer.headers['location'] == '/login'
    assert client.get('/api/analytics/revenue/weeks').status_code == 401


# ── Расчёт ────────────────────────────────────────────────────────────────

def test_windows_are_three_consecutive_weeks_ending_at_end():
    end = date(2026, 10, 21)
    assert [(w['key'], w['start'], w['end']) for w in windows(end)] == [
        ('current', date(2026, 10, 15), date(2026, 10, 21)),
        ('previous', date(2026, 10, 8), date(2026, 10, 14)),
        ('before', date(2026, 10, 1), date(2026, 10, 7))]
    assert span(end) == (date(2026, 10, 1), date(2026, 10, 21))


def test_compare_gives_sum_percent_and_direction():
    assert compare('1120', '1000') == dict(diff='120', percent=12.0, direction='up')
    assert compare('900', '1000') == dict(diff='-100', percent=-10.0, direction='down')
    assert compare('1000', '1000') == dict(diff='0', percent=0.0, direction='flat')
    # Раньше продаж не было: сумма есть, процент не определён.
    assert compare('500', '0') == dict(diff='500', percent=None, direction='up')
    assert compare(None, '1000') == dict(diff=None, percent=None, direction='unknown')
    assert compare('1000', None) == dict(diff=None, percent=None, direction='unknown')
    # Округление до десятых: 1/3 → 33,3 %.
    assert compare('4000', '3000')['percent'] == 33.3


def test_weeks_endpoint_defaults_to_yesterday_and_compares_day_to_day(client, app):
    # Две недели назад — по 100 в день, неделю назад — по 200, последняя — по 230.
    seed(app.state.sales_store, YESTERDAY, lambda offset: 100 if offset < 7 else 200 if offset < 14 else 230)
    data = client.get('/api/analytics/revenue/weeks', auth=('founder', 'secret')).json()
    assert data['end'] == YESTERDAY.isoformat() and data['today'] == TODAY.isoformat()
    assert data['includes_today'] is False and data['window_days'] == 7 and data['complete'] is True
    current, previous, before = data['periods']
    assert (current['key'], current['total'], current['days_with_data'], current['complete']) == ('current', '1610', 7, True)
    assert (previous['total'], before['total']) == ('1400', '700')
    assert current['start'] == (YESTERDAY - timedelta(days=6)).isoformat() and current['end'] == YESTERDAY.isoformat()
    assert previous['end'] == (YESTERDAY - timedelta(days=7)).isoformat()
    assert before['start'] == (YESTERDAY - timedelta(days=20)).isoformat()
    # День к дню: i-я точка каждого окна — тот же день недели.
    for index in range(7):
        weekdays = {period['days'][index]['weekday'] for period in data['periods']}
        assert len(weekdays) == 1
        assert date.fromisoformat(current['days'][index]['day']) - date.fromisoformat(previous['days'][index]['day']) == timedelta(days=7)
    assert current['days'][0]['revenue'] == '230' and current['days'][0]['partial'] is False
    assert data['comparison'] == [
        dict(against='previous', label='Неделей раньше', start=previous['start'], end=previous['end'],
             diff='210', percent=15.0, direction='up'),
        dict(against='before', label='Двумя неделями раньше', start=before['start'], end=before['end'],
             diff='910', percent=130.0, direction='up')]
    assert data['warnings'] == ['iiko не подключён: показана только история продаж, уже сохранённая в базе панели.']
    assert data['updated_at'] is not None


def test_missing_and_open_days_are_said_not_zeroed(client, app):
    # В текущем окне один день не загружен, один — не перечитан после закрытия.
    first, _ = span(YESTERDAY)
    gap = first + timedelta(days=16)
    seed(app.state.sales_store, YESTERDAY, lambda offset: None if offset == 16 else 1000,
         closed=lambda day: day != YESTERDAY)
    data = client.get('/api/analytics/revenue/weeks', auth=('director', 'secret')).json()
    current = data['periods'][0]
    assert current['days_with_data'] == 6 and current['complete'] is False and current['total'] == '6000'
    day = next(item for item in current['days'] if item['day'] == gap.isoformat())
    assert day == dict(day=gap.isoformat(), weekday=day['weekday'], revenue=None, missing=True, partial=False)
    assert current['days'][-1]['partial'] is True
    assert data['complete'] is False
    assert any(gap.strftime('%d.%m') in text and 'не вошли' in text for text in data['warnings'])
    assert any('не перечитаны после закрытия' in text for text in data['warnings'])
    # Сравнение считается по загруженным дням, а не по «нулям» пропусков.
    assert data['comparison'][0]['diff'] == '-1000'


def test_no_history_means_no_totals_and_no_comparison(client):
    data = client.get('/api/analytics/revenue/weeks', auth=('founder', 'secret')).json()
    assert [period['total'] for period in data['periods']] == [None, None, None]
    assert all(item['direction'] == 'unknown' and item['diff'] is None for item in data['comparison'])
    assert data['updated_at'] is None and data['complete'] is False
    assert any('Нет загруженных данных' in text for text in data['warnings'])


def test_end_can_be_moved_but_not_into_the_future(client, app):
    anchor = YESTERDAY - timedelta(days=30)
    seed(app.state.sales_store, anchor, lambda offset: 50)
    data = client.get('/api/analytics/revenue/weeks', params={'end': anchor.isoformat()},
                      auth=('founder', 'secret')).json()
    assert data['end'] == anchor.isoformat() and data['periods'][0]['total'] == '350'
    tomorrow = TODAY + timedelta(days=1)
    answer = client.get('/api/analytics/revenue/weeks', params={'end': tomorrow.isoformat()}, auth=('founder', 'secret'))
    assert answer.status_code == 422 and answer.json()['detail'] == 'Будущие даты недоступны.'
    assert client.get('/api/analytics/revenue/weeks', params={'end': 'вчера'}, auth=('founder', 'secret')).status_code == 422


def test_including_today_marks_the_running_day(client, app):
    seed(app.state.sales_store, TODAY, lambda offset: 10)
    data = client.get('/api/analytics/revenue/weeks', params={'end': TODAY.isoformat()}, auth=('founder', 'secret')).json()
    assert data['includes_today'] is True
    last = data['periods'][0]['days'][-1]
    assert last['day'] == TODAY.isoformat() and last['partial'] is True
    assert data['periods'][0]['complete'] is False
    assert any(text.startswith('Сегодня учтены только закрытые чеки') for text in data['warnings'])


def test_build_report_is_pure_and_reports_history_start():
    end = date(2026, 10, 21)
    report = build_report(end, {}, today=date(2026, 10, 22), history_start=date(2026, 10, 13), configured=True)
    assert report['periods'][0]['days'][0]['weekday'] == 'чт'
    assert report['history_start'] == '2026-10-13'
    assert 'История продаж в базе начинается с 13.10.2026.' in report['warnings']
    assert report['timezone'] == 'Asia/Tashkent' and report['source'].startswith('iiko OLAP SALES')
