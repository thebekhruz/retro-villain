import asyncio
from datetime import date
from io import BytesIO
from ipaddress import ip_network

import httpx
import openpyxl
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.iiko import IikoClient, cash_prepay_from_shifts
from retro.modules.cashier.service import DataError
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


def test_finance_module_ignores_host_header_from_the_network():
    """Host подставляет клиент: сеть ресторана не должна открывать зарплаты."""
    settings = Settings(dashboard_user='viewer', dashboard_password='secret',
                        dashboard_allowed_network=ip_network('10.10.8.0/22'))
    app = create_app(settings)
    with TestClient(app, base_url='http://retro.local', client=('10.10.8.91', 50000)) as client:
        assert client.get('/', auth=('viewer', 'secret')).status_code == 200
        for path in ('/accountant', '/accountant/employees', '/api/accountant/day?date=2026-09-10'):
            spoofed = client.get(path, auth=('viewer', 'secret'), headers={'Host': 'localhost'})
            assert spoofed.status_code == 403, path
            assert client.get(path, auth=('viewer', 'secret')).status_code == 403, path


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
