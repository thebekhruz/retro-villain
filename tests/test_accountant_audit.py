from datetime import date
from decimal import Decimal
import sqlite3

import pytest

from retro.modules.accountant.ledger import FinanceStore, LedgerError
from retro.modules.accountant.payroll import PayrollRow


DAY = date(2026, 9, 15)


def payroll_row():
    return PayrollRow(1, 'Тест Сотрудник', 'официант', 'Обслуживание зала',
                      'late', None, Decimal('270000'), Decimal('270000'), False)


def test_deleting_salary_payment_removes_only_salary_payment(tmp_path):
    path = tmp_path / 'finance.sqlite3'
    store = FinanceStore(path)
    store.add_opening(DAY, '1000000', 'Остаток')
    store.confirm_payroll(DAY, [payroll_row()], 'Финансы')
    salary_id = store.pay_salary(store.accruals(DAY)[0]['id'], DAY, '100000')
    debt_id = store.record_debt(DAY, 'admin_other', 'Ремонт', '200000', '100000')

    with sqlite3.connect(path) as connection:
        debt_payment_id = connection.execute(
            'SELECT id FROM accountant_debt_payments WHERE debt_id=?', (debt_id,)).fetchone()[0]
    assert salary_id == debt_payment_id == 1

    store.delete_operation('salary_payment', salary_id, DAY)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            'SELECT 1 FROM accountant_salary_payments WHERE id=?', (salary_id,)).fetchone() is None
        assert connection.execute(
            'SELECT 1 FROM accountant_debt_payments WHERE id=?', (debt_payment_id,)).fetchone()


def test_financial_mutation_records_before_and_after(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.add_opening(DAY, '1000', 'Остаток')
    movement = store.add_expense(DAY, 'admin_other', 'Бумага', '100')

    store.update_movement(movement, DAY, 'admin_other', 'Бумага', '90')

    row = store.audit_entries(entity_type='movement', entity_id=movement)[-1]
    assert row['action'] == 'update'
    assert row['before']['amount'] == '100'
    assert row['after']['amount'] == '90'


def test_delete_rejects_client_supplied_different_date(tmp_path):
    store = FinanceStore(tmp_path / 'finance.sqlite3')
    store.add_opening(DAY, '1000', 'Остаток')
    movement = store.add_expense(DAY, 'admin_other', 'Бумага', '100')

    with pytest.raises(LedgerError, match='дату'):
        store.delete_operation('movement', movement, date(2026, 9, 14))

