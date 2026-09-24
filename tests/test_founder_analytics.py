from datetime import date, datetime
from decimal import Decimal

import pytest

from retro.modules.cashier.service import DataError
from retro.modules.founder.models import (
    PaymentRow,
    RevenueRow,
    build_analytics,
    classify_direction,
)


START = date(2026, 8, 31)
END = date(2026, 9, 8)


def revenue(day, amount, *, register='Kassa-FiscalBox1', section='Ресторан', item='Плов'):
    return RevenueRow(day, register, section, item, Decimal(str(amount)))


def payment(day, name, amount, *, register='Kassa-FiscalBox1', section='Ресторан', item='Плов'):
    return PaymentRow(day, register, section, item, name, Decimal(str(amount)))


def test_direction_classification_is_exclusive_and_rejects_unknown_values():
    assert classify_direction('Kassa-FiscalBox1', 'Ресторан', 'Плов') == 'retro'
    assert classify_direction('GL-Kassa-Oksbrich', 'Зал', 'Обед') == 'school'
    assert classify_direction(
        'Kassa-FiscalBox1', 'Ресторан', 'Салат Оливье (БЕХРУЗ)') == 'banquet'
    assert classify_direction(
        'Kassa-FiscalBox1', 'Бехруз (Свадьба)', 'Аренда зала') is None

    with pytest.raises(DataError, match='неизвестная касса'):
        classify_direction('New-Kassa', 'Ресторан', 'Плов')
    with pytest.raises(DataError, match='новое отделение Бехруз'):
        classify_direction('Kassa-FiscalBox1', 'Бехруз VIP', 'Плов')
    with pytest.raises(DataError, match='неизвестное отделение'):
        classify_direction('Kassa-FiscalBox1', 'Новый зал', 'Плов')
    with pytest.raises(DataError, match='неизвестное отделение'):
        classify_direction('GL-Kassa-Oksbrich', 'Новый школьный зал', 'Обед')


@pytest.mark.parametrize(
    ('granularity', 'expected_length', 'expected_edges'),
    [
        ('day', 9, (('2026-08-31', '2026-08-31'), ('2026-09-08', '2026-09-08'))),
        ('week', 2, (('2026-08-31', '2026-09-06'), ('2026-09-07', '2026-09-08'))),
        ('month', 2, (('2026-08-31', '2026-08-31'), ('2026-09-01', '2026-09-08'))),
    ],
)
def test_time_groups_use_tashkent_calendar_and_clamp_edges(
        granularity, expected_length, expected_edges):
    rows = [revenue(START, 100), revenue(date(2026, 9, 1), 200), revenue(END, 300)]
    payments = [payment(row.day, 'UzCard', row.amount) for row in rows]

    result = build_analytics(rows, payments, START, END, granularity,
                             ('retro',), now=datetime(2026, 9, 9, 10, 0))

    groups = [(group['start'], group['end']) for group in result['revenue_series']]
    assert len(groups) == expected_length
    assert (groups[0], groups[-1]) == expected_edges


def test_mixed_payments_are_not_double_counted_and_are_split_by_direction():
    day = date(2026, 9, 7)
    revenues = [
        revenue(day, 100),
        revenue(day, 40, register='GL-Kassa-Oksbrich', section='Зал'),
        revenue(day, 60, section='Бехруз (Свадьба)', item='Салат (Бехруз)'),
    ]
    payments = [
        payment(day, 'UzCard', 60),
        payment(day, 'Демо', 40),
        payment(day, 'UzCard', 40, register='GL-Kassa-Oksbrich', section='Зал'),
        payment(day, 'Наличные (Инкасса QR)', 60,
                section='Бехруз (Свадьба)', item='Салат (Бехруз)'),
    ]

    result = build_analytics(revenues, payments, day, day, 'day',
                             ('retro', 'school', 'banquet'),
                             now=datetime(2026, 9, 8, 8, 0))

    assert result['totals'] == {
        'retro': '100', 'school': '40', 'banquet': '60', 'selected': '200'}
    assert result['payment_total'] == '200'
    assert result['reconciled'] is True
    assert result['discrepancy'] == '0'
    assert result['payment_summary'] == [
        {'name': 'UzCard', 'amount': '100', 'share_percent': '50.00'},
        {'name': 'Наличные (Инкасса QR)', 'amount': '60', 'share_percent': '30.00'},
        {'name': 'Демо', 'amount': '40', 'share_percent': '20.00'},
    ]
    assert result['payment_series'][0]['directions']['retro'] == {
        'Демо': '40', 'UzCard': '60'}
    assert result['payment_series'][0]['directions']['school'] == {'UzCard': '40'}
    assert result['payment_series'][0]['directions']['banquet'] == {
        'Наличные (Инкасса QR)': '60'}


def test_payment_mismatch_is_returned_as_unverified_warning():
    day = date(2026, 9, 7)
    result = build_analytics([revenue(day, 100)], [payment(day, 'UzCard', 98)],
                             day, day, 'day', ('retro',),
                             now=datetime(2026, 9, 8, 8, 0))

    assert result['reconciled'] is False
    assert result['discrepancy'] == '-2'
    assert result['warnings'][:1] == [
        'Оплаты расходятся с выручкой на 2 сум. Данные не считаются сверенными.'
    ]


def test_payment_share_uses_payment_total_when_one_sum_difference_is_tolerated():
    day = date(2026, 9, 7)
    result = build_analytics([revenue(day, 100)], [payment(day, 'UzCard', 99)],
                             day, day, 'day', ('retro',),
                             now=datetime(2026, 9, 8, 8, 0))

    assert result['reconciled'] is True
    assert result['payment_summary'] == [
        {'name': 'UzCard', 'amount': '99', 'share_percent': '100.00'}]


def test_zero_total_has_no_payment_share_and_current_day_is_marked_incomplete():
    day = date(2026, 9, 8)
    result = build_analytics([revenue(day, 0)], [payment(day, 'Демо', 0)],
                             day, day, 'day', ('retro',),
                             now=datetime(2026, 9, 8, 12, 30))

    assert result['includes_current_day'] is True
    assert result['revenue_series'][0]['incomplete'] is True
    assert result['payment_summary'][0]['share_percent'] is None


def test_refunds_reduce_sales_and_payment_totals_instead_of_being_discarded():
    day = date(2026, 9, 7)
    result = build_analytics(
        [revenue(day, 100), revenue(day, -20)],
        [payment(day, 'UzCard', 100), payment(day, 'UzCard', -20)],
        day, day, 'day', ('retro',), now=datetime(2026, 9, 8, 8, 0),
    )

    assert result['totals']['retro'] == '80'
    assert result['payment_total'] == '80'
    assert result['payment_summary'] == [
        {'name': 'UzCard', 'amount': '80', 'share_percent': '100.00'}]


def test_unknown_payment_type_is_shown_separately_with_warning():
    day = date(2026, 9, 7)
    result = build_analytics(
        [revenue(day, 100)], [payment(day, 'Crypto', 100)],
        day, day, 'day', ('retro',), now=datetime(2026, 9, 8, 8, 0))

    assert result['payment_summary'] == [
        {'name': 'Crypto', 'amount': '100', 'share_percent': '100.00'}]
    assert result['warnings'][:1] == ['Новые типы оплаты iiko показаны отдельно: Crypto.']
