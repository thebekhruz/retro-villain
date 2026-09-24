import asyncio
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.service import DataError


DAY = date(2026, 9, 15)


def row(**fields):
    return {f'field{index}': {'value': value} for index, value in fields.items()}


def test_live_report_uses_only_retro_payments_and_keeps_three_way_breakdown():
    requests = []

    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        if request.url.path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': [{'id': 'shift-1',
                'openDate': '2026-09-15T11:00:00', 'cashRegNumber': 1,
                'payOrders': 31824000, 'salesCash': 20000000,
                'salesCard': 11824000, 'salesCredit': 0}]})
        body = json.loads(request.content)
        groups = body['groupFields']
        requests.append(body)
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': groups[0]})
        if groups == ['CashRegisterName', 'RestaurantSection']:
            rows = [
                {**row(**{'0': 'GL-Kassa-Oksbrich', '2': 3970000}),
                 'children': [row(**{'1': 'Зал', '2': 3970000})]},
                {**row(**{'0': 'Kassa-FiscalBox1', '2': 54314000}),
                 'children': [row(**{'1': 'Ресторан', '2': 32124000}),
                              row(**{'1': 'Бехруз (Свадьба)', '2': 22190000})]},
            ]
        elif groups == ['OpenDate.Typed']:
            rows = [row(**{'0': DAY.isoformat(), '1': 75, '2': 31824000})]
        elif groups == ['PayTypes']:
            rows = [row(**{'0': 'Демо', '1': 20000000}),
                    row(**{'0': 'UzCard', '1': 11824000})]
        else:
            raise AssertionError(groups)
        return httpx.Response(200, json={'result': {'rows': rows}})

    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load(DAY))
    assert result.revenue == Decimal(31824000)
    assert result.receipt_count == 75
    assert result.json()['revenue_breakdown'] == {
        'retro': '32124000', 'school': '3970000',
        'bekhruz_banquet': '22190000', 'total': '58284000'}
    assert len(requests) == 8
    breakdown = requests[0]
    assert breakdown['storeIds'] == [123]
    assert not any(f.get('field') == 'CashRegisterName' for f in breakdown['filters'])
    for body in requests[2:6]:
        filters = {f['field']: f for f in body['filters']}
        assert filters['CashRegisterName']['valueList'] == ['Kassa-FiscalBox1']
        assert filters['RestaurantSection']['valueList'] == ['Бехруз (Свадьба)']
        assert filters['RestaurantSection']['inclusiveList'] is False
        assert filters['OperationType']['valueList'] == ['PAYMENT']


def test_unknown_cash_register_is_not_silently_counted_as_retro():
    from retro.modules.cashier.service import build_revenue_breakdown

    with pytest.raises(DataError):
        build_revenue_breakdown([{
            **row(**{'0': 'New-Register', '2': 100}),
            'children': [row(**{'1': 'Ресторан', '2': 100})],
        }])


def test_uzum_payment_is_retained_in_report():
    from retro.modules.cashier.service import build_snapshot

    result = build_snapshot(DAY, [row(**{'0': DAY.isoformat(), '1': 1, '2': 100})],
                            [row(**{'0': 'Uzum', '1': 100})])
    assert result.payments[-1].name == 'Uzum'
    assert result.payments[-1].amount == 100
