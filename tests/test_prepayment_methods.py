"""T-440: способ внесения предоплаты — автоматически из iiko (13.10.2026).

Цифры — реальные авансы кассы Retro 06–12.10.2026: из 20 авансов у 14 есть
безналичная запись в смене, у 6 нет (кассир подтвердил: наличные)."""
import asyncio
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest

from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.prepayment_registry import PrepaymentRegistry
from retro.modules.cashier.prepayments import (
    ACQUIRING_ACCOUNT, CARD_METHOD, CASH_METHOD, assign_methods, cashless_prepays_from_shift,
    prepayments_from_olap, shift_window)
from retro.modules.cashier.routes import group_advances
from retro.modules.cashier.service import RETRO_REGISTER, TZ, DataError, Prepayment

POSTGRES_URL = os.getenv('RETRO_TEST_POSTGRES_URL', '')
CLICK = 'Click/Payme Безналичный перевод'
CLICK_ID, XUMO_ID = 'a72fcb93-3168-4c83-aec3-ef2219637a2e', 'af72780c-15ea-4b22-bf31-cda6f48056b2'
TYPES = {CLICK_ID: CLICK, XUMO_ID: {'name': 'Xumo'}, '': 'UNKNOWN'}
SHIFT = {'id': 'de3407fd-0000-4000-8000-000000000010', 'cashRegNumber': 1, 'isOpen': False,
         'openDate': '2026-10-10T11:10:47', 'closeDate': '2026-10-10T23:28:50'}


def seconds(stamp):
    return int(datetime.fromisoformat(stamp).replace(tzinfo=TZ).timestamp())


def record(stamp, amount, type_id, kind='PREPAY'):
    return {'info': {'id': stamp, 'creationDate': seconds(stamp), 'type': kind,
                     'sum': amount, 'paymentTypeId': type_id}}


def details(*records):
    return {'error': False, 'data': {'transactions': {'cashlessRecords': list(records), 'payInRecords': []}},
            'decoration': {'paymentTypes': TYPES}}


def branch(amount, stamp, order, contra='Текущие расчеты с сотрудниками'):
    values = [RETRO_REGISTER, 'Ресторан', stamp, order, 'Предоплата за заказы', contra, 'Кассир', None]
    def node(index):
        value = {f'field{index}': {'value': values[index]}, 'field8': {'value': 0}, 'field9': {'value': amount}}
        if index < 7:
            value['children'] = [node(index + 1)]
        return value
    return node(0)


def entry(stamp, amount, order, method=None):
    return Prepayment(order + stamp, datetime.fromisoformat(stamp).replace(tzinfo=TZ).isoformat(),
                      Decimal(amount), payment_method=method, order_number=order)


def test_acquiring_advance_is_a_card_already_in_the_transaction_report():
    entries = prepayments_from_olap([
        branch(1638000, '2026-10-10T17:56:23', '71314', ACQUIRING_ACCOUNT),
        branch(3510000, '2026-10-10T20:10:44', '71314')])
    assert [item.payment_method for item in entries] == [CARD_METHOD, None]


def test_shift_lists_only_cashless_advances_with_their_type():
    rows = cashless_prepays_from_shift(details(
        record('2026-10-10T13:27:28', 500000, CLICK_ID),
        record('2026-10-10T17:56:23', 1638000, XUMO_ID),
        record('2026-10-10T19:00:00', 900000, CLICK_ID, kind='PAYMENT'),
        record('2026-10-10T21:00:00', 100000, '')))
    assert rows == [(seconds('2026-10-10T13:27:28'), Decimal(500000), CLICK),
                    (seconds('2026-10-10T17:56:23'), Decimal(1638000), 'Xumo'),
                    (seconds('2026-10-10T21:00:00'), Decimal(100000), None)]


@pytest.mark.parametrize('broken', [{}, {'data': {'transactions': None}, 'decoration': {'paymentTypes': {}}},
                                    {'data': {'transactions': {'cashlessRecords': []}}, 'decoration': {}},
                                    details({'info': {'type': 'PREPAY', 'sum': 1, 'creationDate': 'x'}}),
                                    details(record('2026-10-10T13:27:28', 0, CLICK_ID))])
def test_unreadable_shift_is_an_error_not_an_empty_shift(broken):
    with pytest.raises(DataError):
        cashless_prepays_from_shift(broken)


def test_shift_window_open_and_closed():
    start, end = shift_window(SHIFT)
    assert (start.isoformat(), end.isoformat()) == ('2026-10-10T11:10:47+05:00', '2026-10-10T23:28:50+05:00')
    assert shift_window({**SHIFT, 'isOpen': True})[1] is None
    with pytest.raises(DataError):
        shift_window({**SHIFT, 'closeDate': '2026-10-09T10:00:00'})


def test_real_day_every_advance_gets_a_method():
    """10.10.2026: два перевода, карта Xumo через эквайринг и три наличных аванса."""
    entries = (entry('2026-10-10T12:26:41', 500000, '71250'),
               entry('2026-10-10T13:09:15', 500000, '71258'),
               entry('2026-10-10T13:27:28', 500000, '71260'),
               entry('2026-10-10T17:56:23', 1638000, '71314', CARD_METHOD),
               entry('2026-10-10T20:10:44', 3510000, '71314'))
    records = cashless_prepays_from_shift(details(record('2026-10-10T13:27:28', 500000, CLICK_ID),
                                                  record('2026-10-10T17:56:23', 1638000, XUMO_ID)))
    result = assign_methods(entries, records, [shift_window(SHIFT)])
    assert [item.payment_method for item in result] == [CASH_METHOD, CASH_METHOD, CLICK, 'Xumo', CASH_METHOD]
    assert [item.amount for item in result] == [item.amount for item in entries]


def test_no_cash_is_invented_without_a_read_shift():
    entries = (entry('2026-10-10T12:26:41', 500000, '71250'),
               entry('2026-10-10T17:56:23', 1638000, '71314', CARD_METHOD))
    # Смена не прочитана: наличных не выводим, эквайринг остаётся картой.
    assert [item.payment_method for item in assign_methods(entries, [], [])] == [None, CARD_METHOD]
    # Аванс вне прочитанной смены — тоже не наличные.
    later = (entry('2026-10-11T14:10:14', 1000000, '71410'),)
    assert assign_methods(later, [], [shift_window(SHIFT)])[0].payment_method is None


def test_cashless_record_without_known_type_is_never_cash():
    entries = (entry('2026-10-10T21:00:00', 100000, '71500'),)
    records = cashless_prepays_from_shift(details(record('2026-10-10T21:00:00', 100000, '')))
    assert assign_methods(entries, records, [shift_window(SHIFT)])[0].payment_method is None
    # Две записи на одну секунду и сумму с разными типами — не угадываем.
    clash = [(seconds('2026-10-10T21:00:00'), Decimal(100000), CLICK),
             (seconds('2026-10-10T21:00:00'), Decimal(100000), 'Xumo')]
    assert assign_methods(entries, clash, [shift_window(SHIFT)])[0].payment_method is None


def iiko_source(handler):
    return IikoClient(Settings(login='test', password='test', store_id=82907),
                      transport=httpx.MockTransport(handler), poll_delay=0)


def test_iiko_reads_only_shifts_holding_advances_and_names_every_method():
    calls = []
    other = {**SHIFT, 'id': 'b031baf2-0000-4000-8000-000000000011',
             'openDate': '2026-10-11T11:44:01', 'closeDate': '2026-10-11T23:12:51'}
    school = {**SHIFT, 'id': '11111111-0000-4000-8000-000000000012', 'cashRegNumber': 2}
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': 'prepayments'})
        if request.url.path == '/api/cash/shift/list_period':
            assert json.loads(request.content) == {'dateFrom': '2026-10-09', 'dateTo': '2026-10-10'}
            return httpx.Response(200, json={'shifts': [SHIFT, other, school]})
        if request.url.path.startswith('/api/cash/shift/details/'):
            assert request.method == 'GET'
            return httpx.Response(200, json=details(record('2026-10-10T13:27:28', 500000, CLICK_ID),
                                                    record('2026-10-10T17:56:23', 1638000, XUMO_ID)))
        return httpx.Response(200, json={'result': {'rows': [
            branch(500000, '2026-10-10T12:26:41', '71250'),
            branch(500000, '2026-10-10T13:27:28', '71260'),
            branch(1638000, '2026-10-10T17:56:23', '71314', ACQUIRING_ACCOUNT),
            branch(3510000, '2026-10-10T20:10:44', '71314')]}})
    async def run():
        source = iiko_source(handler)
        try:
            return await source.load_prepayments(date(2026, 10, 10))
        finally:
            await source.close()
    result = asyncio.run(run())
    assert [(item.order_number, item.payment_method) for item in result] == [
        ('71250', CASH_METHOD), ('71260', CLICK), ('71314', 'Xumo'), ('71314', CASH_METHOD)]
    details_read = [path for method, path in calls if path.startswith('/api/cash/shift/details/')]
    assert details_read == ['/api/cash/shift/details/' + SHIFT['id']]


def test_failed_shift_keeps_registry_but_invents_nothing():
    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'synthetic'})
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': 'prepayments'})
        if request.url.path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': [SHIFT]})
        if request.url.path.startswith('/api/cash/shift/details/'):
            return httpx.Response(502, text='bad gateway')
        return httpx.Response(200, json={'result': {'rows': [branch(500000, '2026-10-10T12:26:41', '71250')]}})
    async def run():
        source = iiko_source(handler)
        try:
            return await source.load_prepayments(date(2026, 10, 10))
        finally:
            await source.close()
    [only] = asyncio.run(run())
    assert (only.amount, only.payment_method) == (Decimal(500000), None)


def test_order_methods_are_all_or_nothing():
    grouped = {row[0]: row[4] for row in group_advances([
        entry('2026-10-10T20:10:44', 3510000, '71314', CASH_METHOD),
        entry('2026-10-10T17:56:23', 1638000, '71314', 'Xumo'),
        entry('2026-10-10T13:27:28', 500000, '71260', CLICK),
        entry('2026-10-10T12:26:41', 500000, '71250', CLICK),
        entry('2026-10-10T12:30:00', 100000, '71250')])}
    assert grouped == {'71314': ('Xumo', CASH_METHOD), '71260': (CLICK,), '71250': ()}


@contextmanager
def postgres_url():
    """Своя схема на тест — как в test_accountant_categories."""
    import psycopg
    from psycopg import sql
    schema = 'prepay_methods_' + uuid4().hex
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


LEGACY_REGISTRY = '''CREATE TABLE cashier_prepayment_registry (
    order_number TEXT PRIMARY KEY, received_day TEXT, received_at TEXT, amount TEXT,
    guest TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', event_day TEXT,
    method TEXT NOT NULL DEFAULT '', refunded INTEGER NOT NULL DEFAULT 0,
    credited_day TEXT, credited_amount TEXT, credited_methods TEXT NOT NULL DEFAULT '',
    updated_at TEXT, updated_by TEXT)'''
LEGACY_ROW = ("INSERT INTO cashier_prepayment_registry (order_number, amount, method) "
              "VALUES ('70935', '3400000', 'Наличные')")


def test_registry_shows_iiko_method_first_and_keeps_it_when_unknown(database):
    registry = PrepaymentRegistry(database)
    day = date(2026, 10, 10)
    registry.record_received([('71314', day, '2026-10-10T17:56:23+05:00', Decimal(5148000), ('Xumo', CASH_METHOD))])
    registry.annotate('71314', guest='Гость', method='Click')
    row = registry.rows(['71314'])[0]
    assert (row['received_methods'], row['method_shown'], row['method']) == (
        ['Xumo', CASH_METHOD], 'Xumo, Наличные', 'Click')
    # Следующее чтение не смогло определить способ — известный не стирается.
    registry.record_received([('71314', day, '2026-10-10T17:56:23+05:00', Decimal(5148000), ())])
    registry.record_received([('71314', day, '2026-10-10T17:56:23+05:00', Decimal(5148000))])
    assert registry.rows(['71314'])[0]['method_shown'] == 'Xumo, Наличные'
    # Способ из iiko неизвестен — показываем то, что вписал кассир.
    registry.record_received([('71250', day, '2026-10-10T12:26:41+05:00', Decimal(500000), ())])
    registry.annotate('71250', method='Наличные')
    assert registry.rows(['71250'])[0]['method_shown'] == 'Наличные'


def test_registry_migrates_a_base_without_the_method_column(database):
    if isinstance(database, str):
        import psycopg
        with psycopg.connect(database, autocommit=True) as connection:
            connection.execute(LEGACY_REGISTRY)
            connection.execute(LEGACY_ROW)
    else:
        with sqlite3.connect(database) as connection:
            connection.execute(LEGACY_REGISTRY)
            connection.execute(LEGACY_ROW)
    registry = PrepaymentRegistry(database)
    PrepaymentRegistry(database)  # повторный запуск не добавляет колонку второй раз
    row = registry.rows(['70935'])[0]
    assert (row['received_methods'], row['method_shown']) == ([], 'Наличные')
