import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI

from retro.integrations.iiko import IikoClient
from retro.modules.cashier.routes import router
from retro.modules.cashier.service import DataError, Payment, SnapshotCache, demo_snapshot
from retro.modules.cashier.transfer_breakdown import (
    GROUPS, NOTE, PAYMENT_NAME, breakdown_from_olap,
)

DAY = date(2026, 10, 6)


class AsgiClient:
    def __init__(self, app):
        self.app = app

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def get(self, path, *, params):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url='http://test') as client:
                return await client.get(path, params=params)
        return asyncio.run(request())


def payment(amount, suffix='1', *, comment=None):
    values = [PAYMENT_NAME, None, f'transaction-{suffix}', f'order-{suffix}',
              70713, '2026-10-06T15:45:20.167', comment, amount]
    return {f'field{index}': {'value': value} for index, value in enumerate(values)}


def nested_payment(amount, suffix='1'):
    row = payment(amount, suffix)
    node = {'field6': row['field6'], 'field7': row['field7']}
    for index in reversed(range(6)):
        node = {f'field{index}': row[f'field{index}'], 'field7': row['field7'],
                'children': [node]}
    return node


def test_receipt_payments_do_not_double_count_subtotals_or_guess_providers():
    data = breakdown_from_olap(DAY, [nested_payment(amount, str(index))
                                   for index, amount in enumerate([906000, 406500, 42000, 300000])])
    assert data['total'] == '1654500'
    assert len(data['rows']) == 4
    assert data['provider_split_known'] is False
    assert data['note'] == NOTE
    assert data['rows'][0]['received_at'] == '2026-10-06T15:45:20.167000+05:00'


def test_leaf_amount_is_required_and_parent_amount_cannot_fill_it():
    row = nested_payment(300000)
    leaf = row
    while 'children' in leaf:
        leaf = leaf['children'][0]
    del leaf['field7']
    with pytest.raises(DataError):
        breakdown_from_olap(DAY, [row])


@pytest.mark.parametrize('change', [
    lambda row: row.update(children=[]),
    lambda row: row.update(field7={'value': float('nan')}),
    lambda row: row.update(field5={'value': 'bad'}),
    lambda row: row.update(field6={'value': []}),
])
def test_malformed_source_is_not_reported_as_empty(change):
    row = payment(300000)
    change(row)
    with pytest.raises(DataError):
        breakdown_from_olap(DAY, [row])


def test_duplicate_payment_is_rejected_but_empty_day_is_valid():
    with pytest.raises(DataError):
        breakdown_from_olap(DAY, [payment(300000), payment(300000)])
    assert breakdown_from_olap(DAY, [])['total'] == '0'


def test_source_filters_scope_retro_payments_and_exclude_school_banquet_and_prepay():
    client = IikoClient(SimpleNamespace(configured=True))
    seen = {}

    @asynccontextmanager
    async def source():
        yield object()

    async def olap(_client, day, groups, fields, filters):
        seen.update(day=day, groups=groups, fields=fields,
                    filters={item['field']: item for item in filters})
        return [payment(42000)]  # This payment's part of a mixed-payment receipt.

    client._client = source
    client._olap = olap
    result = asyncio.run(client.load_transfer_breakdown(DAY))
    assert result['total'] == '42000'
    assert seen['groups'] == list(GROUPS)
    assert seen['fields'] == ['DishDiscountSumInt']
    assert seen['filters']['CashRegisterName']['valueList'] == ['Kassa-FiscalBox1']
    assert seen['filters']['RestaurantSection']['valueList'] == ['Бехруз (Свадьба)']
    assert seen['filters']['RestaurantSection']['inclusiveList'] is False
    assert seen['filters']['OperationType']['valueList'] == ['PAYMENT']
    assert seen['filters']['PayTypes']['valueList'] == [PAYMENT_NAME]


def endpoint_client(*, amount=300000, source_amount=300000, **snapshot_flags):
    app = FastAPI()
    app.include_router(router)
    app.state.cache = SnapshotCache()
    snapshot = replace(demo_snapshot(DAY), demo=False,
                       payments=(Payment(PAYMENT_NAME, Decimal(amount)),), **snapshot_flags)
    app.state.cache.put(snapshot)

    async def load(_day):
        return breakdown_from_olap(_day, [payment(source_amount)])

    app.state.iiko = SimpleNamespace(load_transfer_breakdown=load)
    return AsgiClient(app), dict(date=DAY.isoformat(), snapshot_id=snapshot.id)


def test_endpoint_uses_displayed_snapshot_and_rejects_wrong_day_id_and_future():
    client, params = endpoint_client()
    with client:
        response = client.get('/api/cashier/payment-breakdown', params=params)
        assert response.status_code == 200
        assert response.json()['total'] == '300000'
        assert client.get('/api/cashier/payment-breakdown',
                          params={**params, 'date': '2026-10-05'}).status_code == 409
        for changes, status in [({'snapshot_id': 'a' * 32}, 409),
                                ({'snapshot_id': 'bad'}, 422),
                                ({'date': '2099-01-01'}, 422)]:
            assert client.get('/api/cashier/payment-breakdown',
                              params={**params, **changes}).status_code == status


@pytest.mark.parametrize('flags', [{'stale': True}, {'refreshing': True}])
def test_endpoint_does_not_mix_stale_snapshot_with_fresh_details(flags):
    client, params = endpoint_client(**flags)
    with client:
        assert client.get('/api/cashier/payment-breakdown', params=params).status_code == 409


def test_endpoint_rejects_a_changed_source_total():
    client, params = endpoint_client(source_amount=450000)
    with client:
        response = client.get('/api/cashier/payment-breakdown', params=params)
        assert response.status_code == 409
        assert response.json()['detail'] == 'Данные iiko изменились. Обновите день.'


def test_endpoint_refreshes_old_details_after_day_refresh():
    client, params = endpoint_client()
    amounts = iter([200000, 300000])

    async def load(day):
        return breakdown_from_olap(day, [payment(next(amounts))])

    client.app.state.iiko.load_transfer_breakdown = load
    with client:
        assert client.get('/api/cashier/payment-breakdown', params=params).json()['total'] == '300000'


def test_endpoint_reports_upstream_failure_without_leaking_response():
    client, params = endpoint_client()

    async def load(day):
        raise DataError('private upstream details')

    client.app.state.iiko.load_transfer_breakdown = load
    with client:
        response = client.get('/api/cashier/payment-breakdown', params=params)
        assert response.status_code == 503
        assert 'private' not in response.text
