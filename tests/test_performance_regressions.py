import asyncio
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from threading import get_ident
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.async_utils import gather_reads
from retro.config import Settings
from retro.integrations.iiko import IikoClient, director_rows_from_olap
from retro.modules.accountant.ledger import FinanceStore
from retro.report_cache import ReportCache, refresh_source
from scripts.performance_probe import olap_counts, poller_event_loop_block


def test_director_uses_three_olaps_and_reuses_raw_reports():
    """Десятидневный отчёт директора: два отчёта себестоимости и один оплат.

    Четвёртым был второй отчёт оплат, отличавшийся только фильтром
    `OperationType=PAYMENT`; теперь вид операции приходит измерением и
    раскладывается на нашей стороне."""
    counts = asyncio.run(olap_counts())
    assert counts['first_load'] == {'auth': 1, 'olap_init': 3, 'olap_fetch': 3}
    assert counts['two_identical_loads'] == counts['first_load']


def test_range_rows_preserve_date_and_zero_revenue_purpose():
    values = ['2026-09-13', 'Kassa-FiscalBox1', 'Ресторан', '(без оплаты)',
              'Самса', 'Кухня', 'Олег', 'order-1', 'Счет Шефа', 2, 0, 5161.60]
    raw = [{f'field{i}': {'value': value} for i, value in enumerate(values)}]
    row, = director_rows_from_olap(None, raw, payment_details=True)
    assert row.day == date(2026, 9, 13)
    assert row.cost == Decimal('5161.60')
    assert row.non_cash_payment_type == 'Счет Шефа'


def test_iiko_reauthorizes_once_and_force_refresh_bypasses_raw_cache():
    async def scenario():
        auths, inits, fetches = 0, 0, 0

        async def handler(request):
            nonlocal auths, inits, fetches
            if request.url.path == '/api/auth/login':
                auths += 1
                return httpx.Response(200, json={'token': str(auths)})
            if request.headers['Authorization'] == 'Bearer 1':
                return httpx.Response(401)
            if request.url.path == '/api/olap/init':
                inits += 1
                return httpx.Response(200, json={'fetchId': str(inits)})
            fetches += 1
            return httpx.Response(200, json={'result': {'rows': []}})

        source = IikoClient(Settings(login='test', password='test', store_id=1),
                            transport=httpx.MockTransport(handler))
        day = date(2026, 9, 20)
        try:
            async with source._client() as client:
                async def report():
                    return await source._olap(client, day, ['PayTypes'], ['DishDiscountSumInt'])
                await gather_reads(report(), report())
                assert (auths, inits, fetches) == (2, 1, 1)
                await report()
                assert inits == 1
                token = refresh_source.set(True)
                try:
                    await report()
                finally:
                    refresh_source.reset(token)
                assert (auths, inits, fetches) == (2, 2, 2)
        finally:
            await source.close()
    asyncio.run(scenario())


def test_failed_parallel_read_cancels_and_drains_its_siblings():
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def failed():
            await started.wait()
            raise ValueError('synthetic')
        async def slow():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        with pytest.raises(ValueError, match='synthetic'):
            await gather_reads(failed(), slow())
        assert stopped.is_set()
    asyncio.run(scenario())


def test_report_queue_has_a_bound_but_existing_readers_can_join():
    async def scenario():
        cache = ReportCache(max_pending=1)
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked():
            started.set()
            await release.wait()
            return 42
        first = asyncio.create_task(cache.get('one', blocked))
        await started.wait()
        with pytest.raises(TimeoutError):
            await cache.get('two', blocked)
        second = asyncio.create_task(cache.get('one', blocked))
        await asyncio.sleep(0)
        release.set()
        assert await first == await second == 42
    asyncio.run(scenario())


def test_accruals_use_two_selects_and_exact_decimal_at_selected_date(tmp_path):
    store = FinanceStore(tmp_path / 'ledger.sqlite3')
    day = date(2026, 9, 20)
    with closing(store._open()) as connection, connection:
        connection.execute("INSERT INTO accountant_accruals "
            "(work_day,employee_id,employee_name,group_name,attendance_status,rate,amount) "
            "VALUES ('2026-09-19',1,'Synthetic','Synthetic','on_time','1','1')")
        connection.executemany("INSERT INTO accountant_salary_payments "
            "(accrual_id,paid_day,amount,created_at) VALUES (1,?,?, 'synthetic')",
            [('2026-09-19', '0.1'), ('2026-09-20', '0.2'), ('2026-09-21', '0.4')])
    statements, original = [], store._open
    def traced():
        connection = original()
        connection.set_trace_callback(statements.append)
        return connection
    store._open = traced
    row, = store.accruals(day)
    assert row['paid'] == '0.3' and row['debt'] == '0.7'
    assert len([s for s in statements if s.startswith('SELECT')]) == 2
    assert not any('CREATE' in s or 'ALTER' in s for s in statements)


def test_poller_sqlite_work_runs_outside_event_loop(tmp_path, monkeypatch):
    from retro.modules.accountant.hikvision import AttendanceStore
    original = AttendanceStore.record_attempt
    event_loop_thread = get_ident()
    def record(*args, **kwargs):
        assert get_ident() != event_loop_thread
        return original(*args, **kwargs)
    # The probe's initial fixture write is synchronous; only instrument run_once.
    from retro.integrations.hikvision_poller import HikvisionPoller
    run = HikvisionPoller.run_once
    async def checked_run(*args):
        monkeypatch.setattr(AttendanceStore, 'record_attempt', record)
        return await run(*args)
    monkeypatch.setattr(HikvisionPoller, 'run_once', checked_run)
    result = asyncio.run(poller_event_loop_block(tmp_path))
    assert result['poll_success']


def test_staff_endpoint_avoids_financial_summary_and_iiko(tmp_path, monkeypatch):
    app = create_app(Settings(data_dir=tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError('Staff page must not load the financial ledger or iiko')
    monkeypatch.setattr(app.state.accountant_finance, 'daily_summary', forbidden)
    monkeypatch.setattr(app.state.accountant_finance, 'accruals', forbidden)
    monkeypatch.setattr(app.state.iiko, 'load', forbidden)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/accountant/staff?date=2026-09-20')
    assert response.status_code == 200
    assert {'employees', 'monthly_employees', 'groups', 'attendance', 'payroll'} <= response.json().keys()
    assert 'finance' not in response.json()


def test_static_files_revalidate_while_api_responses_remain_private(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        first = client.get('/static/founder.js')
        second = client.get('/static/founder.js', headers={'If-None-Match': first.headers['etag']})
        api = client.get('/api/config')
    assert first.status_code == 200 and second.status_code == 304
    assert first.headers['cache-control'] == second.headers['cache-control'] == 'private, no-cache'
    assert api.headers['cache-control'] == 'no-store'


def test_pages_link_static_files_by_content_hash_and_those_are_cached_for_good(tmp_path):
    import re
    app = create_app(Settings(data_dir=tmp_path, shokh_module=True))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        for path in ('/', '/accountant', '/accountant/payroll', '/accountant/employees', '/shokh',
                     '/accountant/salary-day', '/accountant/shoh',
                     '/director', '/director/report', '/founder', '/founder/analytics', '/login'):
            page = client.get(path)
            assert page.status_code == 200 and page.headers['cache-control'] == 'no-store', path
            assert not re.search(r'(?:src|href)="/static/[^"?]+"', page.text), path
            for asset in re.findall(r'(?:src|href)="(/static/[^"]+\?v=[0-9a-f]{12})"', page.text):
                response = client.get(asset)
                assert response.status_code == 200, asset
                assert response.headers['cache-control'] == 'private, max-age=31536000, immutable'


def test_asset_version_follows_the_file_content(tmp_path):
    from retro.static_assets import Pages
    (tmp_path / 'app.js').write_text('one')
    (tmp_path / 'page.html').write_text('<script src="/static/app.js"></script><img src="/static/none.png">')
    pages = Pages(tmp_path)
    first = pages.html('page.html')
    (tmp_path / 'app.js').write_text('two, longer')
    second = pages.html('page.html')
    assert first != second and '?v=' in first and '?v=' in second
    assert 'src="/static/none.png"' in second  # несуществующий файл не трогаем


def test_large_responses_are_compressed(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        response = client.get('/static/i18n-uz.js', headers={'Accept-Encoding': 'gzip'})
    assert response.status_code == 200
    assert response.headers['content-encoding'] == 'gzip'


def week_app(tmp_path, recorder):
    """Стенд недели учредителя с записью всех запросов к базе."""
    import retro.db as db_module
    from test_founder_cabinet import FakeIiko

    original = db_module.Database.connect

    class Recorder:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            recorder[' '.join(sql.split()), tuple(params)] += 1
            return self._connection.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __enter__(self):
            return self._connection.__enter__()

        def __exit__(self, *info):
            return self._connection.__exit__(*info)

    def traced(self):
        connection = original(self)
        return Recorder(connection) if not self.is_postgres else connection

    app = create_app(Settings(manual_handover_only=True),
                     expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     founder_db_path=tmp_path / 'founder.sqlite3')
    app.state.iiko = FakeIiko()
    finance, roster = app.state.accountant_finance, app.state.accountant_roster
    for offset in range(12):
        finance.record_handover(date(2026, 10, 2) + timedelta(days=offset), Decimal('1000000'))
    finance.set_cash_opening(date(2026, 10, 2), '5000000', 'начальный остаток')
    for index in range(3):
        roster.add_monthly(name=f'Окладник {index}', role='Менеджер', salary='3000000')
    db_module.Database.connect = traced
    return app, lambda: setattr(db_module.Database, 'connect', original)


def test_week_reads_each_shared_table_once_not_once_per_day(tmp_path):
    """Семь дней недели читают общие таблицы один раз, а не по разу на день.

    Раньше каждый из семи вложенных вызовов дня бухгалтера заново читал
    реестр окладников, книгу остатков и журнал переноса передач: половина
    запросов недели была буквальным повтором предыдущего."""
    seen = Counter()
    app, restore = week_app(tmp_path, seen)
    try:
        with patch('retro.modules.founder.cabinet.today_tashkent', return_value=date(2026, 10, 13)), \
                patch('retro.modules.founder.routes.today_tashkent', return_value=date(2026, 10, 13)), \
                patch('retro.modules.accountant.routes.today_tashkent', return_value=date(2026, 10, 13)), \
                TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
            client.get('/api/founder/week?date=2026-10-08')  # прогрев кэша iiko
            seen.clear()
            response = client.get('/api/founder/week?date=2026-10-08')
    finally:
        restore()

    assert response.status_code == 200
    assert len(response.json()['days']) == 7
    def times(fragment, params=()):
        return sum(count for (sql, values), count in seen.items()
                   if fragment in sql and values == params)

    # Действующие окладники — одно чтение на ответ. Было двадцать одно.
    assert times('accountant_monthly_employees WHERE archived', (0,)) == 1
    # Книга остатков читается по последний день недели один раз (warm_cash_book).
    assert times('SELECT month, last_day, closing_balance') == 1
    # Журнал переноса передач — один раз на ответ, а не на каждый день.
    assert times('accountant_finance_audit',
                 ('finance_migration', 'handover_receipt_day_v1')) == 1
    # Движения за шесть недель для истории дивидендов — тоже один раз.
    assert times('FROM accountant_handover_days WHERE day >= ? AND day <= ?',
                 ('2026-10-02', '2026-10-04')) == 1
    # Повторов в ответе осталось меньше четверти: остальное — работа по дню.
    repeated = sum(count - 1 for count in seen.values())
    assert repeated * 4 < sum(seen.values())


def test_read_cache_covers_only_safe_methods(tmp_path):
    """Запись из кэша чтений не читает: иначе проверка остатка увидела бы
    состояние до собственной транзакции."""
    import retro.app as app_module

    methods = []
    original = app_module.request_reads.begin

    def spy():
        methods.append('begin')
        return original()

    app = create_app(Settings(), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     founder_db_path=tmp_path / 'founder.sqlite3')
    with patch.object(app_module.request_reads, 'begin', spy), \
            TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
        client.get('/api/accountant/day?date=2026-10-08')
        assert methods == ['begin']
        client.post('/api/accountant/no-such-endpoint', json={})
        assert methods == ['begin']
    # Вне запроса кэша нет вовсе.
    from retro import request_reads
    assert request_reads.active() is None
