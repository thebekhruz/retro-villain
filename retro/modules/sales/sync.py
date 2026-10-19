"""Фоновая синхронизация истории продаж: сегодня, правки недели и дозаливка назад.

Один прогон — один день, и один отчёт iiko за раз (`slot`): история не
отнимает у кассира и аналитики больше одного слота из четырёх. Порядок дел на
каждом шаге:

1. сегодня — если данным больше `today_every` (десять минут);
2. дни, о которых только что спросили и которых в базе нет (`want`);
3. последние семь дней — один раз после 06:00 следующего утра: смена закрыта,
   правки задним числом за неделю подтягиваются каждое утро;
4. дозаливка истории назад, от вчерашнего дня к `SALES_HISTORY_START`.

Неудачный день откладывается на `retry` и пишется в журнал прогонов с текстом
ошибки; сохранённая прежняя версия дня при этом не трогается. Расписание и
защита от двойного запуска живут в базе (аренда дня), а не в памяти процесса.

Удачная загрузка сегодняшнего дня заодно пишет почасовой контрольный снимок
(`sales_hourly_snapshots`, T-507): первый прогон часа по Ташкенту фиксирует итог
нашей базы и контрольного отчёта iiko на этот момент. Снимок — дополнительный
след для контроля и восстановления, источник истории — по-прежнему строки дня.
"""
import asyncio
import logging
from datetime import datetime, time, timedelta
from decimal import Decimal
from time import monotonic
from uuid import uuid4

from retro.logging_config import log_safe_failure
from retro.modules.cashier.archive import archive_boundary
from retro.modules.cashier.service import DataError, TZ

from .service import prepare_day

TODAY_EVERY = timedelta(minutes=10)
RETRY = timedelta(minutes=5)
RECENT_DAYS = 7
# Шаг планировщика: за шаг — не больше одного дня.
TICK = 60
# После выката панель сначала отвечает людям; кассир стартует через минуту,
# меню через две — история продаж идёт третьей.
FIRST_TICK = 150
# Три последовательных отчёта iiko, каждый со своим бюджетом ожидания.
TIMEOUT = 150
WANTED_LIMIT = 31


class SalesSync:
    def __init__(self, store, source, *, history_start, clock=lambda: datetime.now(TZ),
                 today_every=TODAY_EVERY, retry=RETRY, tick=TICK, first_tick=FIRST_TICK, timeout=TIMEOUT):
        self.store, self.source, self.clock = store, source, clock
        self.history_start = history_start
        self.today_every, self.retry = today_every, retry
        self.tick, self.first_tick, self.timeout = tick, first_tick, timeout
        self.jobs, self.failures, self.wanted = {}, {}, []
        self.slot = asyncio.Semaphore(1)
        self.worker = None

    # ── Один день ──────────────────────────────────────────────────────────

    def start_day(self, day, *, reason):
        """Загрузка дня одной задачей: повторный вызов получает ту же задачу."""
        task = self.jobs.get(day)
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self._run(day, reason))
        # Ответ странице может уйти раньше конца загрузки: ошибку забираем,
        # чтобы она не терялась как «never retrieved», ждущих она всё равно найдёт.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self.jobs[day] = task
        return task

    async def _run(self, day, reason):
        started = monotonic()
        owner = uuid4().hex
        try:
            async with self.slot:
                if not await asyncio.to_thread(self.store.acquire, day, owner, self.clock()):
                    # День грузит соседний процесс — его результат и прочитаем.
                    return dict(day=day.isoformat(), status='skipped')
                try:
                    return await self._load(day, reason, started)
                finally:
                    await asyncio.to_thread(self.store.release, day, owner)
        finally:
            self.jobs.pop(day, None)

    async def _load(self, day, reason, started):
        run_id = await asyncio.to_thread(self.store.start_run, day, reason, self.clock())
        try:
            async with asyncio.timeout(self.timeout):
                data = await self.source.load_day(day)
            if data.get('day') != day:
                raise DataError('iiko вернул продажи не за тот день.')
            fetched_at = self.clock()
            prepared = prepare_day(data)
            saved = await asyncio.to_thread(
                self.store.save_day, day, prepared['lines'], prepared['orders'], fetched_at=fetched_at,
                revision=data['revision'], closed=fetched_at >= archive_boundary(day),
                revenue=prepared['revenue'], iiko_revenue=prepared['iiko_revenue'],
                status=prepared['status'], reconciliation=prepared['reconciliation'])
        except Exception as error:
            message = str(error) if isinstance(error, DataError) else 'Не удалось загрузить продажи из iiko.'
            self.failures[day] = self.clock() + self.retry
            await asyncio.to_thread(self.store.finish_run, run_id, 'failed', at=self.clock(), error=message)
            log_safe_failure('sales-sync', error, operation='load_day')
            raise DataError(message) from None
        self.failures.pop(day, None)
        if saved['saved']:
            await self._snapshot(day, fetched_at, data['revision'], prepared)
        status = 'ok' if saved['changed'] else 'unchanged' if saved['saved'] else 'skipped'
        await asyncio.to_thread(
            self.store.finish_run, run_id, status, at=self.clock(), lines=len(prepared['lines']),
            orders=len(prepared['orders']), revenue=prepared['revenue'],
            previous_revenue=saved.get('previous_revenue'), revision=data['revision'])
        logging.getLogger('retro.performance').info(
            'operation=sales_sync day=%s status=%s lines=%d reconciliation=%s duration_ms=%d',
            day, status, len(prepared['lines']), prepared['status'], (monotonic() - started) * 1000)
        return dict(day=day.isoformat(), status=status, lines=len(prepared['lines']),
                    orders=len(prepared['orders']), revenue=str(prepared['revenue']),
                    reconciliation=prepared['status'], differences=prepared['reconciliation']['differences'])

    async def _snapshot(self, day, fetched_at, revision, prepared):
        """Почасовой снимок — только за живой день: (день, час) — это «на какой час
        этого дня». Дозаливка и утреннее перечитывание прошлых дней снимков не
        пишут, иначе час загрузки выдавался бы за час самого дня. Ошибка снимка
        загрузку не роняет: день уже сохранён."""
        local = fetched_at.astimezone(TZ)
        if local.date() != day:
            return None
        lines, orders = prepared['lines'], prepared['orders']
        reconciliation = prepared['reconciliation']
        iiko = reconciliation.get('iiko', {})
        directions = {name: dict(ours=reconciliation.get('lines', {}).get(name), iiko=iiko.get(name))
                      for name in sorted(set(reconciliation.get('lines', {})) | set(iiko))}
        try:
            return await asyncio.to_thread(
                self.store.save_snapshot, day, local.hour, fetched_at=fetched_at, revision=revision,
                lines=len(lines), orders=len(orders),
                receipts=sum(1 for item in orders if item['revenue'] > 0),
                quantity=sum((line['quantity'] for line in lines), Decimal(0)),
                gross=sum((line['gross'] for line in lines), Decimal(0)),
                revenue=prepared['revenue'], iiko_revenue=prepared['iiko_revenue'],
                iiko_orders=sum(int(item['orders']) for item in iiko.values()),
                iiko_quantity=sum((Decimal(item['quantity']) for item in iiko.values()), Decimal(0)),
                last_closed_at=max((line['closed_at'] for line in lines if line['closed_at']), default=None),
                status=prepared['status'], by_direction=directions,
                differences=reconciliation.get('differences', []))
        except Exception as error:
            log_safe_failure('sales-sync', error, operation='hourly_snapshot')
            return None

    async def ensure_fresh(self, day, *, max_age, wait):
        """Сегодняшний день не старше `max_age` секунд; иначе — текст, почему нет."""
        state = await asyncio.to_thread(self.store.state, day)
        now = self.clock()
        if state is not None and (now - state['fetched_at']).total_seconds() <= max_age:
            return None
        if self.failures.get(day, now) > now and state is not None:
            return 'iiko сейчас не отвечает: показаны данные последней загрузки.'
        task = self.start_day(day, reason='request')
        try:
            # shield: ушедший браузер не отменяет загрузку, нужную всем.
            await asyncio.wait_for(asyncio.shield(task), wait)
        except TimeoutError:
            return 'iiko отвечает медленно: показаны данные последней загрузки, обновление идёт.'
        except DataError as error:
            return f'{error} Показаны данные последней загрузки.'
        return None

    def want(self, days):
        """Дни, о которых спросили, а в базе их нет: фон возьмёт их первыми."""
        for day in days:
            if day not in self.wanted and day >= self.history_start:
                self.wanted.append(day)
        del self.wanted[WANTED_LIMIT:]

    # ── Расписание ─────────────────────────────────────────────────────────

    def due(self, now, states=None):
        """Следующий день к загрузке или None. `states` — дни из базы (читаются вне цикла событий)."""
        today = now.astimezone(TZ).date()
        if today < self.history_start:
            return None
        if states is None:
            states = self.store.states(self.history_start, today)
        blocked = {day for day, until in self.failures.items() if until > now}

        def allowed(day):
            return day not in blocked and day not in self.jobs

        current = states.get(today)
        if allowed(today) and (current is None or now - current['fetched_at'] >= self.today_every):
            return today
        while self.wanted:
            day = self.wanted[0]
            if day in states or day > today:
                self.wanted.pop(0)
                continue
            if allowed(day):
                return day
            break
        morning = datetime.combine(today, time(6), TZ)
        for offset in range(1, RECENT_DAYS + 1):
            day = today - timedelta(days=offset)
            if day < self.history_start or not allowed(day):
                continue
            state = states.get(day)
            if state is None:
                return day
            # Вчерашний день до 06:00 ещё не закрыт: смену могли не закрыть.
            required = max(archive_boundary(day), morning) if now >= morning else archive_boundary(day)
            if now >= archive_boundary(day) and state['fetched_at'] < required:
                return day
        day = today - timedelta(days=RECENT_DAYS + 1)
        while day >= self.history_start:
            state = states.get(day)
            if allowed(day) and (state is None or not state['closed']):
                return day
            day -= timedelta(days=1)
        return None

    async def run_once(self):
        """Один шаг планировщика: не больше одного дня."""
        if self.jobs:
            return None  # Запрос страницы уже грузит день — фон подождёт.
        now = self.clock()
        today = now.astimezone(TZ).date()
        states = await asyncio.to_thread(self.store.states, self.history_start, today)
        # Решение — в цикле событий: очередь `wanted` и задачи живут здесь же.
        day = self.due(now, states)
        if day is None:
            return None
        return await self.start_day(day, reason='schedule')

    def start(self):
        async def work():
            await asyncio.sleep(self.first_tick)
            while True:
                try:
                    await self.run_once()
                except Exception as error:
                    # Ошибка уже в журнале прогонов, день отложен — цикл живёт.
                    log_safe_failure('sales-sync', error, operation='schedule')
                await asyncio.sleep(self.tick)
        self.worker = asyncio.create_task(work())

    async def close(self):
        tasks = [*self.jobs.values()] + ([self.worker] if self.worker else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.worker = None
