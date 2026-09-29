"""Справочник меню: чтение номенклатуры iiko, хранение и недельное расписание."""
import asyncio
import json
import os
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.service import DataError, TZ
from retro.modules.menu.iiko import MenuIiko
from retro.modules.menu.store import MenuStore
from retro.modules.menu.sync import MenuSync

NOW = datetime(2026, 9, 29, 10, 0, tzinfo=TZ)

PRODUCTS = [
    {'id': 'dish', 'name': {'customValue': 'Лагман'}, 'num': '00012', 'type': 'DISH',
     'parent': 'hot', 'category': 'kitchen', 'mainUnit': 'port', 'deleted': False,
     'sizePrices': [{'sizeId': None, 'price': {'currentPrice': 45000, 'isIncludedInMenu': True}},
                    {'sizeId': 'big', 'price': {'currentPrice': 62000, 'isIncludedInMenu': True}}]},
    {'id': 'wine', 'name': 'Вино домашнее', 'num': '00100', 'type': 'GOODS', 'parent': 'bar',
     'mainUnit': 'litre', 'deleted': False,
     'sizePrices': [{'price': {'currentPrice': 32000, 'isIncludedInMenu': True}}]},
    # Товар склада: цены в меню у него нет, продаваться он не может.
    {'id': 'tomato', 'name': 'Томаты', 'num': '00001', 'type': 'GOODS', 'parent': 'store',
     'mainUnit': 'kg', 'deleted': False, 'sizePrices': []},
    # Снятое с меню блюдо: цена есть, признака «в меню» нет.
    {'id': 'retired', 'name': 'Долма', 'num': '00044', 'type': 'DISH', 'parent': 'hot',
     'mainUnit': 'port', 'deleted': True,
     'sizePrices': [{'price': {'currentPrice': 30000, 'isIncludedInMenu': False}}]},
    {'id': 'nameless', 'name': '', 'type': 'DISH', 'parent': 'hot', 'deleted': False},
]
GROUPS = [{'id': 'kitchen_group', 'name': 'Кухня'},
          {'id': 'hot', 'name': 'Горячие блюда', 'parent': 'kitchen_group'},
          {'id': 'bar', 'name': 'Бар'},
          {'id': 'store', 'name': 'Склад'}]
UNITS = [{'id': 'port', 'name': 'порц'}, {'id': 'kg', 'name': 'кг'}, {'id': 'litre', 'name': 'л'}]
CATEGORIES = [{'id': 'kitchen', 'name': 'Кухня'}]


class Upstream:
    """iiko под контролем теста: номенклатура, справочники и способы отказать."""

    def __init__(self, mode='ok'):
        self.mode = mode
        self.calls = []

    def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-token'})
        if path == '/api/productV3/list':
            body = json.loads(request.content)
            extended = 'sizePrices' in body['properties']
            if self.mode == 'no_extended' and extended:
                return httpx.Response(200, json={'error': True})
            if self.mode == 'products_down':
                return httpx.Response(503)
            if self.mode == 'empty':
                return httpx.Response(200, json={'data': []})
            products = [{key: value for key, value in product.items()
                         if extended or key not in ('sizePrices', 'category', 'code')}
                        for product in PRODUCTS]
            return httpx.Response(200, json={'error': False, 'data': products})
        if path == '/api/entities/list-of-type':
            kind = request.url.params['type']
            if self.mode == 'groups_down' and kind == 'ProductGroup':
                return httpx.Response(503)
            data = {'ProductGroup': GROUPS, 'MeasureUnit': UNITS, 'ProductCategory': CATEGORIES}[kind]
            return httpx.Response(200, json={'error': False, 'data': data})
        raise AssertionError('Unexpected upstream request: ' + path)


def source_for(mode='ok'):
    settings = Settings(login='test', password='test', store_id=82907)
    return MenuIiko(IikoClient(settings, transport=httpx.MockTransport(Upstream(mode))))


class FakeSource:
    """Источник без сети: тесты хранения и расписания не должны звать iiko."""

    def __init__(self, items, note='', error=None):
        self.items, self.note, self.error = items, note, error
        self.reads = 0

    async def nomenclature(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return dict(source='iiko · номенклатура', read_at=NOW.isoformat(),
                    items=self.items, note=self.note)


def item(product_id='dish', **changes):
    row = dict(product_id=product_id, code='00012', name='Лагман', kind='DISH',
               group_id='hot', group_path='Кухня/Горячие блюда', category='Кухня',
               unit='порц', price='45000.00', in_menu=True, deleted=False)
    row.update(changes)
    return row


@pytest.fixture(params=['sqlite', 'postgres'])
def store(tmp_path, request):
    """Один и тот же справочник на обоих диалектах: SQL здесь общий."""
    if request.param == 'sqlite':
        yield MenuStore(tmp_path / 'director.sqlite3')
        return
    database_url = os.getenv('RETRO_TEST_POSTGRES_URL', '')
    if not database_url:
        pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
    import psycopg
    from psycopg import sql
    schema = 'menu_' + uuid4().hex
    pg = psycopg.connect(database_url, autocommit=True)
    pg.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(database_url)
    query = dict(parse_qsl(parts.query))
    query['options'] = '-csearch_path=' + schema
    try:
        yield MenuStore(urlunsplit(parts._replace(query=urlencode(query))))
    finally:
        pg.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        pg.close()


# ── Чтение номенклатуры ─────────────────────────────────────────────────────

def test_nomenclature_reads_whole_menu_with_groups_prices_and_units():
    result = asyncio.run(source_for().nomenclature())
    by_id = {row['product_id']: row for row in result['items']}
    # Позиция без названия — не позиция меню.
    assert set(by_id) == {'dish', 'wine', 'tomato', 'retired'}
    assert by_id['dish']['group_path'] == 'Кухня/Горячие блюда'
    assert by_id['dish']['unit'] == 'порц'
    assert by_id['dish']['category'] == 'Кухня'
    # Из цен размеров берём ту, что видит гость, — наибольшую из меню.
    assert by_id['dish']['price'] == '62000.00'
    assert by_id['dish']['in_menu'] is True
    assert by_id['tomato']['price'] is None and by_id['tomato']['in_menu'] is None
    assert by_id['retired']['deleted'] is True and by_id['retired']['in_menu'] is False
    assert result['note'] == 'без цены 1 из 4 позиций'


def test_extended_request_rejected_falls_back_to_menu_without_prices():
    source = source_for('no_extended')
    result = asyncio.run(source.nomenclature())
    assert [row['price'] for row in result['items']] == [None] * len(result['items'])
    assert 'расширенный запрос номенклатуры не прошёл' in result['note']
    # Признак «в меню» неизвестен, и врать «не в меню» нельзя.
    assert {row['in_menu'] for row in result['items']} == {None}


def test_unavailable_dictionary_degrades_but_keeps_the_menu():
    result = asyncio.run(source_for('groups_down').nomenclature())
    assert [row['group_path'] for row in result['items']] == [''] * len(result['items'])
    assert 'группы меню не прочитаны' in result['note']
    assert result['items'][0]['unit'] != ''


@pytest.mark.parametrize('mode', ['products_down', 'empty'])
def test_unreadable_nomenclature_is_an_error(mode):
    with pytest.raises(DataError):
        asyncio.run(source_for(mode).nomenclature())


# ── Хранение ────────────────────────────────────────────────────────────────

def test_second_run_changes_nothing_and_keeps_updated_at(store):
    first = store.save([item(), item('wine', name='Вино', kind='GOODS', price='32000.00')],
                       at='2026-09-29T10:00:00+05:00')
    assert (first['items'], first['added'], first['changed']) == (2, 2, 0)
    assert first['priced'] == 2
    second = store.save([item(), item('wine', name='Вино', kind='GOODS', price='32000.00')],
                        at='2026-10-06T10:00:00+05:00')
    assert (second['added'], second['changed'], second['missing']) == (0, 0, 0)
    saved = {row['product_id']: row for row in store.items()}
    assert saved['dish']['updated_at'] == '2026-09-29T10:00:00+05:00'
    assert saved['dish']['synced_at'] == '2026-10-06T10:00:00+05:00'


def test_price_change_moves_updated_at(store):
    store.save([item()], at='2026-09-29T10:00:00+05:00')
    counts = store.save([item(price='48000.00')], at='2026-10-06T10:00:00+05:00')
    assert counts['changed'] == 1
    saved = store.items()[0]
    assert (saved['price'], saved['updated_at']) == ('48000.00', '2026-10-06T10:00:00+05:00')
    assert saved['first_seen_at'] == '2026-09-29T10:00:00+05:00'


def test_missing_item_is_marked_not_deleted_and_can_return(store):
    store.save([item(), item('wine', name='Вино', kind='GOODS')], at='2026-09-29T10:00:00+05:00')
    counts = store.save([item()], at='2026-10-06T10:00:00+05:00')
    assert counts['missing'] == 1
    assert [row['product_id'] for row in store.items()] == ['dish']
    gone = {row['product_id']: row for row in store.items(include_missing=True)}['wine']
    assert gone['missing_since'] == '2026-10-06T10:00:00+05:00'
    back = store.save([item(), item('wine', name='Вино', kind='GOODS')], at='2026-10-13T10:00:00+05:00')
    assert back['returned'] == 1
    assert {row['product_id'] for row in store.items()} == {'dish', 'wine'}


def test_menu_scope_hides_storage_goods_and_deleted_cards(store):
    store.save([item(),
                item('wine', name='Вино', kind='GOODS', group_path='Бар',
                     price='32000.00', in_menu=True),
                item('tomato', name='Томаты', kind='GOODS', group_path='Склад',
                     price=None, in_menu=None),
                item('retired', name='Долма', price='30000.00', in_menu=False, deleted=True)],
               at='2026-09-29T10:00:00+05:00')
    assert {row['product_id'] for row in store.items()} == {'dish', 'wine'}
    assert {row['product_id'] for row in store.items(scope='all')} == {
        'dish', 'wine', 'tomato', 'retired'}
    assert [row['product_id'] for row in store.items(query='вино')] == ['wine']
    assert [row['product_id'] for row in store.items(group='горячие')] == ['dish']
    assert store.count() == dict(total=4, missing=0)


def test_nonsense_price_is_refused(store):
    with pytest.raises(Exception):
        store.save([item(price='45000.005')], at='2026-09-29T10:00:00+05:00')
    assert store.items() == []


# ── Расписание ──────────────────────────────────────────────────────────────

def clock_from(start):
    box = [start]

    def clock():
        return box[0]
    return box, clock


def test_first_run_is_today_and_the_next_one_is_a_week_later(store):
    box, clock = clock_from(NOW)
    source = FakeSource([item()])

    async def scenario():
        sync = MenuSync(store, source, clock=clock)
        first = await sync.run_once()
        assert first['items'] == 1 and source.reads == 1
        assert store.state()['next_run_at'] == (NOW + timedelta(days=7)).isoformat()
        assert store.status()['synced_at'] == NOW.isoformat()

        # Через день трогать iiko незачем: справочник уже свежий.
        box[0] = NOW + timedelta(days=1)
        assert await sync.run_once() is None
        assert source.reads == 1

        box[0] = NOW + timedelta(days=7, minutes=1)
        assert (await sync.run_once())['items'] == 1
        assert source.reads == 2
        assert store.state()['next_run_at'] == (box[0] + timedelta(days=7)).isoformat()

    asyncio.run(scenario())


def test_schedule_survives_restart(store):
    box, clock = clock_from(NOW)

    async def scenario():
        # Каждый MenuSync здесь — новый процесс после выката.
        assert await MenuSync(store, FakeSource([item()]), clock=clock).run_once() is not None
        box[0] = NOW + timedelta(days=2)
        assert await MenuSync(store, FakeSource([item()]), clock=clock).run_once() is None
        assert store.state()['next_run_at'] == (NOW + timedelta(days=7)).isoformat()

    asyncio.run(scenario())


def test_manual_refresh_runs_out_of_turn_and_restarts_the_week(store):
    box, clock = clock_from(NOW)
    source = FakeSource([item()])

    async def scenario():
        sync = MenuSync(store, source, clock=clock)
        await sync.run_once()
        box[0] = NOW + timedelta(days=1)
        assert (await sync.run_once(force=True))['items'] == 1
        assert source.reads == 2
        assert store.state()['next_run_at'] == (box[0] + timedelta(days=7)).isoformat()

    asyncio.run(scenario())


def test_failed_run_is_visible_and_retried_within_the_hour(store):
    box, clock = clock_from(NOW)
    source = FakeSource([], error=DataError('iiko отклонил доступ к номенклатуре.'))

    async def scenario():
        sync = MenuSync(store, source, clock=clock)
        with pytest.raises(DataError):
            await sync.run_once()
        run = store.runs(1)[0]
        assert run['status'] == 'failed' and 'номенклатур' in run['error']
        assert store.state()['next_run_at'] == (NOW + timedelta(hours=1)).isoformat()
        assert store.status()['synced_at'] is None
        # Час не прошёл — повтора нет; прошёл — прогон идёт снова.
        box[0] = NOW + timedelta(minutes=30)
        assert await sync.run_once() is None
        box[0] = NOW + timedelta(hours=1, seconds=1)
        source.error, source.items = None, [item()]
        assert (await sync.run_once())['items'] == 1

    asyncio.run(scenario())


# ── API директора ───────────────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path):
    settings = Settings(login='test', password='test', store_id=82907, data_dir=tmp_path)
    app = create_app(settings)
    upstream = Upstream()
    app.state.iiko = IikoClient(settings, transport=httpx.MockTransport(upstream))
    app.state.menu_iiko = MenuIiko(app.state.iiko)
    app.state.menu_sync = MenuSync(app.state.menu_store, app.state.menu_iiko)
    # Кассовый фоновый обход к меню отношения не имеет.
    app.state.cashier_days = None
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as c:
        yield c, upstream


def test_api_serves_saved_menu_only_after_a_run(client):
    c, upstream = client
    empty = c.get('/api/director/menu').json()
    assert empty['items'] == [] and empty['synced_at'] is None
    assert empty['next_run_at'] is None
    assert upstream.calls == []

    refreshed = c.post('/api/director/menu/refresh')
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()['added'] == 4

    menu = c.get('/api/director/menu').json()
    assert [row['name'] for row in menu['items']] == ['Вино домашнее', 'Лагман']
    assert menu['count'] == dict(total=4, missing=0)
    assert menu['synced_at'] is not None and menu['last_run']['status'] == 'ok'
    assert len(c.get('/api/director/menu?scope=all').json()['items']) == 4
    assert [row['name'] for row in c.get('/api/director/menu?query=лаг').json()['items']] == ['Лагман']
    # Второе чтение меню — из нашей базы, без единого запроса в iiko.
    reads = len(upstream.calls)
    c.get('/api/director/menu')
    assert len(upstream.calls) == reads


def test_refresh_reports_iiko_failure_instead_of_silence(client):
    c, upstream = client
    upstream.mode = 'products_down'
    response = c.post('/api/director/menu/refresh')
    assert response.status_code == 503
    assert c.get('/api/director/menu').json()['last_run']['status'] == 'failed'


def test_without_iiko_menu_is_readable_and_refresh_is_refused(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as c:
        assert app.state.menu_sync is None
        assert c.get('/api/director/menu').json()['items'] == []
        assert c.post('/api/director/menu/refresh').status_code == 503
