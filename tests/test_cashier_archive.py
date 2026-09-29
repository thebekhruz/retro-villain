import asyncio
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from retro.app import create_app
from retro.config import Settings
from retro.integrations.iiko import IikoClient, cashier_read
from retro.modules.cashier.archive import CashierArchive, archive_boundary
from retro.modules.cashier.days import CashierDays
from retro.modules.cashier.service import DataError, TZ, demo_snapshot, today_tashkent
from retro.report_cache import ReportCache, load_iiko

NOW = datetime(2026, 9, 28, 15, tzinfo=TZ)
DAY = NOW.date() - timedelta(days=1)


@pytest.fixture(params=['sqlite', 'postgres'])
def archive(request, tmp_path):
    database = tmp_path / 'cashier.sqlite3'
    if request.param == 'postgres':
        database = os.getenv('RETRO_TEST_POSTGRES_URL')
        if not database:
            pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
    return CashierArchive(database, Settings(store_id=uuid4().int % 1000000000))


def real_snapshot(day=DAY, when=NOW):
    return replace(demo_snapshot(day), demo=False, fetched_at=when,
                   cash_prepayment=Decimal('123456789.123456789'))


def test_roundtrip_exact_money_revisions_and_older_write_cannot_overwrite(archive):
    first = real_snapshot()
    archive.save(first)
    assert archive.get(DAY) == replace(first, source='database')
    second = replace(first, id=uuid4().hex, fetched_at=NOW + timedelta(seconds=1),
                     cash_prepayment=Decimal('0.01'))
    archive.save(second)
    archive.save(first)
    assert archive.get(DAY).id == second.id
    with archive.db.cursor() as connection:
        assert connection.execute('SELECT COUNT(*) FROM cashier_day_revisions WHERE source=?',
                                  (archive.source,)).fetchone()[0] == 1


def test_demo_stale_and_other_store_never_enter_live_archive(archive):
    for value in [demo_snapshot(DAY), replace(real_snapshot(), stale=True)]:
        with pytest.raises(DataError):
            archive.save(value)
    archive.save(real_snapshot())
    other = CashierArchive(archive.db, Settings(store_id=-1))
    assert other.get(DAY) is None


def test_lease_expires_and_old_owner_cannot_release_new_owner(archive):
    assert archive.acquire(DAY, 'first', NOW)
    assert not archive.acquire(DAY, 'second', NOW)
    assert archive.acquire(DAY, 'second', NOW + timedelta(seconds=121))
    archive.release(DAY, 'first')
    assert not archive.acquire(DAY, 'third', NOW + timedelta(seconds=122))
    archive.release(DAY, 'second')
    assert archive.acquire(DAY, 'third', NOW + timedelta(seconds=122))


def test_archived_day_survives_new_service_and_bypasses_full_report_queue(archive):
    async def scenario():
        archive.save(real_snapshot())
        async def forbidden(day):
            raise AssertionError('Historical DB read must not call iiko')
        days = CashierDays(archive, forbidden, clock=lambda: NOW)
        state = SimpleNamespace(iiko=SimpleNamespace(load=forbidden), reports=ReportCache(concurrency=1),
                                cashier_days=days)
        started, release = asyncio.Event(), asyncio.Event()
        async def blocked():
            started.set()
            await release.wait()
        occupied = asyncio.create_task(state.reports.get('heavy', blocked))
        await started.wait()
        try:
            value = await asyncio.wait_for(load_iiko(state, 'load', DAY), 1)
            assert value.source == 'database' and value.cash_prepayment == Decimal('123456789.123456789')
            another = CashierDays(archive, forbidden, clock=lambda: NOW + timedelta(days=10))
            assert (await another.get(DAY)).id == value.id
            await another.close()
        finally:
            release.set()
            await occupied
            await days.close()
            await state.reports.close()
    asyncio.run(scenario())


def test_stale_ui_returns_immediately_but_strict_reader_waits_for_shared_refresh(archive):
    async def scenario():
        day = NOW.date()
        archive.save(real_snapshot(day, NOW - timedelta(minutes=5)))
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def source(day):
            calls.append(day)
            started.set()
            await release.wait()
            return real_snapshot(day)
        days = CashierDays(archive, source, clock=lambda: NOW)
        value = await asyncio.wait_for(days.get(day, allow_stale=True), 1)
        assert value.stale and value.refreshing and value.source == 'database'
        await started.wait()
        strict = asyncio.create_task(days.get(day))
        await asyncio.sleep(.02)
        assert not strict.done()
        release.set()
        result = await strict
        assert not result.stale and len(calls) == 1
        assert (await days.get(day)).id == result.id
        await days.close()
    asyncio.run(scenario())


def test_failed_refresh_preserves_archive_marks_ui_and_blocks_strict_live_read(archive):
    async def scenario():
        day = NOW.date()
        first = real_snapshot(day, NOW - timedelta(minutes=5))
        archive.save(first)
        calls = []
        async def failure(day):
            calls.append(day)
            raise DataError('synthetic upstream failure')
        days = CashierDays(archive, failure, clock=lambda: NOW)
        with pytest.raises(DataError):
            await days.get(day)
        value = await days.get(day, allow_stale=True)
        assert value.id == first.id and value.stale and not value.refreshing and value.refresh_error
        with pytest.raises(DataError):
            await days.get(day)
        assert len(calls) == 1  # Polling and strict reads respect failure backoff.
        assert archive.get(day).id == first.id
        await days.close()
    asyncio.run(scenario())


def test_historical_explicit_refresh_is_single_flight_and_failure_keeps_snapshot(archive):
    async def scenario():
        first = real_snapshot()
        archive.save(first)
        gate = asyncio.Event()
        async def failure(day):
            await gate.wait()
            raise DataError('synthetic')
        days = CashierDays(archive, failure, clock=lambda: NOW)
        value = await days.get(DAY, refresh=True, allow_stale=True)
        assert value.id == first.id and value.refreshing
        job = days.jobs[DAY]
        await days.get(DAY, refresh=True, allow_stale=True)
        assert days.jobs[DAY] is job
        gate.set()
        with pytest.raises(DataError):
            await job
        value = await days.get(DAY, allow_stale=True)
        assert value.id == first.id and value.refresh_error and not value.refreshing
        await days.close()
    asyncio.run(scenario())


def test_two_processes_share_db_lease_and_only_one_source_read(archive):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def source(day):
            calls.append(day)
            started.set()
            await release.wait()
            return real_snapshot(day)
        one = CashierDays(archive, source, clock=lambda: NOW)
        two = CashierDays(archive, source, clock=lambda: NOW)
        a = asyncio.create_task(one.get(DAY))
        await started.wait()
        b = asyncio.create_task(two.get(DAY))
        await asyncio.sleep(.05)
        release.set()
        x, y = await asyncio.gather(a, b)
        assert x.id == y.id and calls == [DAY]
        await one.close()
        await two.close()
    asyncio.run(scenario())


def test_midnight_snapshot_is_not_treated_as_finished_history_and_worker_catches_gaps(archive):
    archive.save(real_snapshot(DAY, NOW - timedelta(days=1)))
    assert DAY not in list(archive.due_days(NOW.replace(hour=3)))
    assert DAY in list(archive.due_days(NOW))
    days = CashierDays(archive, None, clock=lambda: NOW)
    assert days.stale(archive.get(DAY), NOW)
    archive.save(real_snapshot(DAY, NOW))
    assert DAY not in list(archive.due_days(NOW))
    assert DAY in list(archive.due_days(NOW + timedelta(days=1)))  # Recent corrections.
    assert NOW.date() in list(archive.due_days(NOW + timedelta(days=20)))  # Downtime gap.
    assert archive_boundary(DAY).hour == 6


def test_worker_skips_interactive_jobs_and_saves_one_day_per_tick(archive):
    async def scenario():
        calls = []
        async def source(day):
            calls.append(day)
            return real_snapshot(day)
        days = CashierDays(archive, source, clock=lambda: NOW)
        await days.archive_once()
        assert calls == [DAY] and archive.get(DAY)
        await days.archive_once()
        assert calls == [DAY, DAY - timedelta(days=1)]
        blocker = asyncio.create_task(asyncio.Event().wait())
        days.jobs[NOW.date()] = blocker
        await days.archive_once()
        assert len(calls) == 2
        await days.close()
    asyncio.run(scenario())


def test_route_stale_metadata_and_export_guard_then_fresh_export(tmp_path):
    async def scenario():
        settings = Settings(data_dir=tmp_path, login='synthetic', password='synthetic', store_id=1)
        app = create_app(settings)
        now, day = datetime.now(TZ), today_tashkent()
        app.state.cashier_days.archive.save(real_snapshot(day, now - timedelta(minutes=5)))
        release = asyncio.Event()
        async def source(day):
            await release.wait()
            return real_snapshot(day, datetime.now(TZ))
        app.state.iiko.load = source
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=('127.0.0.1', 1)),
                                     base_url='http://127.0.0.1') as client:
            data = (await client.get(f'/api/cashier/day?date={day}&allow_stale=true')).json()
            assert data['stale'] and data['refreshing']
            assert (await client.get(f'/api/cashier/export?date={day}&snapshot_id={data["snapshot_id"]}')).status_code == 409
            job = app.state.cashier_days.jobs[day]
            release.set()
            await job
            fresh = (await client.get(f'/api/cashier/day?date={day}')).json()
            assert not fresh['stale'] and not fresh['refreshing']
            assert (await client.get(f'/api/cashier/export?date={day}&snapshot_id={fresh["snapshot_id"]}')).status_code == 200
        await app.state.cashier_days.close()
        await app.state.iiko.close()
    asyncio.run(scenario())


def test_analytical_olaps_leave_capacity_for_cashier():
    async def scenario():
        source = IikoClient(Settings())
        active, maximum, started = [0], [0], []
        three_started, release = asyncio.Event(), asyncio.Event()
        async def report(client, body):
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            started.append(body)
            if len(started) == 3:
                three_started.set()
            try:
                if body != 'cashier':
                    await release.wait()
                return body
            finally:
                active[0] -= 1
        source._fetch_olap_data = report
        heavy = [asyncio.create_task(source._fetch_olap(None, n)) for n in range(4)]
        await three_started.wait()
        token = cashier_read.set(True)
        try:
            assert await asyncio.wait_for(source._fetch_olap(None, 'cashier'), .5) == 'cashier'
            assert maximum[0] == 4 and 3 not in started
        finally:
            cashier_read.reset(token)
            release.set()
            await asyncio.gather(*heavy)
            await source.close()
    asyncio.run(scenario())


def test_background_history_does_not_hold_the_live_day_slot(archive):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def source(day):
            if day == DAY:
                started.set()
                await release.wait()
            return real_snapshot(day)
        days = CashierDays(archive, source, clock=lambda: NOW)
        history = asyncio.create_task(days.get(DAY))
        await started.wait()
        try:
            assert (await asyncio.wait_for(days.get(NOW.date()), 1)).day == NOW.date()
        finally:
            release.set()
            await history
            await days.close()
    asyncio.run(scenario())


def test_cancelled_ui_reader_keeps_single_archive_job_and_shutdown_releases_lease(archive):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def source(day):
            started.set()
            await release.wait()
            return real_snapshot(day)
        days = CashierDays(archive, source, clock=lambda: NOW)
        reader = asyncio.create_task(days.get(DAY))
        await started.wait()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert not days.jobs[DAY].cancelled()
        release.set()
        await days.jobs[DAY]
        assert archive.get(DAY) is not None
        started.clear()
        release.clear()
        task = asyncio.create_task(days.get(NOW.date()))
        await started.wait()
        await days.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert archive.acquire(NOW.date(), 'new-process', NOW)
    asyncio.run(scenario())


def test_zero_day_is_saved_but_source_failure_is_never_saved_as_zero(archive):
    from retro.modules.cashier.service import build_snapshot
    async def scenario():
        async def source(day):
            if day == DAY:
                return replace(build_snapshot(day, [], []), fetched_at=NOW)
            raise DataError('synthetic')
        days = CashierDays(archive, source, clock=lambda: NOW)
        value = await days.get(DAY)
        assert value.revenue == 0 and archive.get(DAY).receipt_count == 0
        with pytest.raises(DataError):
            await days.get(DAY - timedelta(days=1))
        assert archive.get(DAY - timedelta(days=1)) is None
        await days.close()
    asyncio.run(scenario())
