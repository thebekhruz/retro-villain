"""Day-scoped cashier expenses, kept separate from iiko sales data."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .service import DataError
from retro.db import as_database, table_columns
from retro.runtime import secure_directory, secure_file

@dataclass(frozen=True)
class Expense:
    id: int | None
    day: date
    description: str
    amount: Decimal
    automatic: bool = False

    def json(self):
        result = dict(id=self.id, description=self.description, amount=str(self.amount))
        if self.automatic:
            result['automatic'] = True
        return result


@dataclass(frozen=True)
class SeedResult:
    inserted: int
    skipped: int


class ExpenseStore:
    def __init__(self, path: Path):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        self._initialize()

    def _open(self):
        return self.db.connect()

    def _initialize(self):
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                connection.execute('''CREATE TABLE IF NOT EXISTS cashier_expenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    operation_key TEXT
                )''')
                columns = table_columns(connection, 'cashier_expenses')
                if 'operation_key' not in columns:
                    connection.execute(
                        'ALTER TABLE cashier_expenses ADD COLUMN operation_key TEXT')
                connection.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS cashier_expense_operation_key '
                    'ON cashier_expenses(operation_key) WHERE operation_key IS NOT NULL')
                connection.execute('''CREATE TABLE IF NOT EXISTS cashier_expense_policy (
                    id INTEGER PRIMARY KEY CHECK(id=1), configured_at TEXT NOT NULL,
                    date_from TEXT NOT NULL, date_to TEXT NOT NULL,
                    description TEXT NOT NULL, amount TEXT NOT NULL
                )''')
                connection.execute('''CREATE TABLE IF NOT EXISTS cashier_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount TEXT NOT NULL
                )''')
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list(self, day: date):
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, description, amount FROM cashier_expenses WHERE day = ? ORDER BY id',
                (day.isoformat(),),
            ).fetchall()
        return [Expense(row[0], day, row[1], Decimal(row[2])) for row in rows]

    def total(self, day: date):
        return sum((item.amount for item in self.list(day)), Decimal(0))

    def total_between(self, start: date, end: date):
        if start > end:
            raise ValueError('expense range start must not exceed end')
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT amount FROM cashier_expenses WHERE day >= ? AND day <= ?',
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return sum((Decimal(row[0]) for row in rows), Decimal(0))

    @staticmethod
    def _values(description, amount):
        name = description.strip() if isinstance(description, str) else ''
        if not name or len(name) > 160:
            raise DataError('Укажите название расхода (до 160 символов).')
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            raise DataError('Укажите корректную сумму расхода.') from None
        if (not value.is_finite() or value <= 0 or value > Decimal('1000000000000')
                or value.as_tuple().exponent < -2):
            raise DataError('Сумма расхода должна быть от 0,01 до 1 трлн сум, не более двух знаков после запятой.')
        return name, value

    def add(self, day: date, description: str, amount, *, operation_key=None):
        name, value = self._values(description, amount)
        with closing(self._open()) as connection:
            with connection:
                cursor = connection.execute(
                    'INSERT INTO cashier_expenses (day, description, amount, operation_key) VALUES (?, ?, ?, ?)',
                    (day.isoformat(), name, str(value), operation_key),
                )
                item_id = cursor.lastrowid
        return Expense(item_id, day, name, value)

    def policy_configured(self):
        with closing(self._open()) as connection:
            return connection.execute('SELECT 1 FROM cashier_expense_policy WHERE id=1').fetchone() is not None

    def delete(self, item_id: int, day: date):
        with closing(self._open()) as connection:
            with connection:
                result = connection.execute(
                    'DELETE FROM cashier_expenses WHERE id = ? AND day = ?',
                    (item_id, day.isoformat()),
                )
                return result.rowcount > 0

    def list_receipts(self, day: date):
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, description, amount FROM cashier_receipts WHERE day = ? ORDER BY id',
                (day.isoformat(),),
            ).fetchall()
        return [Expense(row[0], day, row[1], Decimal(row[2])) for row in rows]

    def add_receipt(self, day: date, description: str, amount):
        name = description.strip() if isinstance(description, str) else ''
        if not name or len(name) > 160:
            raise DataError('Укажите назначение поступления (до 160 символов).')
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            raise DataError('Укажите корректную сумму поступления.') from None
        if (not value.is_finite() or value <= 0 or value > Decimal('1000000000000')
                or value.as_tuple().exponent < -2):
            raise DataError('Сумма поступления должна быть от 0,01 до 1 трлн сум, не более двух знаков после запятой.')
        with closing(self._open()) as connection:
            with connection:
                cursor = connection.execute(
                    'INSERT INTO cashier_receipts (day, description, amount) VALUES (?, ?, ?)',
                    (day.isoformat(), name, str(value)),
                )
        return Expense(cursor.lastrowid, day, name, value)

    def delete_receipt(self, item_id: int, day: date):
        with closing(self._open()) as connection:
            with connection:
                result = connection.execute(
                    'DELETE FROM cashier_receipts WHERE id = ? AND day = ?',
                    (item_id, day.isoformat()),
                )
                return result.rowcount > 0


def cash_to_finance(snapshot, expense_total, other_receipts=Decimal(0)):
    demo_amount = next((p.amount for p in snapshot.payments if p.name == 'Демо'), Decimal(0))
    return demo_amount + snapshot.cash_prepayment + other_receipts - expense_total


def seed_cashier_expense(path: Path, date_from: date, date_to: date, description: str,
                         amount: Decimal) -> SeedResult:
    if date_to < date_from:
        raise DataError('Конечная дата не может быть раньше начальной.')
    store = ExpenseStore(path)
    name, value = store._values(description, amount)
    inserted = skipped = 0
    with closing(store._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            day = date_from
            while day <= date_to:
                key = f'policy:{day.isoformat()}:{name}:{value}'
                cursor = connection.execute(
                    'INSERT OR IGNORE INTO cashier_expenses '
                    '(day,description,amount,operation_key) VALUES (?,?,?,?)',
                    (day.isoformat(), name, str(value), key))
                inserted += cursor.rowcount
                skipped += cursor.rowcount == 0
                day += timedelta(days=1)
            from datetime import datetime
            connection.execute(
                'INSERT INTO cashier_expense_policy VALUES (1,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET configured_at=excluded.configured_at, '
                'date_from=excluded.date_from,date_to=excluded.date_to,description=excluded.description,amount=excluded.amount',
                (datetime.now().isoformat(), date_from.isoformat(), date_to.isoformat(), name, str(value)))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return SeedResult(inserted, skipped)
