from datetime import date
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from legacy_app import create_app
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
        # T-442: на экране один курс — Retro, по нему считается вычет из передачи.
        # Курс ЦБ и разница в 1,5% — в подсказке к нему (app.js).
        assert 'Курс Retro' in page
        script = (Path(__file__).resolve().parent.parent / 'retro' / 'static' / 'app.js').read_text(encoding='utf-8')
        assert "'Курс ЦБ '" in script and '1,5%' in script


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


def test_usd_for_the_day_is_the_sum_of_deposits_to_the_safe(tmp_path):
    # Одной суммы на день больше нет: доллары кладут в сейф взносами (5a).
    app = create_app(Settings(data_dir=tmp_path), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     rate_transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[{
                         'Ccy': 'USD', 'Nominal': '1', 'Rate': '11897.90', 'Date': '17.09.2026'}])))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        assert client.post('/api/cashier/usd-balance', json={
            'date': '2026-09-17', 'amount': '1250.50'}).status_code == 410
        for amount in ('1250.50', '120'):
            response = client.post('/api/cashier/usd-deposits', json={
                'date': '2026-09-17', 'amount': amount})
            assert response.status_code == 201
        assert client.get('/api/cashier/usd-balance', params={'date': '2026-09-17'}).json() == {
            'date': '2026-09-17', 'amount': '1370.50'}
        assert client.get('/api/cashier/usd-balance', params={'date': '2026-09-18'}).json()['amount'] is None


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
