"""Недельная цель дивидендов, которую ставит учредитель.

Это только цель: деньги по-прежнему откладывает бухгалтер обычной операцией
«Отложить в сейф» (резерв `dividends`). Второго способа списания здесь нет —
стор хранит число и кто его поставил, историю правок не теряет.
"""

import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from retro.modules.cashier.service import TZ
from retro.db import as_database
from retro.runtime import secure_directory, secure_file

MAX_TARGET = Decimal('1000000000000')


class DividendTargetError(ValueError):
    pass


def target_value(value) -> Decimal:
    try:
        amount = Decimal(str(value).replace(' ', ''))
    except (InvalidOperation, ValueError):
        raise DividendTargetError('Укажите сумму цифрами.') from None
    if not amount.is_finite() or amount < 0 or amount > MAX_TARGET or amount != amount.to_integral_value():
        raise DividendTargetError('Сумма должна быть целым числом сум, не больше 1 трлн.')
    return amount


class DividendTargetStore:
    def __init__(self, path):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        with closing(self._open()) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS founder_dividend_targets (
                    week TEXT PRIMARY KEY,
                    amount TEXT NOT NULL,
                    changed_by TEXT,
                    changed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS founder_dividend_target_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    week TEXT NOT NULL,
                    old_amount TEXT,
                    new_amount TEXT NOT NULL,
                    changed_by TEXT,
                    changed_at TEXT NOT NULL
                );
            ''')

    def _open(self):
        return self.db.connect()

    def get(self, week: str) -> dict | None:
        """Цель недели; если на неделю не ставили — последняя из прошлых.

        Учредитель обычно держит одну сумму неделями, и заставлять его
        подтверждать её каждый понедельник незачем. Откуда взята цель, видно
        по полю `week`."""
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT week, amount, changed_by, changed_at FROM founder_dividend_targets '
                'WHERE week <= ? ORDER BY week DESC LIMIT 1', (week,)).fetchone()
        if row is None:
            return None
        return dict(week=row[0], amount=row[1], changed_by=row[2], changed_at=row[3],
                    inherited=row[0] != week)

    def set(self, week: str, amount, changed_by: str | None) -> dict:
        value = target_value(amount)
        now = datetime.now(TZ).isoformat(timespec='seconds')
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            old = connection.execute('SELECT amount FROM founder_dividend_targets WHERE week = ?',
                                     (week,)).fetchone()
            connection.execute(
                'INSERT INTO founder_dividend_targets (week, amount, changed_by, changed_at) '
                'VALUES (?, ?, ?, ?) ON CONFLICT(week) DO UPDATE SET amount=excluded.amount, '
                'changed_by=excluded.changed_by, changed_at=excluded.changed_at',
                (week, str(value), changed_by, now))
            connection.execute(
                'INSERT INTO founder_dividend_target_history '
                '(week, old_amount, new_amount, changed_by, changed_at) VALUES (?, ?, ?, ?, ?)',
                (week, old[0] if old else None, str(value), changed_by, now))
        return self.get(week)
