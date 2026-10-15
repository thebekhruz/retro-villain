"""KPI официантов: правила чека и расчёт премии (retro/modules/kpi/rules.py) — без iiko и базы."""
from datetime import date

import pytest

from retro.modules.kpi.rules import (
    HALL, KpiError, Line, Order, Stats, aggregate, baseline_window, compute, default_settings, exclusion_reasons,
    login_counts, month_bounds, next_bounds, normalize_scheme, normalize_targets, week_bounds,
)

TEA, SOUP, COLA = 'tea-id', 'soup-id', 'cola-id'
TARGETS = [dict(key='tea', title='Фирменные чаи', group='напиток', target100=5.0, margin=36000, dishes=[(TEA, 'ЧАЙ Фруктовый')]),
           dict(key='soup', title='Эзогелин', group='блюдо', target100=4.0, margin=32000, dishes=[(SOUP, 'Суп Эзогелин')])]
SCHEME = dict(share=0.25, personal=0.7, team=0.3, cap=1_000_000, min_checks=2, min_shifts=1,
              baseline_weeks=4, baseline_min_checks=2, netting=True)


def order(waiter='Алина', day=date(2026, 10, 19), items=(), **over):
    lines = tuple(Line(dish_id, name, 1.0, 20000.0, 5000.0) for dish_id, name in (items or ((COLA, 'Кола'),)))
    base = dict(id=f'{waiter}-{day}-{len(items)}-{id(over)}', day=day, register='Kassa-FiscalBox1', section='Ресторан',
                waiters=frozenset({waiter}), lines=lines)
    base.update(over)
    return Order(**base)


def test_exclusion_reasons_cover_every_rule():
    assert exclusion_reasons(order()) == []
    assert exclusion_reasons(order(register='GL-Kassa-Oksbrich', section='Зал')) == ['school']
    assert exclusion_reasons(order(section='Бехруз (Свадьба)')) == ['banquet']
    assert exclusion_reasons(order(items=((COLA, 'СВАДЬБА плов 1 кг'),))) == ['banquet']
    assert exclusion_reasons(order(items=((COLA, 'Яндекс ДОСТАВКА кола'),))) == ['yandex']
    assert exclusion_reasons(order(pay_types=frozenset({'Яндех Еда'}))) == ['yandex']
    assert exclusion_reasons(order(pay_types=frozenset({'я Узум Тезкор'}))) == ['uzum']
    assert exclusion_reasons(order(items=((COLA, 'Аренда зала'),))) == ['rent']
    assert exclusion_reasons(order(section='Вынос')) == ['takeaway']
    assert exclusion_reasons(order(section='Доставка')) == ['delivery']
    assert exclusion_reasons(order(order_deleted=True)) == ['order_deleted']
    assert exclusion_reasons(order(deleted_lines=True)) == ['deleted_lines']
    assert exclusion_reasons(order(discount_types=frozenset({'Скидка 20%'}))) == ['discount']
    assert exclusion_reasons(order(noncash_types=frozenset({'Счет Шефа'}))) == ['free']
    assert exclusion_reasons(order(noncash_types=frozenset({'Uzum'}))) == []
    assert exclusion_reasons(order(waiters=frozenset({'Алина', 'Нурик'}))) == ['multi_waiter']
    assert exclusion_reasons(order(waiters=frozenset({'Жахонгир Менеджер'}))) == ['service_login']
    assert exclusion_reasons(order(waiters=frozenset({'Касса Оксбрич'}))) == ['service_login']
    free = order(lines=(Line(COLA, 'Кола', 1, 0.0, 5000.0),))
    assert exclusion_reasons(free) == ['empty']


def test_aggregate_counts_only_included_hall_checks():
    orders = [order('Алина', items=((TEA, 'Чай'), (COLA, 'Кола'))),
              order('Алина', day=date(2026, 10, 20), items=((TEA, 'Чай'), (TEA, 'Чай'))),
              order('Нурик', items=((SOUP, 'Суп'),)),
              order('Нурик', section='Вынос', items=((SOUP, 'Суп'),)),            # вынос — вне KPI
              order('Нурик', deleted_lines=True, items=((SOUP, 'Суп'),))]         # удаления — вне KPI
    stats = aggregate(orders, TARGETS)
    assert stats['Алина'].checks == 2 and stats['Алина'].quantity['tea'] == 3 and stats['Алина'].days == {date(2026, 10, 19), date(2026, 10, 20)}
    assert stats['Алина'].rate('tea') == 150.0
    assert stats['Алина'].by_day['2026-10-20']['tea'] == 2
    assert stats['Нурик'].checks == 1 and stats['Нурик'].quantity['soup'] == 1
    assert stats[HALL].checks == 3 and stats[HALL].quantity['tea'] == 3
    # Опт-ин логинов: неотмеченный официант не идёт ни в рейтинг, ни в итоги зала.
    marked = aggregate(orders, TARGETS, {'Алина'})
    assert set(marked) == {'Алина', HALL} and marked[HALL].checks == 2
    assert login_counts(orders) == {'Алина': 2, 'Нурик': 1}


def stats(checks, days, **qty):
    item = Stats(checks=checks, days=set(days))
    for key, value in qty.items():
        item.quantity[key] = value
    return item


def test_compute_pays_share_of_uplift_over_own_base_with_team_pool():
    # База: Алина 2 чая на 100 чеков, Нурик 10 (своя). Зал: чай 6, суп 1.
    base = {'Алина': stats(100, [1], tea=2, soup=2), 'Нурик': stats(100, [1], tea=10, soup=0), HALL: stats(200, [1, 2], tea=12, soup=2)}
    # Неделя: Алина 10 чеков, 1 чай (10 на 100: +8 → 0.8 порции × 36 000 = 28 800), суп 0 (−2 на 100 → −0.2 × 32 000 = −6 400).
    now = {'Алина': stats(10, [1, 2], tea=1, soup=0), 'Нурик': stats(10, [1], tea=0, soup=0),
           'Новичок': stats(10, [1], tea=1, soup=1), HALL: stats(30, [1, 2], tea=2, soup=1)}
    result = compute(now, base, TARGETS, SCHEME, days_total=7)
    rows = {row['waiter']: row for row in result['rows']}
    assert rows['Алина']['uplift'] == 22400 and rows['Алина']['own_base'] and rows['Алина']['eligible']
    assert rows['Нурик']['uplift'] == -36000 and rows['Нурик']['uplift_positive'] == 0
    # Новичку база зала: чай 6→10 (+0.4 × 36 000 = 14 400), суп 1→10 (+0.9 × 32 000 = 28 800).
    assert not rows['Новичок']['own_base'] and rows['Новичок']['uplift'] == 43200
    team_uplift = 22400 + 43200
    assert result['team_uplift'] == team_uplift and result['net_uplift'] == 22400 - 36000 + 43200
    # Фонд 25 %: 70 % лично от своего прироста, 30 % — пул по чекам (у всех по 10 чеков из 30).
    assert rows['Алина']['personal'] == round(0.25 * 0.7 * 22400)
    assert rows['Алина']['team'] == round(0.25 * 0.3 * team_uplift * 10 / 30)
    assert rows['Нурик']['premium'] == rows['Нурик']['team'] > 0
    assert result['fund'] == sum(row['premium'] for row in result['rows'])
    assert result['restaurant_net'] == result['net_uplift'] - result['fund']
    assert result['participants'] == 3 and result['rows'][0]['waiter'] == 'Новичок'
    assert result['hall']['rates']['tea'] == dict(base=6.0, now=6.67, target=5.0)
    assert result['goal'] > 0
    # Цель в штуках для кольца: ожидаемые чеки 200 / 2 дня × 7 = 700; цели 5 + 4 на 100 → 63 порции; продано 3.
    assert result['expected_checks'] == 700 and result['goal_qty'] == 63 and result['sold'] == 3
    assert rows['Алина']['sold'] == 1 and rows['Алина']['goal_qty'] == 63.0, 'своя база 100 чеков за 1 день × 7'
    assert rows['Новичок']['goal_qty'] == 6.3, 'без своей базы — по текущему темпу: 10 чеков за день × 7'


def test_compute_without_netting_counts_only_gains_and_applies_cap():
    base = {'Алина': stats(100, [1], tea=2, soup=2), HALL: stats(100, [1], tea=2, soup=2)}
    now = {'Алина': stats(100, [1, 2, 3], tea=100, soup=0), HALL: stats(100, [1], tea=100, soup=0)}
    netted = compute(now, base, TARGETS, SCHEME)
    gross = compute(now, base, TARGETS, dict(SCHEME, netting=False))
    assert netted['rows'][0]['uplift'] == 98 * 36000 - 2 * 32000
    assert gross['rows'][0]['uplift'] == 98 * 36000
    capped = compute(now, base, TARGETS, dict(SCHEME, cap=100000))
    assert capped['rows'][0]['premium'] == 100000
    monthly = compute(now, base, TARGETS, dict(SCHEME, cap=100000), days_total=30)
    assert monthly['cap'] == 400000


def test_threshold_excludes_small_weeks_from_the_fund():
    base = {HALL: stats(100, [1], tea=2, soup=2)}
    now = {'Алина': stats(1, [1], tea=1, soup=0), HALL: stats(1, [1], tea=1, soup=0)}
    result = compute(now, base, TARGETS, SCHEME)
    row = result['rows'][0]
    assert not row['eligible'] and row['premium'] == 0 and row['uplift'] > 0 and result['fund'] == 0


def test_scheme_and_targets_validation():
    assert normalize_scheme({})['share'] == 0.25
    assert normalize_scheme(dict(share='0.3', netting=False))['netting'] is False
    with pytest.raises(KpiError):
        normalize_scheme(dict(share=0.6))
    with pytest.raises(KpiError):
        normalize_scheme(dict(personal=0.5, team=0.3))
    with pytest.raises(KpiError):
        normalize_scheme(dict(cap=-1))
    targets = normalize_targets([dict(title='Чай', group='напиток', target100='5', dishes=[dict(id=TEA, name='Чай')])])
    assert targets[0]['dishes'] == [(TEA, 'Чай')] and targets[0]['margin'] is None and targets[0]['key'] == 't1'
    for bad in ([], [dict(title='', dishes=[(TEA, 'x')])], [dict(title='a', group='x', dishes=[(TEA, 'x')])],
                [dict(title='a', dishes=[])], [dict(title='a', dishes=[(TEA, 'x')]), dict(title='b', dishes=[(TEA, 'y')])],
                [dict(title='a', dishes=[(TEA, 'x')], target100=200)], [dict(title='a', dishes=[(TEA, 'x')], margin=0)]):
        with pytest.raises(KpiError):
            normalize_targets(bad)
    defaults = default_settings()
    assert len(defaults['targets']) == 12 and all(t['margin'] > 0 for t in defaults['targets'])
    assert normalize_targets(defaults['targets'])[0]['key'] == 'ezogelin'


def test_period_helpers():
    assert week_bounds(date(2026, 10, 22)) == (date(2026, 10, 19), date(2026, 10, 25))
    assert month_bounds(date(2026, 11, 10)) == (date(2026, 11, 1), date(2026, 11, 30))
    assert next_bounds('week', date(2026, 10, 25)) == (date(2026, 10, 26), date(2026, 11, 1))
    assert next_bounds('month', date(2026, 10, 31)) == (date(2026, 11, 1), date(2026, 11, 30))
    assert baseline_window(date(2026, 10, 19), 4) == (date(2026, 9, 21), date(2026, 10, 18))


def test_store_renames_reserved_end_column_in_old_databases(tmp_path):
    """База, созданная до переименования (колонки start/end), открывается и данные остаются на месте."""
    import sqlite3
    from retro.modules.kpi.store import KpiStore
    path = tmp_path / 'director.sqlite3'
    with sqlite3.connect(path) as old:
        old.execute('''CREATE TABLE kpi_periods (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
            status TEXT NOT NULL, settings_json TEXT NOT NULL, snapshot_json TEXT, created_at TEXT NOT NULL,
            created_by TEXT, closed_at TEXT, closed_by TEXT, payouts_json TEXT)''')
        old.execute("INSERT INTO kpi_periods (kind, start, end, status, settings_json, created_at) "
                    "VALUES ('week', '2026-10-19', '2026-10-25', 'open', '{}', 'x')")
    store = KpiStore(path)
    period = store.list()[0]
    assert period['start'] == '2026-10-19' and period['end'] == '2026-10-25' and period['settings'] == {}
    with sqlite3.connect(path) as new:
        columns = {row[1] for row in new.execute('PRAGMA table_info("kpi_periods")')}
    assert {'start_day', 'end_day'} <= columns and not {'start', 'end'} & columns
    assert KpiStore(path).list()[0]['id'] == period['id'], 'повторное открытие не ломает мигрированную базу'
