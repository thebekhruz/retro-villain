"""Day-scoped cashier expenses, kept separate from iiko sales data."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .service import DataError
from retro.runtime import secure_directory, secure_file

DAILY_SALARY = Decimal('350000')
SALARY_LABELS = {'зарплата', 'зп', 'любовь', 'любовь зп', 'любовь зарплата'}


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


class ExpenseStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self):
        secure_directory(self.path.parent)
        connection = sqlite3.connect(self.path, timeout=10)
        secure_file(self.path)
        connection.execute('''CREATE TABLE IF NOT EXISTS cashier_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            description TEXT NOT NULL,
            amount TEXT NOT NULL
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS cashier_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            description TEXT NOT NULL,
            amount TEXT NOT NULL
        )''')
        return connection

    def list(self, day: date):
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, description, amount FROM cashier_expenses WHERE day = ? ORDER BY id',
                (day.isoformat(),),
            ).fetchall()
        manual = [Expense(row[0], day, row[1], Decimal(row[2])) for row in rows]
        if any(item.amount == DAILY_SALARY and
               ' '.join(item.description.casefold().split()) in SALARY_LABELS for item in manual):
            return manual
        return [Expense(None, day, 'Зарплата', DAILY_SALARY, automatic=True), *manual]

    def total(self, day: date):
        return sum((item.amount for item in self.list(day)), Decimal(0))

    def add(self, day: date, description: str, amount):
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
        with closing(self._open()) as connection:
            with connection:
                cursor = connection.execute(
                    'INSERT INTO cashier_expenses (day, description, amount) VALUES (?, ?, ?)',
                    (day.isoformat(), name, str(value)),
                )
                item_id = cursor.lastrowid
        return Expense(item_id, day, name, value)

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
