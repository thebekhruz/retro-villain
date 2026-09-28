from datetime import date

import httpx
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings


def test_usd_rate_is_for_selected_day_and_rounded_down_to_one_decimal(tmp_path):
    def source(request):
        assert request.url.path == '/ru/arkhiv-kursov-valyut/json/USD/2026-04-25/'
        return httpx.Response(200, json=[{
            'Ccy': 'USD', 'Nominal': '1', 'Rate': '12015.96', 'Date': '24.04.2026',
        }])

    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
                     rate_transport=httpx.MockTransport(source))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/cashier/usd-rate', params={'date': '2026-04-25'})
        assert response.status_code == 200
        assert response.json() == {
            'date': '2026-04-25', 'source_date': '2026-04-24',
            'official_rate': '12015.96', 'restaurant_rate': '11800',
            'discount_percent': '1.5',
        }
        page = client.get('/').text
        # Кассир должен видеть оба курса и понимать, чем они отличаются.
        # Скидка теперь подписана в самой метке курса, а не отдельной строкой.
        assert 'Официальный курс ЦБ' in page
        assert 'Курс Retro' in page
        assert '1,5%' in page


def test_historical_rate_stays_unchanged_after_restart_and_source_change(tmp_path):
    path = tmp_path / 'cashier.sqlite3'
    calls = []

    def first_source(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[{
            'Ccy': 'USD', 'Nominal': '1', 'Rate': '11797.46', 'Date': '17.09.2026',
        }])

    with TestClient(create_app(Settings(), expense_db_path=path,
                               rate_transport=httpx.MockTransport(first_source)),
                    client=('127.0.0.1', 50000)) as client:
        first = client.get('/api/cashier/usd-rate', params={'date': '2026-09-17'})
        assert first.json()['restaurant_rate'] == '11600'

    def changed_source(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=[{
            'Ccy': 'USD', 'Nominal': '1', 'Rate': '20000.00', 'Date': '17.09.2026',
        }])

    with TestClient(create_app(Settings(), expense_db_path=path,
                               rate_transport=httpx.MockTransport(changed_source)),
                    client=('127.0.0.1', 50000)) as client:
        old = client.get('/api/cashier/usd-rate', params={'date': '2026-09-17'})
        assert old.json() == first.json()
        assert calls == ['/ru/arkhiv-kursov-valyut/json/USD/2026-09-17/']


def test_usd_balance_can_be_saved_for_selected_day(tmp_path):
    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.post('/api/cashier/usd-balance', json={
            'date': '2026-09-17', 'amount': '1250.50'})
        assert response.status_code == 201
        assert client.get('/api/cashier/usd-balance', params={'date': '2026-09-17'}).json()['amount'] == '1250.50'


def test_rate_source_failure_and_invalid_value_do_not_create_rate(tmp_path):
    path = tmp_path / 'cashier.sqlite3'

    def bad_source(request):
        return httpx.Response(200, json=[{
            'Ccy': 'USD', 'Nominal': '1', 'Rate': '-12000', 'Date': '17.09.2026',
        }])

    with TestClient(create_app(Settings(), expense_db_path=path,
                               rate_transport=httpx.MockTransport(bad_source)),
                    client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/cashier/usd-rate', params={'date': '2026-09-17'})
        assert response.status_code == 503
        assert client.get('/api/cashier/usd-rate', params={'date': '2099-01-01'}).status_code == 422
