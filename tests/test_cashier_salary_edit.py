"""T-442: авто-строку «Зарплата кассира» кассир может поправить за день.

Строка по-прежнему авто: удалить её нельзя, политика не пересоздаёт и не
перезаписывает её. Ноль допустим — в этот день кассир не работал."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings
from retro.modules.cashier.expenses import AutomaticExpense, ExpenseStore, seed_cashier_expense
from retro.modules.cashier.service import DataError

DAY = date(2026, 10, 8)


def salary_row(store, day=DAY):
    return next(item for item in store.list(day) if item.automatic)


def test_cashier_changes_the_salary_of_one_day_and_it_stays_automatic(tmp_path):
    path = tmp_path / 'cashier.sqlite3'
    seed_cashier_expense(path, DAY, DAY + timedelta(days=1), 'Зарплата кассира', Decimal('350000'))
    store = ExpenseStore(path)
    row = salary_row(store)

    changed = store.set_automatic_amount(row.id, DAY, '175 000')

    assert changed.automatic is True and changed.amount == Decimal('175000')
    reopened = ExpenseStore(path)
    assert salary_row(reopened).amount == Decimal('175000')
    assert salary_row(reopened, DAY + timedelta(days=1)).amount == Decimal('350000'), 'соседний день не тронут'
    assert reopened.total(DAY) == Decimal('175000')
    # Повторная засевка той же политики не возвращает прежнюю сумму.
    seed_cashier_expense(path, DAY, DAY + timedelta(days=1), 'Зарплата кассира', Decimal('350000'))
    assert salary_row(ExpenseStore(path)).amount == Decimal('175000')
    with pytest.raises(AutomaticExpense):
        reopened.delete(row.id, DAY)


def test_zero_salary_is_allowed_but_not_negative_or_a_manual_row(tmp_path):
    path = tmp_path / 'cashier.sqlite3'
    seed_cashier_expense(path, DAY, DAY, 'Зарплата кассира', Decimal('350000'))
    store = ExpenseStore(path)
    row = salary_row(store)
    assert store.set_automatic_amount(row.id, DAY, '0').amount == Decimal('0')
    for bad in ('-1', 'abc', '1.001', 'NaN'):
        with pytest.raises(DataError):
            store.set_automatic_amount(row.id, DAY, bad)
    manual = store.add(DAY, 'Такси', '20000')
    with pytest.raises(DataError):
        store.set_automatic_amount(manual.id, DAY, '10000')
    assert store.set_automatic_amount(row.id, DAY + timedelta(days=1), '1') is None


def test_api_changes_the_salary_and_the_day_total(tmp_path):
    path = tmp_path / 'cashier.sqlite3'
    seed_cashier_expense(path, DAY, DAY, 'Зарплата кассира', Decimal('350000'))
    with TestClient(create_app(Settings(), expense_db_path=path), client=('127.0.0.1', 50000)) as client:
        listed = client.get('/api/cashier/expenses', params={'date': DAY.isoformat()}).json()
        salary = next(item for item in listed['expenses'] if item.get('automatic'))
        changed = client.patch(f"/api/cashier/expenses/{salary['id']}",
                               json={'date': DAY.isoformat(), 'amount': '200000'})
        assert changed.status_code == 200, changed.text
        assert changed.json() == {'id': salary['id'], 'description': 'Зарплата кассира',
                                  'amount': '200000', 'automatic': True}
        assert client.get('/api/cashier/expenses', params={'date': DAY.isoformat()}).json()['total'] == '200000'
        assert client.patch(f"/api/cashier/expenses/{salary['id']}",
                            json={'date': DAY.isoformat(), 'amount': '-5'}).status_code == 422
        assert client.patch(f"/api/cashier/expenses/{salary['id']}",
                            json={'date': (DAY - timedelta(days=1)).isoformat(), 'amount': '5'}).status_code == 404
