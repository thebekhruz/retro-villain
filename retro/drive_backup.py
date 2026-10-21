"""Резервная копия отчётов в Google Drive (T-476).

Каждое утро за прошедший день кладём те же файлы, что скачивают из кабинетов:
«Касса» (отчёт кассира) и «Бухгалтер» («Финансы дня» с закупом Шоха). После
закрытия месяца — «Зарплата · месяц», «Зарплата · день» и «Сверка». Каждый
файл превращается в Google Таблицу в папке месяца: «Retro … / 2026-10 / …».

День выгружаем один раз, когда он окончательный: смену iiko закрывает за
полночь, и архив кассы считает день готовым только с 06:00 следующего утра.
Уже выгруженное не перезаписываем: резерв не должен повторять поздние поломки
в базе. Пропущенные дни (сбой Google, iiko, выкат) догоняются сами.
"""
import asyncio
import logging
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

from retro.accounting_period import ACCOUNTING_START
from retro.db import as_database
from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import TZ

READY_AT = time(6, 30)
CLAIM_MINUTES = 30
RETRY_MINUTES = 30
JOBS_PER_TICK = 4
DAILY = ('cashier', 'accountant')
MONTHLY = ('payroll', 'salary_day', 'reconciliation')
TITLES = dict(cashier='Касса', accountant='Бухгалтер', payroll='Зарплата · месяц',
              salary_day='Зарплата · день', reconciliation='Сверка')


class Skip(Exception):
    """Отчёт за этот период выгружать нечего (демо, нет iiko)."""


@dataclass(frozen=True, order=True)
class Job:
    period: str      # 2026-10-14 или 2026-10
    kind: str

    @property
    def month(self) -> str:
        return self.period[:7]

    @property
    def name(self) -> str:
        if len(self.period) == 7:
            from retro.modules.accountant.closing import month_name
            return f'{month_name(self.period)} · {TITLES[self.kind]}'
        return f'{date.fromisoformat(self.period).strftime("%d.%m.%Y")} · {TITLES[self.kind]}'


def last_final_day(now: datetime) -> date:
    local = now.astimezone(TZ)
    return local.date() - timedelta(days=1 if local.time() >= READY_AT else 2)


def due_jobs(start: date, now: datetime, kinds=DAILY) -> list[Job]:
    """Все периоды с `start` до последнего окончательного дня — уже по порядку."""
    last = last_final_day(now)
    jobs, day = [], start
    while day <= last:
        jobs += [Job(day.isoformat(), kind) for kind in kinds]
        following = day + timedelta(days=1)
        if following.month != day.month:
            jobs += [Job(day.strftime('%Y-%m'), kind) for kind in MONTHLY]
        day = following
    return jobs


class BackupStore:
    def __init__(self, database):
        self.db = as_database(database)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS drive_backup_files (
                kind TEXT NOT NULL, period TEXT NOT NULL, file_id TEXT NOT NULL,
                claimed_at TEXT NOT NULL, uploaded_at TEXT, PRIMARY KEY(kind, period))''')
            connection.execute('''CREATE TABLE IF NOT EXISTS drive_backup_folders (
                path TEXT PRIMARY KEY, folder_id TEXT NOT NULL)''')

    def done(self) -> set[Job]:
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                "SELECT period, kind FROM drive_backup_files WHERE file_id <> ''").fetchall()
        return {Job(period, kind) for period, kind in rows}

    def claim(self, job: Job, now: datetime) -> bool:
        """Один выгружающий на файл, даже если процессов два: строка-заявка."""
        stamp, expired = now.isoformat(), (now - timedelta(minutes=CLAIM_MINUTES)).isoformat()
        with closing(self.db.connect()) as connection, connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO drive_backup_files (kind, period, file_id, claimed_at) VALUES (?, ?, '', ?)",
                (job.kind, job.period, stamp)).rowcount
            if inserted:
                return True
            return connection.execute(
                "UPDATE drive_backup_files SET claimed_at = ? WHERE kind = ? AND period = ? "
                "AND file_id = '' AND claimed_at < ?", (stamp, job.kind, job.period, expired)).rowcount == 1

    def release(self, job: Job):
        with closing(self.db.connect()) as connection, connection:
            connection.execute("DELETE FROM drive_backup_files WHERE kind = ? AND period = ? AND file_id = ''",
                               (job.kind, job.period))

    def finish(self, job: Job, file_id: str, now: datetime):
        with closing(self.db.connect()) as connection, connection:
            connection.execute('UPDATE drive_backup_files SET file_id = ?, uploaded_at = ? WHERE kind = ? AND period = ?',
                               (file_id, now.isoformat(), job.kind, job.period))

    def folder(self, path: str) -> str | None:
        with closing(self.db.connect()) as connection:
            row = connection.execute('SELECT folder_id FROM drive_backup_folders WHERE path = ?', (path,)).fetchone()
        return row[0] if row else None

    def save_folder(self, path: str, folder_id: str):
        with closing(self.db.connect()) as connection, connection:
            connection.execute('INSERT OR IGNORE INTO drive_backup_folders (path, folder_id) VALUES (?, ?)',
                               (path, folder_id))


def background_request(app):
    """Обработчики кабинетов читают `request.app.state`: даём им то же без HTTP."""
    return SimpleNamespace(app=app, state=SimpleNamespace(
        request_id='drive-backup', dashboard_user='резервная копия', dashboard_role='all'))


async def build(app, job: Job) -> bytes:
    """Ровно тот файл, что дала бы кнопка «Скачать» в кабинете."""
    request = background_request(app)
    state = app.state
    if job.kind == 'cashier':
        from retro.report_cache import load_iiko
        from retro.modules.cashier.routes import CLOSED_DAY_TTL, export_bytes, with_prepayments
        from retro.modules.cashier.service import DataError
        if not state.settings.configured:
            raise Skip('iiko не настроен')
        day = date.fromisoformat(job.period)
        snapshot = await load_iiko(state, 'load', day, timeout=120, ttl=CLOSED_DAY_TTL)
        if snapshot.demo:
            raise Skip('демо-данные')
        try:
            entries = await load_iiko(state, 'load_prepayments', day, timeout=60, ttl=CLOSED_DAY_TTL)
            snapshot = with_prepayments(snapshot, entries)
        except (DataError, TimeoutError):
            pass  # без реестра предоплат файл честно пишет «требует проверки»
        return await asyncio.to_thread(export_bytes, state, day, snapshot)
    if job.kind == 'accountant':
        from retro.modules.accountant.routes import day_export_bytes
        return await day_export_bytes(request, date.fromisoformat(job.period))
    from retro.modules.accountant import routes
    if job.kind == 'payroll':
        from retro.modules.accountant.sheets_export import payroll_workbook
        return await asyncio.to_thread(lambda: payroll_workbook(routes.payroll_month(request, job.period)))
    if job.kind == 'salary_day':
        from retro.modules.accountant.salary_day_export import salary_day_workbook
        return await asyncio.to_thread(
            lambda: salary_day_workbook(routes.salary_day_month(request, job.period, 'payment')))
    if job.kind == 'reconciliation':
        from retro.modules.accountant.sheets_export import reconciliation_workbook
        return await asyncio.to_thread(
            lambda: reconciliation_workbook(routes.reconciliation(request, job.period)))
    raise ValueError(job.kind)


class DriveBackup:
    def __init__(self, app, drive, store, config, *, clock=lambda: datetime.now(TZ), builder=build):
        self.app, self.drive, self.store, self.config = app, drive, store, config
        self.clock, self.builder = clock, builder
        self.start_day = config.start or ACCOUNTING_START
        self.retry_after: dict[Job, datetime] = {}
        self.worker = None

    async def folder(self, month: str) -> str:
        root = await self._folder(self.config.folder_name, None)
        return await self._folder(month, root, path=f'{self.config.folder_name}/{month}')

    async def _folder(self, name, parent, *, path=None):
        path = path or name
        found = await asyncio.to_thread(self.store.folder, path)
        if found:
            return found
        created = await self.drive.create_folder(name, parent)
        await asyncio.to_thread(self.store.save_folder, path, created)
        # Если второй процесс успел раньше — берём его папку, наша останется пустой.
        return await asyncio.to_thread(self.store.folder, path) or created

    async def run_once(self) -> list[Job]:
        now = self.clock()
        done = await asyncio.to_thread(self.store.done)
        waiting = [job for job in due_jobs(self.start_day, now)
                   if job not in done and self.retry_after.get(job, now) <= now]
        uploaded = []
        for job in waiting[:JOBS_PER_TICK]:
            if not await asyncio.to_thread(self.store.claim, job, now):
                continue
            try:
                content = await self.builder(self.app, job)
                file_id = await self.drive.upload_sheet(job.name, await self.folder(job.month), content)
            except Skip as reason:
                # Выгружать нечего — помечаем, чтобы не спрашивать iiko каждые 10 минут.
                await asyncio.to_thread(self.store.finish, job, f'skip:{reason}', now)
                continue
            except Exception as error:
                await asyncio.to_thread(self.store.release, job)
                self.retry_after[job] = now + timedelta(minutes=RETRY_MINUTES)
                log_safe_failure('drive-backup', error, operation=f'{job.kind}:{job.period}')
                logging.getLogger(__name__).warning('drive backup %s %s failed: %s', job.kind, job.period, error)
                continue
            await asyncio.to_thread(self.store.finish, job, file_id, now)
            self.retry_after.pop(job, None)
            uploaded.append(job)
            logging.getLogger(__name__).info('drive backup %s %s uploaded', job.kind, job.period)
        return uploaded

    def start(self, every: float = 600):
        async def work():
            await asyncio.sleep(90)  # не мешаем старту и первым запросам после выката
            while True:
                try:
                    await self.run_once()
                except Exception as error:
                    log_safe_failure('drive-backup', error, operation='schedule')
                await asyncio.sleep(every)
        self.worker = asyncio.create_task(work())

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.drive.close()
