import asyncio
from datetime import date
from io import BytesIO
from ipaddress import ip_network

import httpx
import openpyxl
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.iiko import IikoClient, cash_prepay_from_shifts, detail_rows_from_olap
from retro.modules.cashier.service import DataError, demo_snapshot
import pytest


def local_client(settings=None):
    return TestClient(create_app(settings or Settings()), client=('127.0.0.1', 50000))


def test_missing_connection_not_replaced_by_demo():
    with local_client() as client:
        assert client.get('/api/cashier/day?date=2026-09-10').status_code == 503
        response = client.get('/api/cashier/day?date=2026-09-10&demo=true')
        assert response.status_code == 200
        assert response.json()['demo'] is True


def test_export_matches_displayed_snapshot_and_rejects_wrong_day():
    with local_client() as client:
        data = client.get('/api/cashier/day?date=2026-09-10&demo=true').json()
        params = dict(date='2026-09-10', snapshot_id=data['snapshot_id'])
        response = client.get('/api/cashier/export', params=params)
        assert response.status_code == 200
        assert 'DEMO' in response.headers['content-disposition']
        w = openpyxl.load_workbook(BytesIO(response.content))
        assert w['отчет']['B2'].value == 18450000
        assert w['отчет']['B25'].value == 126
        params['date'] = '2026-09-11'
        assert client.get('/api/cashier/export', params=params).status_code == 409


def test_invalid_and_future_dates_are_rejected():
    with local_client() as client:
        assert client.get('/api/cashier/day?date=bad').status_code == 422
        assert client.get('/api/cashier/day?date=2099-01-01').status_code == 422


def test_new_daily_report_does_not_cancel_another_readers_report():
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    release_first = asyncio.Event()

    class IikoStub:
        calls = []

        async def load(self, day):
            self.calls.append(day)
            if len(self.calls) == 1:
                first_started.set()
                try:
                    await release_first.wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    raise
            return demo_snapshot(day)

    async def scenario():
        app = create_app(Settings())
        app.state.iiko = IikoStub()
        transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 50000))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as client:
            old_request = asyncio.create_task(
                client.get('/api/cashier/day?date=2026-09-10'))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            new_response = await asyncio.wait_for(
                client.get('/api/cashier/day?date=2026-09-11'), timeout=1)
            assert not first_cancelled.is_set()
            release_first.set()
            old_response = await asyncio.wait_for(old_request, timeout=1)
        return app.state.iiko.calls, old_response, new_response

    calls, old_response, new_response = asyncio.run(scenario())

    assert calls == [date(2026, 9, 10), date(2026, 9, 11)]
    assert not first_cancelled.is_set()
    assert old_response.status_code == 200
    assert old_response.json()['date'] == '2026-09-10'
    assert new_response.status_code == 200
    assert new_response.json()['date'] == '2026-09-11'


def test_iiko_detail_rows_keep_dimensions_metrics_and_report_truncation():
    rows = [{
        'field0': {'value': '2026-09-16'},
        'children': [
            {'field1': {'value': 'Стейк'}, 'field2': {'value': 2},
             'field3': {'value': 500000}, 'field4': {'value': 120000},
             'field5': {'value': 60000}, 'field6': {'value': 1}},
            {'field1': {'value': 'Салат'}, 'field2': {'value': 3},
             'field3': {'value': 210000}, 'field4': {'value': 30000},
             'field5': {'value': 10000}, 'field6': {'value': 2}},
        ],
    }]

    result, total = detail_rows_from_olap(
        rows, ('OpenDate.Typed', 'DishName'), limit=1)

    assert total == 2
    assert result == [{
        'dimensions': {'OpenDate.Typed': '2026-09-16', 'DishName': 'Стейк'},
        'quantity': '2', 'revenue': '500000',
        'product_cost_per_unit': '60000', 'product_cost_total': '120000',
        'orders': '1',
    }]


def test_iiko_detail_report_uses_allowlisted_olap_dimensions_and_metrics():
    captured = {}

    def handler(request):
        payload = __import__('json').loads(request.content) if request.content else {}
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'safe-token'})
        captured['body'] = payload
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': 'details-1'})
        return httpx.Response(200, json={'result': {'rows': [{
            'field0': {'value': 'Стейк'}, 'field1': {'value': 2},
            'field2': {'value': 500000}, 'field3': {'value': 120000},
            'field4': {'value': 60000}, 'field5': {'value': 1},
        }]}})

    source = IikoClient(
        Settings(login='test', password='test', store_id=123),
        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load_sales_details(
        date(2026, 9, 16), date(2026, 9, 16), ('DishName',), limit=25))

    assert captured['body']['groupFields'] == ['DishName']
    assert captured['body']['dataFields'] == [
        'DishAmountInt', 'DishDiscountSumInt', 'ProductCostBase.ProductCost',
        'ProductCostBase.OneItem', 'UniqOrderId.OrdersCount']
    assert result['rows'][0]['dimensions'] == {'DishName': 'Стейк'}
    assert result['truncated'] is False


def test_external_access_requires_configured_credentials():
    with TestClient(create_app(Settings()), client=('192.168.1.80', 50000)) as client:
        assert client.get('/api/config').status_code == 403
    with local_client(Settings(dashboard_user='cashier', dashboard_password='secret')) as client:
        assert client.get('/api/config').status_code == 401
        assert client.get('/api/config', auth=('cashier', 'wrong')).status_code == 401
        assert client.get('/api/config', auth=('cashier', 'secret')).status_code == 200


def test_lan_access_is_limited_to_allowed_network_and_password():
    settings = Settings(dashboard_user='viewer', dashboard_password='secret',
                        dashboard_allowed_network=ip_network('10.10.8.0/22'))
    app = create_app(settings)
    with TestClient(app, client=('10.10.8.91', 50000)) as client:
        assert client.get('/api/config').status_code == 401
        assert client.get('/api/config', auth=('viewer', 'secret')).status_code == 200
    with TestClient(app, client=('10.10.12.1', 50000)) as client:
        assert client.get('/api/config', auth=('viewer', 'secret')).status_code == 403


def test_pages_are_closed_without_login_and_served_after_it():
    settings = Settings(dashboard_user='viewer', dashboard_password='secret')
    app = create_app(settings)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        for path in ('/', '/accountant', '/accountant/employees'):
            # Страницу без входа отдавать нельзя: гостя уводит на форму входа,
            # а не показывает содержимое с пустыми полями.
            closed = client.get(path, follow_redirects=False)
            assert closed.status_code == 303, path
            assert closed.headers['location'] == '/login', path
            wrong = client.get(path, auth=('viewer', 'wrong'), follow_redirects=False)
            assert wrong.status_code == 303, path
            page = client.get(path, auth=('viewer', 'secret'))
            assert page.status_code == 200, path
            assert page.headers['content-type'].startswith('text/html'), path
    # Без настроенного пароля нелокальные запросы не получают и страницу.
    with TestClient(create_app(Settings()), client=('192.168.1.80', 50000)) as client:
        assert client.get('/').status_code == 403


def test_finance_module_is_available_to_authorized_user_from_allowed_network():
    settings = Settings(dashboard_panel_users={
                            'bookkeeper': ('secret', 'accountant'),
                            'cashier': ('secret', 'cashier'),
                        },
                        dashboard_allowed_network=ip_network('10.10.8.0/22'))
    app = create_app(settings)
    with TestClient(app, base_url='http://retro.local', client=('10.10.8.91', 50000)) as client:
        assert client.get('/accountant', auth=('bookkeeper', 'secret')).status_code == 200
        assert client.get('/accountant/employees', auth=('bookkeeper', 'secret')).status_code == 200
        assert client.get('/accountant', auth=('cashier', 'secret')).status_code == 403


def test_iiko_protocol_pending_poll_and_total_query():
    polls = {}
    def handler(request):
        import json
        body = json.loads(request.content) if request.content else {}
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        assert request.headers['Authorization'] == 'Bearer test-only'
        if request.url.path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': [{'id':'shift-1','openDate':'2026-09-10T10:00:00',
                                                           'cashRegNumber':1,'payOrders':100000,
                                                           'salesCash':40000,'salesCard':60000,'salesCredit':0}]})
        assert body['storeIds'] == [123]
        assert body['filters'][0]['dateFrom'] == '2026-09-10'
        assert body['includeVoidTransactions'] is False
        group = body['groupFields'][0]
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': group})
        polls[group] = polls.get(group, 0) + 1
        if polls[group] == 1:
            return httpx.Response(400, text='Request data not found')
        def row(*values):
            return {f'field{i}': {'value': v} for i, v in enumerate(values)}
        if group == 'CashRegisterName':
            assert body['groupFields'] == ['CashRegisterName', 'RestaurantSection']
            rows = [{'field0': {'value': 'Kassa-FiscalBox1'},
                     'field2': {'value': 100000},
                     'children': [{'field1': {'value': 'Ресторан'},
                                   'field2': {'value': 100000}}]}]
        elif group == 'OpenDate.Typed':
            assert body['dataFields'] == ['UniqOrderId.OrdersCount', 'DishDiscountSumInt']
            rows = [row('2026-09-10', 2, 100000)]
        else:
            assert group == 'PayTypes'
            rows = [row('Наличные (Инкасса QR)', 40000), row('UzCard', 60000)]
        return httpx.Response(200, json={'result': {'rows': rows}})
    source = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    result = asyncio.run(source.load(date(2026, 9, 10)))
    assert result.receipt_count == 2
    assert result.revenue == 100000
    assert result.payments[1].amount == 60000
    assert result.payments[2].amount == 40000


def test_cash_prepayment_excludes_card_advance_and_matches_15_september():
    shifts = [{'id':'one','openDate':'2026-09-15T11:16:11','cashRegNumber':1,
               'payOrders':32424000,'salesCash':17649000,'salesCard':14775000,'salesCredit':0}]
    total, cash = cash_prepay_from_shifts(date(2026, 9, 15), 31824000,
                                           {'Демо':14839000,'Наличные (Инкасса QR)':2210000}, shifts)
    assert (total, cash) == (600000, 600000)


def test_cash_prepayment_excludes_cashless_prepays():
    shifts = [{'id':'one','openDate':'2026-09-12T11:00:00','cashRegNumber':1,
               'payOrders':64817000,'salesCash':28677500,'salesCard':36139500,'salesCredit':0}]
    assert cash_prepay_from_shifts(date(2026, 9, 12), 63117000,
                                  {'Демо':27028500,'Наличные (Инкасса QR)':1649000}, shifts) == (1700000, 0)


def test_cash_prepayment_rejects_incomplete_shift_details():
    shifts = [{'id':'one','openDate':'2026-09-15T11:00:00','cashRegNumber':1,
               'payOrders':32424000}]
    with pytest.raises(DataError):
        cash_prepay_from_shifts(date(2026, 9, 15), 31824000, {'Демо':14839000}, shifts)
