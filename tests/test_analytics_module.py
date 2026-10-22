"""Модуль «Аналитика» (T-524): выручка Retro за 7 дней против недели раньше и года назад.

Панель открыта только роли `analytics` (аналитик) и полному доступу, остальным закрыта.
История продаж — в базе панели (sales_days); iiko в тестах не нужен. Направление
Retro берётся из сверки дня (reconciliation.lines), школа и банкет не считаются."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from retro.app import ROLE_PATHS, create_app
from retro.config import Settings
from retro.modules.analytics.service import WINDOW, YEAR_SHIFT, build_report, compare, retro_revenue, spans, windows
from retro.modules.cashier.service import TZ, today_tashkent

USERS = {'analyst': ('secret', 'analytics'), 'director': ('secret', 'director'), 'founder': ('secret', 'founder'),
         'cashier': ('secret', 'cashier'), 'karina': ('secret', 'manager'), 'buh': ('secret', 'accountant'),
         'boss': ('secret', 'admin')}
TODAY = today_tashkent()
YESTERDAY = TODAY - timedelta(days=1)


def reconciliation(retro, school=0, banquet=0):
    """Сверка дня, как её пишет prepare_day: итоги по направлениям в `lines`."""
    lines = {name: dict(revenue=str(value), gross=str(value), quantity='1', orders=1)
             for name, value in (('retro', retro), ('school', school), ('banquet', banquet)) if value}
    return dict(tolerance='1', lines=lines, iiko=lines, operations={}, differences=[])


def seed(store, end, revenue_of, *, closed=lambda day: True, school=0, banquet=0):
    """Дни окон по `end` с выручкой Retro `revenue_of(offset)`, где offset 0…6 — год
    назад, 7…13 — неделей раньше, 14…20 — последние 7 дней; None — день не загружен.
    Школа и банкет добавляются в день отдельными направлениями и в итог Retro не входят."""
    starts = [end - timedelta(days=YEAR_SHIFT + WINDOW - 1), end - timedelta(days=2 * WINDOW - 1), end - timedelta(days=WINDOW - 1)]
    for index, start in enumerate(starts):
        for shift in range(WINDOW):
            offset, day = index * WINDOW + shift, start + timedelta(days=shift)
            amount = revenue_of(offset)
            if amount is None:
                continue
            total = Decimal(amount) + Decimal(school) + Decimal(banquet)
            store.save_day(day, [], [], fetched_at=datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=23),
                           revision='r1', closed=closed(day), revenue=total, iiko_revenue=total,
                           status='ok', reconciliation=reconciliation(amount, school, banquet))


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

def test_analyst_role_lands_in_the_panel(client):
    assert ROLE_PATHS['analytics'] == '/analytics'
    answer = client.post('/api/session', json={'username': 'analyst', 'password': 'secret'})
    assert answer.status_code == 200 and answer.json() == {'role': 'analytics', 'path': '/analytics'}


def test_menu_shows_analytics_to_analyst_and_admin_only(client):
    assert modules(client, 'analyst') == ['analytics']
    assert modules(client, 'director') == ['director']
    assert modules(client, 'founder') == ['founder']
    assert modules(client, 'cashier') == ['cashier']
    assert modules(client, 'karina') == ['manager']
    assert modules(client, 'buh') == ['accountant']
    assert 'analytics' in modules(client, 'boss')
    row = next(row for row in client.get('/api/config', auth=('analyst', 'secret')).json()['modules']
               if row['id'] == 'analytics')
    assert row == {'id': 'analytics', 'name': 'Аналитика', 'path': '/analytics', 'available': True}


@pytest.mark.parametrize('user, status', [('analyst', 200), ('boss', 200), ('director', 403), ('founder', 403),
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

def test_windows_are_this_week_last_week_and_52_weeks_ago():
    end = date(2026, 10, 21)
    assert [(w['key'], w['start'], w['end']) for w in windows(end)] == [
        ('current', date(2026, 10, 15), date(2026, 10, 21)),
        ('previous', date(2026, 10, 8), date(2026, 10, 14)),
        ('year', date(2025, 10, 16), date(2025, 10, 22))]
    # 364 дня: те же дни недели, что и сейчас.
    assert date(2025, 10, 16).weekday() == date(2026, 10, 15).weekday()
    assert spans(end) == [(date(2026, 10, 8), date(2026, 10, 21)), (date(2025, 10, 16), date(2025, 10, 22))]


def test_retro_revenue_comes_from_the_day_reconciliation_only():
    state = dict(reconciliation=reconciliation(1000, school=300, banquet=5000))
    assert retro_revenue(state) == Decimal(1000)
    # Школа и банкет без Retro — ноль по Retro, а не их сумма.
    assert retro_revenue(dict(reconciliation=reconciliation(0, school=300))) == Decimal(0)
    # Старая запись без разбивки по направлениям — день для модуля не загружен.
    assert retro_revenue(dict(reconciliation={})) is None
    assert retro_revenue(dict(reconciliation=None)) is None


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
    # Год назад — по 100 в день, неделю назад — по 200, последняя — по 230; школа и
    # банкет в каждом дне есть, но в Retro не входят.
    seed(app.state.sales_store, YESTERDAY, lambda offset: 100 if offset < 7 else 200 if offset < 14 else 230,
         school=40, banquet=9000)
    data = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret')).json()
    assert data['end'] == YESTERDAY.isoformat() and data['today'] == TODAY.isoformat()
    assert data['direction'] == 'retro' and 'без школы и банкетного зала' in data['direction_name']
    assert data['includes_today'] is False and data['window_days'] == 7 and data['complete'] is True
    current, previous, year = data['periods']
    assert (current['key'], current['total'], current['days_with_data'], current['complete']) == ('current', '1610', 7, True)
    assert (previous['total'], year['total']) == ('1400', '700')
    assert current['start'] == (YESTERDAY - timedelta(days=6)).isoformat() and current['end'] == YESTERDAY.isoformat()
    assert previous['end'] == (YESTERDAY - timedelta(days=7)).isoformat()
    assert year['key'] == 'year' and year['end'] == (YESTERDAY - timedelta(days=364)).isoformat()
    # День к дню: i-я точка каждого окна — тот же день недели.
    for index in range(7):
        weekdays = {period['days'][index]['weekday'] for period in data['periods']}
        assert len(weekdays) == 1
        assert date.fromisoformat(current['days'][index]['day']) - date.fromisoformat(previous['days'][index]['day']) == timedelta(days=7)
    assert current['days'][0]['revenue'] == '230' and current['days'][0]['partial'] is False
    assert data['comparison'] == [
        dict(against='previous', label='Неделей раньше', start=previous['start'], end=previous['end'],
             diff='210', percent=15.0, direction='up'),
        dict(against='year', label='Год назад', start=year['start'], end=year['end'],
             diff='910', percent=130.0, direction='up')]
    assert data['warnings'] == ['iiko не подключён: показана только история продаж, уже сохранённая в базе панели.']
    assert data['updated_at'] is not None


def test_missing_and_open_days_are_said_not_zeroed(client, app):
    # В текущем окне один день не загружен, один — не перечитан после закрытия.
    gap = YESTERDAY - timedelta(days=4)
    seed(app.state.sales_store, YESTERDAY, lambda offset: None if offset == 16 else 1000,
         closed=lambda day: day != YESTERDAY)
    data = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret')).json()
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
    data = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret')).json()
    assert [period['total'] for period in data['periods']] == [None, None, None]
    assert all(item['direction'] == 'unknown' and item['diff'] is None for item in data['comparison'])
    assert data['updated_at'] is None and data['complete'] is False
    # Без iiko на стенде — честное «данных нет», по окну; год назад — до начала истории.
    assert any(text.startswith('Последние 7 дней (') and 'данных нет' in text for text in data['warnings'])
    assert any(text.startswith('Год назад (') and 'история продаж в базе начинается с' in text for text in data['warnings'])
    assert all('ещё загружаются' not in text for text in data['warnings'])  # iiko не подключён — ничего не грузится


def test_end_can_be_moved_but_not_into_the_future(client, app):
    anchor = YESTERDAY - timedelta(days=30)
    seed(app.state.sales_store, anchor, lambda offset: 50)
    data = client.get('/api/analytics/revenue/weeks', params={'end': anchor.isoformat()},
                      auth=('analyst', 'secret')).json()
    assert data['end'] == anchor.isoformat() and data['periods'][0]['total'] == '350'
    tomorrow = TODAY + timedelta(days=1)
    answer = client.get('/api/analytics/revenue/weeks', params={'end': tomorrow.isoformat()}, auth=('analyst', 'secret'))
    assert answer.status_code == 422 and answer.json()['detail'] == 'Будущие даты недоступны.'
    assert client.get('/api/analytics/revenue/weeks', params={'end': 'вчера'}, auth=('analyst', 'secret')).status_code == 422


def test_including_today_marks_the_running_day(client, app):
    seed(app.state.sales_store, TODAY, lambda offset: 10)
    data = client.get('/api/analytics/revenue/weeks', params={'end': TODAY.isoformat()}, auth=('analyst', 'secret')).json()
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
    assert any(text.startswith('Год назад (16.10.2025 – 22.10.2025)') and 'начинается с 13.10.2026' in text
               for text in report['warnings'])
    # Дни после начала истории при подключённом iiko названы загружающимися.
    assert any(text.startswith('Последние 7 дней (15.10 – 21.10)') and 'ещё загружаются из iiko' in text
               for text in report['warnings'])
    assert [period['key'] for period in report['periods']] == ['current', 'previous', 'year']
    assert report['timezone'] == 'Asia/Tashkent' and report['source'].startswith('iiko OLAP SALES')
