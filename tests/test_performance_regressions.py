import asyncio
from contextlib import closing
from datetime import date
from decimal import Decimal
from threading import get_ident

import httpx
import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.async_utils import gather_reads
from retro.config import Settings
from retro.integrations.iiko import IikoClient, director_rows_from_olap
from retro.modules.accountant.ledger import FinanceStore
from retro.report_cache import ReportCache, refresh_source
from scripts.performance_probe import olap_counts, poller_event_loop_block


def test_director_uses_four_olaps_and_reuses_raw_reports():
    counts = asyncio.run(olap_counts())
    assert counts['first_load'] == {'auth': 1, 'olap_init': 4, 'olap_fetch': 4}
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
