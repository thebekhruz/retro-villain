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


# ── T-526: курс из браузера кассира ───────────────────────────────────────
# cbu.uz роняет соединения с зарубежных дата-центров (Railway) — сервер курс
# не получает никогда. Браузер в Узбекистане берёт JSON у ЦБ сам, сервер
# сверяет его с рыночным курсом и сохраняет.

CBU_PAYLOAD = [{'Ccy': 'USD', 'Nominal': '1', 'Rate': '11838.92', 'Date': '24.10.2026'}]


def blocked_cbu(reference_uzs='11870.04'):
    def source(request):
        if request.url.host == 'open.er-api.com':
            assert request.url.path == '/v6/latest/USD'
            return httpx.Response(200, json={'result': 'success', 'rates': {'UZS': reference_uzs}})
        raise httpx.ConnectTimeout('timed out', request=request)
    return source


def test_browser_submitted_rate_is_checked_against_market_and_unblocks_deposits(tmp_path):
    app = create_app(Settings(data_dir=tmp_path), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     rate_transport=httpx.MockTransport(blocked_cbu()))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        blocked = client.get('/api/cashier/usd-rate', params={'date': '2026-10-24'})
        assert blocked.status_code == 503
        refused = client.post('/api/cashier/usd-deposits', json={'date': '2026-10-24', 'amount': '200'})
        assert refused.status_code == 503 and 'Взнос не сохранён' in refused.json()['detail']

        submitted = client.post('/api/cashier/usd-rate', json={'date': '2026-10-24', 'payload': CBU_PAYLOAD})
        assert submitted.status_code == 200
        assert submitted.json() == {
            'date': '2026-10-24', 'source_date': '2026-10-24',
            'official_rate': '11838.92', 'restaurant_rate': '11600', 'discount_percent': '1.5'}
        assert client.get('/api/cashier/usd-rate', params={'date': '2026-10-24'}).json() == submitted.json()
        assert client.post('/api/cashier/usd-deposits', json={
            'date': '2026-10-24', 'amount': '200'}).status_code == 201

        # Повторная отправка (даже с другим курсом) не перезаписывает сохранённый.
        again = client.post('/api/cashier/usd-rate', json={'date': '2026-10-24', 'payload': [
            dict(CBU_PAYLOAD[0], Rate='11900.00')]})
        assert again.status_code == 200 and again.json()['official_rate'] == '11838.92'


def test_browser_submitted_rate_far_from_market_or_malformed_is_rejected(tmp_path):
    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
                     rate_transport=httpx.MockTransport(blocked_cbu()))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        inflated = client.post('/api/cashier/usd-rate', json={'date': '2026-10-24', 'payload': [
            dict(CBU_PAYLOAD[0], Rate='12300.00')]})  # +3,6% к рынку
        assert inflated.status_code == 422 and 'расходится с рыночным' in inflated.json()['detail']
        for payload in ([], [{'Ccy': 'EUR', 'Nominal': '1', 'Rate': '13000', 'Date': '24.10.2026'}],
                        [dict(CBU_PAYLOAD[0], Date='25.10.2026')], [dict(CBU_PAYLOAD[0], Rate='abc')]):
            assert client.post('/api/cashier/usd-rate', json={
                'date': '2026-10-24', 'payload': payload}).status_code == 422
        assert client.get('/api/cashier/usd-rate', params={'date': '2026-10-24'}).status_code == 503


def test_browser_submitted_rate_without_market_reference_is_rejected(tmp_path):
    def nothing_reachable(request):
        raise httpx.ConnectTimeout('timed out', request=request)

    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
                     rate_transport=httpx.MockTransport(nothing_reachable))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.post('/api/cashier/usd-rate', json={'date': '2026-10-24', 'payload': CBU_PAYLOAD})
        assert response.status_code == 422 and 'сверить' in response.json()['detail']


def test_browser_fallback_is_wired_in_the_cashier_screen():
    root = Path(__file__).resolve().parent.parent / 'retro'
    script = (root / 'static' / 'app.js').read_text(encoding='utf-8')
    assert 'https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/' in script
    assert "request('/api/cashier/usd-rate'" in script
    # Политика безопасности страницы должна пускать браузер к ЦБ.
    assert "connect-src 'self' https://cbu.uz" in (root / 'app.py').read_text(encoding='utf-8')
