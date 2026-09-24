import asyncio
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from retro.config import Settings
from retro.integrations.iiko import (
    IikoClient, founder_internal_costs_from_olap, founder_pnl_from_kpi,
    founder_rows_from_olap,
)
from retro.modules.cashier.service import DataError


def node(index, value, children=None, amount=None, cost=None):
    result = {f'field{index}': {'value': value}}
    if children is not None:
        result['children'] = children
    if amount is not None:
        result[f'field{index + 1}'] = {'value': amount}
    if cost is not None:
        result[f'field{index + 2}'] = {'value': cost}
    return result


def test_founder_includes_prepaid_sales_and_cost_without_payment_duplication():
    requests = []

    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        body = json.loads(request.content)
        if request.url.path == '/api/kpi/dashboard/get-data':
            assert body == {
                'dateFrom': '2026-09-01', 'dateTo': '2026-09-01',
                'metricCodes': ['PL_SALES_TOTAL', 'PL_COS_TOTAL', 'PL_PROFIT_GROSS'],
                'storeIds': [123], 'dataType': 'DATA_SUMMARY_BY_PERIODS'}
            return httpx.Response(200, json={'data': {
                'PL_SALES_TOTAL': {'2026-09-01-2026-09-01': 250},
                'PL_COS_TOTAL': {'2026-09-01-2026-09-01': 82.5},
                'PL_PROFIT_GROSS': {'2026-09-01-2026-09-01': 167.5},
            }})
        groups = body['groupFields']
        assert not any(item.get('field') == 'OperationType' for item in body['filters'])
        if request.url.path == '/api/olap/init':
            requests.append(body)
            report = ('payments' if 'PayTypes' in groups else
                      'internal' if 'NonCashPaymentType' in groups else 'revenue')
            return httpx.Response(200, json={'fetchId': report})
        if 'NonCashPaymentType' in groups:
            rows = [node(0, '2026-09-01', [
                node(1, 'Дегустация', amount=5),
                node(1, 'Счет Шефа', amount=12.25),
                node(1, 'Комплимент', amount=3),
            ])]
            return httpx.Response(200, json={'result': {'rows': rows}})
        if 'PayTypes' in groups:
            assert body['dataFields'] == ['DishDiscountSumInt']
            dishes = [node(3, 'Плов', [node(4, 'UzCard', amount=100),
                                     node(4, 'Демо', amount=50)]),
                      node(3, 'Дегустация', [node(4, '(без оплаты)', amount=0)])]
            banquet = [node(3, 'Салат (Бехруз)', [node(4, 'Демо', amount=60)]),
                       node(3, 'Аренда зала', [node(4, 'Демо', amount=940)])]
            school = [node(3, 'Обед', [node(4, 'UzCard', amount=40)])]
        else:
            assert body['dataFields'] == ['DishDiscountSumInt', 'ProductCostBase.ProductCost']
            dishes = [node(3, 'Плов', amount=150, cost=45),
                      node(3, 'Дегустация', amount=0, cost=5)]
            banquet = [node(3, 'Салат (Бехруз)', amount=60, cost=20),
                       node(3, 'Аренда зала', amount=940, cost=300)]
            school = [node(3, 'Обед', amount=40, cost=17)]
        rows = [node(0, '2026-09-01', [
            node(1, 'Kassa-FiscalBox1', [node(2, 'Ресторан', dishes),
                                       node(2, 'Бехруз (Свадьба)', banquet)]),
            node(1, 'GL-Kassa-Oksbrich', [node(2, 'Зал', school)]),
        ])]
        return httpx.Response(200, json={'result': {'rows': rows}})

    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load_founder_analytics(
        date(2026, 9, 1), date(2026, 9, 1), 'day', ('retro', 'school', 'banquet')))

    assert result['totals'] == {
        'retro': '150', 'school': '40', 'banquet': '60', 'selected': '250'}
    assert result['olap_product_cost_totals'] == {
        'retro': '50', 'school': '17', 'banquet': '20', 'selected': '87'}
    assert 'cost_totals' not in result
    assert result['pnl'] == {'sales': '250', 'cost': '82.5', 'gross_profit': '167.5'}
    assert result['internal_costs'] == {'tasting': '5', 'chef_account': '12.25'}
    assert result['payment_total'] == '250'
    assert result['reconciled'] is True
    assert result['scope_excluded_revenue'] == '940'
    assert len(requests) == 3
    for body in requests:
        assert body['filters'][0] == {
            'filterType': 'date_range', 'dateFrom': '2026-09-01', 'dateTo': '2026-09-01',
            'includeLeft': True, 'includeRight': True, 'field': 'OpenDate.Typed'}
        assert not any(item['field'] == 'DishGroup' for item in body['filters'])
        assert ('DishName' in body['groupFields']
                or body['groupFields'] == ['OpenDate.Typed', 'NonCashPaymentType'])


def test_founder_large_range_is_split_before_requesting_iiko():
    requests = []

    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        body = json.loads(request.content)
        if request.url.path == '/api/kpi/dashboard/get-data':
            return httpx.Response(200, json={'data': {
                'PL_SALES_TOTAL': {'period': 220},
                'PL_COS_TOTAL': {'period': 86},
                'PL_PROFIT_GROSS': {'period': 134},
            }})
        period = body['filters'][0]
        start = date.fromisoformat(period['dateFrom'])
        end = date.fromisoformat(period['dateTo'])
        if request.url.path == '/api/olap/init':
            if (end - start).days >= 31:
                return httpx.Response(500, json={'error': 'range too large'})
            requests.append((start, end))
            return httpx.Response(200, json={'fetchId': f'{start}-{len(requests)}'})

        if 'NonCashPaymentType' in body['groupFields']:
            rows = []
            return httpx.Response(200, json={'result': {'rows': rows}})
        if 'PayTypes' in body['groupFields']:
            leaves = [node(3, 'Плов', [node(4, 'Демо', amount=100)]),
                      node(3, 'Салат (Бехруз)', [node(4, 'Демо', amount=10)])]
        else:
            leaves = [node(3, 'Плов', amount=100, cost=40),
                      node(3, 'Салат (Бехруз)', amount=10, cost=3)]
        rows = [node(0, start.isoformat(), [
            node(1, 'Kassa-FiscalBox1', [node(2, 'Ресторан', leaves)]),
        ])]
        return httpx.Response(200, json={'result': {'rows': rows}})

    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load_founder_analytics(
        date(2026, 1, 1), date(2026, 2, 1), 'month', ('retro', 'banquet')))

    assert result['totals']['selected'] == '220'
    assert result['payment_total'] == '220'
    assert result['olap_product_cost_totals']['selected'] == '86'
    assert requests == ([(date(2026, 1, 1), date(2026, 1, 31))] * 3
                        + [(date(2026, 2, 1), date(2026, 2, 1))] * 3)



def test_founder_eight_month_range_runs_each_chunk_reports_concurrently():
    def handler(request):
        assert request.url.path == '/api/auth/login'
        return httpx.Response(200, json={'token': 'test-only'})

    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    active = 0
    max_active = 0
    calls = []

    async def fake_olap_range(client, start, end, groups, fields, extra_filters=()):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        calls.append((start, end, tuple(groups), tuple(fields), tuple(extra_filters)))
        await asyncio.sleep(.01)
        active -= 1
        return []

    source._olap_range = fake_olap_range
    async def fake_pnl(client, start, end):
        return {'sales': '0', 'cost': '0', 'gross_profit': '0'}
    source._founder_pnl = fake_pnl
    result = asyncio.run(source.load_founder_analytics(
        date(2026, 1, 1), date(2026, 9, 22), 'month',
        ('retro', 'school', 'banquet')))

    assert result['period'] == {'start': '2026-01-01', 'end': '2026-09-22'}
    assert len(calls) == 27
    assert max_active == 6


@pytest.mark.parametrize('cost', [None, '40', float('nan')])
def test_founder_rejects_missing_or_invalid_source_cost(cost):
    values = ['2026-09-22', 'Kassa-FiscalBox1', 'Ресторан', 'Плов', 100, cost]
    row = {f'field{i}': {'value': value} for i, value in enumerate(values)}
    with pytest.raises(DataError):
        founder_rows_from_olap([row], include_cost=True)


def test_founder_pnl_and_internal_cost_parsers_preserve_decimal_precision():
    pnl = founder_pnl_from_kpi({
        'PL_SALES_TOTAL': {'day-1': 38453100, 'day-2': 40070500},
        'PL_COS_TOTAL': {'day-1': 12515128.92, 'day-2': 17211349.18},
        'PL_PROFIT_GROSS': {'day-1': 25937971.08, 'day-2': 22859150.82},
    })
    internal = founder_internal_costs_from_olap([
        node(0, '2026-09-22', [node(1, 'Дегустация', amount=Decimal('90829.32470417733')),
                               node(1, 'Счет Шефа', amount=Decimal('745775.9779090909'))]),
        node(0, '2026-09-23', [node(1, 'Счет Шефа', amount=Decimal('406730.3006904762'))]),
    ])

    assert pnl == {
        'sales': '78523600', 'cost': '29726478.10', 'gross_profit': '48797121.90'}
    assert str(internal['Дегустация']) == '90829.32470417733'
    assert str(internal['Счет Шефа']) == '1152506.2785995671'


def test_founder_rejects_inconsistent_pnl():
    with pytest.raises(DataError, match='не сходятся'):
        founder_pnl_from_kpi({
            'PL_SALES_TOTAL': {'period': 100},
            'PL_COS_TOTAL': {'period': 40},
            'PL_PROFIT_GROSS': {'period': 50},
        })
