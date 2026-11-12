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
from retro.modules.analytics.service import (MAX_DAYS, WINDOW, YEAR_SHIFT, build_report, compare, day_word, history_report,
                                             retro_revenue, spans, windows)
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

def test_windows_follow_the_chosen_period_length():
    start, end = date(2026, 10, 15), date(2026, 10, 21)
    assert [(w['key'], w['label'], w['start'], w['end']) for w in windows(start, end)] == [
        ('current', 'Выбранный период', date(2026, 10, 15), date(2026, 10, 21)),
        ('previous', '7 дней раньше', date(2026, 10, 8), date(2026, 10, 14)),
        ('year', 'Год назад · те же дни недели', date(2025, 10, 16), date(2025, 10, 22))]
    # 364 дня: те же дни недели, что и сейчас.
    assert date(2025, 10, 16).weekday() == date(2026, 10, 15).weekday()
    assert spans(start, end) == [(date(2026, 10, 8), date(2026, 10, 21)), (date(2025, 10, 16), date(2025, 10, 22))]
    # 21 день: «раньше» — 21 день перед периодом, «год назад» — те же 21 день.
    three = windows(date(2026, 10, 1), date(2026, 10, 21))
    assert (three[1]['label'], three[1]['start'], three[1]['end']) == ('21 день раньше', date(2026, 9, 10), date(2026, 9, 30))
    assert (three[2]['start'], three[2]['end']) == (date(2025, 10, 2), date(2025, 10, 22))
    assert [day_word(n) for n in (1, 2, 5, 7, 11, 21, 30)] == ['день', 'дня', 'дней', 'дней', 'дней', 'день', 'дней']


# T-648: окно «год назад» — те же дни недели (по умолчанию) или те же календарные даты.
PERIODS = [(date(2026, 12, 31), date(2026, 12, 31)), (date(2026, 11, 7), date(2026, 11, 9)),
           (date(2026, 10, 15), date(2026, 10, 21)), (date(2026, 10, 1), date(2026, 10, 30)),
           (date(2026, 8, 1), date(2026, 10, 31))]


def days_of(first, last):
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


def test_year_window_by_weekday_is_the_old_364_day_shift():
    for start, end in PERIODS:
        by_weekday = windows(start, end, 'weekday')
        assert windows(start, end) == by_weekday
        year = by_weekday[2]
        assert (year['label'], year['start'], year['end']) == (
            'Год назад · те же дни недели', start - timedelta(days=364), end - timedelta(days=364))
        assert year['start'].weekday() == start.weekday()
        # Окно «раньше» от режима не зависит.
        assert windows(start, end, 'date')[:2] == by_weekday[:2]


def test_year_window_by_date_keeps_the_calendar_date_and_the_length():
    # 31.12 — самый выручечный день года: по дням недели он встаёт против 1 января.
    new_year = date(2026, 12, 31)
    assert windows(new_year, new_year, 'weekday')[2]['start'] == date(2026, 1, 1)
    year = windows(new_year, new_year, 'date')[2]
    assert (year['label'], year['start'], year['end']) == ('Год назад · те же даты', date(2025, 12, 31), date(2025, 12, 31))
    year = windows(date(2026, 11, 7), date(2026, 11, 9), 'date')[2]
    assert (year['start'], year['end']) == (date(2025, 11, 7), date(2025, 11, 9))
    for start, end in PERIODS:
        current, _, year = windows(start, end, 'date')
        assert year['start'] == start.replace(year=start.year - 1)
        assert year['end'] - year['start'] == current['end'] - current['start']


def test_year_window_by_date_around_29_february():
    # 29.02 в невисокосном году — 28.02, а не ValueError.
    year = windows(date(2028, 2, 29), date(2028, 2, 29), 'date')[2]
    assert (year['start'], year['end']) == (date(2027, 2, 28), date(2027, 2, 28))
    year = windows(date(2028, 2, 29), date(2028, 3, 6), 'date')[2]
    assert (year['start'], year['end']) == (date(2027, 2, 28), date(2027, 3, 6))
    # Период через 29.02 (8 дней) — окно тоже 8 дней, сдвиг считается от начала.
    year = windows(date(2028, 2, 25), date(2028, 3, 3), 'date')[2]
    assert (year['start'], year['end']) == (date(2027, 2, 25), date(2027, 3, 4))
    # И в обратную сторону: 7 дней 2029 года, а окно 2028-го накрывает 29.02 — тоже 7 дней.
    year = windows(date(2029, 2, 25), date(2029, 3, 3), 'date')[2]
    assert (year['start'], year['end']) == (date(2028, 2, 25), date(2028, 3, 2))


def test_spans_cover_every_window_day_in_both_modes():
    # Каждый день с 2027 по март 2029 — два перехода через 29 февраля и смена года.
    start = date(2027, 1, 1)
    while start <= date(2029, 3, 31):
        for year in ('weekday', 'date'):
            for length in (1, 2, 7, 28, 29, 30, 31, 60, MAX_DAYS):
                end = start + timedelta(days=length - 1)
                current, previous, past = windows(start, end, year)
                assert (past['end'] - past['start']).days + 1 == length, (year, start, length)
                assert past['end'] < previous['start']  # окна не налезают друг на друга
                loaded = set().union(*(days_of(first, last) for first, last in spans(start, end, year)))
                for window in (current, previous, past):
                    assert days_of(window['start'], window['end']) <= loaded, (year, start, length, window['key'])
        start += timedelta(days=1)
    # Первый отрезок — период с окном «раньше» — от режима не зависит.
    assert spans(date(2026, 10, 15), date(2026, 10, 21), 'date') == [
        (date(2026, 10, 8), date(2026, 10, 21)), (date(2025, 10, 15), date(2025, 10, 21))]


def test_weeks_endpoint_switches_the_year_window(client, app):
    end = YESTERDAY - timedelta(days=30)
    start = end - timedelta(days=6)
    def revenue(day):
        return day.day * 10  # у каждого дня своя сумма: окна разных режимов дают разные итоги
    # Сохраняем все дни, которые нужны окнам обоих режимов.
    needed = set()
    for year in ('weekday', 'date'):
        for first, last in spans(start, end, year):
            needed |= days_of(first, last)
    for day in needed:
        save(app.state.sales_store, day, revenue(day))
    answers = {}
    for year in ('weekday', 'date'):
        answers[year] = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret'),
                                   params=dict(start=start.isoformat(), end=end.isoformat(), year=year)).json()
        window = windows(start, end, year)[2]
        past = answers[year]['periods'][2]
        assert answers[year]['year'] == year
        assert (past['label'], past['start'], past['end']) == (window['label'], window['start'].isoformat(), window['end'].isoformat())
        assert past['total'] == str(sum(revenue(day) for day in days_of(window['start'], window['end'])))
        # Короткого отрезка загрузки нет: все дни окна на месте.
        assert past['days_with_data'] == 7 and not any(day['missing'] for day in past['days'])
        assert answers[year]['comparison'][1]['label'] == window['label']
    assert answers['date']['periods'][2]['start'] == start.replace(year=start.year - 1).isoformat()
    assert answers['date']['periods'][2]['total'] != answers['weekday']['periods'][2]['total']
    # Окно «раньше» одинаково в обоих режимах.
    assert answers['date']['periods'][:2] == answers['weekday']['periods'][:2]
    # Без параметра — те же дни недели, как до T-648.
    default = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret'),
                         params=dict(start=start.isoformat(), end=end.isoformat())).json()
    assert default['periods'] == answers['weekday']['periods'] and default['comparison'] == answers['weekday']['comparison']


@pytest.mark.skipif(TODAY <= date(2026, 3, 1), reason='период февраля 2026 ещё в будущем')
def test_february_2026_against_2025_in_both_modes(client, app):
    # Сумма дня — от дня недели, 2026 год чуть выше, 14 февраля — праздник.
    def revenue(day):
        return 1000 * (day.weekday() + 1) + (5000 if (day.month, day.day) == (2, 14) else 0) + (100 if day.year == 2026 else 0)
    for day in days_of(date(2025, 1, 15), date(2026, 3, 5)):
        save(app.state.sales_store, day, revenue(day))
    start, end = date(2026, 2, 1), date(2026, 3, 1)
    data = {}
    for year in ('weekday', 'date'):
        data[year] = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret'),
                                params=dict(start=start.isoformat(), end=end.isoformat(), year=year)).json()
        current, previous, past = data[year]['periods']
        window = windows(start, end, year)[2]
        assert past['total'] == str(sum(revenue(day) for day in days_of(window['start'], window['end'])))
        assert (current['days_with_data'], previous['days_with_data'], past['days_with_data']) == (29, 29, 29)
        assert data[year]['complete'] is True
        # Пропусков нет — значит, кроме «iiko не подключён», предупреждать не о чем.
        assert data[year]['warnings'] == ['iiko не подключён: показана только история продаж, уже сохранённая в базе панели.']
    assert (data['weekday']['periods'][2]['start'], data['weekday']['periods'][2]['end']) == ('2025-02-02', '2025-03-02')
    assert (data['date']['periods'][2]['start'], data['date']['periods'][2]['end']) == ('2025-02-01', '2025-03-01')
    # 14-й день периода — 14.02.2026 (сб): по датам против праздника 14.02.2025 (пт),
    # по дням недели — против обычной субботы 15.02.2025.
    by_date, by_weekday = data['date']['periods'][2]['days'][13], data['weekday']['periods'][2]['days'][13]
    assert (by_date['day'], by_date['weekday'], by_date['revenue']) == ('2025-02-14', 'пт', '10000')
    assert (by_weekday['day'], by_weekday['weekday'], by_weekday['revenue']) == ('2025-02-15', 'сб', '6000')
    # Один праздничный день: по датам праздник сравнивается с праздником.
    holiday = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret'),
                         params=dict(start='2026-02-14', end='2026-02-14', year='date')).json()
    assert holiday['periods'][0]['total'] == '11100' and holiday['periods'][2]['total'] == '10000'
    assert holiday['comparison'][1]['diff'] == '1100' and holiday['comparison'][1]['direction'] == 'up'


def test_weeks_endpoint_rejects_an_unknown_year_mode(client):
    for value in ('month', '', 'DATE', 'Weekday'):
        answer = client.get('/api/analytics/revenue/weeks', params={'year': value}, auth=('analyst', 'secret'))
        assert answer.status_code == 422, value
        assert answer.json()['detail'] == 'Режим «год назад» — weekday (те же дни недели) или date (те же даты).'


def test_weeks_endpoint_by_date_with_days_and_with_today(client):
    # `days` без `start` и конец сегодня — режим работает и с этими формами периода.
    data = client.get('/api/analytics/revenue/weeks', params={'days': 14, 'year': 'date'}, auth=('analyst', 'secret')).json()
    start = YESTERDAY - timedelta(days=13)
    assert data['year'] == 'date' and data['window_days'] == 14
    assert data['periods'][2]['start'] == windows(start, YESTERDAY, 'date')[2]['start'].isoformat()
    assert len(data['periods'][2]['days']) == 14
    data = client.get('/api/analytics/revenue/weeks', params={'end': TODAY.isoformat(), 'year': 'date'},
                      auth=('analyst', 'secret')).json()
    assert data['includes_today'] is True and len(data['periods'][2]['days']) == 7
    assert data['periods'][2]['end'] == windows(TODAY - timedelta(days=6), TODAY, 'date')[2]['end'].isoformat()


def test_year_window_by_date_before_history_says_where_history_starts():
    end = date(2026, 10, 21)
    report = build_report(end - timedelta(days=6), end, {}, today=date(2026, 10, 22), history_start=date(2026, 10, 13),
                          configured=True, year='date')
    assert report['year'] == 'date'
    assert any(text.startswith('Год назад · те же даты (15.10.2025 – 21.10.2025)') and 'начинается с 13.10.2026' in text
               for text in report['warnings'])


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
    assert data['start'] == (YESTERDAY - timedelta(days=6)).isoformat()
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
        dict(against='previous', label='7 дней раньше', start=previous['start'], end=previous['end'],
             diff='210', percent=15.0, direction='up'),
        dict(against='year', label='Год назад · те же дни недели', start=year['start'], end=year['end'],
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


def test_no_history_means_no_totals_and_no_comparison(tmp_path):
    # Начало истории — месяц назад, чтобы «год назад» гарантированно было до него.
    settings = Settings(dashboard_panel_users=USERS, data_dir=tmp_path, sales_history_start=TODAY - timedelta(days=30))
    app = create_app(settings, expense_db_path=tmp_path / 'cashier.sqlite3', accountant_db_path=tmp_path / 'accountant.sqlite3',
                     director_db_path=tmp_path / 'director.sqlite3', founder_db_path=tmp_path / 'founder.sqlite3')
    with TestClient(app, base_url='http://dashboard.example.com', client=('203.0.113.10', 50000)) as client:
        data = client.get('/api/analytics/revenue/weeks', auth=('analyst', 'secret')).json()
    assert [period['total'] for period in data['periods']] == [None, None, None]
    assert all(item['direction'] == 'unknown' and item['diff'] is None for item in data['comparison'])
    assert data['updated_at'] is None and data['complete'] is False
    # Без iiko на стенде — честное «данных нет», по окну; год назад — до начала истории.
    assert any(text.startswith('Выбранный период (') and 'данных нет' in text for text in data['warnings'])
    assert any(text.startswith('Год назад · те же дни недели (') and 'история продаж в базе начинается с' in text for text in data['warnings'])
    assert all('ещё загружаются' not in text for text in data['warnings'])  # iiko не подключён — ничего не грузится


def test_end_can_be_moved_but_not_into_the_future(client, app):
    anchor = YESTERDAY - timedelta(days=30)
    seed(app.state.sales_store, anchor, lambda offset: 50)
    data = client.get('/api/analytics/revenue/weeks', params={'end': anchor.isoformat()},
                      auth=('analyst', 'secret')).json()
    assert data['end'] == anchor.isoformat() and data['periods'][0]['total'] == '350'
    # Явный период: 3 дня — «раньше» тоже 3 дня, прямо перед ними.
    data = client.get('/api/analytics/revenue/weeks', params={'start': (anchor - timedelta(days=2)).isoformat(),
                                                             'end': anchor.isoformat()}, auth=('analyst', 'secret')).json()
    assert data['window_days'] == 3 and data['periods'][0]['total'] == '150'
    assert data['periods'][1]['label'] == '3 дня раньше' and data['periods'][1]['total'] == '150'
    assert data['periods'][1]['end'] == (anchor - timedelta(days=3)).isoformat()
    assert len(data['periods'][2]['days']) == 3
    # `days` без `start`: 14 дней по `end`.
    data = client.get('/api/analytics/revenue/weeks', params={'end': anchor.isoformat(), 'days': 14},
                      auth=('analyst', 'secret')).json()
    assert data['window_days'] == 14 and data['periods'][1]['label'] == '14 дней раньше'
    for params, detail in ((dict(start=anchor.isoformat(), end=(anchor - timedelta(days=1)).isoformat()),
                            'Дата начала должна быть не позже даты конца.'),
                           (dict(start=(anchor - timedelta(days=MAX_DAYS)).isoformat(), end=anchor.isoformat()),
                            f'Период — не длиннее {MAX_DAYS} дней.'),
                           (dict(end=anchor.isoformat(), days=0), f'Период — от 1 до {MAX_DAYS} дней.')):
        answer = client.get('/api/analytics/revenue/weeks', params=params, auth=('analyst', 'secret'))
        assert answer.status_code == 422 and answer.json()['detail'] == detail, params
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
    report = build_report(end - timedelta(days=6), end, {}, today=date(2026, 10, 22), history_start=date(2026, 10, 13),
                          configured=True)
    assert report['periods'][0]['days'][0]['weekday'] == 'чт'
    assert report['history_start'] == '2026-10-13'
    assert any(text.startswith('Год назад · те же дни недели (16.10.2025 – 22.10.2025)') and 'начинается с 13.10.2026' in text
               for text in report['warnings'])
    # Дни после начала истории при подключённом iiko названы загружающимися.
    assert any(text.startswith('Выбранный период (15.10 – 21.10)') and 'ещё загружаются из iiko' in text
               for text in report['warnings'])
    assert [period['key'] for period in report['periods']] == ['current', 'previous', 'year']
    assert report['timezone'] == 'Asia/Tashkent' and report['source'].startswith('iiko OLAP SALES')


# ── Вся история (T-592) ───────────────────────────────────────────────────

def save(store, day, retro, *, closed=True, school=0, banquet=0):
    total = Decimal(retro) + Decimal(school) + Decimal(banquet)
    store.save_day(day, [], [], fetched_at=datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=23),
                   revision='r1', closed=closed, revenue=total, iiko_revenue=total,
                   status='ok', reconciliation=reconciliation(retro, school, banquet))


def test_history_buckets_follow_the_calendar_and_flag_clipped_edges():
    # История с четверга 01.10 по вторник 13.10: первая неделя обрезана началом
    # истории, последняя — сегодняшним днём; 05.10 не загружен, 12.10 не закрыт.
    first, today = date(2026, 10, 1), date(2026, 10, 13)
    states = {}
    for offset in range(13):
        day = first + timedelta(days=offset)
        if day == date(2026, 10, 5):
            continue
        states[day] = dict(day=day, closed=day != date(2026, 10, 12), reconciliation=reconciliation(100, school=50),
                           fetched_at=datetime.combine(day, datetime.min.time(), TZ))
    report = history_report(first, today, states, group='week', history_start=first)
    weeks = report['buckets']
    assert [(week['start'], week['end'], week['days'], week['days_with_data']) for week in weeks] == [
        ('2026-10-01', '2026-10-04', 4, 4), ('2026-10-05', '2026-10-11', 7, 6), ('2026-10-12', '2026-10-13', 2, 2)]
    assert [week['label'] for week in weeks] == ['28.09 – 04.10', '05.10 – 11.10', '12.10 – 18.10']
    assert [week['total'] for week in weeks] == ['400', '600', '200']  # школа в Retro не входит
    assert [(week['clipped'], week['partial'], week['complete']) for week in weeks] == [
        (True, False, False), (False, False, False), (True, True, False)]
    assert weeks[1]['average'] == '100'
    assert (report['total'], report['days'], report['days_with_data'], report['average']) == ('1200', 13, 12, '100')
    assert report['warnings'] == ['Нет данных за 1 день из 13 — они ещё загружаются из iiko, по дню в минуту; в суммы пока не вошли.']
    months = history_report(first, today, states, group='month', history_start=first)['buckets']
    assert [(month['label'], month['total'], month['clipped']) for month in months] == [('окт 2026', '1200', True)]
    days = history_report(first, today, states, group='day', history_start=first)['buckets']
    assert len(days) == 13 and days[0]['label'] == 'чт 01.10' and days[4]['total'] is None and days[4]['average'] is None
    assert days[0]['complete'] is True and days[-1]['partial'] is True


def test_history_months_cross_the_year_with_full_month_bounds():
    first, today = date(2025, 11, 15), date(2026, 2, 3)
    states = {day: dict(day=day, closed=True, reconciliation=reconciliation(10), fetched_at=datetime.combine(day, datetime.min.time(), TZ))
              for day in (first + timedelta(days=offset) for offset in range((today - first).days))}
    months = history_report(first, today, states, group='month', history_start=first)['buckets']
    assert [(m['label'], m['start'], m['end'], m['days']) for m in months] == [
        ('ноя 2025', '2025-11-15', '2025-11-30', 16), ('дек 2025', '2025-12-01', '2025-12-31', 31),
        ('янв 2026', '2026-01-01', '2026-01-31', 31), ('фев 2026', '2026-02-01', '2026-02-03', 3)]
    assert [m['complete'] for m in months] == [False, True, True, False]
    # Сегодняшний день не загружен — последний месяц неполный по данным.
    assert months[-1]['days_with_data'] == 2
    weeks = history_report(first, today, states, group='week', history_start=first)['buckets']
    assert weeks[0]['label'] == '10.11.2025 – 16.11.2025' and weeks[-1]['label'] == '02.02 – 08.02'


def test_history_endpoint_starts_at_the_first_stored_day(client, app):
    store = app.state.sales_store
    start = app.state.sales.settings.sales_history_start
    early = start - timedelta(days=3)
    for offset in range(3):
        save(store, early + timedelta(days=offset), 1000, banquet=5000)
    save(store, YESTERDAY, 2000, closed=False)
    data = client.get('/api/analytics/revenue/history?group=day', auth=('analyst', 'secret')).json()
    assert data['start'] == early.isoformat() and data['end'] == TODAY.isoformat() and data['group'] == 'day'
    assert data['direction'] == 'retro' and data['days'] == (TODAY - early).days + 1
    assert data['days_with_data'] == 4 and data['total'] == '5000'
    assert data['buckets'][0]['total'] == '1000'
    yesterday = next(bucket for bucket in data['buckets'] if bucket['start'] == YESTERDAY.isoformat())
    assert yesterday['partial'] is True and yesterday['complete'] is False
    assert data['warnings'][0].startswith('iiko не подключён')
    assert client.get('/api/analytics/revenue/history', auth=('analyst', 'secret')).json()['group'] == 'week'
    assert client.get('/api/analytics/revenue/history?group=year', auth=('analyst', 'secret')).status_code == 422


def test_history_endpoint_without_sales_says_so(client):
    data = client.get('/api/analytics/revenue/history?group=month', auth=('analyst', 'secret')).json()
    assert data['total'] is None and data['average'] is None and data['days_with_data'] == 0
    assert all(bucket['total'] is None for bucket in data['buckets'])
    assert data['updated_at'] is None


@pytest.mark.parametrize('user,status', [('analyst', 200), ('boss', 200), ('director', 403), ('founder', 403), ('cashier', 403)])
def test_history_endpoint_follows_the_panel(client, user, status):
    assert client.get('/api/analytics/revenue/history', auth=(user, 'secret')).status_code == status


def test_sales_history_starts_with_iiko_but_accounting_boundary_does_not_move(client):
    """T-592: история продаж — с первого дня продаж Retro в iiko (16.09.2025);
    граница учёта бухгалтера и кассира от неё не зависит и остаётся 13.10.2026."""
    from retro.accounting_period import ACCOUNTING_START
    from retro.config import SALES_HISTORY_START, parse_sales_history_start
    assert SALES_HISTORY_START == date(2025, 9, 16) == parse_sales_history_start('')
    assert Settings().sales_history_start == SALES_HISTORY_START
    assert ACCOUNTING_START == date(2026, 10, 13)
    before = (ACCOUNTING_START - timedelta(days=1)).isoformat()
    for path in (f'/api/accountant/day?date={before}', f'/api/cashier/expenses?date={before}'):
        answer = client.get(path, auth=('boss', 'secret'))
        assert answer.status_code == 422 and 'Учёт доступен с 13.10.2026' in answer.json()['detail'], path
