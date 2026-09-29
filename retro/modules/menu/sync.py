"""Недельная синхронизация меню: первый прогон сегодня, дальше раз в семь суток.

Расписание держит база (`menu_sync_state.next_run_at`), а не память процесса:
выкат не должен ни сбивать неделю, ни дёргать iiko заново. Неудача не уносит
срок на следующую неделю — она назначает короткую повторную попытку.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from time import monotonic

from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, TZ

INTERVAL = timedelta(days=7)
# Прогон не удался — пробуем через час, а не через неделю: иначе одна потеря
# связи с iiko оставила бы меню без обновления до следующего понедельника.
RETRY = timedelta(hours=1)
# Как часто смотреть на часы. Прогон раз в неделю, поэтому минута точности
# здесь роскошь; частый опрос базы не нужен никому.
TICK = 300
# Старт после выката не должен совпадать с наплывом первых запросов панели.
FIRST_TICK = 120
# Чтение всей номенклатуры — один запрос списка плюс три справочника.
TIMEOUT = 120


class MenuSync:
    def __init__(self, store, source, *, clock=lambda: datetime.now(TZ), interval=INTERVAL,
                 retry=RETRY, tick=TICK, first_tick=FIRST_TICK, timeout=TIMEOUT):
        self.store, self.source, self.clock = store, source, clock
        self.interval, self.retry = interval, retry
        self.tick, self.first_tick, self.timeout = tick, first_tick, timeout
        self.worker = None
        # Ручное обновление и прогон по расписанию не должны идти одновременно:
        # iiko от параллельных чтений не ускоряется, а деградирует.
        self.lock = asyncio.Lock()

    async def run_once(self, *, force=False):
        """Один прогон. `None` — время ещё не пришло, прогон не запускался."""
        async with self.lock:
            now = self.clock()
            if not await asyncio.to_thread(self.store.claim, now, self.interval, force=force):
                return None
            run_id = await asyncio.to_thread(self.store.start_run, now.isoformat())
            started = monotonic()
            try:
                async with asyncio.timeout(self.timeout):
                    data = await self.source.nomenclature()
                counts = await asyncio.to_thread(self.store.save, data['items'],
                                                 at=self.clock().isoformat())
            except Exception as error:
                message = str(error) if isinstance(error, DataError) else \
                    'Не удалось обновить меню из iiko. Следующая попытка через час.'
                await asyncio.to_thread(self.store.finish_run, run_id, 'failed',
                                        at=self.clock().isoformat(), error=message)
                await asyncio.to_thread(self.store.reschedule,
                                        (self.clock() + self.retry).isoformat())
                log_safe_failure('menu-sync', error, operation='nomenclature')
                raise DataError(message) from None
            await asyncio.to_thread(self.store.finish_run, run_id, 'ok',
                                    at=self.clock().isoformat(), counts=counts,
                                    note=data.get('note', ''))
            logging.getLogger('retro.performance').info(
                'operation=menu_sync items=%d added=%d changed=%d missing=%d duration_ms=%d',
                counts['items'], counts['added'], counts['changed'], counts['missing'],
                (monotonic() - started) * 1000)
            status = await asyncio.to_thread(self.store.status)
            return dict(counts, note=data.get('note', ''), **status)

    def start(self):
        async def work():
            await asyncio.sleep(self.first_tick)
            while True:
                try:
                    await self.run_once()
                except Exception as error:
                    # Срок уже сдвинут на повторную попытку, цикл живёт дальше.
                    log_safe_failure('menu-sync', error, operation='weekly')
                await asyncio.sleep(self.tick)
        self.worker = asyncio.create_task(work())

    async def close(self):
        if self.worker is None:
            return
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        self.worker = None
