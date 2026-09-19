import asyncio
import json
from datetime import date

import httpx

from retro.config import Settings
from retro.integrations.iiko import IikoClient


def node(index, value, children=None, amount=None):
    result = {f'field{index}': {'value': value}}
    if children is not None:
        result['children'] = children
    if amount is not None:
        result[f'field{index + 1}'] = {'value': amount}
    return result


def test_founder_range_uses_payment_sales_without_menu_exclusions():
    requests = []

    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        body = json.loads(request.content)
        groups = body['groupFields']
        if request.url.path == '/api/olap/init':
            requests.append(body)
            return httpx.Response(200, json={'fetchId': 'payments' if 'PayTypes' in groups else 'revenue'})
        if groups == ['OpenDate.Typed', 'CashRegisterName', 'RestaurantSection']:
            rows = [node(0, '2026-09-01', [
                node(1, 'Kassa-FiscalBox1', [node(2, 'Ресторан', amount=100)]),
                node(1, 'GL-Kassa-Oksbrich', [node(2, 'Школа', amount=40)]),
            ])]
        else:
            assert groups == ['OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'PayTypes']
            rows = [node(0, '2026-09-01', [
                node(1, 'Kassa-FiscalBox1', [
                    node(2, 'Ресторан', [node(3, 'UzCard', amount=60), node(3, 'Демо', amount=40)])]),
                node(1, 'GL-Kassa-Oksbrich', [node(2, 'Школа', [node(3, 'UzCard', amount=40)])]),
            ])]
        return httpx.Response(200, json={'result': {'rows': rows}})

    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load_founder_analytics(
        date(2026, 9, 1), date(2026, 9, 1), 'day', ('retro', 'school')))

    assert result['totals']['selected'] == '140'
    assert result['payment_total'] == '140'
    assert len(requests) == 2
    for body in requests:
        assert body['filters'][0] == {
            'filterType': 'date_range', 'dateFrom': '2026-09-01', 'dateTo': '2026-09-01',
            'includeLeft': True, 'includeRight': True, 'field': 'OpenDate.Typed'}
        filters = {item['field']: item for item in body['filters'][1:]}
        assert filters['OperationType']['valueList'] == ['PAYMENT']
        assert 'DishGroup' not in filters
        assert body['dataFields'] == ['DishDiscountSumInt']
