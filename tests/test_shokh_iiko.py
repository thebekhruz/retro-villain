"""Procurement contracts and failure recovery, without production writes."""
import asyncio
from types import SimpleNamespace
import copy
import json
import os
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.service import TZ
from retro.modules.shokh.iiko import ProcurementIiko
from retro.modules.shokh.sync import ProcurementSync
from retro.modules.shokh.store import ShokhStore

DAY = date(2026, 9, 27)


class Upstream:
    def __init__(self):
        self.calls = []
        self.documents = {}
        self.mode = 'ok'
        self.catalog_down = False

    def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-token'})
        if self.catalog_down and path == '/api/documents/storage/list':
            return httpx.Response(503)
        if path == '/api/documents/storage/list':
            data = [{'id': 'storage', 'name': 'Кухня', 'storageType': 'INVENTORY_ASSETS'}]
        elif path == '/api/documents/suppliers/list':
            data = {'supplier': {'id': 'supplier', 'name': 'Поставщик'},
                    'deleted': {'id': 'deleted', 'name': 'Удалён', 'deleted': True}}
        elif path == '/api/entities/list-of-type':
            data = ([{'id': 'kg', 'name': 'кг'}] if request.url.params['type'] == 'MeasureUnit'
                    else [{'id': 'vat', 'vatPercent': 12}])
        elif path == '/api/productV3/list':
            data = [{'id': 'product', 'name': {'customValue': 'Томаты'}, 'num': '00001',
                     'type': 'GOODS', 'mainUnit': 'kg', 'taxCategory': 'vat', 'deleted': False},
                    {'id': 'deleted', 'name': 'Deleted', 'type': 'GOODS', 'mainUnit': 'kg', 'deleted': True}]
        elif path == '/api/permissions/my':
            return httpx.Response(200, json=['DOCUMENT_BUNDLE_INCOMING_INVOICE_POST'])
        elif path == '/api/documents/config/store-settings':
            data = {'docSettings': {'defaultDocumentTimes': [
                {'documentType': 'INCOMING_INVOICE', 'timeType': 'SPECIFIC_TIME', 'minutes': 540}]}}
        elif path == '/api/documents/create':
            if self.mode == 'rejected':
                return httpx.Response(200, json={'error': True, 'errorMessage': 'Период закрыт', 'data': None})
            if self.mode == 'timeout_before':
                raise httpx.ReadTimeout('Lost response', request=request)
            if self.mode == 'http_timeout':
                return httpx.Response(408, json={'error': True})
            payload = json.loads(request.content)
            doc = copy.deepcopy(payload)
            doc.update(id=str(uuid4()), documentNumber='4001', sum=payload['items'][0]['sum'])
            if self.mode == 'mismatch':
                doc['items'][0]['amount'] += 1
            if self.mode == 'malformed_amount':
                doc['items'][0]['amount'] = 'invalid'
            if self.mode == 'draft':
                doc['status'] = 'DRAFT'
            self.documents[doc['id']] = doc
            if self.mode == 'timeout_after':
                raise httpx.ReadTimeout('Saved but response lost', request=request)
            return httpx.Response(200, json={'data': {'id': doc['id'], 'documentNumber': '4001'}})
        elif path.startswith('/api/documents/get/'):
            data = self.documents[path.rsplit('/', 1)[1]]
        elif path == '/api/documents/list':
            data = {'documents': list(self.documents.values())}
        else:
            raise AssertionError('Unexpected upstream request: ' + path)
        return httpx.Response(200, json={'error': False, 'data': data})

    @property
    def writes(self):
        return self.calls.count(('POST', '/api/documents/create'))


@pytest.fixture(params=['sqlite', 'postgres'])
def live(tmp_path, request):
    database_url = ''
    pg = None
    if request.param == 'postgres':
        database_url = os.getenv('RETRO_TEST_POSTGRES_URL', '')
        if not database_url:
            pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
        import psycopg
        from psycopg import sql
        schema = 'shokh_' + uuid4().hex
        pg = psycopg.connect(database_url, autocommit=True)
        pg.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        parts = urlsplit(database_url)
        query = dict(parse_qsl(parts.query)); query['options'] = '-csearch_path=' + schema
        database_url = urlunsplit(parts._replace(query=urlencode(query)))
    settings = Settings(database_url=database_url, login='test', password='test', store_id=82907, data_dir=tmp_path)
    app = create_app(settings)
    upstream = Upstream()
    app.state.iiko = IikoClient(settings, transport=httpx.MockTransport(upstream))
    app.state.shokh_iiko = ProcurementIiko(app.state.iiko)
    app.state.shokh_sync = ProcurementSync(app.state.shokh, app.state.shokh_iiko)
    # Do not run the unrelated cashier background worker in procurement tests.
    app.state.cashier_days = None
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as c:
        try:
            yield c, upstream
        finally:
            if pg is not None:
                pg.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
                pg.close()


def fields(**changes):
    result = dict(operation_id=str(uuid4()), date=DAY.isoformat(), point='Базар', item='UNTRUSTED',
                  unit='wrong', product_id='product', supplier_id='supplier', storage_id='storage',
                  unit_id='kg', quantity='1.125', price='11200')
    result.update(changes)
    return result


def test_catalog_maps_iiko_ids_filters_deleted_and_home_is_independent(live):
    c, upstream = live
    catalog = c.get('/api/shokh/catalog').json()
    assert catalog['source'] == 'iiko'
    assert catalog['can_create'] is True
    assert [(x['id'], x['unit_id']) for x in catalog['items']] == [('product', 'kg')]
    assert len(catalog['suppliers']) == 1
    assert 'settings' not in catalog
    c.app.state.shokh_iiko._until = 0
    upstream.catalog_down = True
    # Просрочен, но моложе часа: прежний справочник, iiko обновит фоном (T-400).
    assert c.get('/api/shokh/catalog').status_code == 200
    c.app.state.shokh_iiko._loaded_at = -10 ** 9
    assert c.get('/api/shokh/catalog').status_code == 503
    home = c.get('/api/shokh/home')
    assert home.status_code == 200 and 'level' not in home.json()


def test_purchase_posts_and_reads_back_exact_invoice_and_replay_does_not_duplicate(live):
    c, upstream = live
    body = fields()
    response = c.post('/api/shokh/purchase', data=body)
    assert response.status_code == 201, response.text
    purchase = response.json()['purchase']
    assert purchase['item'] == 'Томаты' and purchase['unit'] == 'кг'
    assert purchase['quantity'] == '1.125'
    assert purchase['total'] == '12600.00'
    assert purchase['iiko']['status'] == 'synced'
    doc = next(iter(upstream.documents.values()))
    assert doc['status'] == 'PROCESSED'
    assert doc['dateIncoming'] == '2026-09-27T09:00:00+05:00'
    assert doc['items'][0]['ndsPercent'] == 12
    assert doc['items'][0]['priceWithoutNds'] == 10000
    assert doc['items'][0]['sumWithoutNds'] == 11250
    assert doc['supplier'] == 'supplier' and doc['storage'] == 'storage'
    assert c.post('/api/shokh/purchase', data=body).json()['purchase']['id'] == purchase['id']
    assert upstream.writes == 1
    body['price'] = '12000'
    assert c.post('/api/shokh/purchase', data=body).status_code == 422
    assert upstream.writes == 1


def test_lost_response_is_reconciled_after_store_restart_without_second_write(live):
    c, upstream = live
    upstream.mode = 'timeout_after'
    response = c.post('/api/shokh/purchase', data=fields())
    assert response.status_code == 202
    row = response.json()['purchase']
    assert row['iiko']['status'] == 'uncertain'
    c.app.state.shokh_sync = ProcurementSync(c.app.state.shokh, c.app.state.shokh_iiko)
    result = c.get(f'/api/shokh/purchase/{row["id"]}/sync').json()['purchase']
    assert result['iiko']['status'] == 'synced'
    assert upstream.writes == 1
    assert len(c.app.state.shokh.purchases(DAY)) == 1


@pytest.mark.parametrize('mode', ['timeout_before', 'http_timeout', 'mismatch', 'malformed_amount', 'draft'])
def test_unknown_or_mismatched_result_cannot_be_resent_or_accepted(live, mode):
    c, upstream = live
    upstream.mode = mode
    row = c.post('/api/shokh/purchase', data=fields()).json()['purchase']
    assert row['iiko']['status'] == 'uncertain'
    assert c.post(f'/api/shokh/purchase/{row["id"]}/retry').json()['purchase']['iiko']['status'] != 'synced'
    assert upstream.writes == 1
    assert c.post(f'/api/accountant/shokh/purchases/{row["id"]}/accept',
                  json={'date': DAY.isoformat()}).status_code == 409


def test_definitive_rejection_can_retry_same_purchase(live):
    c, upstream = live
    upstream.mode = 'rejected'
    row = c.post('/api/shokh/purchase', data=fields()).json()['purchase']
    assert row['iiko']['status'] == 'rejected'
    upstream.mode = 'ok'
    result = c.post(f'/api/shokh/purchase/{row["id"]}/retry').json()['purchase']
    assert result['iiko']['status'] == 'synced' and result['id'] == row['id']
    assert upstream.writes == 2
    assert len(c.app.state.shokh.purchases(DAY)) == 1


@pytest.mark.parametrize('changes', [dict(product_id='deleted'), dict(supplier_id='deleted'),
    dict(storage_id='fake'), dict(unit_id='lb'), dict(operation_id='invalid'),
    dict(quantity='0.0001'), dict(quantity='NaN'), dict(quantity='1000000', price='1000000')])
def test_bad_mapping_or_amount_does_not_write_locally_or_to_iiko(live, changes):
    c, upstream = live
    assert c.post('/api/shokh/purchase', data=fields(**changes)).status_code == 422
    assert not c.app.state.shokh.purchases(DAY)
    assert upstream.writes == 0


def test_closed_trip_cannot_receive_a_purchase(live):
    c, upstream = live
    trip = c.post('/api/shokh/trip', params={'date': DAY.isoformat()}).json()['trip_id']
    c.post(f'/api/shokh/trip/{trip}/finish')
    assert c.post('/api/shokh/purchase', data=fields(trip_id=str(trip))).status_code == 422
    assert not c.app.state.shokh.purchases(DAY)
    assert upstream.writes == 0


def test_concurrent_acceptance_only_debits_once_and_failed_debit_rolls_back(live):
    c, upstream = live
    row = c.post('/api/shokh/purchase', data=fields()).json()['purchase']
    url = f'/api/accountant/shokh/purchases/{row["id"]}/accept'
    assert c.post(url, json={'date': DAY.isoformat()}).status_code == 422
    assert c.app.state.shokh.purchase(row['id'])['accepted_at'] is None
    finance = c.app.state.accountant_finance
    finance.reserve_entry(DAY, 'shoh', 'opening', '100000', 'Opening')
    def accept(_):
        return c.app.state.shokh.accept_with_finance(row['id'], DAY, datetime.now(TZ), finance)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, range(2)))
    assert sorted(results) == [False, True]
    assert Decimal(finance.reserves(DAY)['shoh']['balance']) == Decimal('87400')


def test_catalog_groups_same_item_by_unit(tmp_path):
    store = ShokhStore(tmp_path / 'store.sqlite3')
    for unit in ('кг', 'шт'):
        store.add_purchase(DAY, datetime.now(TZ), point='Базар', item='Томаты', unit=unit, quantity=1, price=1)
    assert {x['unit'] for x in store.frequent_items()} == {'кг', 'шт'}


def test_concurrent_same_key_claims_one_send(live):
    c, upstream = live
    source = c.app.state.shokh_iiko
    original = source.create
    async def delayed(payload):
        await asyncio.sleep(.08)
        return await original(payload)
    source.create = delayed
    body = fields()
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: c.post('/api/shokh/purchase', data=body), range(2)))
    assert {r.status_code for r in responses} <= {201, 202}
    assert len({r.json()['purchase']['id'] for r in responses}) == 1
    assert upstream.writes == 1
    assert len(c.app.state.shokh.purchases(DAY)) == 1


def test_unsent_reservation_survives_restart_and_can_be_sent(live):
    c, upstream = live
    sync = c.app.state.shokh_sync
    real_send = sync.send
    async def interrupted(purchase_id, **kwargs):
        return sync.result(purchase_id)
    sync.send = interrupted
    body = fields()
    row = c.post('/api/shokh/purchase', data=body).json()['purchase']
    assert row['iiko']['status'] == 'pending' and upstream.writes == 0
    c.app.state.shokh_sync = ProcurementSync(c.app.state.shokh, c.app.state.shokh_iiko)
    result = c.post(f'/api/shokh/purchase/{row["id"]}/retry').json()['purchase']
    assert result['iiko']['status'] == 'synced' and upstream.writes == 1
    assert c.get('/api/shokh/operation/' + body['operation_id']).json()['purchase']['id'] == row['id']


def test_accountant_photo_and_role_isolation(live):
    from dataclasses import replace
    c, upstream = live
    app = c.app
    # Authentication middleware closes over the initial settings object, so
    # set the mutable role dictionary rather than replacing app.state.settings.
    app.state.settings.dashboard_panel_users.update({
        'buyer': ('buyer', 'shokh'), 'bookkeeper': ('bookkeeper', 'accountant'),
        'cashier': ('cashier', 'cashier')})
    response = c.post('/api/shokh/purchase', data=fields(),
                      files={'photo': ('photo.png', b'\x89PNG\r\n\x1a\n', 'image/png')}, auth=('buyer', 'buyer'))
    assert response.status_code == 201
    row = response.json()['purchase']
    response = c.get(f'/api/accountant/shokh/photo/{row["id"]}', auth=('bookkeeper', 'bookkeeper'))
    assert response.status_code == 200 and response.content.startswith(b'\x89PNG')
    assert c.post(f'/api/shokh/purchase/{row["id"]}/retry', auth=('bookkeeper', 'bookkeeper')).status_code == 403
    assert c.get('/api/shokh/catalog', auth=('cashier', 'cashier')).status_code == 403


def test_half_kopeck_rounding_matches_ui_local_ledger_and_iiko(live):
    c, upstream = live
    response = c.post('/api/shokh/purchase', data=fields(quantity='1.125', price='1'))
    assert response.status_code == 201
    # Накладная — до тийина, наличные — целые сумы.
    assert response.json()['purchase']['invoice_total'] == '1.13'
    assert response.json()['purchase']['total'] == '1.00'
    assert next(iter(upstream.documents.values()))['sum'] == 1.13


def test_catalog_remembers_supplier_and_storage_of_the_last_invoice_per_point(live):
    """Нажатие на точку ведёт сразу к товару: поставщик и склад — с прошлой
    накладной этой точки."""
    c, _ = live
    assert c.get('/api/shokh/catalog').json()['point_defaults'] == {}
    assert c.post('/api/shokh/purchase', data=fields(point='RETRO')).status_code == 201
    defaults = c.get('/api/shokh/catalog').json()['point_defaults']
    assert defaults == {'RETRO': {'supplier_id': 'supplier', 'storage_id': 'storage'}}


# ── T-399: товар не из справочника iiko, наличные без тийинов, история ─────

def off_catalog(**changes):
    result = dict(operation_id=str(uuid4()), date=DAY.isoformat(), point='RETRO', item='Лепёшка тандырная',
                  unit='шт', quantity='10', price='5000', off_catalog='true',
                  supplier_id='supplier', storage_id='storage')
    result.update(changes)
    return result


def test_off_catalog_purchase_is_saved_without_invoice_and_accepted_by_accountant(live):
    c, upstream = live
    c.get('/api/shokh/catalog')  # справочник в памяти — оттуда названия поставщика и склада
    body = off_catalog()
    response = c.post('/api/shokh/purchase', data=body)
    assert response.status_code == 201, response.text
    row = response.json()['purchase']
    assert row['off_catalog'] is True and row['iiko']['status'] == 'manual'
    assert (row['item'], row['unit'], row['total']) == ('Лепёшка тандырная', 'шт', '50000.00')
    assert (row['supplier'], row['storage']) == ('Поставщик', 'Кухня')
    assert upstream.writes == 0
    # Потерянный ответ и «Повторить» — та же покупка, а не вторая.
    again = c.post('/api/shokh/purchase', data=body)
    assert again.json()['purchase']['id'] == row['id']
    assert len(c.app.state.shokh.purchases(DAY)) == 1
    assert c.post('/api/shokh/purchase', data=dict(body, price='6000')).status_code == 422
    assert c.get('/api/shokh/operation/' + body['operation_id']).json()['purchase']['id'] == row['id']
    # Ключ обязателен, единица — из списка, название — не пустое.
    assert c.post('/api/shokh/purchase', data=off_catalog(operation_id='bad')).status_code == 422
    assert c.post('/api/shokh/purchase', data=off_catalog(unit='мешок')).status_code == 422
    assert c.post('/api/shokh/purchase', data=off_catalog(item='  ')).status_code == 422
    assert c.post('/api/shokh/purchase', data=off_catalog(unit='пучок', item='Укроп')).status_code == 201
    # Шох — деньги ушли, как у любой покупки.
    finance = c.app.state.accountant_finance
    finance.reserve_entry(DAY, 'shoh', 'opening', '1000000', 'Opening')
    home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
    assert Decimal(home['pending']) == Decimal('100000')
    # Бухгалтер видит покупку «Нет в iiko» и может её принять.
    listed = c.get('/api/accountant/shokh/purchases', params={'date': DAY.isoformat()}).json()['purchases']
    assert {p['iiko']['status'] for p in listed} == {'manual'}
    accepted = c.post(f'/api/accountant/shokh/purchases/{row["id"]}/accept', json={'date': DAY.isoformat()})
    assert accepted.status_code == 200, accepted.text
    assert Decimal(finance.reserves(DAY)['shoh']['balance']) == Decimal('950000')
    assert upstream.writes == 0


def test_uneven_total_keeps_exact_invoice_but_cash_figures_are_whole(live):
    """«За всё» 100 000 на 3 шт: накладная 3 × 33 333,33 = 99 999,99, а наличных
    ушло 100 000 — на руках, потрачено, подотчёт и резервы без тийинов."""
    c, upstream = live
    finance = c.app.state.accountant_finance
    finance.reserve_entry(DAY, 'shoh', 'opening', '3108000', 'Opening')
    response = c.post('/api/shokh/purchase', data=fields(quantity='3', price='33333.33'))
    assert response.status_code == 201, response.text
    row = response.json()['purchase']
    assert row['invoice_total'] == '99999.99'
    assert row['total'] == '100000.00'
    assert next(iter(upstream.documents.values()))['sum'] == 99999.99
    assert Decimal(response.json()['pocket']) == Decimal('3008000')
    home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
    for field in ('pocket', 'pending', 'spent_day', 'spent_today', 'day_start'):
        assert Decimal(home[field]) == Decimal(home[field]).to_integral_value(), (field, home[field])
    assert c.post(f'/api/accountant/shokh/purchases/{row["id"]}/accept',
                  json={'date': DAY.isoformat()}).status_code == 200
    assert Decimal(finance.reserves(DAY)['shoh']['balance']) == Decimal('3008000')
    home = c.get('/api/shokh/home', params={'date': DAY.isoformat()}).json()
    assert Decimal(home['pocket']) == Decimal('3008000')


def test_history_matches_iiko_ids_per_point_and_defaults_survive_restart(live):
    c, upstream = live
    for point, qty in (('RETRO', '1'), ('RETRO', '2'), ('Школа MU', '1')):
        assert c.post('/api/shokh/purchase', data=fields(point=point, quantity=qty, price='9000')).status_code == 201
    history = c.get('/api/shokh/history').json()
    tomato = next(h for h in history['history'] if h['product_id'] == 'product')
    assert tomato['times'] == 3 and tomato['points'] == {'RETRO': 2, 'Школа MU': 1}
    assert tomato['usual_price'] == '9000.00'
    assert history['point_defaults']['RETRO'] == {'supplier_id': 'supplier', 'storage_id': 'storage'}
    catalog = c.get('/api/shokh/catalog').json()
    assert catalog['history'] == history['history'] and catalog['point_defaults'] == history['point_defaults']
    assert 'пучок' in catalog['custom_units']
    # Покупки из старой версии (без iiko_refs): поставщик, склад и товар
    # восстанавливаются из их накладной при старте.
    with c.app.state.shokh.db.connect() as connection:
        connection.execute('UPDATE shokh_purchases SET iiko_refs = NULL')
        connection.commit()
    assert c.get('/api/shokh/history').json()['point_defaults'] == {}
    c.app.state.shokh_sync = ProcurementSync(c.app.state.shokh, c.app.state.shokh_iiko)
    again = c.get('/api/shokh/history').json()
    assert again['point_defaults']['Школа MU'] == {'supplier_id': 'supplier', 'storage_id': 'storage'}
    assert next(h for h in again['history'] if h['product_id'] == 'product')['times'] == 3


def test_history_keeps_product_when_iiko_renames_it(tmp_path):
    store = ShokhStore(tmp_path / 'store.sqlite3')
    at = datetime(2026, 9, 27, 10, tzinfo=TZ)
    for name, price in (('Томаты', '10'), ('Томаты розовые', '12'), ('Томаты розовые', '14')):
        store.add_purchase(DAY, at, point='RETRO', item=name, unit='кг', quantity=1, price=price,
                           iiko_unit=True, refs=dict(supplier_id='s', storage_id='t', product_id='p'))
    store.add_purchase(DAY, at, point='Школа YA', item='Лук', unit='кг', quantity=1, price='3')
    rows = store.item_history(today=DAY)
    assert [(r['product_id'], r['item'], r['times'], r['usual_price']) for r in rows] == [
        ('p', 'Томаты розовые', 3, '12.00'), (None, 'Лук', 1, '3.00')]


def test_legacy_purchase_gets_whole_cash_total_on_start(tmp_path):
    path = tmp_path / 'store.sqlite3'
    store = ShokhStore(path)
    row = store.add_purchase(DAY, datetime.now(TZ), point='RETRO', item='Лук', unit='кг',
                             quantity='3', price='33333.33')
    assert (row['invoice_total'], row['total']) == ('99999.99', '100000.00')
    with store.db.connect() as connection:
        connection.execute('UPDATE shokh_purchases SET cash_total = NULL')
        connection.commit()
    assert ShokhStore(path).purchase(row['id'])['total'] == '100000.00'


def test_expired_catalog_is_served_at_once_and_refreshed_in_background(monkeypatch):
    """T-400: после пяти минут Шох не ждёт iiko — видит прежний справочник, новый едет фоном."""
    import retro.modules.shokh.iiko as procurement
    now = [1000.0]
    monkeypatch.setattr(procurement, 'monotonic', lambda: now[0])
    source = procurement.ProcurementIiko(SimpleNamespace(settings=SimpleNamespace(configured=True, store_id=82907)))
    loads = []
    release = asyncio.Event()

    async def read(path, *, params=None, body=None):
        if path == '/api/productV3/list':
            loads.append(path)
            if len(loads) > 1:
                await release.wait()
            return [dict(id='p1', name=f'Лук {len(loads)}', num='1', mainUnit='kg', type='GOODS')]
        return dict({
            '/api/documents/storage/list': [dict(id='s1', name='Склад', storageType='INVENTORY_ASSETS')],
            '/api/documents/suppliers/list': {'x': dict(id='sp1', name='Базар')},
            '/api/entities/list-of-type': [dict(id='kg', name='кг')],
            '/api/permissions/my': [],
            '/api/documents/config/store-settings': {},
        })[path]
    source.read = read

    async def scenario():
        first = await source.catalog()
        now[0] += procurement.CATALOG_TTL + 1
        stale = await source.catalog()
        assert stale is first                           # отдали сразу, не дожидаясь iiko
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(loads) == 2                          # обновление идёт фоном
        assert await source.catalog() is first          # второе фоновое не заводим
        assert len(loads) == 2
        release.set()
        await source._refreshing
        assert (await source.catalog())['items'][0]['item'] == 'Лук 2'
        now[0] += procurement.CATALOG_STALE_LIMIT + 1   # слишком старый — ждём загрузку
        assert (await source.catalog())['items'][0]['item'] == 'Лук 3'
    asyncio.run(scenario())
