from datetime import date
from decimal import Decimal

from retro.integrations.iiko import director_rows_from_olap


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
