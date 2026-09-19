import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from threading import Barrier

import pytest

from retro.modules.cashier.expenses import ExpenseStore, cash_to_finance, seed_cashier_expense
from retro.modules.cashier.service import DataError, Payment, Snapshot


DAY = date(2026, 9, 12)


def test_legacy_expense_schema_can_open_concurrently(tmp_path):
    database = tmp_path / 'cashier.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('''CREATE TABLE cashier_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            description TEXT NOT NULL,
            amount TEXT NOT NULL
        )''')
    workers = 12
    barrier = Barrier(workers)

    def open_store(_):
        barrier.wait()
        return ExpenseStore(database).list_receipts(DAY)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        assert list(pool.map(open_store, range(workers))) == [[]] * workers


def test_expenses_survive_reopening_and_are_scoped_to_day(tmp_path):
    path = tmp_path / 'cashier.sqlite3'
    store = ExpenseStore(path)
    first = store.add(DAY, '  Зарплата  ', Decimal('350000'))
    store.add(DAY, 'Напитки', Decimal('82000'))
    store.add(date(2026, 9, 11), 'Другой день', Decimal('1000'))

    reopened = ExpenseStore(path)
    assert [(item.id, item.description, item.amount) for item in reopened.list(DAY)] == [
        (first.id, 'Зарплата', Decimal('350000')),
        (first.id + 1, 'Напитки', Decimal('82000')),
    ]
    assert reopened.total(DAY) == Decimal('432000')
    assert reopened.delete(first.id, date(2026, 9, 11)) is False
    assert reopened.total(DAY) == Decimal('432000')
    assert reopened.delete(first.id, DAY) is True
    assert reopened.total(DAY) == Decimal('82000')
    assert reopened.list(DAY)[0].description == 'Напитки'


def test_empty_day_has_no_implicit_salary(tmp_path):
    store = ExpenseStore(tmp_path / 'cashier.sqlite3')
    for day in (DAY, date(2026, 4, 25)):
        assert store.list(day) == []
        assert store.total(day) == Decimal('0')
        assert store.delete(-1, day) is False


def test_salary_seed_is_explicit_and_idempotent(tmp_path):
    database = tmp_path / 'cashier.sqlite3'
    next_day = DAY + timedelta(days=1)

    first = seed_cashier_expense(database, DAY, next_day, 'Зарплата', Decimal('350000'))
    second = seed_cashier_expense(database, DAY, next_day, 'Зарплата', Decimal('350000'))

    assert first.inserted == 2
    assert second.inserted == 0
    assert ExpenseStore(database).policy_configured() is True


@pytest.mark.parametrize('description,amount', [('', '100'), ('  ', '100'), ('Кофе', '0'),
                                                  ('Кофе', '-1'), ('Кофе', 'NaN'),
                                                  ('Кофе', '1.001'),
                                                  ('Кофе', '10000000000000000')])
def test_invalid_manual_expense_is_rejected(tmp_path, description, amount):
    with pytest.raises(DataError):
        ExpenseStore(tmp_path / 'cashier.sqlite3').add(DAY, description, Decimal(amount))


def test_handover_uses_demo_payment_not_total_sales():
    snapshot = Snapshot('a' * 32, DAY, Decimal('63117000'), 137,
                        (Payment('Демо', Decimal('27028500')),
                         Payment('UzCard', Decimal('13265000'))),
                        None)
    assert cash_to_finance(snapshot, Decimal('432000')) == Decimal('26596500')
    assert cash_to_finance(snapshot, Decimal('28000000')) == Decimal('-971500')


def test_handover_adds_cash_prepayment_and_other_receipts():
    snapshot = Snapshot('b' * 32, DAY, Decimal('31824000'), 24,
                        (Payment('Демо', Decimal('14839000')),), None,
                        cash_prepayment=Decimal('600000'))
    assert cash_to_finance(snapshot, Decimal('2003000'), Decimal('0')) == Decimal('13436000')
    assert cash_to_finance(snapshot, Decimal('2003000'), Decimal('854000')) == Decimal('14290000')


def test_other_receipts_are_day_scoped_and_persistent(tmp_path):
    path = tmp_path / 'cashier.sqlite3'
    store = ExpenseStore(path)
    item = store.add_receipt(DAY, 'Вернули долг', '854000')
    store.add_receipt(date(2026, 9, 11), 'Другой день', '100')
    assert [(x.description, x.amount) for x in ExpenseStore(path).list_receipts(DAY)] == [
        ('Вернули долг', Decimal('854000'))]
    assert store.delete_receipt(item.id, date(2026, 9, 11)) is False
    assert store.delete_receipt(item.id, DAY) is True
