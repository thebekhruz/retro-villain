from datetime import date
from decimal import Decimal

import httpx

from retro.integrations.iiko import director_rows_from_olap
from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.service import DataError
from retro.modules.director.models import ItemMetric
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
        return [node(0, 'Kassa-FiscalBox1', [node(1, 'Ресторан', [
            node(2, 'Яндех Еда', [node(3, 'Доставка', [
                node(4, 'ДОСТАВКА ЯНДЕКС', [node(5, 'Олег', [
                    {**node(6, f'order-{day}'), 'field7': {'value': 1},
                     'field8': {'value': 65000}, 'field9': {'value': 10000}}
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
