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

    async def get(self, key, operation, *, ttl=60, timeout=90, refresh=False, label="report"):
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._slots = loop, asyncio.Semaphore(self.concurrency)
            self.pending = {}
        now = self.clock()
        for expired in [name for name, (until, _, _) in self.entries.items() if until <= now]:
            self.entries.pop(expired)
        if refresh:
            self.entries.pop(key, None)
        elif key in self.entries:
            self.entries.move_to_end(key)
            logging.getLogger('retro.performance').info('operation=%s cache=hit', label)
            return self.entries[key][1]
        flight = self.pending.get(key)
        if flight is None:
            if len(self.pending) >= self.max_pending:
                raise TimeoutError("Report queue is full")

            async def produce():
                started = self.clock()
                try:
                    async with asyncio.timeout(timeout):
                        async with self._slots:
                            acquired = self.clock()
                            value = await operation()
                            weight = (await asyncio.to_thread(self.weigh, value)
                                      if self.max_weight is not None else self.weigh(value))
                            if self.max_weight is None or weight <= self.max_weight:
                                self.entries[key] = (self.clock() + ttl, value, weight)
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

            flight = Flight(asyncio.create_task(produce()))
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

    async def close(self):
        tasks = [flight.task for flight in self.pending.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.entries.clear()


async def load_iiko(state, method, *args, refresh=False, request=None, timeout=90, **kwargs):
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

    ttl = 300 if method == 'load_director_report' else 30 if method == 'load' else 60
    reader = asyncio.create_task(cache.get(key, operation, ttl=ttl, timeout=timeout, refresh=refresh, label=method))
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
