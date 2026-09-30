"""Bounded single-flight reads: deadlines include queueing; cancellation is per reader."""
import asyncio
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from time import monotonic


refresh_source = ContextVar('refresh_source', default=False)


@dataclass
class Flight:
    task: asyncio.Task
    readers: int = 0


class ReportCache:
    def __init__(self, *, concurrency=4, limit=64, max_weight=None, weigh=None, max_pending=128, clock=monotonic):
        self.concurrency, self.limit, self.clock = concurrency, limit, clock
        self.max_weight, self.weigh = max_weight, weigh or (lambda value: 1)
        self.entries = OrderedDict()
        self.pending = {}
        self.max_pending = max_pending
        self._loop = None
        self._slots = None

    async def get(self, key, operation, *, ttl=60, timeout=90, refresh=False, label="report", stale=0):
        """`stale` — сколько секунд после ttl можно отдавать прежний ответ сразу,
        обновляя его в фоне. Только для данных, которые сами не меняются
        (закрытые дни iiko): пользователь не ждёт отчёт заново каждые 15 минут."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._slots = loop, asyncio.Semaphore(self.concurrency)
            self.pending = {}
        now = self.clock()
        for expired in [name for name, (until, _, _, keep) in self.entries.items() if keep <= now]:
            self.entries.pop(expired)
        if not refresh and key in self.entries:
            self.entries.move_to_end(key)
            until, value = self.entries[key][:2]
            if until > now:
                logging.getLogger('retro.performance').info('operation=%s cache=hit', label)
                return value
            if key not in self.pending and len(self.pending) < self.max_pending:
                # Фоновое обновление: читателей у него нет, поэтому уход
                # пользователя со страницы его не отменяет.
                task = asyncio.create_task(self._produce(key, operation, ttl, timeout, label, stale))
                task.add_done_callback(_background_done(label))
                self.pending[key] = Flight(task)
            logging.getLogger('retro.performance').info('operation=%s cache=stale', label)
            return value
        flight = self.pending.get(key)
        if flight is None:
            if len(self.pending) >= self.max_pending:
                raise TimeoutError("Report queue is full")

            flight = Flight(asyncio.create_task(self._produce(key, operation, ttl, timeout, label, stale)))
            self.pending[key] = flight
        flight.readers += 1
        try:
            # A reader's own deadline also covers joining an existing flight.
            async with asyncio.timeout(timeout):
                return await asyncio.shield(flight.task)
        finally:
            flight.readers -= 1
            if not flight.readers and not flight.task.done():
                if self.pending.get(key) is flight:
                    self.pending.pop(key)
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)

    async def _produce(self, key, operation, ttl, timeout, label, stale):
        started = self.clock()
        try:
            async with asyncio.timeout(timeout):
                # Слот освобождается сразу после чтения: измерение веса
                # сериализует весь отчёт и держало очередь на себе.
                async with self._slots:
                    acquired = self.clock()
                    value = await operation()
                weight = (await asyncio.to_thread(self.weigh, value)
                          if self.max_weight is not None else self.weigh(value))
                if self.max_weight is None or weight <= self.max_weight:
                    self.entries[key] = (self.clock() + ttl, value, weight, self.clock() + ttl + stale)
                    self.entries.move_to_end(key)
                    while len(self.entries) > self.limit or (
                            self.max_weight is not None and
                            sum(row[2] for row in self.entries.values()) > self.max_weight):
                        self.entries.popitem(last=False)
                logging.getLogger('retro.performance').info(
                    'operation=%s cache=miss queue_ms=%d duration_ms=%d',
                    label, (acquired-started)*1000, (self.clock()-started)*1000)
                return value
        finally:
            current = self.pending.get(key)
            if current is not None and current.task is asyncio.current_task():
                self.pending.pop(key, None)

    async def close(self):
        tasks = [flight.task for flight in self.pending.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.entries.clear()


def _background_done(label):
    def done(task):
        # Прежний ответ уже отдан; ошибка фонового обновления только в журнал,
        # следующий читатель попробует снова.
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger('retro.performance').warning(
                'operation=%s cache=stale refresh_failed=%s', label, type(task.exception()).__name__)
    return done


async def load_iiko(state, method, *args, refresh=False, request=None, timeout=90, ttl=None,
                    allow_stale=False, stale=0, **kwargs):
    cache = getattr(state, 'reports', None)
    if cache is None:
        state.reports = cache = ReportCache()
    key = (id(state.iiko), method, args, tuple(sorted(kwargs.items())))

    async def operation():
        token = refresh_source.set(refresh)
        try:
            return await getattr(state.iiko, method)(*args, **kwargs)
        finally:
            refresh_source.reset(token)

    if ttl is None:
        ttl = 300 if method == 'load_director_report' else 30 if method == 'load' else 60
    days = getattr(state, 'cashier_days', None)
    if method == 'load' and days is not None:
        async def daily_read():
            async with asyncio.timeout(timeout):
                return await days.get(*args, refresh=refresh, allow_stale=allow_stale)
        reader = asyncio.create_task(daily_read())
    else:
        reader = asyncio.create_task(cache.get(key, operation, ttl=ttl, timeout=timeout, refresh=refresh,
                                               label=method, stale=stale))
    try:
        if request is not None and hasattr(request, 'is_disconnected'):
            while not reader.done():
                await asyncio.wait({reader}, timeout=.2)
                if not reader.done() and await request.is_disconnected():
                    from fastapi import HTTPException
                    raise HTTPException(499, 'Запрос отменён клиентом.')
        return await reader
    finally:
        if not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
