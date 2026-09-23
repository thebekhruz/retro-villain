from datetime import date, timedelta
from decimal import Decimal
from dataclasses import replace

import pytest

from retro.modules.cashier.service import DataError
from retro.modules.director.models import SalesRow, build_snapshot, completed_period, payment_total
from retro.modules.founder.models import PaymentRow


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


def test_sales_and_zero_revenue_purposes_reconcile_for_items_and_waiters():
    rows = []
    for offset in range(10):
        day = date(2026, 9, 8) + timedelta(days=offset)
        rows.extend([
            sale(day=day, quantity='234', revenue='2808000', cost='1343137.55'),
            replace(sale(day=day, quantity='52', revenue='0', cost='280048.13'),
                    non_cash_payment_type='Счет Шефа'),
            replace(sale(day=day, quantity='10', revenue='0', cost='57167.12'),
                    non_cash_payment_type='Дегустация'),
            replace(sale(day=day, quantity='1', revenue='0', cost='2000'),
                    non_cash_payment_type='Другая причина'),
        ])
    report = build_snapshot(rows, CATEGORIES, date(2026, 9, 8), date(2026, 9, 17))
    metric = report.item_metrics['all']['Плов']
    assert report.item_metrics['retro']['Плов'] == report.waiter_metrics['Олег'] == metric
    assert metric.breakdown['sales'].quantity == 2340
    assert metric.breakdown['chef'].quantity == 520
    assert metric.breakdown['tasting'].quantity == 100
    assert metric.breakdown['other_zero'].quantity == 10
    for attr in ('quantity', 'revenue', 'cost', 'gross_profit'):
        assert getattr(metric, attr) == sum(getattr(part, attr) for part in metric.breakdown.values())
    assert metric.breakdown['sales'].gross_profit == Decimal('14648624.50')
    assert report.json()['item_metrics']['all']['Плов']['breakdown']['chef']['cost'] == '2800481.30'


@pytest.mark.parametrize('purpose,revenue,expected', [
    ('  Счёт Шефа  ', '0', 'chef'), ('ДЕГУСТАЦИЯ', '0', 'tasting'),
    ('', '0', 'other_zero'), ('Комплимент', '0', 'other_zero'),
    ('Счет Шефа', '100', 'sales'),
])
def test_classification_uses_actual_revenue_and_known_iiko_purpose(purpose, revenue, expected):
    from retro.modules.director.models import sale_kind
    assert sale_kind(replace(sale(revenue=revenue), non_cash_payment_type=purpose)) == expected


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


def test_authoritative_yandex_payments_are_not_reduced_by_excluded_dish_groups():
    days = [date(2026, 9, 8) + timedelta(days=offset) for offset in range(10)]
    rows = [sale(category='ДОСТАВКА ЯНДЕКС', day=day, order_id=str(offset))
            for offset, day in enumerate(days)]
    snapshot = build_snapshot(
        rows, {}, date(2026, 9, 8), date(2026, 9, 17),
        excluded_groups={'ДОСТАВКА ЯНДЕКС'}, yandex_revenue=Decimal('28004000'))

    assert snapshot.cash_total == Decimal(0)
    assert snapshot.item_metrics['yandex'] == {}
    assert snapshot.yandex_revenue == Decimal('28004000')


def test_yandex_payment_total_normalizes_iiko_alias_and_applies_direction_rules():
    rows = [
        PaymentRow(date(2026, 9, 13), 'Kassa-FiscalBox1', 'Ресторан',
                   'Доставка', 'Яндех Еда', Decimal('27000000')),
        PaymentRow(date(2026, 9, 14), 'GL-Kassa-Oksbrich', 'Зал',
                   'Обед', 'Яндекс Еда', Decimal('1004000')),
        PaymentRow(date(2026, 9, 15), 'Kassa-FiscalBox1', 'Бехруз (Свадьба)',
                   'Старая банкетная строка', 'Яндекс Еда', Decimal('999999')),
    ]

    assert payment_total(rows, 'Яндекс Еда') == Decimal('28004000')


def test_banquet_is_counted_by_bekhruz_marker_in_dish_name():
    rows = [sale(day=date(2026, 9, 8) + timedelta(days=offset), order_id=str(offset),
                 item='СВАДЬБА Салат Оливье (Бехруз)' if offset == 0 else f'Плов {offset}',
                 section='Бехруз (Свадьба)' if offset == 1 else 'Ресторан')
            for offset in range(10)]
    snapshot = build_snapshot(rows, {}, date(2026, 9, 8), date(2026, 9, 17))
    assert snapshot.cash_total == Decimal('1800000')
    assert snapshot.item_metrics['banquet']['СВАДЬБА Салат Оливье (Бехруз)'].revenue == Decimal('200000')
    assert 'Плов 1' not in snapshot.item_metrics['all']
