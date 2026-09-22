from datetime import date
from decimal import Decimal

import httpx

from retro.integrations.iiko import director_rows_from_olap
from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.service import DataError
import asyncio


def node(index, value, children=()):
    result = {f'field{index}': {'value': value}}
    if children:
        result['children'] = list(children)
    return result


def test_director_rows_flattens_grouped_iiko_result_and_derives_cost():
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
    assert result[0].cost == Decimal('80000')


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
