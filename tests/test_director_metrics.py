from datetime import date, timedelta
from decimal import Decimal

import pytest

from retro.modules.cashier.service import DataError
from retro.modules.director.models import SalesRow, build_snapshot, completed_period


CATEGORIES = {'Основное меню': 'menu', 'Десерты': 'dessert', 'Напитки': 'drink'}


def sale(*, register='Kassa-FiscalBox1', section='Ресторан', payment='Яндекс Еда',
         item='Плов', category='Основное меню', quantity='2', revenue='200000',
         cost='80000', waiter='Олег', order_id='o-1', day=date(2026, 9, 8)):
    return SalesRow(day, register, section, payment, item, category,
                    Decimal(quantity), Decimal(revenue), Decimal(cost), waiter, order_id)


def test_completed_period_excludes_current_tashkent_day():
    assert completed_period(date(2026, 9, 18)) == (date(2026, 9, 8), date(2026, 9, 17))


def test_yandex_overlaps_cash_direction_without_double_counting():
    rows = [sale(order_id=f'o-{offset}', item=f'Плов {offset}',
                 payment='Яндекс Еда' if offset == 0 else 'UzCard',
                 register='Kassa-FiscalBox1' if offset % 2 == 0 else 'GL-Kassa-Oksbrich',
                 day=date(2026, 9, 8) + timedelta(days=offset))
            for offset in range(10)]
    snapshot = build_snapshot(rows, CATEGORIES, date(2026, 9, 8), date(2026, 9, 17))
    assert snapshot.cash_total == Decimal('2000000')
    assert snapshot.yandex_revenue == Decimal('200000')
    assert snapshot.item_metrics['all']['Плов 0'].margin_percent == Decimal('60.00')
    assert snapshot.waiter_metrics['Олег'].revenue == Decimal('2000000')


def test_missing_waiter_is_rejected():
    days = [date(2026, 9, 8) + timedelta(days=offset) for offset in range(10)]
    with pytest.raises(DataError, match='официант'):
        build_snapshot([sale(waiter='', day=day, order_id=str(offset))
                        for offset, day in enumerate(days)],
                       CATEGORIES, date(2026, 9, 8), date(2026, 9, 17))


def test_empty_category_map_includes_all_groups_and_explicit_map_still_fails_closed():
    days = [date(2026, 9, 8) + timedelta(days=offset) for offset in range(10)]
    rows = [sale(category='Миллий', day=day, order_id=str(offset))
            for offset, day in enumerate(days)]
    automatic = build_snapshot(rows, {}, date(2026, 9, 8), date(2026, 9, 17))
    assert automatic.cash_total == Decimal('2000000')
    with pytest.raises(DataError, match='категор'):
        build_snapshot(rows, {'Десерты': 'dessert'}, date(2026, 9, 8), date(2026, 9, 17),
                       excluded_groups={'Контейнеры'})
    excluded = build_snapshot([sale(category='Контейнеры', day=day, order_id=str(offset))
                               for offset, day in enumerate(days)], {'Десерты': 'dessert'},
                              date(2026, 9, 8), date(2026, 9, 17), excluded_groups={'Контейнеры'})
    assert excluded.cash_total == Decimal(0)


def test_banquet_is_counted_by_bekhruz_marker_in_dish_name():
    rows = [sale(day=date(2026, 9, 8) + timedelta(days=offset), order_id=str(offset),
                 item='СВАДЬБА Салат Оливье (Бехруз)' if offset == 0 else f'Плов {offset}',
                 section='Бехруз (Свадьба)' if offset == 1 else 'Ресторан')
            for offset in range(10)]
    snapshot = build_snapshot(rows, {}, date(2026, 9, 8), date(2026, 9, 17))
    assert snapshot.cash_total == Decimal('1800000')
    assert snapshot.item_metrics['banquet']['СВАДЬБА Салат Оливье (Бехруз)'].revenue == Decimal('200000')
    assert 'Плов 1' not in snapshot.item_metrics['all']
