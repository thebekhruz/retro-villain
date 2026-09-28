"""Manual payable expenses and their dated cash payments."""

from contextlib import closing
from datetime import datetime
from decimal import Decimal

from .expense_catalog import ITEMS
from .ledger import LedgerError, amount_value, required_text
from .audit import record_audit


def _payments(connection, debt_id, through):
    return sum((Decimal(row[0]) for row in connection.execute(
        'SELECT amount FROM accountant_debt_payments WHERE debt_id=? AND day<=?',
        (debt_id, through))), Decimal(0))


def debt_summary(store, day):
    outstanding = []
    created = []
    with closing(store._open()) as connection:
        rows = connection.execute('SELECT id, day, item_code, description, total_amount '
                                  'FROM accountant_debts WHERE day<=? ORDER BY day,id',
                                  (day.isoformat(),)).fetchall()
        for debt_id, created_day, item_code, description, total in rows:
            paid = _payments(connection, debt_id, day.isoformat())
            left = Decimal(total) - paid
            item = dict(id=debt_id, day=created_day, item_code=item_code,
                        description=description, total=str(total), paid=str(paid),
                        debt=str(left))
            if created_day == day.isoformat():
                created.append(item)
            if left > 0:
                outstanding.append(item)
    return dict(manual_debt_total=str(sum((Decimal(item['debt']) for item in outstanding), Decimal(0))),
                manual_debts=outstanding, debts_created_today=created)


def record_debt(store, day, item_code, note, total, paid, cashier_amount):
    if item_code not in ITEMS or ITEMS[item_code][0] == 'income':
        raise LedgerError('Выберите наименование затрат из справочника.')
    note = required_text(note, 'наименование расхода')
    total_value = amount_value(total)
    paid_value = amount_value(paid, allow_zero=True)
    if paid_value >= total_value:
        raise LedgerError('Для полной оплаты используйте обычную запись расхода.')
    description = f'{ITEMS[item_code][1]} · {note}'
    with closing(store._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            store._validate_salary_expense(connection, day, item_code)
            if paid_value and store.available_cash(connection, day, cashier_amount) < paid_value:
                raise LedgerError('На выбранный день недостаточно денег от кассира.')
            now = datetime.now().isoformat()
            debt_id = connection.execute(
                'INSERT INTO accountant_debts (day,item_code,description,total_amount,created_at) '
                'VALUES (?,?,?,?,?)',
                (day.isoformat(), item_code, description, str(total_value), now)).lastrowid
            record_audit(connection, 'debt', debt_id, 'create', None,
                         store._row_dict(connection, 'accountant_debts', debt_id))
            if paid_value:
                movement_id = connection.execute(
                    'INSERT INTO accountant_movements '
                    '(day,kind,description,amount,item_code,created_at) VALUES (?,?,?,?,?,?)',
                    (day.isoformat(), 'other_expense', description, str(paid_value), item_code, now)).lastrowid
                connection.execute(
                    'INSERT INTO accountant_debt_payments '
                    '(debt_id,day,amount,movement_id,created_at) VALUES (?,?,?,?,?)',
                    (debt_id, day.isoformat(), str(paid_value), movement_id, now))
                payment_id = connection.execute(
                    'SELECT id FROM accountant_debt_payments WHERE movement_id = ?',
                    (movement_id,)).fetchone()[0]
                record_audit(connection, 'movement', movement_id, 'create', None,
                             store._row_dict(connection, 'accountant_movements', movement_id))
                record_audit(connection, 'debt_payment', payment_id, 'create', None,
                             store._row_dict(connection, 'accountant_debt_payments', payment_id))
                store._check_cash_balances(connection, day)
            connection.commit()
            return debt_id
        except Exception:
            connection.rollback()
            raise


def pay_debt(store, debt_id, day, amount, cashier_amount):
    value = amount_value(amount)
    with closing(store._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            debt = connection.execute('SELECT day,item_code,description,total_amount '
                                      'FROM accountant_debts WHERE id=?', (debt_id,)).fetchone()
            if debt is None:
                raise LedgerError('Долг не найден.')
            created_day, item_code, description, total = debt
            if day.isoformat() < created_day:
                raise LedgerError('Оплата не может быть раньше записи долга.')
            left = Decimal(total) - _payments(connection, debt_id, '9999-12-31')
            if value > left:
                raise LedgerError('Оплата превышает оставшийся долг.')
            if store.available_cash(connection, day, cashier_amount) < value:
                raise LedgerError('На выбранный день недостаточно денег от кассира.')
            now = datetime.now().isoformat()
            movement_id = connection.execute(
                'INSERT INTO accountant_movements '
                '(day,kind,description,amount,item_code,created_at) VALUES (?,?,?,?,?,?)',
                (day.isoformat(), 'other_expense', description + ' · погашение долга',
                 str(value), item_code, now)).lastrowid
            payment_id = connection.execute(
                'INSERT INTO accountant_debt_payments '
                '(debt_id,day,amount,movement_id,created_at) VALUES (?,?,?,?,?)',
                (debt_id, day.isoformat(), str(value), movement_id, now)).lastrowid
            record_audit(connection, 'movement', movement_id, 'create', None,
                         store._row_dict(connection, 'accountant_movements', movement_id))
            record_audit(connection, 'debt_payment', payment_id, 'create', None,
                         store._row_dict(connection, 'accountant_debt_payments', payment_id))
            store._check_known_future_balances(connection, day)
            connection.commit()
            return payment_id
        except Exception:
            connection.rollback()
            raise
