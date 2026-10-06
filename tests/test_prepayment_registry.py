import asyncio
import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import httpx
import pytest

from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.archive import decode_snapshot
from retro.modules.cashier.prepayments import prepayments_from_olap, report_body
from retro.modules.cashier.service import DataError, RETRO_REGISTER, demo_snapshot

DAY = date(2026, 10, 4)


def branch(amount=500000, *, register=RETRO_REGISTER, section=None,
           stamp='2026-10-04T18:10:31.522', order='70508', account='Предоплата за заказы'):
    values = [register, section, stamp, order, account,
              'Текущие расчеты с сотрудниками', 'Кассир', None]
    def node(index):
        value = {f'field{index}': {'value': values[index]},
                 'field8': {'value': 0}, 'field9': {'value': amount}}
        if index < 7:
            value['children'] = [node(index + 1)]
        return value
    return node(0)


def test_registry_counts_only_one_side_and_excludes_school_and_banquet():
    rows = [branch(), branch(account='Текущие расчеты с сотрудниками'),
            branch(register='GL-Kassa-Oksbrich'), branch(section='Бехруз (Свадьба)'),
            branch(200000, section='Ресторан', stamp='2026-10-04T15:21:13.703', order='70474')]
    result = prepayments_from_olap(rows)
    assert [entry.amount for entry in result] == [Decimal(200000), Decimal(500000)]
    assert result[0].order_number == '70474'
    assert result[0].received_at.endswith('+05:00')
    # An employee settlement is used for both cash and noncash advances in iiko.
    assert all(entry.payment_method is None and entry.comment == '' for entry in result)


def test_real_four_advance_shape_has_no_double_counting():
    values = [(200000, '15:21:13.703', '70474'), (500000, '15:21:39.466', '70475'),
              (500000, '18:10:31.522', '70508'), (1000000, '20:54:29.592', '70551')]
    rows = [branch(amount, stamp='2026-10-04T' + stamp, order=order)
            for amount, stamp, order in values]
    entries = prepayments_from_olap(rows)
    assert len(entries) == 4
    assert len({entry.id for entry in entries}) == 4
    assert sum(entry.amount for entry in entries) == Decimal(2200000)
    assert prepayments_from_olap([]) == ()


@pytest.mark.parametrize('rows', [None, [{}], [{'children': []}], [branch(-1)],
                                  [branch(stamp='wrong')]])
def test_registry_rejects_partial_or_invalid_data(rows):
    with pytest.raises(DataError):
        prepayments_from_olap(rows)


def test_registry_roundtrip_and_legacy_unknown_are_distinct():
    snapshot = replace(demo_snapshot(DAY), demo=False,
                       prepayments=prepayments_from_olap([branch()]))
    assert decode_snapshot(json.dumps(snapshot.json())).prepayments == snapshot.prepayments
    payload = snapshot.json()
    del payload['prepayments']
    del payload['prepayments_issue']
    assert decode_snapshot(json.dumps(payload)).prepayments is None


def test_leaf_cannot_borrow_parent_subtotal():
    row = branch()
    leaf = row
    while 'children' in leaf:
        leaf = leaf['children'][0]
    del leaf['field9']
    with pytest.raises(DataError):
        prepayments_from_olap([row])


def test_iiko_registry_uses_received_operation_and_operational_day():
    requests = []
    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': 'prepayments'})
        return httpx.Response(200, json={'result': {'rows': [branch()]}})
    async def run():
        source = IikoClient(Settings(login='test', password='test', store_id=82907),
                            transport=httpx.MockTransport(handler), poll_delay=0)
        try:
            return await source.load_prepayments(DAY)
        finally:
            await source.close()
    assert asyncio.run(run())[0].amount == 500000
    assert requests == [report_body(82907, DAY)] * 2
    assert requests[0]['filters'][0]['field'] == 'DateTime.OperDayFilter'
    assert requests[0]['filters'][1]['valueList'] == ['PREPAY']
    assert requests[0]['filters'][2]['valueList'] == [RETRO_REGISTER]


def test_day_enriches_old_snapshot_and_preserves_export_revision(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from retro.app import create_app
    from retro.modules.cashier import routes

    app = create_app(Settings(data_dir=tmp_path))
    base = replace(demo_snapshot(DAY), demo=False)
    outcomes = iter([prepayments_from_olap([branch()]), (), DataError('Unavailable')])
    async def load(state, method, day, **kwargs):
        assert day == DAY
        if method == 'load':
            return base
        assert method == 'load_prepayments'
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(routes, 'load_iiko', load)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        first, empty, failed = [client.get('/api/cashier/day?date=2026-10-04').json() for _ in range(3)]
        assert first['prepayments'][0]['amount'] == '500000'
        assert empty['prepayments'] == []
        assert failed['prepayments'] is None and failed['prepayments_issue']
        assert len({first['snapshot_id'], empty['snapshot_id'], failed['snapshot_id']}) == 3
        original = app.state.cache.get(first['snapshot_id'], DAY)
        assert original.prepayments[0].amount == 500000
        assert original.revenue == base.revenue
        response = client.get('/api/cashier/export', params={'date': DAY.isoformat(),
                              'snapshot_id': first['snapshot_id']})
        assert response.status_code == 200
        from io import BytesIO
        import openpyxl
        workbook = openpyxl.load_workbook(BytesIO(response.content))
        assert workbook['отчет']['B4'].value == 500000
