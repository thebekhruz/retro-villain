"""T-440: дата визита предоплаты (13.10.2026).

Сценарии — по реальным авансам 06–13.10.2026: закрытые заказы, единственная
бронь стола 22 (71314 → 16.10), стол 23 с двумя открытыми заказами и тремя
бронями (выбор кассиру), стол 1 только в банкетном зале."""
import asyncio
import json
import os
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest

from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.integrations.iiko_cloud import IikoCloudReserves, Reserve, reserves_from_workload
from retro.modules.cashier.prepayment_registry import PrepaymentRegistry
from retro.modules.cashier.service import TZ, DataError
from retro.modules.cashier.visits import Visit, link_visits, load_visits, visit_day

POSTGRES_URL = os.getenv('RETRO_TEST_POSTGRES_URL', '')
TODAY = date(2026, 10, 13)
RESTAURANT = '3509386a-4ef7-4f88-9bd3-a31b02ad3432'
WEDDING = '93e83115-71f5-4705-a203-42089343f4ba'


def at(stamp):
    return datetime.fromisoformat(stamp).replace(tzinfo=TZ)


def reserve(table, stamp, section='Ресторан'):
    return Reserve(str(table), section, at(stamp))


RESERVES = (reserve(22, '2026-10-16T19:00'), reserve(20, '2026-10-13T19:00'),
            reserve(23, '2026-10-14T19:00'), reserve(23, '2026-10-15T19:00'), reserve(23, '2026-10-18T19:00'))
TABLES = {'70935': ('22', False), '71260': ('22', False), '71314': ('22', False), '71623': ('20', False),
          '71250': ('23', False), '71258': ('23', False), '71590': ('23', False), '71410': ('1', False)}
CLOSED = {'70935': at('2026-10-08T22:54:06'), '71260': at('2026-10-10T22:36:16'),
          '71250': at('2026-10-10T22:18:02')}


def test_real_week_dates():
    visits = link_visits(['70935', '71260', '71314', '71623', '71250', '71258', '71590', '71410'],
                         CLOSED, TABLES, RESERVES, TODAY)
    assert visits['70935'] == Visit(date(2026, 10, 8), 'closed')
    assert visits['71260'] == Visit(date(2026, 10, 10), 'closed')
    # На столе 22 три заказа, но открыт один — единственная бронь его.
    assert visits['71314'] == Visit(date(2026, 10, 16), 'reserve')
    assert visits['71623'] == Visit(date(2026, 10, 13), 'reserve')
    # Стол 23: два открытых заказа, три брони — кассиру на выбор, не угадываем.
    options = (date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 18))
    assert visits['71258'] == visits['71590'] == Visit(None, '', options)
    assert visits['71410'] == Visit(None, '', ())


def test_after_midnight_close_is_the_previous_evening():
    assert visit_day(at('2026-10-11T00:40:00')) == date(2026, 10, 10)
    assert visit_day(at('2026-10-11T06:00:00')) == date(2026, 10, 11)


def test_returned_past_and_other_hall_reserves_never_link():
    tables = {'1': ('5', True), '2': ('7', False), '3': ('9', False)}
    reserves = (reserve(5, '2026-10-20T19:00'), reserve(7, '2026-10-12T19:00'),
                reserve(9, '2026-10-20T19:00'), reserve(9, '2026-10-20T19:00', 'Бар ресторан'))
    visits = link_visits(['1', '2', '3'], {}, tables, reserves, TODAY)
    assert visits['1'] == Visit(None, '', ())                    # предоплату вернули
    assert visits['2'] == Visit(None, '', ())                    # бронь в прошлом
    assert visits['3'] == Visit(None, '', (date(2026, 10, 20),))  # стол 9 есть в двух залах


def workload_sections():
    return [{'id': RESTAURANT, 'name': 'Ресторан', 'tables': [
                {'id': '7dbea681-86e6-441b-999b-429b294df620', 'number': 22}]},
            {'id': WEDDING, 'name': 'Бехруз (Свадьба)', 'tables': [
                {'id': '8fceec8c-46a9-482f-bf97-5fe93c5e58c2', 'number': 1}]}]


def workload():
    return {'reserves': [
        {'id': '2118a858-4a40-4a9c-9231-d65cb1931610', 'tableIds': ['7dbea681-86e6-441b-999b-429b294df620'],
         'estimatedStartTime': '2026-10-16 19:00:00.000', 'durationInMinutes': 180, 'guestsCount': 2},
        {'id': '889aac1d-135b-404d-ba5a-35ec37a5d801', 'tableIds': ['8fceec8c-46a9-482f-bf97-5fe93c5e58c2'],
         'estimatedStartTime': '2026-10-25 19:00:00.000', 'durationInMinutes': 180, 'guestsCount': 2}]}


def test_workload_keeps_restaurant_and_drops_banquet_hall():
    assert reserves_from_workload(workload_sections(), workload()) == [reserve(22, '2026-10-16T19:00')]
    broken = workload()
    broken['reserves'][0]['tableIds'] = ['00000000-0000-4000-8000-000000000000']
    with pytest.raises(DataError):
        reserves_from_workload(workload_sections(), broken)


def settings():
    return Settings(login='test', password='test', store_id=82907)


def test_cloud_client_only_reads_and_reuses_its_token():
    calls = []
    def handler(request):
        calls.append((request.url.host, request.url.path))
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'web'})
        if request.url.path == '/api/integration-management/api-logins/get':
            return httpx.Response(200, json={'apiLoginInfo': {'apiKey': 'synthetic-key'}})
        if request.url.path == '/api/1/access_token':
            assert json.loads(request.content) == {'apiLogin': 'synthetic-key'}
            return httpx.Response(200, json={'token': 'cloud'})
        assert request.headers['Authorization'] == 'Bearer cloud'
        if request.url.path == '/api/1/reserve/available_restaurant_sections':
            return httpx.Response(200, json={'restaurantSections': workload_sections()})
        return httpx.Response(200, json=workload())
    transport = httpx.MockTransport(handler)
    async def run():
        web = IikoClient(settings(), transport=transport, poll_delay=0)
        cloud = IikoCloudReserves(settings(), web, transport=transport)
        try:
            first = await cloud.reserves(TODAY, date(2026, 12, 31))
            second = await cloud.reserves(TODAY, date(2027, 1, 31))
            with pytest.raises(DataError):
                await cloud._send('/api/1/reserve/create', {})
            return first, second
        finally:
            await cloud.close()
            await web.close()
    first, second = asyncio.run(run())
    assert first == second == (reserve(22, '2026-10-16T19:00'),)
    assert [path for _, path in calls].count('/api/1/access_token') == 1
    assert all(path != '/api/1/reserve/create' for _, path in calls)


def test_event_log_tables_and_close_times_from_iiko():
    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        if request.url.path == '/api/web-event-log':
            body = json.loads(request.content)
            assert body['types'] == ['orderPrepaid', 'orderPrepayReturned'] and body['storeId'] == '82907'
            return httpx.Response(200, json={'data': {'events': [
                {'type': 'orderPrepaid', 'attributes': {'orderNum': 71314, 'tableNum': '22', 'prepaySum': 1638000}},
                {'type': 'orderPrepaid', 'attributes': {'orderNum': 71314, 'tableNum': '22', 'prepaySum': 3510000}},
                {'type': 'orderPrepaid', 'attributes': {'orderNum': 71500, 'tableNum': '5'}},
                {'type': 'orderPrepayReturned', 'attributes': {'orderNum': 71500}},
                {'type': 'reserveCreated', 'attributes': {'numGuests': 2}}]}})
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': 'close'})
        return httpx.Response(200, json={'result': {'rows': [
            {'field0': {'value': 70935}, 'children': [
                {'field1': {'value': '2026-10-08T22:54:06.11'}, 'field2': {'value': 5211000}}]},
            {'field0': {'value': 71260}, 'children': [
                {'field1': {'value': '2026-10-10T22:36:16'}, 'field2': {'value': 1077000}}]}]}})
    async def run():
        source = IikoClient(settings(), transport=httpx.MockTransport(handler), poll_delay=0)
        try:
            return (await source.prepaid_tables(date(2026, 7, 15), TODAY),
                    await source.order_close_times(['70935', '71260', '71314'], date(2026, 7, 14), TODAY))
        finally:
            await source.close()
    tables, closed = asyncio.run(run())
    assert tables == {'71314': ('22', False), '71500': ('5', True)}
    assert closed == {'70935': at('2026-10-08T22:54:06.11'), '71260': at('2026-10-10T22:36:16')}


class FakeIiko:
    def __init__(self, *, log_fails=False):
        self.log_fails = log_fails
    async def prepaid_tables(self, start, end):
        if self.log_fails:
            raise DataError('журнал недоступен')
        return TABLES
    async def order_close_times(self, orders, start, end):
        assert {'71314', '70935'} <= set(orders)
        return CLOSED


class FakeCloud:
    configured = True
    def __init__(self, fails=False):
        self.fails = fails
    async def reserves(self, start, end):
        if self.fails:
            raise httpx.ConnectError('cloud down')
        return RESERVES


def test_unavailable_log_or_cloud_leaves_only_iiko_facts():
    full = asyncio.run(load_visits(FakeIiko(), FakeCloud(), ['71314', '70935'], TODAY))
    assert full['71314'] == Visit(date(2026, 10, 16), 'reserve')
    no_cloud = asyncio.run(load_visits(FakeIiko(), FakeCloud(fails=True), ['71314', '70935'], TODAY))
    no_log = asyncio.run(load_visits(FakeIiko(log_fails=True), FakeCloud(), ['71314', '70935'], TODAY))
    for visits in (no_cloud, no_log):
        assert visits['70935'] == Visit(date(2026, 10, 8), 'closed')
        assert visits['71314'] == Visit(None, '', ())


@contextmanager
def postgres_url():
    """Своя схема на тест — как в test_accountant_categories."""
    import psycopg
    from psycopg import sql
    schema = 'prepay_visits_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query), options='-csearch_path=' + schema)
    try:
        yield urlunsplit(parts._replace(query=urlencode(query)))
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture(params=['sqlite', pytest.param('postgres', marks=pytest.mark.skipif(
    not POSTGRES_URL, reason='RETRO_TEST_POSTGRES_URL не задан'))])
def database(request, tmp_path):
    if request.param == 'postgres':
        with postgres_url() as url:
            yield url
    else:
        yield tmp_path / 'cashier.sqlite3'


def received(registry, *orders):
    registry.record_received([(order, date(2026, 10, 10), '2026-10-10T12:00:00+05:00', Decimal(500000))
                              for order in orders])


def test_registry_shows_iiko_fact_then_cashier_then_reserve(database):
    registry = PrepaymentRegistry(database)
    received(registry, '71314', '70935', '71258', '71623')
    registry.annotate('70935', event_day=date(2026, 10, 9))   # кассир ошибся — закрытие важнее
    registry.annotate('71623', event_day=date(2026, 10, 20))  # кассир знает лучше брони
    registry.record_visits({'71314': Visit(date(2026, 10, 16), 'reserve'),
                            '70935': Visit(date(2026, 10, 8), 'closed'),
                            '71258': Visit(None, '', (date(2026, 10, 14), date(2026, 10, 15))),
                            '71623': Visit(date(2026, 10, 13), 'reserve'),
                            'never-received': Visit(date(2026, 10, 1), 'closed')})
    rows = {row['order_number']: row for row in registry.rows(['71314', '70935', '71258', '71623'])}
    assert (rows['71314']['event_day'], rows['71314']['event_day_source'], rows['71314']['status']) == (
        '2026-10-16', 'reserve', 'pending')
    assert (rows['70935']['event_day'], rows['70935']['event_day_source'], rows['70935']['status'],
            rows['70935']['event_day_manual']) == ('2026-10-08', 'closed', 'credited', '2026-10-09')
    assert (rows['71258']['event_day'], rows['71258']['visit_options']) == (None, ['2026-10-14', '2026-10-15'])
    assert (rows['71623']['event_day'], rows['71623']['event_day_source']) == ('2026-10-20', 'cashier')
    assert registry.rows(['never-received']) == []
    # Источник не ответил — известная бронь не стирается; факт закрытия не меняется.
    registry.record_visits({'71314': Visit(None, '', ()), '70935': Visit(date(2026, 10, 20), 'reserve')})
    assert registry.rows(['71314'])[0]['event_day'] == '2026-10-16'
    assert registry.rows(['70935'])[0]['event_day'] == '2026-10-08'
    # Появился второй открытый заказ на стол — бронь становится выбором.
    registry.record_visits({'71314': Visit(None, '', (date(2026, 10, 16),))})
    row = registry.rows(['71314'])[0]
    assert (row['event_day'], row['visit_options']) == (None, ['2026-10-16'])
    # Период «по датам события» видит и даты из iiko.
    assert [item['order_number'] for item in registry.rows_between(date(2026, 10, 8), date(2026, 10, 8))] == ['70935']
