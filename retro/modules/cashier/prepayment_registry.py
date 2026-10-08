"""Реестр предоплат кассира (ТЗ «Выручка, оплаты и предоплаты», 08.10.2026, п. 3.2).

Деньги и время — из iiko: аванс приходит транзакцией PREPAY с номером заказа,
зачёт — продажей с операцией PREPAY при закрытии того же заказа. Кто внёс,
телефон и на какую дату событие iiko не отдаёт — это заполняет кассир, и
хранится здесь же, по номеру заказа. Факты iiko кэшируются в той же строке,
чтобы «зачтено сегодня» знало, когда аванс был получен.
"""
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal

from retro.db import as_database
from .service import TZ, DataError

STATUS_PENDING, STATUS_CREDITED, STATUS_REFUND = 'pending', 'credited', 'refund'
LIMITS = dict(guest=120, phone=40, method=60)
COLUMNS = ('order_number', 'received_day', 'received_at', 'amount', 'guest', 'phone', 'event_day',
           'method', 'refunded', 'credited_day', 'credited_amount', 'credited_methods',
           'updated_at', 'updated_by')


class PrepaymentRegistry:
    def __init__(self, database):
        self.db = as_database(database)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS cashier_prepayment_registry (
                order_number TEXT PRIMARY KEY,
                received_day TEXT, received_at TEXT, amount TEXT,
                guest TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
                event_day TEXT, method TEXT NOT NULL DEFAULT '',
                refunded INTEGER NOT NULL DEFAULT 0,
                credited_day TEXT, credited_amount TEXT, credited_methods TEXT NOT NULL DEFAULT '',
                updated_at TEXT, updated_by TEXT)''')

    # ── факты iiko ──────────────────────────────────────────────────────────
    def record_received(self, entries):
        """entries: [(order, received_day, received_at, amount)] — авансы из iiko.

        Сумму и время аванса пишет только iiko; то, что ввёл кассир, не трогаем."""
        with closing(self.db.connect()) as connection, connection:
            for order, day, received_at, amount in entries:
                connection.execute(
                    'INSERT INTO cashier_prepayment_registry (order_number, received_day, received_at, amount) '
                    'VALUES (?,?,?,?) ON CONFLICT(order_number) DO UPDATE SET '
                    'received_day=excluded.received_day, received_at=excluded.received_at, amount=excluded.amount',
                    (str(order), day.isoformat(), received_at, str(amount)))

    def record_credited(self, day, orders):
        """orders: [(order, amount, methods)] — заказы, закрытые в этот день с зачётом аванса."""
        with closing(self.db.connect()) as connection, connection:
            for order, amount, methods in orders:
                connection.execute(
                    'INSERT INTO cashier_prepayment_registry (order_number, credited_day, credited_amount, credited_methods) '
                    'VALUES (?,?,?,?) ON CONFLICT(order_number) DO UPDATE SET '
                    'credited_day=excluded.credited_day, credited_amount=excluded.credited_amount, '
                    'credited_methods=excluded.credited_methods',
                    (str(order), day.isoformat(), str(amount), ', '.join(methods)))

    # ── ввод кассира ────────────────────────────────────────────────────────
    def annotate(self, order, *, guest='', phone='', event_day=None, method='', refunded=False, by=None):
        order = str(order).strip()
        if not order or len(order) > 40:
            raise DataError('Не указан номер заказа предоплаты.')
        values = dict(guest=(guest or '').strip(), phone=(phone or '').strip(), method=(method or '').strip())
        for key, limit in LIMITS.items():
            if len(values[key]) > limit:
                raise DataError(f'Слишком длинное поле: не больше {limit} символов.')
        if event_day is not None and not isinstance(event_day, date):
            raise DataError('Дата события указана неверно.')
        stamp = datetime.now(TZ).isoformat(timespec='seconds')
        with closing(self.db.connect()) as connection, connection:
            connection.execute(
                'INSERT INTO cashier_prepayment_registry (order_number, guest, phone, event_day, method, refunded, '
                'updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(order_number) DO UPDATE SET '
                'guest=excluded.guest, phone=excluded.phone, event_day=excluded.event_day, method=excluded.method, '
                'refunded=excluded.refunded, updated_at=excluded.updated_at, updated_by=excluded.updated_by',
                (order, values['guest'], values['phone'], event_day.isoformat() if event_day else None,
                 values['method'], 1 if refunded else 0, stamp, by))
        return self.rows([order])[0]

    # ── чтение ──────────────────────────────────────────────────────────────
    def rows(self, orders):
        orders = [str(order) for order in orders]
        if not orders:
            return []
        marks = ','.join('?' * len(orders))
        with closing(self.db.connect()) as connection:
            found = connection.execute(
                f'SELECT {", ".join(COLUMNS)} FROM cashier_prepayment_registry WHERE order_number IN ({marks})',
                orders).fetchall()
        by_order = {row[0]: row for row in found}
        return [registry_row(by_order[order]) for order in orders if order in by_order]

    def rows_between(self, start, end):
        """Все предоплаты, полученные или назначенные на даты периода."""
        with closing(self.db.connect()) as connection:
            found = connection.execute(
                f'SELECT {", ".join(COLUMNS)} FROM cashier_prepayment_registry '
                'WHERE (received_day>=? AND received_day<=?) OR (event_day>=? AND event_day<=?) '
                'OR (credited_day>=? AND credited_day<=?) ORDER BY received_at, order_number',
                (start.isoformat(), end.isoformat()) * 3).fetchall()
        return [registry_row(row) for row in found]

    def missing_received(self, orders):
        """Заказы, для которых ещё не знаем, когда получен аванс."""
        known = {row['order_number'] for row in self.rows(orders) if row['received_at']}
        return [str(order) for order in orders if str(order) not in known]


def registry_row(row):
    item = dict(zip(COLUMNS, row))
    item['refunded'] = bool(item['refunded'])
    item['credited_methods'] = [part for part in (item['credited_methods'] or '').split(', ') if part]
    item['status'] = (STATUS_REFUND if item['refunded'] else
                      STATUS_CREDITED if item['credited_day'] else STATUS_PENDING)
    # Способ — что ввёл кассир; нет — чем был аванс по закрытию заказа в iiko.
    item['method_shown'] = item['method'] or ', '.join(item['credited_methods'])
    for key in ('amount', 'credited_amount'):
        item[key] = str(Decimal(item[key])) if item[key] is not None else None
    return item
