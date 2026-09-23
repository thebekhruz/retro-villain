"""Offline performance/coordination probes; synthetic data, no external requests.

Run from the repository root: PYTHONPATH=. python scripts/performance_probe.py
All databases are temporary and removed on exit. Timings are diagnostic, not SLOs.
"""
import asyncio
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace

import httpx
from fastapi import HTTPException

from retro.config import Settings, HikvisionConfig
from retro.integrations.iiko import IikoClient
from retro.integrations.hikvision_poller import HikvisionPoller
from retro.modules.accountant.hikvision import AttendanceStore
from retro.modules.accountant.ledger import FinanceStore
from retro.modules.accountant.roster import RosterStore
from retro.modules.cashier.routes import day_report
from retro.modules.cashier.service import SnapshotCache, demo_snapshot, TZ
from retro.report_cache import ReportCache


async def olap_counts():
    counts = Counter()

    async def upstream(request):
        path = request.url.path
        if path == '/api/auth/login':
            counts['auth'] += 1
            return httpx.Response(200, json={'token': 'synthetic'})
        if path == '/api/olap/init':
            counts['olap_init'] += 1
            return httpx.Response(200, json={'fetchId': str(counts['olap_init'])})
        counts['olap_fetch'] += 1
        body = json.loads(request.content)
        groups = body['groupFields']
        rows = []
        if 'UniqOrderId.Id' in groups:
            values = {'CashRegisterName': 'Kassa-FiscalBox1', 'RestaurantSection': 'Ресторан',
                      'PayTypes': 'Демо', 'DishName': 'Synthetic', 'DishGroup': 'Synthetic',
                      'WaiterName': 'Synthetic', 'UniqOrderId.Id': 'synthetic-order',
                      'OpenDate.Typed': '2026-09-13', 'NonCashPaymentType': ''}
            row = {f'field{i}': {'value': values[name]} for i, name in enumerate(groups)}
            row.update({f'field{len(groups)+i}': {'value': value}
                        for i, value in enumerate((1, 100, 20))})
            rows = [{**row, 'field0': {'value': date(2026, 9, day).isoformat()}}
                    for day in range(13, 23)]
        return httpx.Response(200, json={'result': {'rows': rows}})

    source = IikoClient(Settings(login='synthetic', password='synthetic', store_id=1),
                        transport=httpx.MockTransport(upstream))
    await source.load_director_report(date(2026, 9, 23))
    first = dict(counts)
    await source.load_director_report(date(2026, 9, 23))
    await source.close()
    return {'first_load': first, 'two_identical_loads': dict(counts)}


async def queue_timeout():
    """A 50 ms deadline must expire while all report slots are occupied."""
    cache = ReportCache(concurrency=1)
    started = asyncio.Event()

    async def occupied():
        started.set()
        await asyncio.sleep(.2)

    first = asyncio.create_task(cache.get('occupied', occupied))
    await started.wait()
    start = time.perf_counter()
    try:
        await cache.get('queued', lambda: asyncio.sleep(.01), timeout=.05)
        result = 'completed'
    except TimeoutError:
        result = 'deadline'
    elapsed = (time.perf_counter() - start) * 1000
    await first
    await cache.close()
    return {'scaled_timeout_ms': 50, 'queue_hold_ms': 200,
            'elapsed_ms': round(elapsed, 1), 'result': result}


async def cross_user_replacement():
    started = asyncio.Event()

    class Source:
        async def load(self, day):
            started.set()
            await asyncio.sleep(.1)
            return replace(demo_snapshot(day), demo=False)

    state = SimpleNamespace(iiko=Source(), reports=ReportCache(), cache=SnapshotCache(),
                            expenses=SimpleNamespace(policy_configured=lambda: False))

    async def call(owner, day):
        request = SimpleNamespace(app=SimpleNamespace(state=state),
                                  state=SimpleNamespace(request_id=owner, dashboard_user=owner))
        try:
            await day_report(request, day, False)
            return 200
        except HTTPException as error:
            return error.status_code

    first = asyncio.create_task(call('synthetic-user-A', date(2026, 9, 21)))
    await started.wait()
    second = asyncio.create_task(call('synthetic-user-B', date(2026, 9, 22)))
    results = await asyncio.gather(first, second)
    return dict(zip(('user_A_status', 'user_B_status'), results))


async def poller_event_loop_block(root):
    path = root / 'poller.sqlite3'
    store, roster = AttendanceStore(path), RosterStore(path)
    now = datetime(2026, 9, 23, 10, tzinfo=TZ)
    store.record_attempt('offline', now)
    roster.list()

    class Client:
        async def fetch_events(self, *args):
            return []

    poller = HikvisionPoller(HikvisionConfig('http://localhost', 'synthetic', 'synthetic', source='offline'),
                            Client(), roster, store, now=lambda: now)
    locked = threading.Event()

    def writer():
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('BEGIN IMMEDIATE')
            locked.set()
            time.sleep(.4)
            connection.rollback()

    ticks, done = [], False

    async def heartbeat():
        while not done:
            ticks.append(time.perf_counter())
            await asyncio.sleep(.01)

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(.03)
    thread = threading.Thread(target=writer)
    thread.start()
    await asyncio.to_thread(locked.wait)
    start = time.perf_counter()
    result = await poller.run_once()
    elapsed = (time.perf_counter() - start) * 1000
    await asyncio.sleep(.03)
    done = True
    await beat
    thread.join()
    return {'poll_success': result.success, 'writer_hold_ms': 400,
            'poll_ms': round(elapsed, 1),
            'max_event_loop_gap_ms': round(max(b-a for a, b in zip(ticks, ticks[1:])) * 1000, 1)}


def ledger_scaling(root):
    measurements = []
    for count in (1000, 5000, 10000):
        store = FinanceStore(root / f'ledger-{count}.sqlite3')
        with closing(store._open()) as connection, connection:
            start = date(2026, 1, 1)
            connection.executemany(
                'INSERT INTO accountant_accruals '
                '(work_day,employee_id,employee_name,group_name,attendance_status,rate,amount) '
                'VALUES (?,?,?,?,?,?,?)',
                (((start + timedelta(days=i//100)).isoformat(), i % 100,
                  'Synthetic', 'Synthetic', 'on_time', '100', '100') for i in range(count)))
            connection.executemany(
                'INSERT INTO accountant_salary_payments (accrual_id,paid_day,amount,created_at) '
                'VALUES (?,?,?,?)', ((i+1, '2026-09-23', '100', 'synthetic') for i in range(count)))
            plan = [row[3] for row in connection.execute(
                'EXPLAIN QUERY PLAN SELECT amount FROM accountant_salary_payments '
                'WHERE accrual_id = ? AND paid_day <= ?', (1, '2026-09-23'))]
        queries = 0
        old_open = store._open

        def traced():
            nonlocal queries
            connection = old_open()

            def trace(statement):
                nonlocal queries
                if statement.startswith('SELECT'):
                    queries += 1

            connection.set_trace_callback(trace)
            return connection

        store._open = traced
        begin = time.perf_counter()
        rows = store.accruals(date(2026, 9, 23))
        seconds = time.perf_counter() - begin
        assert len(rows) == count and all(row['debt'] == '0' for row in rows)
        measurements.append({'accruals': count, 'salary_payments': count,
                             'selects': queries, 'seconds': round(seconds, 3), 'plan': plan})
    return measurements


async def main():
    output = {}
    output['director_olap_counts'] = await olap_counts()
    output['queue_timeout'] = await queue_timeout()
    output['cross_user_replacement'] = await cross_user_replacement()
    build = Path(__file__).resolve().parents[1] / 'build'
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='performance-probe-', dir=build) as temporary:
        root = Path(temporary)
        output['poller_event_loop'] = await poller_event_loop_block(root)
        output['ledger_scaling'] = ledger_scaling(root)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
