import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.prepayment_methods import breakdown_from_shifts
from retro.modules.cashier.routes import router
from retro.modules.cashier.service import DataError, SnapshotCache, demo_snapshot

DAY = date(2026, 10, 6)
SHIFT = '11111111-1111-4111-8111-111111111111'


def detail(*, open=False, available=True):
    return dict(data=dict(shift=dict(id=SHIFT, cashRegNumber=1,
                                    openDate='2026-10-06T10:00:00', isOpen=open,
                                    sessionStatus='OPEN' if open else 'UNACCEPTED', sessionNumber=543),
                          transactions=dict(cashlessRecords=[
                              dict(info=dict(id='a', type='PREPAY', sum=300000,
                                             paymentTypeId='qr', creationDate=1791270000)),
                              dict(info=dict(id='b', type='CARD', sum=900000,
                                             paymentTypeId='qr', creationDate=1791270000)),
                              dict(info=dict(id='c', type='PREPAY', sum=200000,
                                             paymentTypeId='card', creationDate=1791271000)),
                              dict(info=dict(id='d', type='PREPAY', sum=100000,
                                             paymentTypeId='qr', creationDate=1791272000)),
                          ]) if available else None),
                decoration=dict(paymentTypes={'qr': 'Единый QR', 'card': 'Xumo'}))


def test_incoming_payments_are_grouped_once_and_keep_times_and_ids():
    result = breakdown_from_shifts(DAY, [detail()])
    assert result['status'] == 'ready' and result['total'] == '600000'
    groups = {p['name']: p for p in result['payments']}
    assert groups['Единый QR']['amount'] == '400000'
    assert [e['id'] for e in groups['Единый QR']['entries']] == ['a', 'd']
    assert groups['Xumo']['entries'][0]['received_at'].endswith('+05:00')
    assert 'Инкасса QR' in result['note']


def test_availability_is_read_from_iiko_not_inferred_from_day_or_open_flag():
    assert breakdown_from_shifts(DAY, [detail(open=True)])['status'] == 'ready'
    result = breakdown_from_shifts(DAY, [detail(open=True, available=False)])
    assert result['status'] == 'pending' and result['total'] is None
    assert result['payments'] == [] and 'завтра' in result['note']
    with pytest.raises(DataError):
        breakdown_from_shifts(DAY, [detail(available=False)])


def test_partially_loaded_day_is_not_reported_as_complete():
    result = breakdown_from_shifts(DAY, [detail(), detail(open=True, available=False)])
    assert result['status'] == 'pending' and result['payments'] == []


@pytest.mark.parametrize('field,value', [('sum', -1), ('sum', None), ('sum', float('nan')),
                                       ('id', None), ('paymentTypeId', 'missing'),
                                       ('creationDate', None), ('creationDate', True)])
def test_partial_operations_never_become_zero_or_guessed_payments(field, value):
    data = detail()
    data['data']['transactions']['cashlessRecords'][0]['info'][field] = value
    with pytest.raises(DataError):
        breakdown_from_shifts(DAY, [data])


def test_duplicates_other_registers_and_missing_rows_are_rejected():
    for change in [lambda d: d['data']['shift'].update(cashRegNumber=2),
                   lambda d: d['data']['transactions'].pop('cashlessRecords'),
                   lambda d: d['data']['transactions']['cashlessRecords'].append(
                       deepcopy(d['data']['transactions']['cashlessRecords'][0]))]:
        data = detail()
        change(data)
        with pytest.raises(DataError):
            breakdown_from_shifts(DAY, [data])
    assert breakdown_from_shifts(DAY, [])['total'] == '0'


def test_iiko_reads_only_selected_shifts_and_refreshes_auth_on_detail_get():
    calls, rejected = [], False

    def handler(request):
        nonlocal rejected
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        if request.url.path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': [detail()['data']['shift'],
                dict(id='school', cashRegNumber=2, openDate=DAY.isoformat())]})
        assert request.method == 'GET' and request.content == b''
        if not rejected:
            rejected = True
            return httpx.Response(401)
        return httpx.Response(200, json=detail())

    async def run():
        source = IikoClient(Settings(login='test', password='test', store_id=82907),
                            transport=httpx.MockTransport(handler), poll_delay=0)
        try:
            return await source.load_prepayment_methods(DAY)
        finally:
            await source.close()
    assert asyncio.run(run())['total'] == '600000'
    assert calls.count(('POST', '/api/auth/login')) == 2
    assert calls.count(('GET', f'/api/cash/shift/details/{SHIFT}')) == 2


def endpoint(*, result=None, error=False, **flags):
    app = FastAPI()
    app.include_router(router)
    app.state.cache = SnapshotCache()
    snapshot = replace(demo_snapshot(DAY), demo=False, new_prepayment=Decimal(800000),
                       cash_prepayment=Decimal(200000), **flags)
    app.state.cache.put(snapshot)
    async def load(day):
        if error:
            raise DataError('secret upstream response')
        return result or breakdown_from_shifts(day, [detail()])
    app.state.iiko = SimpleNamespace(load_prepayment_methods=load)
    return app, dict(date=DAY.isoformat(), snapshot_id=snapshot.id)


def get(app, params):
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            return await client.get('/api/cashier/prepayment-breakdown', params=params)
    return asyncio.run(request())


def test_api_binds_breakdown_to_displayed_day_and_preserves_totals():
    app, params = endpoint()
    response = get(app, params)
    assert response.status_code == 200
    assert response.json()['snapshot_id'] == params['snapshot_id']
    assert response.json()['total'] == '600000'
    assert app.state.cache.get(params['snapshot_id'], DAY).new_prepayment == 800000
    assert get(app, {**params, 'date': '2026-10-05'}).status_code == 409
    assert get(app, {**params, 'snapshot_id': 'a' * 32}).status_code == 409
    assert get(app, {**params, 'date': '2099-01-01'}).status_code == 422


def test_api_reports_pending_even_when_totals_are_unknown():
    app, params = endpoint(result=breakdown_from_shifts(DAY, [detail(open=True, available=False)]),
                           prepayment_issue='unknown')
    response = get(app, params)
    assert response.status_code == 200 and response.json()['status'] == 'pending'


@pytest.mark.parametrize('flags', [dict(stale=True), dict(refreshing=True), dict(prepayment_issue='unknown')])
def test_api_rejects_stale_or_unreconciled_snapshot(flags):
    app, params = endpoint(**flags)
    assert get(app, params).status_code == 409


def test_api_mismatch_and_upstream_failure_do_not_become_zero():
    app, params = endpoint(result=dict(status='ready', payments=[], total='0'))
    assert get(app, params).status_code == 409
    app, params = endpoint(error=True)
    response = get(app, params)
    assert response.status_code == 503 and 'secret' not in response.text
