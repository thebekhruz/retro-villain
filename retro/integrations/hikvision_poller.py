"""Recoverable background polling for one read-only Hikvision entrance."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from retro.config import HikvisionConfig
from retro.integrations.hikvision import HikvisionClient, HikvisionError
from retro.modules.accountant.hikvision import AttendanceStore
from retro.modules.accountant.roster import RosterStore


TZ = ZoneInfo('Asia/Tashkent')
OVERLAP = timedelta(minutes=5)
PEOPLE_SYNC_INTERVAL = timedelta(hours=6)


@dataclass(frozen=True)
class PollResult:
    success: bool
    events: int = 0
    linked: int = 0
    error: str | None = None


class HikvisionPoller:
    def __init__(self, config: HikvisionConfig, client: HikvisionClient,
                 roster: RosterStore, store: AttendanceStore, *,
                 now: Callable[[], datetime] | None = None):
        self.config = config
        self.client = client
        self.roster = roster
        self.store = store
        self._now = now or (lambda: datetime.now(TZ))
        self._task: asyncio.Task | None = None
        self._last_people_sync: datetime | None = None
        self._failures = 0

    def _current_time(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError('Poller time must include a timezone.')
        return value.astimezone(TZ)

    async def run_once(self) -> PollResult:
        now = self._current_time()
        self.store.record_attempt(self.config.source, now)
        try:
            employees = self.roster.list()
            existing_links = {employee.hikvision_id for employee in employees
                              if employee.hikvision_id is not None}
            should_sync_people = any(employee.hikvision_id is None for employee in employees) and (
                self._last_people_sync is None
                or now - self._last_people_sync >= PEOPLE_SYNC_INTERVAL)
            linked = 0
            if should_sync_people:
                people = await self.client.fetch_people()
                linked = self.roster.link_hikvision_people(people)['linked']
                self._last_people_sync = now
                employees = self.roster.list()

            employee_ids = {employee.hikvision_id: employee.id for employee in employees
                            if employee.hikvision_id is not None}
            if linked:
                self.store.reconcile_links({employee_no: employee_id
                                            for employee_no, employee_id in employee_ids.items()
                                            if employee_no not in existing_links})

            state = self.store.sync_state(self.config.source)
            if state.cursor_at is None:
                start = datetime.combine(now.date() - timedelta(days=1), time.min, TZ)
            else:
                start = state.cursor_at.astimezone(TZ) - OVERLAP
            events = await self.client.fetch_events(start, now)
            for event in events:
                self.store.ingest(event, employee_ids.get(event.employee_no), received_at=now)
            self.store.record_success(
                self.config.source, at=now, cursor_at=now,
                covered_from=start, covered_through=now)
            self._failures = 0
            return PollResult(True, events=len(events), linked=linked)
        except HikvisionError as error:
            self._failures += 1
            self.store.record_failure(self.config.source, at=now, code=error.code)
            return PollResult(False, error=error.code)
        except Exception as error:
            self._failures += 1
            self.store.record_failure(self.config.source, at=now, code='internal')
            logging.getLogger('retro.hikvision').warning(
                'component=hikvision-poller operation=run_once error_class=%s',
                error.__class__.__name__)
            return PollResult(False, error='internal')

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop(), name='hikvision-poller')

    async def _run_loop(self):
        while True:
            result = await self.run_once()
            delay = self.config.poll_seconds
            if not result.success:
                delay = min(self.config.poll_seconds, 2 ** min(self._failures, 6))
            await asyncio.sleep(delay)

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.client.close()
