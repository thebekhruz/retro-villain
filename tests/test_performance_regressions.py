import asyncio
from contextlib import closing
from datetime import date
from decimal import Decimal
from threading import get_ident

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
