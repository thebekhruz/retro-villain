"""Счёт Шефа за неделю дошёл до порога — сообщение учредителю (ТЗ 16.10, п. 4.2).

Сумма недели та же, что в блоке «Счёт Шефа · iiko» кабинета учредителя: по
меню (DishSumInt), неделя с понедельника по воскресенье по Ташкенту, не раньше
начала учёта. Новая неделя — новый отсчёт.

Правило по умолчанию: одно сообщение, когда сумма за неделю впервые стала не
меньше порога (CHEF_WEEK_LIMIT, 30 000 000). Повтор — только если с прошлого
сообщения сумма выросла ещё на шаг (CHEF_WEEK_REPEAT_STEP, 10 000 000), и не
чаще раза в день; шаг 0 — без повторов.

Проверка идёт раз в 15 минут с 10:00 до 23:59. Каждое событие — строка в
`founder_chef_alerts`: неделя, порог, сумма на момент отправки, время, статус
и код ошибки. Строка-заявка не даёт двум процессам (выкат) отправить одно
событие дважды. Упавшая отправка повторяется на следующей проверке. Без чата
учредителя (TELEGRAM_FOUNDER_CHAT_ID) событие пишется со статусом
`no_recipient`: порог видно только в кабинете.
"""
import asyncio
import logging
from contextlib import closing
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from retro.accounting_period import accounting_range_start
from retro.db import as_database
from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, TZ

from . import overview

CHECK_FROM = time(10)
TICK_MINUTES = 15
CLAIM_MINUTES = 10
# Отправлено или записано без получателя — событие закрыто.
DELIVERED = 'sent'
NO_RECIPIENT = 'no_recipient'
OPEN = ('failed', 'sending')

logger = logging.getLogger(__name__)


# ── Журнал событий ───────────────────────────────────────────────────────────

class ChefAlertStore:
    """События недели по порядку (`seq`): первое — порог, дальше — повторы."""

    COLUMNS = ('week', 'seq', 'week_start', 'week_end', 'threshold', 'amount', 'bills', 'status',
               'error', 'attempts', 'claimed_at', 'sent_at', 'message_id')

    def __init__(self, database):
        self.db = as_database(database)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS founder_chef_alerts (
                week TEXT NOT NULL, seq INTEGER NOT NULL,
                week_start TEXT NOT NULL, week_end TEXT NOT NULL,
                threshold TEXT NOT NULL, amount TEXT NOT NULL, bills INTEGER NOT NULL,
                status TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL,
                claimed_at TEXT NOT NULL, sent_at TEXT, message_id TEXT,
                PRIMARY KEY (week, seq))''')

    def events(self, week: str) -> list[dict]:
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                f'SELECT {", ".join(self.COLUMNS)} FROM founder_chef_alerts WHERE week = ? ORDER BY seq',
                (week,)).fetchall()
        return [dict(zip(self.COLUMNS, row)) for row in rows]

    def claim(self, week: str, seq: int, summary: dict, now: datetime) -> bool:
        """Заявка на событие: новая строка или упавшая/брошенная попытка."""
        stamp = now.isoformat()
        expired = (now - timedelta(minutes=CLAIM_MINUTES)).isoformat()
        values = (summary['limit'], summary['total'], summary['bills'])
        with closing(self.db.connect()) as connection, connection:
            inserted = connection.execute(
                'INSERT OR IGNORE INTO founder_chef_alerts (week, seq, week_start, week_end, threshold, '
                "amount, bills, status, attempts, claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'sending', 1, ?)",
                (week, seq, summary['start'], summary['end'], *values, stamp)).rowcount
            if inserted:
                return True
            return connection.execute(
                "UPDATE founder_chef_alerts SET status = 'sending', claimed_at = ?, attempts = attempts + 1, "
                'threshold = ?, amount = ?, bills = ? WHERE week = ? AND seq = ? '
                "AND (status = 'failed' OR (status = 'sending' AND claimed_at < ?))",
                (stamp, *values, week, seq, expired)).rowcount == 1

    def finish(self, week: str, seq: int, status: str, now: datetime, message_id=None):
        with closing(self.db.connect()) as connection, connection:
            connection.execute(
                'UPDATE founder_chef_alerts SET status = ?, sent_at = ?, message_id = ? WHERE week = ? AND seq = ?',
                (status, now.isoformat(), None if message_id is None else str(message_id), week, seq))

    def fail(self, week: str, seq: int, code: str):
        with closing(self.db.connect()) as connection, connection:
            connection.execute(
                "UPDATE founder_chef_alerts SET status = 'failed', error = ? WHERE week = ? AND seq = ?",
                (code, week, seq))


# ── Правило ──────────────────────────────────────────────────────────────────

def local_day(stamp: str) -> date:
    return datetime.fromisoformat(stamp).astimezone(TZ).date()


def delivered(events, config) -> list[dict]:
    """Закрытые события. Пока получателя нет, им считается и запись без
    отправки; когда чат появился, первое сообщение недели всё же уходит."""
    done = (DELIVERED,) if config.recipient else (DELIVERED, NO_RECIPIENT)
    return [event for event in events if event['status'] in done]


def due_event(events, summary, config, today: date):
    """(seq, вид, прошлое событие) — что отправить сейчас; иначе None.

    Вид — `first` (порог за неделю достигнут) или `repeat` (сумма выросла
    ещё на шаг с прошлого сообщения, и сегодня сообщений ещё не было)."""
    total = Decimal(summary['total'])
    if total < config.week_limit:
        return None
    if events and events[-1]['status'] in OPEN:
        seq = events[-1]['seq']
    else:
        seq = (events[-1]['seq'] + 1) if events else 1
    closed = delivered(events, config)
    if not closed:
        return seq, 'first', None
    last = closed[-1]
    if not config.repeat_step or total < Decimal(last['amount']) + config.repeat_step:
        return None
    if local_day(last['sent_at']) >= today:
        return None
    return seq, 'repeat', last


def status_view(events, config) -> dict | None:
    """Последнее событие недели для кабинета. Запись «без получателя» после
    того, как чат задан, уже ничего не значит: первое сообщение впереди."""
    relevant = [event for event in events if not (config.recipient and event['status'] == NO_RECIPIENT)]
    if not relevant:
        return None
    event = relevant[-1]
    return dict(status=event['status'], at=event['sent_at'] or event['claimed_at'],
                amount=event['amount'], error=event['error'] if event['status'] == 'failed' else None,
                attempts=event['attempts'])


# ── Текст сообщения ──────────────────────────────────────────────────────────

def money(value) -> str:
    """«31 250 000» — целые сумы, как в кабинете."""
    return f'{int(Decimal(value)):,}'.replace(',', ' ')


def dm(iso: str) -> str:
    return f'{iso[8:10]}.{iso[5:7]}'


def message(summary: dict, *, kind: str, last: dict | None = None, link: str = '') -> str:
    title = ('Счёт Шефа за неделю дошёл до порога' if kind == 'first'
             else 'Счёт Шефа за неделю продолжает расти')
    lines = [title,
             f'Неделя {dm(summary["start"])}–{dm(summary["end"])}: {money(summary["total"])} сум '
             f'при пороге {money(summary["limit"])}.']
    if last is not None:
        grown = Decimal(summary['total']) - Decimal(last['amount'])
        when = datetime.fromisoformat(last['sent_at']).astimezone(TZ).strftime('%d.%m')
        lines.append(f'С сообщения {when}: +{money(grown)}.')
    lines.append(f'Счетов: {summary["bills"]}.')
    if summary['top']:
        lines.append('Самые крупные:')
        for bill in summary['top']:
            parts = [dm(bill['day'])]
            if bill.get('table') not in (None, ''):
                parts.append(f'стол {bill["table"]}')
            parts.append(', '.join(bill['waiters']) or 'без официанта')
            lines.append(f'• {", ".join(parts)}: {money(bill["amount"])}')
    if link:
        lines.append(f'Кабинет: {link}/founder')
    return '\n'.join(lines)


# ── Проверка по расписанию ───────────────────────────────────────────────────

class ChefWeekAlert:
    def __init__(self, store, config, *, loader, bot=None, clock=lambda: datetime.now(TZ)):
        self.store, self.config, self.loader, self.bot, self.clock = store, config, loader, bot, clock
        self.worker = None

    async def week_summary(self, day: date) -> tuple[str, dict]:
        monday, sunday, week = overview.week_of(day)
        start = accounting_range_start(monday, day)
        rows = await self.loader(start, day)
        return week, overview.chef_week(rows, start=start, end=sunday, limit=self.config.week_limit)

    async def run_once(self) -> dict | None:
        """Одна проверка. Возвращает событие, если что-то записано, иначе None."""
        now = self.clock().astimezone(TZ)
        if now.time() < CHECK_FROM:
            return None
        try:
            week, summary = await self.week_summary(now.date())
        except (DataError, TimeoutError) as error:
            log_safe_failure('chef-alert', error, operation='load')
            return None
        events = await asyncio.to_thread(self.store.events, week)
        due = due_event(events, summary, self.config, now.date())
        if due is None:
            return None
        seq, kind, last = due
        if not await asyncio.to_thread(self.store.claim, week, seq, summary, now):
            return None
        event = dict(week=week, seq=seq, kind=kind, total=summary['total'])
        if not self.config.recipient or self.bot is None:
            await asyncio.to_thread(self.store.finish, week, seq, NO_RECIPIENT, now)
            logger.info('chef week alert %s #%s: порог достигнут, получатель не настроен', week, seq)
            return {**event, 'status': NO_RECIPIENT}
        text = message(summary, kind=kind, last=last, link=self.config.public_url)
        try:
            message_id = await self.bot.send_message(self.config.chat_id, text)
        except Exception as error:
            code = getattr(error, 'code', type(error).__name__)
            await asyncio.to_thread(self.store.fail, week, seq, code)
            log_safe_failure('chef-alert', error, operation=f'send:{week}:{seq}')
            logger.warning('chef week alert %s #%s failed: %s', week, seq, code)
            return {**event, 'status': 'failed', 'error': code}
        await asyncio.to_thread(self.store.finish, week, seq, DELIVERED, self.clock().astimezone(TZ),
                                message_id)
        logger.info('chef week alert %s #%s sent', week, seq)
        return {**event, 'status': DELIVERED, 'text': text}

    def start(self, every: float = TICK_MINUTES * 60):
        async def work():
            await asyncio.sleep(60)  # не мешаем старту и первым запросам после выката
            while True:
                try:
                    await self.run_once()
                except Exception as error:
                    log_safe_failure('chef-alert', error, operation='schedule')
                await asyncio.sleep(every)
        self.worker = asyncio.create_task(work())

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        if self.bot is not None:
            await self.bot.close()
