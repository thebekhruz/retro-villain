import asyncio
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.integrations.iiko_prepayment_sources import (
    IikoPrepaymentSources, CLOUD_READ_PATHS, payment_matches, payments_from_shift,
    reservations_from_workload,
)
from retro.modules.cashier.service import DataError, Prepayment, RETRO_REGISTER, TZ

DAY = date(2026, 10, 10)
IDS = [f'00000000-0000-4000-8000-{i:012d}' for i in range(10)]
SHIFT, PAYMENT, TYPE, TABLE, SECTION, RESERVE, ORDER, SECOND = IDS[:8]
SECTIONS = [{'id': SECTION, 'name': 'Ресторан', 'tables': [{'id': TABLE, 'number': 22}]}]
RESERVES = {'reserves': [{'id': RESERVE, 'estimatedStartTime': '2026-10-16 19:00:00.000',
                         'durationInMinutes': 180, 'guestsCount': 4, 'tableIds': [TABLE]}]}


def shift_payload():
    return {'data': {'transactions': {'cashlessRecords': [{'info': {
        'id': PAYMENT, 'type': 'PREPAY', 'sum': 500000, 'paymentTypeId': TYPE,
        'creationDate': int(datetime(2026, 10, 10, 13, 27, 28, tzinfo=TZ).timestamp()),
    }}]}}, 'decoration': {'paymentTypes': {TYPE: 'Click/Payme'}}}


def entry(id='ledger', order=ORDER, stamp='2026-10-10T13:27:28.998+05:00', amount='500000'):
    return Prepayment(id, stamp, Decimal(amount), order_number='71260', order_id=order)


def payment():
    return payments_from_shift(shift_payload(), SHIFT, DAY, DAY)[0]


def test_workload_preserves_future_date_and_does_not_link_order():
    data = deepcopy(RESERVES)
    data['reserves'] *= 2
    rows = reservations_from_workload(SECTIONS, data, DAY, date(2026, 10, 20))
    assert len(rows) == 1
    assert rows[0]['estimated_start_time'] == '2026-10-16T19:00:00+05:00'
    assert rows[0]['tables'][0]['number'] == 22
    assert rows[0]['order_id'] is None
    assert reservations_from_workload(SECTIONS, data, DAY, DAY) == []
    with pytest.raises(DataError):
        reservations_from_workload([], data, DAY, date(2026, 10, 20))


def test_payment_types_use_records_not_accounts_or_absence():
    data = shift_payload()
    data['data']['transactions']['cashlessRecords'].append({'info': {'type': 'CARD'}})
    data['data']['transactions']['cashlessRecords'] *= 2
    rows = payments_from_shift(data, SHIFT, DAY, DAY)
    assert len(rows) == 1 and rows[0]['payment_method'] == 'Click/Payme'
    assert rows[0]['order_id'] is None and rows[0]['amount'] == '500000'
    data['decoration']['paymentTypes'] = {}
    assert payments_from_shift(data, SHIFT, DAY, DAY)[0]['payment_method'] is None
    assert payments_from_shift(data, SHIFT, date(2026, 10, 11), date(2026, 10, 11)) == []


def test_conflicting_duplicate_and_invalid_amount_fail_closed():
    data = shift_payload()
    duplicate = deepcopy(data['data']['transactions']['cashlessRecords'][0])
    duplicate['info']['sum'] = 600000
    data['data']['transactions']['cashlessRecords'].append(duplicate)
    with pytest.raises(DataError):
        payments_from_shift(data, SHIFT, DAY, DAY)
    data = shift_payload()
    data['data']['transactions']['cashlessRecords'][0]['info']['sum'] = -1
    with pytest.raises(DataError):
        payments_from_shift(data, SHIFT, DAY, DAY)


def test_unique_match_is_still_unconfirmed_and_never_mutates_source():
    original = payment()
    result = payment_matches([original], [entry()])[0]
    assert result['candidate_order']['order_id'] == ORDER
    assert result['link_status'] == 'unconfirmed' and result['order_id'] is None
    assert 'candidate_order' not in original
    assert payment_matches([original], [entry(amount='499999')])[0]['link_status'] == 'unlinked'
    assert payment_matches([original], [entry(stamp='2026-10-10T13:27:29+05:00')])[0]['link_status'] == 'unlinked'


@pytest.mark.parametrize('data', [{'data': None}, {'data': {'transactions': None}},
    {'data': {'transactions': {'cashlessRecords': [{'info': None}]}}, 'decoration': {'paymentTypes': {}}}])
def test_missing_shift_data_is_not_silently_an_empty_payment_list(data):
    with pytest.raises(DataError):
        payments_from_shift(data, SHIFT, DAY, DAY)


def test_ambiguous_matches_in_either_direction_do_not_suggest_order():
    payments = [payment()]
    result = payment_matches(payments, [entry(), entry(id='other', order=SECOND)])[0]
    assert result['link_status'] == 'ambiguous' and result['candidate_order'] is None
    result = payment_matches([payment(), {**payment(), 'id': SECOND}], [entry()])
    assert all(row['link_status'] == 'ambiguous' and row['candidate_order'] is None for row in result)


def clients(*, fail_cloud=False, fail_ledger=False, fail_payments=False, missing_open_shift=False):
    calls = []

    def web_handler(request):
        path = request.url.path
        calls.append(('web', request.method, path))
        if path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'web-test-secret'})
        if path == '/api/integration-management/api-logins/get':
            return httpx.Response(200, json={'apiLoginInfo': {'apiKey': 'cloud-test-secret'}})
        if path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': [{'id': SHIFT, 'cashRegNumber': 1},
                {'id': SHIFT, 'cashRegNumber': 1}, {'id': SECOND, 'cashRegNumber': 1 if missing_open_shift else 2}]})
        if path == '/api/cash/shift/details/' + SECOND and missing_open_shift:
            return httpx.Response(200, json={'data': {'transactions': None}, 'decoration': {}})
        if path == '/api/cash/shift/details/' + SHIFT:
            assert request.method == 'GET'
            return httpx.Response(503, text='secret upstream content') if fail_payments else httpx.Response(200, json=shift_payload())
        if path == '/api/olap/init':
            body = json.loads(request.content)
            assert body['groupFields'][-1] == 'OrderId'
            assert body['filters'][1]['valueList'] == ['PREPAY']
            return httpx.Response(200, json={'fetchId': 'safe-report'})
        if path == '/api/olap/fetch/safe-report/grouped-table':
            values = [RETRO_REGISTER, None, '2026-10-10T13:27:28.998', '71260',
                      'Предоплата за заказы', 'Текущие расчеты с сотрудниками', None, None,
                      ORDER, 0, 500000]
            rows = [{}] if fail_ledger else [{f'field{i}': {'value': value} for i, value in enumerate(values)}]
            return httpx.Response(200, json={'result': {'rows': rows}})
        raise AssertionError('Unexpected web request: ' + path)

    def cloud_handler(request):
        path = request.url.path
        calls.append(('cloud', request.method, path))
        assert path in CLOUD_READ_PATHS
        if path == '/api/1/access_token':
            assert json.loads(request.content) == {'apiLogin': 'cloud-test-secret'}
            return httpx.Response(200, json={'token': 'cloud-test-token'})
        assert request.headers['Authorization'] == 'Bearer cloud-test-token'
        if fail_cloud:
            return httpx.Response(403, text='secret upstream content')
        if path == '/api/1/reserve/available_restaurant_sections':
            return httpx.Response(200, json={'restaurantSections': SECTIONS})
        return httpx.Response(200, json=RESERVES)

    settings = Settings(login='test', password='test', store_id=82907)
    web = IikoClient(settings, transport=httpx.MockTransport(web_handler), poll_delay=0)
    source = IikoPrepaymentSources(settings, web, transport=httpx.MockTransport(cloud_handler))
    return source, web, calls


def run_read(**kwargs):
    async def run():
        source, web, calls = clients(**kwargs)
        try:
            result = await source.read(DAY, date(2026, 10, 20))
            count = len(calls)
            again = await source.read(DAY, date(2026, 10, 20))
            return result, again, calls, count
        finally:
            await source.close()
            await web.close()
    return asyncio.run(run())


def test_live_shape_integration_uses_only_reads_and_caches_success():
    result, again, calls, count = run_read()
    assert result == again and len(calls) == count
    assert result['reservations']['items'][0]['id'] == RESERVE
    assert result['payments']['items'][0]['candidate_order']['order_id'] == ORDER
    assert result['links_verified'] is False
    assert 'secret' not in json.dumps(result)
    assert sum(path.endswith('/details/' + SHIFT) for _, _, path in calls) == 1
    assert not any('init_by' in path or 'save' in path or 'delete' in path for _, _, path in calls)


@pytest.mark.parametrize('failure,available,unavailable', [
    ('fail_cloud', 'payments', 'reservations'), ('fail_payments', 'reservations', 'payments'),
])
def test_source_failure_isolated_and_not_shown_as_empty(failure, available, unavailable, caplog):
    result, _, _, _ = run_read(**{failure: True})
    assert result[available]['status'] == 'ok'
    assert result[unavailable]['status'] == 'unavailable' and result[unavailable]['items'] is None
    assert 'secret' not in caplog.text and 'secret' not in json.dumps(result)


def test_ledger_failure_keeps_payment_but_never_confirms_match():
    result, _, _, _ = run_read(fail_ledger=True)
    assert result['payments']['status'] == 'ok' and result['matching_issue']
    assert result['payments']['items'][0]['candidate_order'] is None


def test_unavailable_open_shift_preserves_other_payments_with_partial_status():
    result, _, _, _ = run_read(missing_open_shift=True)
    assert result['payments']['status'] == 'partial'
    assert result['payments']['issue']
    assert len(result['payments']['items']) == 1
    assert result['payments']['items'][0]['candidate_order']['order_id'] == ORDER


def test_future_period_never_requests_cash_shifts_or_olap():
    async def run():
        source, web, calls = clients()
        try:
            result = await source.read(date(2099, 1, 1), date(2099, 1, 10))
            assert result['payments']['items'] == []
            assert not any('/shift/' in path or '/olap/' in path for _, _, path in calls)
            with pytest.raises(DataError):
                await source._cloud_send('/api/1/order/init_by_table', {})
        finally:
            await source.close()
            await web.close()
    asyncio.run(run())


def test_endpoint_future_demo_and_period_validation_without_registry_writes(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/cashier/prepayment-sources?start=2099-01-01&end=2099-01-31&demo=true')
        assert response.status_code == 200
        assert response.json()['reservations']['items'][0]['estimated_start_time'].startswith('2099-01-01')
        assert response.json()['payments']['items'] == []
        assert app.state.prepayment_registry.rows_between(date(2000, 1, 1), date(2100, 1, 1)) == []
        for query in ['start=2026-10-10', 'start=2026-10-11&end=2026-10-10',
                      'start=2026-10-01&end=2026-11-01']:
            assert client.get('/api/cashier/prepayment-sources?' + query).status_code == 422
        result = client.get('/api/cashier/prepayment-sources?start=2026-10-10&end=2026-10-10').json()
        assert result['reservations']['status'] == result['payments']['status'] == 'unavailable'
