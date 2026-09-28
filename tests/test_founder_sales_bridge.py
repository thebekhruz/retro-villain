from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from retro.modules.founder.models import PaymentRow, build_sales_bridge
from retro.modules.cashier.service import DataError

DAY = date(2026, 9, 21)


def row(amount, operation='', **kwargs):
    return replace(PaymentRow(DAY, 'Kassa-FiscalBox1', 'Ресторан', 'Плов',
                              'Демо', D(amount), operation), **kwargs)


def bridge(sales, operations, end=DAY, directions=('retro',), granularity='day'):
    return build_sales_bridge(sales, operations, DAY, end, granularity, directions)


def test_september_21_sales_and_redeemed_advance_are_separate():
    result = bridge([row('42463500')], [row('42163500', 'Оплата'), row('300000', 'Предоплата')])
    assert result['totals'] == dict(paid='42163500.00', prepaid='300000.00',
                                    other='0.00', sales='42463500.00')
    assert result['reconciled']
    assert result['payments'][0]['prepaid'] == '300000.00'
    assert result['series'][0]['paid'] == '42163500.00'


def test_opposite_errors_in_different_payment_methods_or_days_do_not_cancel():
    tomorrow = DAY + timedelta(days=1)
    result = bridge([row(100), row(100, payment='UzCard'), row(100, day=tomorrow)],
                    [row(90, 'Оплата'), row(120, 'Оплата', payment='UzCard'),
                     row(90, 'Оплата', day=tomorrow)], end=tomorrow)
    assert not result['reconciled']
    assert len(result['differences']) == 3
    assert result['totals']['sales'] == '300.00'


def test_returns_empty_days_unknown_operations_and_filters():
    school = row(900, 'Оплата', register='GL-Kassa-Oksbrich', section='Зал')
    result = bridge([row(-100), school], [row(-80, 'Оплата'), row(-20, 'Предоплата'), school],
                    end=DAY + timedelta(days=1))
    assert result['totals']['sales'] == '-100.00'
    assert result['totals']['prepaid'] == '-20.00'
    assert result['series'][1]['sales'] == '0.00'
    unknown = bridge([row(75)], [row(75, 'Новый тип')])
    assert unknown['totals']['prepaid'] == '0.00'
    assert unknown['totals']['other'] == '75.00'
    assert unknown['unknown_operations'] == ['Новый тип']
    assert bridge([], [])['reconciled']


def test_direction_errors_and_dates_are_not_hidden_by_equal_totals():
    school = row(100, register='GL-Kassa-Oksbrich', section='Зал')
    result = bridge([row(100), school], [row(90, 'Оплата'), replace(school, amount=D(110), operation='Оплата')],
                    directions=('retro', 'school'))
    assert not result['reconciled']
    with pytest.raises(DataError):
        bridge([], [row(100, 'Оплата', day=DAY-timedelta(days=1))])


def test_weekly_and_monthly_aggregates_preserve_advance_reconciliation():
    end=DAY+timedelta(days=14)
    rows=[row(100), row(50, day=end)]
    operations=[row(75,'Оплата'), row(25,'Предоплата'), row(50,'Оплата', day=end)]
    for granularity in ('week','month'):
        result=bridge(rows,operations,end=end,granularity=granularity)
        assert sum(D(part['sales']) for part in result['series']) == 150
        assert sum(D(part['prepaid']) for part in result['series']) == 25
        assert result['reconciled']


def test_total_rounding_errors_do_not_hide_accumulated_mismatch():
    payments=[row(100, day=DAY+timedelta(days=i)) for i in range(3)]
    operations=[replace(item, amount=D('100.50'), operation='Оплата') for item in payments]
    result=bridge(payments, operations, end=DAY+timedelta(days=2))
    assert result['differences'] == []
    assert result['discrepancy'] == '1.50'
    assert not result['reconciled']
