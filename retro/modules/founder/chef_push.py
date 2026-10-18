"""Уведомление в браузер на каждый заказ за Счёт Шефа (ТЗ 16.10, п. 4.2).

Раз в две минуты читаем из iiko заказы, закрытые типом оплаты «Счет Шефа»,
за вчера и сегодня (заказ, открытый до полуночи, закрывают после неё). Новый
заказ — уведомление на все устройства, где учредитель или администратор
включил уведомления. iiko отдаёт заказ в отчёт, когда его закрыли оплатой,
поэтому уведомление приходит после закрытия счёта, а не при пробитии блюда.

Журнал `founder_chef_order_pushes` — по строке на заказ: что и кому ушло.
Строка-заявка не даёт двум процессам (выкат) отправить заказ дважды. Самый
первый запуск, когда журнал пуст, только запоминает уже закрытые заказы, не
рассылая их: иначе после выката пришла бы пачка за весь день. Заказы, пока
устройств нет, тоже записываются — включённые позже уведомления старое не шлют.
"""
import asyncio
import logging
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal

from retro.db import as_database
from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, TZ
from retro.web_push import broadcast

TICK_SECONDS = 120
SHOWN_DISHES = 4
BASELINE, SENT, NO_RECIPIENT, FAILED, SENDING = 'baseline', 'sent', 'no_recipient', 'failed', 'sending'

logger = logging.getLogger(__name__)


class ChefOrderStore:
    COLUMNS = ('order_id', 'day', 'amount', 'status', 'devices', 'error', 'created_at', 'sent_at')

    def __init__(self, database):
        self.db = as_database(database)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS founder_chef_order_pushes (
                order_id TEXT PRIMARY KEY, day TEXT NOT NULL, amount TEXT NOT NULL,
                status TEXT NOT NULL, devices INTEGER NOT NULL DEFAULT 0, error TEXT,
                created_at TEXT NOT NULL, sent_at TEXT)''')

    def empty(self) -> bool:
        with closing(self.db.connect()) as connection:
            return connection.execute('SELECT 1 FROM founder_chef_order_pushes LIMIT 1').fetchone() is None

    def known(self, days: list[str]) -> set[str]:
        marks = ', '.join('?' for _ in days)
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                f'SELECT order_id FROM founder_chef_order_pushes WHERE day IN ({marks})', tuple(days)).fetchall()
        return {row[0] for row in rows}

    def claim(self, order: dict, status: str, now: datetime) -> bool:
        with closing(self.db.connect()) as connection, connection:
            return connection.execute(
                'INSERT OR IGNORE INTO founder_chef_order_pushes (order_id, day, amount, status, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (str(order['order_id']), order['day'], order['amount'], status, now.isoformat())).rowcount == 1

    def finish(self, order_id: str, status: str, now: datetime, *, devices: int = 0, error: str | None = None):
        with closing(self.db.connect()) as connection, connection:
            connection.execute(
                'UPDATE founder_chef_order_pushes SET status = ?, devices = ?, error = ?, sent_at = ? '
                'WHERE order_id = ?', (status, devices, error, now.isoformat(), str(order_id)))

    def rows(self, limit: int = 20) -> list[dict]:
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                f'SELECT {", ".join(self.COLUMNS)} FROM founder_chef_order_pushes '
                'ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
        return [dict(zip(self.COLUMNS, row)) for row in rows]


def money(value) -> str:
    return f'{int(Decimal(value)):,}'.replace(',', ' ')


def quantity(value) -> str:
    number = Decimal(value)
    return str(int(number)) if number == number.to_integral_value() else str(number).rstrip('0').rstrip('.')


def notification(order: dict) -> dict:
    """Текст уведомления: стол в заголовке, блюда и сумма в теле."""
    table = order.get('table')
    title = 'Счёт Шефа' + (f' · стол {table}' if table not in (None, '') else '')
    dishes = order.get('dishes') or []
    names = [f'{dish["name"]} ×{quantity(dish["quantity"])}' for dish in dishes[:SHOWN_DISHES]]
    if len(dishes) > SHOWN_DISHES:
        names.append(f'ещё {len(dishes) - SHOWN_DISHES}')
    lines = [', '.join(names)] if names else []
    tail = f'{money(order["amount"])} сум'
    if order.get('waiters'):
        tail += ' · ' + ', '.join(order['waiters'])
    lines.append(tail)
    return dict(title=title, body='\n'.join(lines), url='/founder', tag=f'chef-{order["order_id"]}')


class ChefOrderPush:
    def __init__(self, store, subscriptions, *, sender, loader, clock=lambda: datetime.now(TZ)):
        self.store, self.subscriptions, self.sender = store, subscriptions, sender
        self.loader, self.clock = loader, clock
        self.worker = None

    async def run_once(self) -> list[dict]:
        """Одна проверка. Возвращает заказы, записанные в журнал сейчас."""
        now = self.clock().astimezone(TZ)
        days = [now.date() - timedelta(days=1), now.date()]
        try:
            orders = await self.loader(days[0], days[1])
        except (DataError, TimeoutError) as error:
            log_safe_failure('chef-push', error, operation='load')
            return []
        first_run = await asyncio.to_thread(self.store.empty)
        known = await asyncio.to_thread(self.store.known, [day.isoformat() for day in days])
        fresh = [order for order in orders if str(order['order_id']) not in known]
        done = []
        if first_run:
            for order in fresh:
                await asyncio.to_thread(self.store.claim, order, BASELINE, now)
            if fresh:
                logger.info('chef push: запомнено %s уже закрытых заказов без рассылки', len(fresh))
            return [dict(order_id=order['order_id'], status=BASELINE) for order in fresh]
        for order in fresh:
            if not await asyncio.to_thread(self.store.claim, order, SENDING, now):
                continue
            devices = await asyncio.to_thread(self.subscriptions.all)
            if not devices:
                await asyncio.to_thread(self.store.finish, order['order_id'], NO_RECIPIENT, now)
                done.append(dict(order_id=order['order_id'], status=NO_RECIPIENT))
                continue
            result = await broadcast(self.sender, self.subscriptions, notification(order), now, devices)
            status = SENT if result['sent'] else FAILED
            error = ','.join(sorted(set(result['errors'])))[:80] or None
            await asyncio.to_thread(self.store.finish, order['order_id'], status, self.clock().astimezone(TZ),
                                    devices=result['sent'], error=error)
            logger.info('chef push %s: %s, устройств %s', order['order_id'], status, result['sent'])
            done.append(dict(order_id=order['order_id'], status=status, devices=result['sent']))
        return done

    def start(self, every: float = TICK_SECONDS):
        async def work():
            await asyncio.sleep(90)  # не мешаем старту и первым запросам после выката
            while True:
                try:
                    await self.run_once()
                except Exception as error:
                    log_safe_failure('chef-push', error, operation='schedule')
                await asyncio.sleep(every)
        self.worker = asyncio.create_task(work())

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
