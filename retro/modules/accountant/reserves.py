"""Dated subsidiary ledgers. Moving cash to the safe is not an owner payout."""
from contextlib import closing
from datetime import datetime
from decimal import Decimal

from .ledger import LedgerError, amount_value, required_text
from .audit import record_audit

SHIFT_SALARY_CODES = {'salary_cashier', 'salary_staff', 'salary_technical', 'salary_carryover'}


def is_monthly_salary(item_code):
    return item_code == 'salary_monthly' or (
        isinstance(item_code, str) and item_code.startswith('salary_')
        and item_code not in SHIFT_SALARY_CODES)


def _entries(connection, account, through=None):
    rows = [dict(id=r[0], day=r[1], kind=r[2], amount=r[3], note=r[4]) for r in connection.execute(
        'SELECT id, day, kind, amount, note FROM accountant_reserves WHERE account = ? ORDER BY day, id',
        (account,))]
    if account == 'shoh':
        rows += [dict(id=None, day=r[0], kind='deposit', amount=r[1], note=r[2]) for r in connection.execute(
            "SELECT day, amount, description FROM accountant_movements WHERE "
            "(kind='other_expense' AND item_code='proc_shoh') OR "
            "(kind='procurement_advance' AND description LIKE 'Шох:%')")]
    return sorted((r for r in rows if through is None or r['day'] <= through), key=lambda r: r['day'])


def _balance(rows):
    if not any(r['kind'] == 'opening' for r in rows):
        return None
    return sum((Decimal(r['amount']) * (-1 if r['kind'] == 'withdrawal' else 1) for r in rows), Decimal(0))


def add_reserve_entry(store, day, account, kind, amount, note, cashier_amount):
    allowed = {'dividends': {'opening', 'transfer', 'withdrawal'},
               'usd': {'opening', 'deposit', 'withdrawal'}, 'shoh': {'opening', 'withdrawal'}}
    if account not in allowed or kind not in allowed[account]:
        raise LedgerError('Выберите допустимую операцию и счёт.')
    value = amount_value(amount, allow_zero=kind == 'opening')
    note = required_text(note, 'основание операции')
    with closing(store._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            rows = _entries(connection, account)
            if kind == 'opening':
                if any(r['kind'] == 'opening' for r in rows):
                    raise LedgerError('Начальный остаток уже указан.')
                if any(r['day'] < day.isoformat() for r in rows):
                    raise LedgerError('Начальный остаток должен быть не позже первой операции.')
            elif not any(r['kind'] == 'opening' and r['day'] <= day.isoformat() for r in rows):
                raise LedgerError('Сначала укажите подтверждённый начальный остаток на этот день или раньше.')
            if kind == 'transfer':
                if cashier_amount is None:
                    raise LedgerError('Нет данных кассира для перевода в сейф.')
                if store.available_cash(connection, day, cashier_amount) < value:
                    raise LedgerError('Недостаточно денег от кассира для перевода в сейф.')
            cursor = connection.execute(
                'INSERT INTO accountant_reserves (day, account, kind, amount, note, created_at) VALUES (?, ?, ?, ?, ?, ?)',
                (day.isoformat(), account, kind, str(value), note, datetime.now().isoformat()))
            after = store._row_dict(connection, 'accountant_reserves', cursor.lastrowid)
            record_audit(connection, 'reserve', cursor.lastrowid, 'create', None, after)
            rows = _entries(connection, account)
            for cutoff in {r['day'] for r in rows}:
                balance = _balance([r for r in rows if r['day'] <= cutoff])
                if balance is not None and balance < 0:
                    raise LedgerError('Операция превышает остаток на этот или последующий день.')
            if kind == 'transfer' and cashier_amount is not None:
                store._check_known_future_balances(connection, day)
            connection.commit()
            return cursor.lastrowid
        except Exception:
            connection.rollback()
            raise


def set_monthly_plan(store, day, amount, note):
    value = amount_value(amount, allow_zero=True)
    note = required_text(note, 'основание плана выплат')
    with closing(store._open()) as connection:
        try:
            with connection:
                connection.execute('INSERT INTO accountant_monthly_plans VALUES (?, ?, ?, ?)',
                                   (day.isoformat()[:7], str(value), note, datetime.now().isoformat()))
                record_audit(connection, 'monthly_plan', day.isoformat()[:7], 'create', None,
                             dict(month=day.isoformat()[:7], amount=str(value), note=note))
        except Exception as error:
            import sqlite3
            if isinstance(error, sqlite3.IntegrityError):
                raise LedgerError('План на этот месяц уже задан. Изменение требует отдельной сверки.') from None
            raise


def reserve_summary(store, day):
    result = {}
    with closing(store._open()) as connection:
        for account in ('dividends', 'usd', 'shoh'):
            rows = _entries(connection, account, day.isoformat())
            balance = _balance(rows)
            result[account] = dict(balance=str(balance) if balance is not None else None,
                                   currency='USD' if account == 'usd' else 'UZS',
                                   entries=[r for r in rows if r['day'] == day.isoformat()])
        month = day.isoformat()[:7]
        plan = connection.execute('SELECT amount, note FROM accountant_monthly_plans WHERE month = ?',
                                  (month,)).fetchone()
        paid = sum((Decimal(r[0]) for r in connection.execute(
            'SELECT amount, item_code FROM accountant_movements WHERE day >= ? AND day <= ? '
            "AND kind = 'other_expense'", (month + '-01', day.isoformat())) if is_monthly_salary(r[1])), Decimal(0))
        result['monthly'] = dict(month=month, plan=plan[0] if plan else None, paid=str(paid),
                                 balance=str(Decimal(plan[0]) - paid) if plan else None)
    return result
