from datetime import date
from decimal import Decimal

import pytest

from retro.modules.cashier.expenses import ExpenseStore, cash_to_finance
from retro.modules.cashier.service import DataError, Payment, Snapshot


DAY = date(2026, 9, 12)


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
    assert reopened.total(DAY) == Decimal('432000')
    assert reopened.list(DAY)[0].automatic is True


def test_salary_is_present_each_day_without_creating_database_rows(tmp_path):
    store = ExpenseStore(tmp_path / 'cashier.sqlite3')
    for day in (DAY, date(2026, 4, 25)):
        items = store.list(day)
        assert [(item.description, item.amount, item.automatic) for item in items] == [
            ('Зарплата', Decimal('350000'), True)]
        assert store.total(day) == Decimal('350000')
        assert store.delete(-1, day) is False


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
