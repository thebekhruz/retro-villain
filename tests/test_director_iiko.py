from datetime import date
from decimal import Decimal

import httpx
import pytest

from retro.integrations.iiko import (
    DIRECTOR_COST_GROUPS, DIRECTOR_DETAIL_GROUPS, director_rows_from_olap,
    reconcile_director_costs,
)
from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.service import DataError
from retro.modules.director.models import ItemMetric, SalesRow
import asyncio


def node(index, value, children=()):
    result = {f'field{index}': {'value': value}}
    if children:
        result['children'] = list(children)
    return result


def test_director_rows_uses_iiko_total_cost_without_multiplying_by_quantity():
    rows = [node(0, 'Kassa-FiscalBox1', [node(1, 'Ресторан', [node(2, 'Яндекс Еда', [
        node(3, 'Плов', [node(4, 'Миллий', [node(5, 'Олег', [
            {**node(6, 'order-1'), 'field7': {'value': 2}, 'field8': {'value': 200000},
             'field9': {'value': 40000}}
        ])])])
    ])])])]
    result = director_rows_from_olap(date(2026, 9, 8), rows)

    assert result[0].item == 'Плов'
    assert result[0].quantity == Decimal('2')
    assert result[0].revenue == Decimal('200000')
    assert result[0].cost == Decimal('40000')
    metric = ItemMetric(result[0].quantity, result[0].revenue, result[0].cost)
    assert metric.gross_profit == Decimal('160000')
    assert metric.margin_percent == Decimal('80.00')


def test_director_load_without_group_configuration_reaches_iiko():
    def handler(request):
        return httpx.Response(403)

    source = IikoClient(Settings(login='x', password='x', store_id=1),
                        transport=httpx.MockTransport(handler))

    try:
        asyncio.run(source.load_director_report(date(2026, 9, 18)))
    except DataError as error:
        assert 'iiko отклонил доступ' in str(error)
    else:
        raise AssertionError('Expected upstream authorization error')


def test_sale_without_a_dish_group_gets_a_configurable_name():
    """Пустую группу нельзя ни отнести к типу, ни исключить — отчёт падал
    целиком. Теперь она приходит под именем и настраивается как обычная."""
    from retro.integrations.iiko import director_rows_from_olap
    from datetime import date
    rows = [{'field0': {'value': 'Retro'}, 'field1': {'value': 'Зал'},
             'field2': {'value': 'Наличные'}, 'field3': {'value': 'Чай'},
             'field4': {'value': None}, 'field5': {'value': 'Азиз'},
             'field6': {'value': 'order-1'},
             'field7': {'value': 1}, 'field8': {'value': 9000}, 'field9': {'value': 1000}}]
    parsed = director_rows_from_olap(date(2026, 9, 21), rows)
    assert parsed[0].category == 'Без группы'


def test_director_loads_yandex_headline_from_payment_report_not_excluded_group():
    def handler(request):
        assert request.url.path == '/api/auth/login'
        return httpx.Response(200, json={'token': 'test-only'})

    source = IikoClient(
        Settings(login='test', password='test', store_id=123,
                 director_excluded_groups=frozenset({'ДОСТАВКА ЯНДЕКС'})),
        transport=httpx.MockTransport(handler), poll_delay=0)
    payment_calls = []

    async def fake_olap(client, day, groups, fields, extra_filters=()):
        if groups == DIRECTOR_COST_GROUPS:
            return [dict((f'field{index}', {'value': value}) for index, value in enumerate([
                'Kassa-FiscalBox1', 'Ресторан', 'Доставка', 'ДОСТАВКА ЯНДЕКС',
                'Олег', f'order-{day}', 1, 65000, 10000]))]
        return [node(0, 'Kassa-FiscalBox1', [node(1, 'Ресторан', [
            node(2, 'Яндех Еда', [node(3, 'Доставка', [
                node(4, 'ДОСТАВКА ЯНДЕКС', [node(5, 'Олег', [
                    {**node(6, f'order-{day}'), 'field7': {'value': ''},
                     'field8': {'value': 1}, 'field9': {'value': 65000},
                     'field10': {'value': 10000}}
                ])])
            ])])
        ])])]

    async def fake_olap_range(client, start, end, groups, fields, extra_filters=()):
        payment_calls.append(tuple(extra_filters))
        if extra_filters:
            return [node(0, '2026-09-13', [node(1, 'Kassa-FiscalBox1', [
                node(2, 'Ресторан', [node(3, 'Доставка', [
                    {**node(4, 'Яндех Еда'), 'field5': {'value': 28004000}}
                ])])
            ])])]
        return []

    source._olap = fake_olap
    source._olap_range = fake_olap_range
    result = asyncio.run(source.load_director_report(date(2026, 9, 23)))

    assert result.period_start == date(2026, 9, 13)
    assert result.period_end == date(2026, 9, 22)
    assert result.cash_total == Decimal(0)
    assert result.yandex_revenue == Decimal('28004000')
    assert len(payment_calls) == 2
    assert payment_calls[0] == ({
        'field': 'OperationType', 'filterType': 'value_list',
        'valueList': ['PAYMENT'], 'inclusiveList': True,
    },)
    assert payment_calls[1] == ()


def sale(payment, quantity, revenue, cost, *, order='order-1'):
    return SalesRow(date(2026, 9, 13), 'Kassa-FiscalBox1', 'Ресторан', payment,
                    'Олот Самса', 'Кухня', Decimal(quantity), Decimal(revenue),
                    Decimal(cost), 'Олег', order)


def test_cost_is_conserved_across_mixed_payments_and_distinct_orders():
    payments = [
        sale('Демо', '1.8', '21600', '10000'),
        sale('UzCard', '.2', '2400', '10000'),
        sale('UzCard', '2', '24000', '10000', order='order-2'),
        sale('(без оплаты)', '2', '0', '10000', order='free'),
    ]
    costs = [sale('', '2', '24000', '10000'),
             sale('', '2', '24000', '10000', order='order-2'),
             sale('', '2', '0', '10000', order='free')]
    result = reconcile_director_costs(payments, costs)
    assert sum(row.cost for row in result) == Decimal('30000')
    assert sum(row.revenue for row in result) == Decimal('48000')
    assert sum(row.quantity for row in result) == Decimal('6')
    assert next(row.cost for row in result if row.payment_type == 'Демо') == Decimal('9000')
    assert next(row.cost for row in result if row.order_id == 'free') == Decimal('10000')


def test_rounded_payment_quantities_do_not_change_authoritative_cost():
    payments = [sale(name, '.333', '4000', '5161.60') for name in ('Демо', 'UzCard', 'Xumo')]
    result = reconcile_director_costs(payments, [sale('', '1', '12000', '5161.60')])
    assert sum(row.cost for row in result) == Decimal('5161.60')
    assert all(row.cost > 0 for row in result)


def test_zero_quantity_and_zero_cost_do_not_divide_by_zero():
    result = reconcile_director_costs([sale('Демо', '0', '0', '0')],
                                      [sale('', '0', '0', '0')])
    assert result[0].cost == 0


def test_non_cash_purpose_survives_parsing_and_cost_reconciliation():
    raw = [{f'field{index}': {'value': value} for index, value in enumerate([
        'Kassa-FiscalBox1', 'Ресторан', '(без оплаты)', 'Олот Самса', 'Кухня',
        'Олег', 'order-1', 'Счет Шефа', 52, 0, 280048.13])}]
    payments = director_rows_from_olap(date(2026, 9, 13), raw, payment_details=True)
    costs = [sale('', '52', '0', '280048.13')]
    result = reconcile_director_costs(payments, costs)
    assert result[0].non_cash_payment_type == 'Счет Шефа'
    assert result[0].quantity == 52
    assert result[0].cost == Decimal('280048.13')
    assert 'NonCashPaymentType' not in DIRECTOR_COST_GROUPS


@pytest.mark.parametrize('costs', [
    [],
    [sale('', '2', '24000', '10000', order='wrong-order')],
    [sale('', '2', '24000', '10000'), sale('', '2', '24000', '10000')],
    [sale('', '3', '24000', '10000')],
    [sale('', '2', '25000', '10000')],
    [sale('', '2', '24000', '-10000')],
])
def test_mismatched_cost_report_fails_closed(costs):
    with pytest.raises(DataError, match='не совпала'):
        reconcile_director_costs([sale('Демо', '2', '24000', '10000')], costs)


def test_nonzero_cost_without_quantity_fails_closed():
    with pytest.raises(DataError, match='не совпала'):
        reconcile_director_costs([sale('Демо', '0', '0', '100')],
                                 [sale('', '0', '0', '100')])


def test_director_load_reconciles_cost_before_item_and_waiter_aggregation():
    source = IikoClient(Settings(login='test', password='test', store_id=1),
                        transport=httpx.MockTransport(
                            lambda request: httpx.Response(200, json={'token': 'test-only'})))
    cost_days = []

    def flat(values):
        return {f'field{index}': {'value': value} for index, value in enumerate(values)}

    async def fake_olap(client, day, groups, fields, extra_filters=()):
        common = ['Олот Самса', 'Кухня', 'Олег', f'order-{day}']
        if groups == DIRECTOR_COST_GROUPS:
            assert 'PayTypes' not in groups
            cost_days.append(day)
            return [flat(['Kassa-FiscalBox1', 'Ресторан', *common, 2, 24000, 10000])]
        assert groups == DIRECTOR_DETAIL_GROUPS
        return [flat(['Kassa-FiscalBox1', 'Ресторан', payment, *common, '',
                      quantity, revenue, 10000]) for payment, quantity, revenue in (
                          ('Демо', 1.8, 21600), ('Яндех Еда', .2, 2400))]

    async def fake_range(client, start, end, groups, fields, extra_filters=()):
        return []

    source._olap = fake_olap
    source._olap_range = fake_range
    result = asyncio.run(source.load_director_report(date(2026, 9, 23)))
    assert len(set(cost_days)) == 10
    item = result.item_metrics['all']['Олот Самса']
    assert item.quantity == 20
    assert item.revenue == 240000
    assert item.cost == 100000  # Previously 200000, once per payment type.
    assert item.gross_profit == 140000
    assert result.waiter_metrics['Олег'] == item
    assert result.item_metrics['retro']['Олот Самса'] == item
    assert result.item_metrics['yandex']['Олот Самса'].cost == 10000
