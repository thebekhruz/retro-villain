"""One daily source for cashier, accountant and founder; UI may show labelled stale data."""
import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4
from time import monotonic

from retro.logging_config import log_safe_failure
from retro.report_cache import refresh_source
from .archive import archive_boundary
from .service import DataError, TZ

REFRESH_ERROR = 'Не удалось обновить iiko. Показаны последние сохранённые данные.'


class CashierDays:
    def __init__(self, archive, loader, *, clock=lambda: datetime.now(TZ), timeout=60):
        self.archive, self.loader, self.clock, self.timeout = archive, loader, clock, timeout
        self.jobs, self.failures = {}, {}
        self.live_slots = asyncio.Semaphore(1)
        self.history_slots = asyncio.Semaphore(1)
        self.worker = None

    def stale(self, snapshot, now):
        if snapshot.day < now.date():
            return snapshot.fetched_at < archive_boundary(snapshot.day)
        return (now - snapshot.fetched_at).total_seconds() > 30

    def start_refresh(self, day, *, force=False, previous_id=None):
        task = self.jobs.get(day)
        if task is not None and not task.done():
            return task
        now = self.clock()
        self.failures = {key: until for key, until in self.failures.items() if until > now}
        if not force and day in self.failures:
            return None
        if len(self.jobs) >= (8 if day >= now.date() else 7):
            return None

        async def run():
            started = monotonic()
            try:
                async with asyncio.timeout(self.timeout):
                    slots = self.live_slots if day >= self.clock().date() else self.history_slots
                    async with slots:
                        acquired = monotonic()
                        result = await self.synchronize(day, previous_id)
                        logging.getLogger('retro.performance').info(
                            'operation=cashier_sync day=%s source=%s queue_ms=%d duration_ms=%d',
                            day, result.source, (acquired-started)*1000, (monotonic()-started)*1000)
                        return result
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.failures[day] = self.clock() + timedelta(seconds=60)
                log_safe_failure('cashier-archive', error, operation='synchronize')
                raise DataError('Не удалось обновить данные iiko. Повторите позже.') from None
            finally:
                self.jobs.pop(day, None)

        task = asyncio.create_task(run())
        # A UI read can return before synchronization completes; retrieve errors
        # even when no HTTP reader is left, without swallowing errors for waiters.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self.jobs[day] = task
        return task

    async def synchronize(self, day, previous_id):
        owner = uuid4().hex
        while not await asyncio.to_thread(self.archive.acquire, day, owner, self.clock()):
            await asyncio.sleep(.25)
            saved = await asyncio.to_thread(self.archive.get, day)
            if saved is not None and saved.id != previous_id and not self.stale(saved, self.clock()):
                return saved
        try:
            # Another process may have committed between our initial DB read
            # and acquiring the lease. Do not rebuild its just-finished result.
            saved = await asyncio.to_thread(self.archive.get, day)
            if saved is not None and saved.id != previous_id and not self.stale(saved, self.clock()):
                return saved
            token = refresh_source.set(True)
            try:
                snapshot = await self.loader(day)
            finally:
                refresh_source.reset(token)
            if snapshot.day != day or snapshot.demo:
                raise DataError('iiko вернул неподходящий снимок дня.')
            await asyncio.to_thread(self.archive.save, snapshot)
            self.failures.pop(day, None)
            return snapshot
        finally:
            await asyncio.to_thread(self.archive.release, day, owner)

    async def get(self, day, *, refresh=False, allow_stale=False):
        now = self.clock()
        if day > now.date():
            raise DataError('Выберите сегодняшний или прошедший день.')
        saved = await asyncio.to_thread(self.archive.get, day)
        stale = saved is not None and self.stale(saved, now)
        if saved is not None and not stale and not refresh:
            logging.getLogger('retro.performance').info('operation=cashier_day day=%s source=database', day)
            return replace(saved, refreshing=day in self.jobs,
                           refresh_error=REFRESH_ERROR if self.failures.get(day, now) > now else None)
        task = self.start_refresh(day, force=refresh, previous_id=saved.id if saved else None)
        if saved is not None and allow_stale:
            return replace(saved, stale=stale, refreshing=task is not None,
                           refresh_error=REFRESH_ERROR if task is None else None)
        if task is None:
            raise DataError('iiko временно недоступен. Повторите обновление через минуту.')
        # A cancelled browser must not discard a completed day's only archive job.
        return await asyncio.shield(task)

    async def archive_once(self):
        if self.jobs:
            return  # Interactive refreshes have priority over background history.
        days = await asyncio.to_thread(lambda: list(self.archive.due_days(self.clock())))
        for day in days:
            if self.failures.get(day, self.clock()) > self.clock():
                continue
            previous = await asyncio.to_thread(self.archive.get, day)
            task = self.start_refresh(day, previous_id=previous.id if previous else None)
            if task is not None:
                await task
            return

    def start(self):
        async def work():
            while True:
                # Stagger startup and process at most one day per minute. Never
                # launch a full historical backfill in a page request.
                await asyncio.sleep(60)
                try:
                    await self.archive_once()
                except Exception as error:
                    log_safe_failure('cashier-archive', error, operation='daily_archive')
        self.worker = asyncio.create_task(work())

    async def close(self):
        tasks = [*self.jobs.values()] + ([self.worker] if self.worker else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
