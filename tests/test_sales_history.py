"""История продаж (T-499): загрузка дня, замена при правке, пропуски, окно часов, сверка, AI."""
import asyncio
import json
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import LUNCH_HOURS, Settings, parse_lunch_hours, parse_sales_history_start
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.archive import CashierArchive
from retro.modules.cashier.service import (BANQUET_SECTION, RETRO_REGISTER, SCHOOL_REGISTER, DataError,
                                           RevenueBreakdown, TZ, demo_snapshot)
from retro.modules.director.tools import DirectorChatTools
from retro.modules.founder.tools import FounderChatTools
from retro.modules.menu.catalog import MenuCatalog
from retro.modules.sales.iiko import (CONTROL_GROUPS, LINE_GROUPS, OPERATION_GROUPS, SalesIiko,
                                      control_from_olap, lines_from_olap, operations_from_olap, revision_of)
from retro.modules.sales.service import SalesService, make_window
from retro.modules.sales.store import SalesStore
from retro.modules.sales.sync import SalesSync

NOW = datetime(2026, 10, 17, 13, 5, tzinfo=TZ)
TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)


# ── Данные iiko под контролем теста ─────────────────────────────────────────

def order(order_id, closed, items, *, opened=None, register=RETRO_REGISTER, section='Ресторан',
          waiter='Малика', ops=None, day=TODAY):
    """Заказ: items — (блюдо, количество, сумма до скидки, выручка, себестоимость[, тип])."""
    opened = opened or closed
    revenue = sum(Decimal(str(item[3])) for item in items)
    return dict(id=order_id, num=str(abs(hash(order_id)) % 100000), day=day,
                opened=f'{day.isoformat()}T{opened}:00', closed=f'{day.isoformat()}T{closed}:30.125',
                register=register, section=section, waiter=waiter, items=items,
                ops=ops if ops is not None else {'Оплата': revenue})


def leaf(values, metrics):
    row = {f'field{index}': {'value': value} for index, value in enumerate(values)}
    for index, value in enumerate(metrics):
        row[f'field{len(values) + index}'] = {'type': 'MONEY', 'value': value}
    return row


def olap_answers(orders):
    """Три отчёта, как их отдаёт iiko: строки, операции заказов, контроль кассы."""
    lines, operations, control = [], [], {}
    for item in orders:
        for name, quantity, gross, revenue, cost, *kind in item['items']:
            lines.append(leaf([item['id'], int(item['num']), item['opened'], item['closed'], item['register'],
                               item['section'], item['waiter'], 'id-' + name, name, 'Кухня',
                               kind[0] if kind else 'Блюдо', 'Нет'],
                              [quantity, gross, revenue, cost, 0]))
            total = control.setdefault((item['register'], item['section']), [0, 0, 0, set()])
            total[0] += revenue
            total[1] += gross
            total[2] += quantity
            total[3].add(item['id'])
        for operation, amount in item['ops'].items():
            # iiko отдаёт целые суммы целыми числами, доли — дробными.
            amount = Decimal(amount)
            operations.append(leaf([item['id'], operation], [int(amount) if amount == amount.to_integral()
                                                              else float(amount)]))
    control_rows = [leaf([register, section], [revenue, gross, quantity, len(ids)])
                    for (register, section), (revenue, gross, quantity, ids) in control.items()]
    return control_rows, lines, operations


def day_data(day, orders):
    control_rows, line_rows, operation_rows = olap_answers(orders)
    lines, operations, control = (lines_from_olap(line_rows), operations_from_olap(operation_rows),
                                  control_from_olap(control_rows))
    return dict(day=day, lines=lines, operations=operations, control=control,
                revision=revision_of(lines, operations, control))


LUNCH = [
    order('a', '12:10', [('Лагман', 2, 90000, 90000, 30000), ('Чай', 1, 18000, 18000, 1500)]),
    order('b', '14:55', [('Плов', 1, 60000, 50000, 20000), ('Мед', 1, 8000, 8000, 2000)]),
    order('c', '11:40', [('Плов', 1, 60000, 60000, 20000)]),
    order('d', '15:00', [('Шашлык', 3, 120000, 120000, 50000)]),
    order('e', '13:20', [('ШКОЛА ОБЕД', 10, 300000, 300000, 100000)], register=SCHOOL_REGISTER, section='Зал',
          waiter='Касса Оксбрич'),
    order('f', '13:30', [('БЕХРУЗ ПЛОВ', 100, 5000000, 5000000, 2000000)], section=BANQUET_SECTION,
          ops={'Оплата': Decimal(2000000), 'Предоплата': Decimal(3000000)}),
    order('g', '12:30', [('Лагман', 1, 45000, 0, 15000)], ops={'(без оплаты)': Decimal(0)}),
    order('h', '12:40', [('Салат Ачичук сет', 1, 0, 0, 3000, 'Модификатор'),
                         ('Сет Ретро', 1, 150000, 150000, 60000)]),
]


class FakeSource:
    def __init__(self, days=None, error=None):
        self.days, self.error, self.calls = days or {}, error, []

    async def load_day(self, day):
        self.calls.append(day)
        if self.error:
            raise self.error
        return day_data(day, self.days.get(day, []))


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request, tmp_path):
    database = tmp_path / 'director.sqlite3'
    if request.param == 'postgres':
        database = os.getenv('RETRO_TEST_POSTGRES_URL')
        if not database:
            pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
    # Свой store_id — своя история: тесты на общей базе Postgres не видят друг друга.
    return SalesStore(database, Settings(store_id=uuid4().int % 1000000000))


def clock_at(moment):
    box = [moment]
    return box, (lambda: box[0])


def settings(**changes):
    return Settings(**{'sales_history_start': date(2026, 10, 10), **changes})


def service_for(store, sync=None, clock=lambda: NOW, catalog=None, cashier=None):
    return SalesService(store, settings=settings(), sync=lambda: sync, clock=clock,
                        catalog=catalog or (lambda: MenuCatalog([])), cashier=lambda: cashier)


def row_count(store, table, day):
    with store.db.cursor() as connection:
        return connection.execute(f'SELECT COUNT(*) FROM {table} WHERE source=? AND day=?',
                                  (store.source, day.isoformat())).fetchone()[0]


# ── Синхронизация: идемпотентность, правки, журнал ─────────────────────────

def test_repeated_sync_is_idempotent_and_edit_replaces_the_day(store):
    box, clock = clock_at(NOW)
    source = FakeSource({YESTERDAY: LUNCH})

    async def scenario():
        sync = SalesSync(store, source, history_start=date(2026, 10, 1), clock=clock)
        first = await sync.start_day(YESTERDAY, reason='test')
        assert first['status'] == 'ok' and first['reconciliation'] == 'ok', first
        lines, orders = row_count(store, 'sales_lines', YESTERDAY), row_count(store, 'sales_orders', YESTERDAY)
        assert (lines, orders) == (11, 8)
        # Тот же ответ iiko ещё раз: ничего не задвоилось, версия та же.
        box[0] = NOW + timedelta(minutes=10)
        second = await sync.start_day(YESTERDAY, reason='test')
        assert second['status'] == 'unchanged'
        assert row_count(store, 'sales_lines', YESTERDAY) == lines
        state = store.state(YESTERDAY)
        assert state['revisions'] == 1 and state['fetched_at'] == box[0]
        # В iiko удалили заказ «d» и поправили скидку в «b»: день заменён целиком.
        edited = [item for item in LUNCH if item['id'] != 'd']
        edited[1] = order('b', '14:55', [('Плов', 1, 60000, 40000, 20000), ('Мед', 1, 8000, 8000, 2000)])
        source.days[YESTERDAY] = edited
        box[0] = NOW + timedelta(minutes=20)
        third = await sync.start_day(YESTERDAY, reason='test')
        assert third['status'] == 'ok'
        assert row_count(store, 'sales_orders', YESTERDAY) == 7
        assert store.state(YESTERDAY)['revisions'] == 2
        assert {row['order_id'] for row in store.orders(YESTERDAY, YESTERDAY)} == set('abcefgh')
        runs = store.runs(5, day=YESTERDAY)
        assert [run['status'] for run in runs] == ['ok', 'unchanged', 'ok']
        assert Decimal(runs[0]['previous_revenue']) - Decimal(runs[0]['revenue']) == Decimal(130000)

    asyncio.run(scenario())


def test_older_answer_never_overwrites_newer_day(store):
    data = day_data(YESTERDAY, LUNCH)
    from retro.modules.sales.service import prepare_day
    prepared = prepare_day(data)
    common = dict(revenue=prepared['revenue'], iiko_revenue=prepared['iiko_revenue'],
                  status='ok', reconciliation=prepared['reconciliation'], closed=True)
    assert store.save_day(YESTERDAY, prepared['lines'], prepared['orders'], fetched_at=NOW,
                          revision=data['revision'], **common)['saved']
    late = store.save_day(YESTERDAY, [], [], fetched_at=NOW - timedelta(minutes=1), revision='old', **common)
    assert not late['saved']
    assert store.state(YESTERDAY)['revision'] == data['revision']
    assert row_count(store, 'sales_lines', YESTERDAY) == 11


def test_duplicate_iiko_rows_are_rejected_instead_of_doubling():
    _, lines, _ = olap_answers(LUNCH[:1])
    with pytest.raises(DataError, match='повторяющиеся'):
        lines_from_olap(lines + lines[:1])


def test_failed_load_is_logged_keeps_previous_day_and_retries_later(store):
    box, clock = clock_at(NOW)
    source = FakeSource({TODAY: LUNCH})

    async def scenario():
        sync = SalesSync(store, source, history_start=TODAY, clock=clock)
        await sync.start_day(TODAY, reason='test')
        source.error = DataError('iiko отклонил запрос отчёта.')
        box[0] = NOW + timedelta(minutes=11)
        with pytest.raises(DataError):
            await sync.start_day(TODAY, reason='test')
        failed = store.runs(1, status='failed')[0]
        assert failed['error'] == 'iiko отклонил запрос отчёта.'
        assert store.state(TODAY)['orders'] == 8  # прежняя версия дня цела
        # Пять минут отказа: планировщик день не берёт; потом берёт снова.
        assert sync.due(box[0]) is None
        box[0] += timedelta(minutes=6)
        assert sync.due(box[0]) == TODAY

    asyncio.run(scenario())


def test_lease_blocks_second_process_and_expires(store):
    assert store.acquire(TODAY, 'first', NOW)
    assert not store.acquire(TODAY, 'second', NOW)
    source = FakeSource({TODAY: LUNCH})

    async def scenario():
        sync = SalesSync(store, source, history_start=TODAY, clock=lambda: NOW)
        assert (await sync.start_day(TODAY, reason='test'))['status'] == 'skipped'
        assert source.calls == []

    asyncio.run(scenario())
    assert store.acquire(TODAY, 'second', NOW + timedelta(seconds=301))


def test_schedule_today_then_requested_then_morning_reread_then_backfill_backwards(store):
    source = FakeSource()
    box, clock = clock_at(datetime(2026, 10, 17, 5, 30, tzinfo=TZ))

    async def scenario():
        sync = SalesSync(store, source, history_start=date(2026, 10, 1), clock=clock)
        assert sync.due(box[0]) == TODAY
        await sync.run_once()
        # До 06:00 вчерашний день ещё не закрыт, но его нет вовсе — грузим.
        assert sync.due(box[0]) == YESTERDAY
        await sync.run_once()
        assert not store.state(YESTERDAY)['closed']
        sync.want([date(2026, 10, 2)])
        assert sync.due(box[0]) == date(2026, 10, 2)
        await sync.run_once()
        assert sync.due(box[0]) == date(2026, 10, 15)  # дальше — неделя назад по дню
        for _ in range(6):
            await sync.run_once()
        # Дозаливка идёт от свежих дней к началу истории.
        assert sync.due(box[0]) == date(2026, 10, 9)
        # После 06:00: сначала сегодняшний день (ему полчаса), затем вчерашний —
        # уже как закрытый.
        box[0] = datetime(2026, 10, 17, 6, 1, tzinfo=TZ)
        assert sync.due(box[0]) == TODAY
        await sync.run_once()
        assert sync.due(box[0]) == YESTERDAY
        await sync.run_once()
        assert store.state(YESTERDAY)['closed']
        # Сегодня — не чаще раза в десять минут.
        assert sync.due(box[0]) != TODAY
        box[0] = datetime(2026, 10, 17, 6, 11, tzinfo=TZ)
        assert sync.due(box[0]) == TODAY

    asyncio.run(scenario())


# ── Сводка: пропуски, окно часов, сегодня, топ блюд ─────────────────────────

def load(store, day, orders, at):
    async def scenario():
        sync = SalesSync(store, FakeSource({day: orders}), history_start=date(2026, 10, 1), clock=lambda: at)
        return await sync.start_day(day, reason='test')
    return asyncio.run(scenario())


def test_missing_days_are_listed_never_zero(store):
    day = date(2026, 10, 14)
    load(store, day, [replace_day(item, day) for item in LUNCH], datetime(2026, 10, 15, 7, tzinfo=TZ))
    service = service_for(store)
    result = asyncio.run(service.summary(date(2026, 10, 13), date(2026, 10, 15), refresh=False))
    assert result['missing_days'] == ['2026-10-13', '2026-10-15']
    assert not result['complete'] and result['days_with_data'] == 1
    assert result['totals']['revenue'] == '5796000'
    assert [row['day'] for row in result['by_day']] == ['2026-10-14']
    assert any('13.10, 15.10' in warning for warning in result['warnings'])
    empty = asyncio.run(service.summary(date(2026, 10, 11), date(2026, 10, 12), refresh=False))
    assert empty['totals'] is None and empty['by_hour'] == [] and empty['days_with_data'] == 0


def replace_day(item, day):
    return order(item['id'], item['closed'][11:16], item['items'], register=item['register'],
                 section=item['section'], waiter=item['waiter'], ops=item['ops'], day=day)


def test_lunch_window_by_close_time_and_direction(store):
    load(store, TODAY, LUNCH, NOW)
    service = service_for(store)
    lunch = make_window(lunch=LUNCH_HOURS)
    result = asyncio.run(service.summary(TODAY, TODAY, window=lunch, refresh=False))
    assert result['window']['label'] == '12:00–15:00'
    # «c» закрыт в 11:40, «d» — ровно в 15:00: правая граница не входит.
    assert result['totals']['revenue'] == str(108000 + 58000 + 300000 + 5000000 + 0 + 150000)
    assert result['totals']['orders'] == 6 and result['totals']['receipts'] == 5  # «g» без оплаты
    assert [row['hour'] for row in result['by_hour']] == ['12:00', '13:00', '14:00']
    assert set(result['by_direction']) == {'retro', 'school', 'banquet'}
    retro = asyncio.run(service.summary(TODAY, TODAY, window=lunch, direction='retro', refresh=False))
    assert retro['totals']['revenue'] == str(108000 + 58000 + 150000)
    assert retro['totals']['average_check'] == '105333'
    hours = make_window('11', '12:30')
    early = asyncio.run(service.summary(TODAY, TODAY, window=hours, refresh=False))
    assert early['totals']['revenue'] == str(60000 + 108000)


def test_several_directions_at_once_lunch_without_banquet(store):
    """T-500: плитка обеда директора — Retro и Школа одним запросом, без банкетного зала."""
    load(store, TODAY, LUNCH, NOW)
    service = service_for(store)
    lunch = make_window(lunch=LUNCH_HOURS)
    result = asyncio.run(service.summary(TODAY, TODAY, window=lunch, direction='retro,school', top=5,
                                         refresh=False))
    assert result['direction'] == 'retro,school' and not result['configured']
    assert result['totals']['revenue'] == str(108000 + 58000 + 150000 + 300000)
    assert result['totals']['receipts'] == 4 and result['totals']['average_check'] == '154000'
    assert set(result['by_direction']) == {'retro', 'school'}
    assert 'БЕХРУЗ ПЛОВ' not in [item['name'] for item in result['top_items']]
    assert sum(int(row['revenue']) for row in result['by_hour']) == 616000
    for wrong in ('retro,retro', 'retro,bar', '', 'retro,'):
        with pytest.raises(DataError):
            asyncio.run(service.summary(TODAY, TODAY, window=lunch, direction=wrong, refresh=False))
    assert asyncio.run(service_for(store, sync=FakeSync()).summary(TODAY, TODAY, refresh=False))['configured']


class FakeSync:
    """Синхронизация есть, но в тесте ничего не грузит."""
    jobs = {}


def test_today_is_partial_with_update_time_and_window_in_progress(store):
    load(store, TODAY, LUNCH, NOW - timedelta(minutes=1))
    result = asyncio.run(service_for(store).summary(TODAY, TODAY, window=make_window(lunch=LUNCH_HOURS),
                                                    refresh=False))
    assert result['partial'] and result['partial_days'] == ['2026-10-17'] and not result['complete']
    assert result['updated_at'] == '2026-10-17T13:04:00+05:00'
    assert any('только закрытые чеки на 13:04' in warning for warning in result['warnings'])
    assert any('ещё не закончилось' in warning for warning in result['warnings'])


def test_stale_today_is_synced_before_answer_and_fresh_is_not(store):
    box, clock = clock_at(NOW)
    source = FakeSource({TODAY: LUNCH[:2]})

    async def scenario():
        sync = SalesSync(store, source, history_start=date(2026, 10, 10), clock=clock)
        service = service_for(store, sync, clock=clock)
        first = await service.summary(TODAY, TODAY)
        assert first['totals']['receipts'] == 2 and len(source.calls) == 1
        box[0] = NOW + timedelta(seconds=90)
        await service.summary(TODAY, TODAY)
        assert len(source.calls) == 1  # моложе двух минут — iiko не трогаем
        source.days[TODAY] = LUNCH
        box[0] = NOW + timedelta(minutes=3)
        fresh = await service.summary(TODAY, TODAY)
        assert len(source.calls) == 2 and fresh['totals']['receipts'] == 7
        # iiko лёг: ответ — сохранённые данные с пометкой, а не ошибка.
        source.error = DataError('iiko не выполнил запрос (HTTP 500).')
        box[0] = NOW + timedelta(minutes=6)
        degraded = await service.summary(TODAY, TODAY)
        assert degraded['totals']['receipts'] == 7 and degraded['refresh_error']
        # Пропущенный прошлый день ставится в очередь фона, а не грузится в ответе.
        await service.summary(date(2026, 10, 12), TODAY)
        assert sync.wanted[0] == date(2026, 10, 12) and date(2026, 10, 12) not in source.calls

    asyncio.run(scenario())


def test_top_items_are_ready_dishes_by_default(store):
    load(store, TODAY, LUNCH, NOW)
    cards = [dict(product_id='1', name='Лагман', kind='DISH'), dict(product_id='2', name='Плов', kind='DISH'),
             dict(product_id='3', name='Чай', kind='DISH'), dict(product_id='4', name='Салат Ачичук сет',
                                                                 kind='MODIFIER'),
             dict(product_id='5', name='Сет Ретро', kind='DISH'), dict(product_id='6', name='Шашлык', kind='DISH'),
             dict(product_id='7', name='БЕХРУЗ ПЛОВ', kind='DISH')]
    service = service_for(store, catalog=lambda: MenuCatalog(cards))
    result = asyncio.run(service.summary(TODAY, TODAY, direction='retro', refresh=False))
    names = [item['name'] for item in result['top_items']]
    assert 'Мед' not in names and 'Салат Ачичук сет' not in names
    assert names[:2] == ['Шашлык', 'Лагман']  # по количеству: 3 и 3, при равенстве — выручка
    assert result['excluded_items'] == {'ingredient': {'items': 1, 'revenue': '8000'},
                                        'set_component': {'items': 1, 'revenue': '0'}}
    assert result['catalog_checked']
    by_revenue = asyncio.run(service.summary(TODAY, TODAY, sort='revenue', top=1, refresh=False))
    assert by_revenue['top_items'][0]['name'] == 'БЕХРУЗ ПЛОВ'
    everything = asyncio.run(service.summary(TODAY, TODAY, include_non_dishes=True, top=50, refresh=False))
    assert {'Мед', 'Салат Ачичук сет'} <= {item['name'] for item in everything['top_items']}
    # Без справочника решает тип позиции iiko: модификатор не блюдо, мёд — по правилу.
    plain = asyncio.run(service_for(store).summary(TODAY, TODAY, refresh=False))
    assert {'Мед', 'Салат Ачичук сет'}.isdisjoint(item['name'] for item in plain['top_items'])
    assert not plain['catalog_checked']


# ── Сверка ──────────────────────────────────────────────────────────────────

def test_reconciliation_flags_mismatch_with_iiko_control(store):
    class Broken(FakeSource):
        async def load_day(self, day):
            data = await super().load_day(day)
            data['control'][0] = dict(data['control'][0], revenue=data['control'][0]['revenue'] + 5000)
            return data

    async def scenario():
        sync = SalesSync(store, Broken({YESTERDAY: LUNCH}), history_start=YESTERDAY, clock=lambda: NOW)
        return await sync.start_day(YESTERDAY, reason='test')

    result = asyncio.run(scenario())
    assert result['reconciliation'] == 'mismatch'
    assert any('выручка в базе' in item for item in result['differences'])
    summary = asyncio.run(service_for(store).summary(YESTERDAY, YESTERDAY, refresh=False))
    assert any('Сверка с итогом iiko не сошлась' in warning for warning in summary['warnings'])


def test_reconciliation_against_cashier_snapshot(store, tmp_path):
    load(store, YESTERDAY, [replace_day(item, YESTERDAY) for item in LUNCH], NOW)
    archive = CashierArchive(tmp_path / 'cashier.sqlite3', Settings(store_id=1))
    snapshot = replace(demo_snapshot(YESTERDAY), demo=False, fetched_at=NOW, revenue=Decimal(496000),
                       revenue_breakdown=RevenueBreakdown(Decimal(496000), Decimal(300000), Decimal(5000000)))
    archive.save(snapshot)
    result = asyncio.run(service_for(store, cashier=archive).reconcile(YESTERDAY))
    assert result['status'] == 'ok' and result['iiko_revenue'] == result['revenue']
    assert result['cashier']['status'] == 'ok', result['cashier']
    assert result['cashier']['directions']['retro'] == dict(ours='496000', cashier='496000', difference='0')
    assert result['cashier']['paid_retro'] == '496000' and result['cashier']['comparable']
    assert result['operations']['prepaid'] == '3000000'
    archive.save(replace(snapshot, fetched_at=NOW + timedelta(seconds=1),
                         revenue_breakdown=RevenueBreakdown(Decimal(490000), Decimal(300000), Decimal(5000000))))
    shifted = asyncio.run(service_for(store, cashier=archive).reconcile(YESTERDAY))
    assert shifted['cashier']['status'] == 'mismatch'
    assert asyncio.run(service_for(store).reconcile(date(2026, 10, 1)))['status'] == 'missing'


# ── Источник iiko: три отчёта по одному ─────────────────────────────────────

def test_iiko_source_reads_three_reports_one_at_a_time():
    control, lines, operations = olap_answers(LUNCH)
    bodies, inflight, peak = {}, [0], [0]

    def handler(request):
        path = request.url.path
        if path == '/api/auth/login':
            return httpx.Response(200, json={'token': 't'})
        if path == '/api/olap/init':
            body = json.loads(request.content)
            fetch_id = f'f{len(bodies)}'
            bodies[fetch_id] = body
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
            return httpx.Response(200, json={'fetchId': fetch_id})
        fetch_id = path.split('/')[4]
        inflight[0] -= 1
        groups = tuple(bodies[fetch_id]['groupFields'])
        rows = {CONTROL_GROUPS: control, LINE_GROUPS: lines, OPERATION_GROUPS: operations}[groups]
        return httpx.Response(200, json={'result': {'rows': rows}})

    client = IikoClient(Settings(login='u', password='p', store_id=7), transport=httpx.MockTransport(handler),
                        poll_delay=0)

    async def scenario():
        try:
            return await SalesIiko(lambda: client).load_day(TODAY)
        finally:
            await client.close()

    data = asyncio.run(scenario())
    assert peak[0] == 1 and len(bodies) == 3
    assert all(body['filters'][0]['dateFrom'] == body['filters'][0]['dateTo'] == '2026-10-17'
               for body in bodies.values())
    line = next(item for item in data['lines'] if item['order_id'] == 'b' and item['dish_name'] == 'Плов')
    assert line['closed_at'] == '2026-10-17T14:55:30.125000+05:00'
    assert line['discount'] == Decimal(10000) and line['cost'] == Decimal(20000)
    assert data['operations']['f'] == dict(paid=Decimal(2000000), prepaid=Decimal(3000000), other=Decimal(0))


# ── AI и API ────────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path):
    application = create_app(Settings(data_dir=tmp_path, sales_history_start=date(2026, 10, 1)))
    load(application.state.sales_store, TODAY, LUNCH, NOW)
    application.state.sales = SalesService(application.state.sales_store, settings=application.state.settings,
                                           clock=lambda: NOW)
    return application


def test_ai_tool_for_founder_and_director_names_window_and_update_time(app):
    arguments = {'start': '2026-10-17', 'end': '2026-10-17', 'window': 'lunch', 'direction': 'retro'}
    founder = asyncio.run(FounderChatTools(app).execute('get_sales_by_time', arguments))
    director = asyncio.run(DirectorChatTools(app).execute('get_sales_by_time', dict(arguments, top=3)))
    assert founder['window']['label'] == director['window']['label'] == '12:00–15:00'
    assert founder['updated_at'] == '2026-10-17T13:05:00+05:00' and founder['partial']
    assert founder['totals'] == director['totals'] and len(director['top_items']) <= 3
    for wrong in ({'start': '2026-10-17'}, dict(arguments, extra=1), dict(arguments, window='dinner'),
                  dict(arguments, from_time='12:00'), dict(arguments, end='2026-10-18'),
                  dict(arguments, from_time='15:00', to_time='12:00', window=None)):
        wrong = {key: value for key, value in wrong.items() if value is not None}
        with pytest.raises(DataError):
            asyncio.run(FounderChatTools(app).execute('get_sales_by_time', wrong))


def test_ai_chat_gets_current_time_and_sales_tool(app):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(200, json={'stop_reason': 'tool_use', 'content': [{
                'type': 'tool_use', 'id': 'tool-1', 'name': 'get_sales_by_time',
                'input': {'start': '2026-10-17', 'end': '2026-10-17', 'window': 'lunch'}}]})
        return httpx.Response(200, json={'stop_reason': 'end_turn', 'content': [
            {'type': 'text', 'text': 'Обед 12:00–15:00, данные на 13:05.'}]})

    from retro.integrations.claude import ClaudeClient
    claude = ClaudeClient(SimpleNamespace(claude_api_key='k', claude_model='m', claude_configured=True),
                          transport=httpx.MockTransport(handler))
    tools = DirectorChatTools(app)
    answer = asyncio.run(claude.chat([{'role': 'user', 'content': 'Выручка за обед сегодня?'}],
                                     tools=tools.definitions, tool_handler=tools.execute,
                                     current_date='2026-10-17', current_time='13:05', audience='director'))
    assert answer.startswith('Обед 12:00–15:00')
    assert 'Текущее время ресторана в Asia/Tashkent: 13:05' in seen[0]['system']
    result = json.loads(seen[1]['messages'][-1]['content'][0]['content'])
    assert result['window']['label'] == '12:00–15:00' and result['totals']['receipts'] == 5


def test_api_sales_for_director_and_founder_without_accounting_boundary(app):
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        lunch = client.get('/api/director/sales?start=2026-10-17&end=2026-10-17&window=lunch&direction=retro')
        assert lunch.status_code == 200, lunch.text
        assert lunch.json()['totals']['revenue'] == '316000'
        # T-500: обед Retro и Школы одним запросом; повтор и чужое имя — отказ.
        both = client.get('/api/director/sales?start=2026-10-17&window=lunch&direction=retro,school&top=5')
        assert both.status_code == 200 and both.json()['totals']['revenue'] == '616000'
        assert client.get('/api/director/sales?direction=retro,retro').status_code == 422
        assert client.get('/api/director/sales?direction=retro,bar').status_code == 422
        hours = client.get('/api/founder/sales?start=2026-10-17&from_hour=12&to_hour=13:00')
        assert hours.status_code == 200 and hours.json()['window']['label'] == '12:00–13:00'
        # До 13.10 (граница учёта) история доступна: это аналитика iiko, не учёт.
        early = client.get('/api/director/sales?start=2026-10-05&end=2026-10-06')
        assert early.status_code == 200 and early.json()['missing_days'] == ['2026-10-05', '2026-10-06']
        assert client.get('/api/director/sales?start=2026-10-18').status_code == 422
        assert client.get('/api/director/sales?window=lunch&from_hour=12').status_code == 422
        assert client.get('/api/director/sales?from_hour=25').status_code == 422
        assert client.get('/api/director/sales/reconcile?date=2026-10-17').json()['status'] == 'ok'
        status = client.get('/api/director/sales/status').json()
        assert status['lunch_hours'] == '12:00–15:00' and not status['configured']
        assert client.post('/api/director/sales/sync?date=2026-10-17').status_code == 503


def test_sales_api_follows_panel_roles(tmp_path):
    users = {'director': ('password', 'director'), 'cashier': ('password', 'cashier'),
             'accountant': ('password', 'accountant'), 'founder': ('password', 'founder')}
    app = create_app(Settings(dashboard_panel_users=users, data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000), base_url='http://127.0.0.1') as client:
        for user, director, founder in (('director', 200, 403), ('founder', 403, 200), ('cashier', 403, 403)):
            client.post('/api/session', json={'username': user, 'password': 'password'})
            assert client.get('/api/director/sales').status_code == director, user
            assert client.get('/api/founder/sales').status_code == founder, user
            client.post('/api/session/logout')


def test_background_sync_does_not_reach_iiko_during_tests(tmp_path):
    calls = []
    app = create_app(Settings(login='u', password='p', store_id=7, data_dir=tmp_path))
    app.state.iiko = IikoClient(app.state.settings, transport=httpx.MockTransport(
        lambda request: calls.append(request.url.path) or httpx.Response(503)))
    app.state.cashier_days = None
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)):
        assert app.state.sales_sync.worker is not None
    assert app.state.sales_sync.worker is None and calls == []


def test_settings_parse_lunch_and_history_start():
    assert parse_lunch_hours('') == (720, 900)
    assert parse_lunch_hours('11:30-15') == (690, 900)
    assert parse_lunch_hours('12:00-24:00') == (720, 1440)
    for wrong in ('15:00-12:00', '12', '12:00-25:00', 'обед'):
        with pytest.raises(ValueError):
            parse_lunch_hours(wrong)
    assert parse_sales_history_start('') == date(2026, 9, 1)
    assert parse_sales_history_start('2026-10-01') == date(2026, 10, 1)
    with pytest.raises(ValueError):
        parse_sales_history_start('01.10.2026')


def test_director_phone_shows_lunch_tile_from_this_api(app):
    """T-500: плитка «Обед сегодня» и лист обеда на телефоне директора берут ответ отсюда."""
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        phone = client.get('/director').text
        logic = client.get('/static/director-logic.js').text
        tile = client.get('/api/director/sales?start=2026-10-17&end=2026-10-17&window=lunch'
                          '&direction=retro,school&top=5').json()
    assert 'id="lunch-open"' in phone and 'id="lunch-sheet"' in phone and 'id="lunch-date"' in phone
    assert "LUNCH_DIRECTIONS = ['retro', 'school']" in logic and '&window=lunch&direction=' in logic
    # Банкетный зал в плитку не попадает; окно и время данных — в ответе.
    assert set(tile['by_direction']) == {'retro', 'school'} and len(tile['top_items']) <= 5
    assert tile['window']['label'] == '12:00–15:00' and tile['updated_at'] and tile['partial']
