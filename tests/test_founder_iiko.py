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


def test_founder_range_uses_payment_sales_plus_only_tagged_banquet_dishes():
    requests = []

    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        body = json.loads(request.content)
        groups = body['groupFields']
        payment_sales = any(item.get('field') == 'OperationType' for item in body['filters'])
        if request.url.path == '/api/olap/init':
            requests.append(body)
            return httpx.Response(200, json={'fetchId': 'payments' if 'PayTypes' in groups else 'revenue'})
        if groups == ['OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'DishName']:
            if payment_sales:
                rows = [node(0, '2026-09-01', [
                    node(1, 'Kassa-FiscalBox1', [node(2, 'Ресторан', [
                        node(3, 'Плов', amount=100),
                        node(3, 'Салат (Бехруз)', amount=10),
                    ])]),
                    node(1, 'GL-Kassa-Oksbrich', [node(2, 'Зал', [
                        node(3, 'Обед', amount=40),
                    ])]),
                ])]
            else:
                rows = [node(0, '2026-09-01', [
                    node(1, 'Kassa-FiscalBox1', [node(2, 'Бехруз (Свадьба)', [
                        node(3, 'Салат (Бехруз)', amount=60),
                        node(3, 'Аренда зала', amount=940),
                    ])]),
                ])]
        else:
            assert groups == [
                'OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'DishName', 'PayTypes']
            if payment_sales:
                rows = [node(0, '2026-09-01', [
                    node(1, 'Kassa-FiscalBox1', [node(2, 'Ресторан', [
                        node(3, 'Плов', [node(4, 'UzCard', amount=60),
                                        node(4, 'Демо', amount=40)]),
                        node(3, 'Салат (Бехруз)', [node(4, 'Демо', amount=10)]),
                    ])]),
                    node(1, 'GL-Kassa-Oksbrich', [node(2, 'Зал', [
                        node(3, 'Обед', [node(4, 'UzCard', amount=40)]),
                    ])]),
                ])]
            else:
                rows = [node(0, '2026-09-01', [
                    node(1, 'Kassa-FiscalBox1', [node(2, 'Бехруз (Свадьба)', [
                        node(3, 'Салат (Бехруз)', [node(4, 'Демо', amount=60)]),
                        node(3, 'Аренда зала', [node(4, 'Демо', amount=940)]),
                    ])]),
                ])]
        return httpx.Response(200, json={'result': {'rows': rows}})

    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load_founder_analytics(
        date(2026, 9, 1), date(2026, 9, 1), 'day', ('retro', 'school', 'banquet')))

    assert result['totals'] == {
        'retro': '100', 'school': '40', 'banquet': '60', 'selected': '200'}
    assert result['payment_total'] == '200'
    assert result['reconciled'] is True
    assert len(requests) == 4
    assert sum(any(item.get('field') == 'OperationType' for item in body['filters'])
               for body in requests) == 2
    for body in requests:
        assert body['filters'][0] == {
            'filterType': 'date_range', 'dateFrom': '2026-09-01', 'dateTo': '2026-09-01',
            'includeLeft': True, 'includeRight': True, 'field': 'OpenDate.Typed'}
        filters = {item['field']: item for item in body['filters'][1:]}
        if 'OperationType' in filters:
            assert filters['OperationType']['valueList'] == ['PAYMENT']
        assert 'DishGroup' not in filters
        assert 'DishName' in body['groupFields']
        assert body['dataFields'] == ['DishDiscountSumInt']


def test_founder_large_range_is_split_before_requesting_iiko():
    requests = []

    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        body = json.loads(request.content)
        period = body['filters'][0]
        start = date.fromisoformat(period['dateFrom'])
        end = date.fromisoformat(period['dateTo'])
        if request.url.path == '/api/olap/init':
            if (end - start).days >= 31:
                return httpx.Response(500, json={'error': 'range too large'})
            requests.append((start, end))
            return httpx.Response(200, json={'fetchId': f'{start}-{len(requests)}'})

        payment_sales = any(item.get('field') == 'OperationType' for item in body['filters'])
        banquet = not payment_sales
        item = 'Салат (Бехруз)' if banquet else 'Плов'
        amount = 10 if banquet else 100
        if 'PayTypes' in body['groupFields']:
            leaf = node(3, item, [node(4, 'Демо', amount=amount)])
        else:
            leaf = node(3, item, amount=amount)
        rows = [node(0, start.isoformat(), [
            node(1, 'Kassa-FiscalBox1', [node(2, 'Ресторан', [leaf])]),
        ])]
        return httpx.Response(200, json={'result': {'rows': rows}})

    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load_founder_analytics(
        date(2026, 1, 1), date(2026, 2, 1), 'month', ('retro', 'banquet')))

    assert result['totals']['selected'] == '220'
    assert result['payment_total'] == '220'
    assert requests == ([(date(2026, 1, 1), date(2026, 1, 31))] * 4
                        + [(date(2026, 2, 1), date(2026, 2, 1))] * 4)



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
    result = asyncio.run(source.load_founder_analytics(
        date(2026, 1, 1), date(2026, 9, 22), 'month',
        ('retro', 'school', 'banquet')))

    assert result['period'] == {'start': '2026-01-01', 'end': '2026-09-22'}
    assert len(calls) == 36
    assert max_active == 8
