import asyncio
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from retro.app import create_app
from retro.config import Settings
from retro.modules.cashier.service import demo_snapshot
from retro.report_cache import ReportCache, load_iiko


def test_identical_readers_share_work_and_one_cancellation_keeps_other_alive():
    async def scenario():
        cache = ReportCache()
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return 'result'

        first = asyncio.create_task(cache.get('same', operation))
        await started.wait()
        second = asyncio.create_task(cache.get('same', operation))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == 'result'
        assert await cache.get('same', operation) == 'result'
        assert calls == 1

    asyncio.run(scenario())


def test_last_reader_cancellation_releases_capacity_and_new_request_can_start():
    async def scenario():
        cache = ReportCache(concurrency=1)
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def stuck():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        reader = asyncio.create_task(cache.get('old', stuck))
        await started.wait()
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert cancelled.is_set()
        assert not cache.pending
        assert await cache.get('new', lambda: asyncio.sleep(0, result=12), timeout=.1) == 12

    asyncio.run(scenario())


def test_deadline_includes_time_waiting_for_capacity():
    async def scenario():
        cache = ReportCache(concurrency=1)
        started, release = asyncio.Event(), asyncio.Event()
        ran = False

        async def occupied():
            started.set()
            await release.wait()

        async def queued():
            nonlocal ran
            ran = True

        first = asyncio.create_task(cache.get('first', occupied))
        await started.wait()
        with pytest.raises(TimeoutError):
            await cache.get('queued', queued, timeout=.03)
        assert not ran
        release.set()
        await first
        assert not cache.pending

    asyncio.run(scenario())


def test_ttl_refresh_failed_refresh_and_lru_bound():
    async def scenario():
        now = [0]
        cache = ReportCache(limit=2, clock=lambda: now[0])
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            return calls

        assert await cache.get('a', operation, ttl=5) == 1
        assert await cache.get('a', operation) == 1
        assert await cache.get('a', operation, refresh=True, ttl=5) == 2
        now[0] = 6
        assert await cache.get('a', operation) == 3
        await cache.get('b', operation)
        await cache.get('c', operation)
        assert list(cache.entries) == ['b', 'c']

        async def failure():
            raise ValueError('synthetic')

        with pytest.raises(ValueError):
            await cache.get('c', failure, refresh=True)
        assert 'c' not in cache.entries
        assert await cache.get('c', operation) == 6

    asyncio.run(scenario())


def test_weight_bound_skips_oversized_values_and_evicts_previous_entries():
    async def scenario():
        cache = ReportCache(max_weight=5, weigh=len)
        for name, value in [('a', '123'), ('b', '456'), ('c', '123456')]:
            assert await cache.get(name, lambda: asyncio.sleep(0, result=value)) == value
        assert list(cache.entries) == ['b']
    asyncio.run(scenario())


def test_cancelled_http_reader_does_not_cancel_same_day_reader(tmp_path):
    async def scenario():
        app = create_app(Settings(data_dir=tmp_path))
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        class Source:
            async def load(self, day):
                calls.append(day)
                started.set()
                await release.wait()
                return demo_snapshot(day)

        app.state.iiko = Source()
        transport = httpx.ASGITransport(app=app, client=('127.0.0.1', 50000))
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1') as client:
            first = asyncio.create_task(client.get('/api/cashier/day?date=2026-09-20'))
            await started.wait()
            second = asyncio.create_task(client.get('/api/cashier/day?date=2026-09-20'))
            # Synchronize on two actual subscribers, not on an arbitrary sleep.
            for _ in range(100):
                if next(iter(app.state.reports.pending.values())).readers == 2:
                    break
                await asyncio.sleep(.001)
            assert next(iter(app.state.reports.pending.values())).readers == 2
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            assert (await second).status_code == 200
            assert len(calls) == 1
            assert (await client.get('/api/cashier/day?date=2026-09-20')).status_code == 200
            assert len(calls) == 1
            assert (await client.get('/api/cashier/day?date=2026-09-20&refresh=true')).status_code == 200
            assert len(calls) == 2

    asyncio.run(scenario())


def test_disconnected_request_stops_its_work():
    async def scenario():
        cancelled = asyncio.Event()

        async def load(day):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def disconnected():
            return True

        state = SimpleNamespace(iiko=SimpleNamespace(load=load))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            await load_iiko(state, 'load', date(2026, 9, 20),
                            request=SimpleNamespace(is_disconnected=disconnected))
        assert error.value.status_code == 499
        assert cancelled.is_set()

    asyncio.run(scenario())
