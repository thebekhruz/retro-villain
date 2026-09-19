"""Demo finance ledger: earned wages, actual payments, and available cash."""

import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .payroll import PayrollRow
from .expense_catalog import ITEMS
from retro.runtime import secure_directory, secure_file


class LedgerError(ValueError):
    pass


def amount_value(value, *, allow_zero=False) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise LedgerError('Укажите корректную сумму.') from None
    if (not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero)
            or amount > Decimal('1000000000000') or amount.as_tuple().exponent < -2):
        raise LedgerError('Сумма должна быть положительной, не более 1 трлн сум.')
    return amount


def required_text(value: str, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ''
    if not text or len(text) > 160:
        raise LedgerError(f'Укажите {label} (до 160 символов).')
    return text


class FinanceStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self):
        secure_directory(self.path.parent)
        connection = sqlite3.connect(self.path, timeout=10)
        secure_file(self.path)
        connection.execute('PRAGMA foreign_keys=ON')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS accountant_exceptions (
                employee_id INTEGER PRIMARY KEY,
                day TEXT NOT NULL,
                reason TEXT NOT NULL,
                approver TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_payroll_days (
                day TEXT PRIMARY KEY,
                approver TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_accruals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_day TEXT NOT NULL,
                employee_id INTEGER NOT NULL,
                employee_name TEXT NOT NULL,
                group_name TEXT NOT NULL,
                attendance_status TEXT NOT NULL,
                rate TEXT NOT NULL,
                amount TEXT NOT NULL,
                UNIQUE(work_day, employee_id)
            );
            CREATE TABLE IF NOT EXISTS accountant_salary_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                accrual_id INTEGER NOT NULL REFERENCES accountant_accruals(id),
                paid_day TEXT NOT NULL,
                amount TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                kind TEXT NOT NULL,
                description TEXT NOT NULL,
                amount TEXT NOT NULL,
                item_code TEXT,
                reference TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(kind, reference)
            );
            CREATE TABLE IF NOT EXISTS accountant_reserves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL, account TEXT NOT NULL, kind TEXT NOT NULL,
                amount TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_monthly_plans (
                month TEXT PRIMARY KEY, amount TEXT NOT NULL, note TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_handover_days (
                day TEXT PRIMARY KEY, amount TEXT NOT NULL, checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_cash_opening (
                id INTEGER PRIMARY KEY CHECK(id=1), day TEXT NOT NULL,
                amount TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                item_code TEXT NOT NULL, description TEXT NOT NULL,
                total_amount TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accountant_debt_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                debt_id INTEGER NOT NULL REFERENCES accountant_debts(id),
                day TEXT NOT NULL, amount TEXT NOT NULL,
                movement_id INTEGER NOT NULL REFERENCES accountant_movements(id),
                created_at TEXT NOT NULL
            );
        ''')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(accountant_movements)')}
        if 'item_code' not in columns:
            connection.execute('ALTER TABLE accountant_movements ADD COLUMN item_code TEXT')
        return connection

    def reserves(self, day: date):
        from .reserves import reserve_summary
        return reserve_summary(self, day)

    def reserve_entry(self, day, account, kind, amount, note, *, cashier_amount=None):
        from .reserves import add_reserve_entry
        return add_reserve_entry(self, day, account, kind, amount, note, cashier_amount)

    def set_monthly_plan(self, day, amount, note):
        from .reserves import set_monthly_plan
        return set_monthly_plan(self, day, amount, note)

    def record_handover(self, day: date, amount: Decimal):
        with closing(self._open()) as connection, connection:
            connection.execute('INSERT INTO accountant_handover_days VALUES (?, ?, ?) '
                               'ON CONFLICT(day) DO UPDATE SET amount=excluded.amount, '
                               'checked_at=excluded.checked_at',
                               (day.isoformat(), str(amount), datetime.now().isoformat()))

    def handover_for_day(self, day: date) -> Decimal | None:
        with closing(self._open()) as connection:
            row = connection.execute('SELECT amount FROM accountant_handover_days WHERE day = ?',
                                     (day.isoformat(),)).fetchone()
        return Decimal(row[0]) if row else None

    def delete_handover(self, day: date):
        with closing(self._open()) as connection, connection:
            connection.execute('DELETE FROM accountant_handover_days WHERE day = ?', (day.isoformat(),))

    def set_cash_opening(self, day: date, amount, note):
        value = amount_value(amount, allow_zero=True)
        note = required_text(note, 'основание начального остатка')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                handover = connection.execute('SELECT 1 FROM accountant_handover_days WHERE day = ?',
                                              (day.isoformat(),)).fetchone()
                if handover is None:
                    raise LedgerError('Сначала запишите передачу кассы за первый день учёта.')
                connection.execute('INSERT INTO accountant_cash_opening VALUES (1,?,?,?,?)',
                                   (day.isoformat(), str(value), note, datetime.now().isoformat()))
                self._check_known_future_balances(connection, day)
                connection.commit()
            except sqlite3.IntegrityError:
                connection.rollback()
                raise LedgerError('Начальный остаток денег уже указан.') from None
            except Exception:
                connection.rollback()
                raise

    def cash_opening(self):
        with closing(self._open()) as connection:
            row = connection.execute('SELECT day,amount,note FROM accountant_cash_opening WHERE id=1').fetchone()
        return dict(day=row[0], amount=row[1], note=row[2]) if row else None

    def cash_position(self, connection, day: date, start_day: date | None = None):
        """Carry verified daily handovers forward; never silently fill an unobserved day."""
        rows = connection.execute('SELECT day, amount FROM accountant_handover_days '
                                  'WHERE day <= ? ORDER BY day', (day.isoformat(),)).fetchall()
        if start_day is not None:
            rows = [row for row in rows if row[0] >= start_day.isoformat()]
        anchor = connection.execute('SELECT day,amount FROM accountant_cash_opening WHERE id=1').fetchone()
        if anchor and day.isoformat() >= anchor[0]:
            rows = [row for row in rows if row[0] >= anchor[0]]
        if not rows:
            return None, None, day.isoformat(), None
        first = date.fromisoformat(rows[0][0])
        expected = first
        for recorded, _ in rows:
            if date.fromisoformat(recorded) != expected:
                return None, None, expected.isoformat(), first.isoformat()
            expected = date.fromordinal(expected.toordinal() + 1)
        if expected < day:
            return None, None, expected.isoformat(), first.isoformat()
        opening = Decimal(anchor[1]) if anchor and first.isoformat() == anchor[0] else Decimal(0)
        opening += sum((Decimal(value) for recorded, value in rows if recorded < day.isoformat()), Decimal(0))
        for cutoff, kind, amount in connection.execute(
                "SELECT day, kind, amount FROM accountant_movements WHERE day >= ? AND day <= ? "
                "AND kind IN ('other_expense','procurement_advance','other_receipt')",
                (first.isoformat(), day.isoformat())):
            if cutoff < day.isoformat():
                opening += Decimal(amount) if kind == 'other_receipt' else -Decimal(amount)
        for cutoff, amount in connection.execute(
                'SELECT paid_day, amount FROM accountant_salary_payments WHERE paid_day >= ? AND paid_day <= ?',
                (first.isoformat(), day.isoformat())):
            if cutoff < day.isoformat():
                opening -= Decimal(amount)
        for cutoff, amount in connection.execute(
                "SELECT day, amount FROM accountant_reserves WHERE kind='transfer' AND day >= ? AND day <= ?",
                (first.isoformat(), day.isoformat())):
            if cutoff < day.isoformat():
                opening -= Decimal(amount)
        if rows[-1][0] != day.isoformat():
            return opening, None, day.isoformat(), first.isoformat()
        receipts = sum((Decimal(row[0]) for row in connection.execute(
            "SELECT amount FROM accountant_movements WHERE day = ? AND kind = 'other_receipt'",
            (day.isoformat(),))), Decimal(0))
        return (opening, opening + Decimal(rows[-1][1]) + receipts - self._daily_outflows(connection, day),
                None, first.isoformat())

    def available_cash(self, connection, day: date, cashier_amount: Decimal | None):
        if cashier_amount is None:
            return self._cash_balance(connection, day)
        opening, closing, missing, _ = self.cash_position(connection, day)
        if missing == day.isoformat() and not connection.execute(
                'SELECT 1 FROM accountant_handover_days LIMIT 1').fetchone():
            connection.execute('INSERT INTO accountant_handover_days VALUES (?,?,?)',
                               (day.isoformat(), str(cashier_amount), datetime.now().isoformat()))
            opening, closing, missing, _ = self.cash_position(connection, day)
        if missing:
            raise LedgerError(f'Для переноса остатка загрузите данные кассира за {missing}.')
        return closing

    def _check_known_future_balances(self, connection, day: date):
        for (recorded,) in connection.execute(
                'SELECT day FROM accountant_handover_days WHERE day>=? ORDER BY day',
                (day.isoformat(),)):
            _, balance, missing, _ = self.cash_position(connection, date.fromisoformat(recorded))
            if missing is None and balance < 0:
                raise LedgerError('Операция делает остаток отрицательным в последующие дни.')

    def debt_summary(self, day: date):
        from .debts import debt_summary
        return debt_summary(self, day)

    def record_debt(self, day, item_code, note, total, paid, *, cashier_amount=None):
        from .debts import record_debt
        return record_debt(self, day, item_code, note, total, paid, cashier_amount)

    def pay_debt(self, debt_id, day, amount, *, cashier_amount=None):
        from .debts import pay_debt
        return pay_debt(self, debt_id, day, amount, cashier_amount)

    def exceptions_for_day(self, day: date) -> set[int]:
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT employee_id FROM accountant_exceptions WHERE day = ?',
                                      (day.isoformat(),)).fetchall()
        return {row[0] for row in rows}

    def grant_exception(self, employee_id: int, day: date, reason: str, approver: str):
        reason = required_text(reason, 'причину исключения')
        approver = required_text(approver, 'имя подтвердившего')
        with closing(self._open()) as connection:
            with connection:
                try:
                    connection.execute('INSERT INTO accountant_exceptions '
                                       '(employee_id, day, reason, approver, created_at) VALUES (?, ?, ?, ?, ?)',
                                       (employee_id, day.isoformat(), reason, approver, datetime.now().isoformat()))
                except sqlite3.IntegrityError:
                    raise LedgerError('Однодневное исключение уже использовано для этого сотрудника.') from None

    def confirm_payroll(self, day: date, rows: list[PayrollRow], approver: str) -> bool:
        approver = required_text(approver, 'имя подтвердившего')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if connection.execute('SELECT 1 FROM accountant_payroll_days WHERE day = ?',
                                      (day.isoformat(),)).fetchone():
                    connection.rollback()
                    return False
                connection.execute('INSERT INTO accountant_payroll_days (day, approver, confirmed_at) '
                                   'VALUES (?, ?, ?)', (day.isoformat(), approver, datetime.now().isoformat()))
                for row in rows:
                    if row.payable is None or row.payable <= 0:
                        continue
                    if row.rate is None:
                        raise LedgerError('Нельзя подтвердить начисление без ставки.')
                    connection.execute('INSERT INTO accountant_accruals '
                                       '(work_day, employee_id, employee_name, group_name, attendance_status, rate, amount) '
                                       'VALUES (?, ?, ?, ?, ?, ?, ?)',
                                       (day.isoformat(), row.employee_id, row.name, row.group_name,
                                        row.status, str(row.rate), str(row.payable)))
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def accruals(self, day: date) -> list[dict]:
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT id, work_day, employee_id, employee_name, group_name, '
                                      'attendance_status, rate, amount FROM accountant_accruals '
                                      'WHERE work_day <= ? ORDER BY work_day, id', (day.isoformat(),)).fetchall()
            result = []
            for row in rows:
                paid = sum((Decimal(p[0]) for p in connection.execute(
                    'SELECT amount FROM accountant_salary_payments WHERE accrual_id = ? AND paid_day <= ?',
                    (row[0], day.isoformat()))), Decimal(0))
                result.append(dict(id=row[0], work_day=row[1], employee_id=row[2],
                                   name=row[3], group=row[4], status=row[5], rate=str(row[6]),
                                   amount=str(row[7]), paid=str(paid),
                                   debt=str(Decimal(row[7]) - paid)))
        return result

    @staticmethod
    def _cash_balance(connection, day: date) -> Decimal:
        movements = connection.execute('SELECT kind, amount FROM accountant_movements WHERE day <= ?',
                                       (day.isoformat(),)).fetchall()
        balance = sum((Decimal(amount) if kind in ('opening', 'cashier_transfer', 'other_receipt') else -Decimal(amount)
                       for kind, amount in movements), Decimal(0))
        payments = connection.execute('SELECT amount FROM accountant_salary_payments WHERE paid_day <= ?',
                                      (day.isoformat(),)).fetchall()
        transfers = sum((Decimal(row[0]) for row in connection.execute(
            "SELECT amount FROM accountant_reserves WHERE kind = 'transfer' AND day <= ?",
            (day.isoformat(),))), Decimal(0))
        return balance - sum((Decimal(row[0]) for row in payments), Decimal(0)) - transfers

    @classmethod
    def _check_future_balances(cls, connection, day: date):
        dates = connection.execute(
            'SELECT day FROM accountant_movements WHERE day >= ? '
            'UNION SELECT paid_day FROM accountant_salary_payments WHERE paid_day >= ?',
            (day.isoformat(), day.isoformat())).fetchall()
        if any(cls._cash_balance(connection, date.fromisoformat(row[0])) < 0 for row in dates):
            raise LedgerError('Операция делает остаток отрицательным в последующие дни.')

    @staticmethod
    def _daily_outflows(connection, day: date) -> Decimal:
        spent = sum((Decimal(row[0]) for row in connection.execute(
            "SELECT amount FROM accountant_movements WHERE day = ? AND kind IN ('other_expense', 'procurement_advance')",
            (day.isoformat(),))), Decimal(0))
        salaries = sum((Decimal(row[0]) for row in connection.execute(
            'SELECT amount FROM accountant_salary_payments WHERE paid_day = ?',
            (day.isoformat(),))), Decimal(0))
        transfers = sum((Decimal(row[0]) for row in connection.execute(
            "SELECT amount FROM accountant_reserves WHERE kind = 'transfer' AND day = ?",
            (day.isoformat(),))), Decimal(0))
        return spent + salaries + transfers

    def _movement(self, day: date, kind: str, description: str, amount,
                  *, reference: str | None = None, item_code: str | None = None,
                  allow_zero=False, cashier_amount: Decimal | None = None):
        value = amount_value(amount, allow_zero=allow_zero)
        description = required_text(description, 'назначение')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                if kind not in ('opening', 'cashier_transfer', 'other_receipt'):
                    available = self.available_cash(connection, day, cashier_amount)
                    if available < value:
                        raise LedgerError('На выбранный день недостаточно денег от кассира.')
                cursor = connection.execute('INSERT INTO accountant_movements '
                                            '(day, kind, description, amount, item_code, reference, created_at) '
                                            'VALUES (?, ?, ?, ?, ?, ?, ?)',
                                            (day.isoformat(), kind, description, str(value), item_code, reference,
                                             datetime.now().isoformat()))
                if cashier_amount is None:
                    self._check_future_balances(connection, day)
                else:
                    self._check_known_future_balances(connection, day)
                connection.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                connection.rollback()
                raise LedgerError('Эта передача уже подтверждена.') from None
            except Exception:
                connection.rollback()
                raise

    def add_opening(self, day: date, amount, note: str):
        return self._movement(day, 'opening', note, amount, reference='initial', allow_zero=True)

    def confirm_transfer(self, cashier_day: date, received_day: date, amount):
        if received_day < cashier_day:
            raise LedgerError('Дата получения не может быть раньше кассового дня.')
        return self._movement(received_day, 'cashier_transfer',
                              f'Касса за {cashier_day.isoformat()}', amount,
                              reference=cashier_day.isoformat(), allow_zero=True)

    def add_expense(self, day: date, item_code: str, note: str, amount,
                    *, cashier_amount: Decimal | None = None):
        if item_code not in ITEMS:
            raise LedgerError('Выберите наименование затрат из справочника.')
        note = note.strip() if isinstance(note, str) else ''
        if len(note) > 160:
            raise LedgerError('Пояснение должно быть не длиннее 160 символов.')
        label = ITEMS[item_code][1]
        description = f'{label} · {note}' if note else label
        return self._movement(day, 'other_expense', description, amount, item_code=item_code,
                              cashier_amount=cashier_amount)

    def add_income(self, day: date, item_code: str, note: str, amount):
        if item_code != 'income_other':
            raise LedgerError('Для этого типа используйте отдельную операцию кассы.')
        note = note.strip() if isinstance(note, str) else ''
        if len(note) > 160:
            raise LedgerError('Пояснение должно быть не длиннее 160 символов.')
        description = f'Прочие поступления · {note}' if note else 'Прочие поступления'
        return self._movement(day, 'other_receipt', description, amount, item_code=item_code,
                              allow_zero=True)

    def update_movement(self, movement_id, day: date, item_code: str, note: str, amount):
        value = amount_value(amount, allow_zero=True)
        note = required_text(note, 'назначение')
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute(
                    'SELECT kind, amount, item_code FROM accountant_movements WHERE id = ?',
                    (movement_id,)).fetchone()
                if row is None or row[0] not in ('other_expense', 'other_receipt'):
                    raise LedgerError('Эту операцию нельзя изменить здесь.')
                kind, old_amount, old_code = row
                if kind == 'other_receipt':
                    if item_code != 'income_other':
                        raise LedgerError('Выберите тип прочего поступления.')
                    description = f'Прочие поступления · {note}' if note else 'Прочие поступления'
                else:
                    if item_code not in ITEMS:
                        raise LedgerError('Выберите наименование затрат из справочника.')
                    description = f'{ITEMS[item_code][1]} · {note}' if note else ITEMS[item_code][1]
                linked = connection.execute(
                    'SELECT debt_id FROM accountant_debt_payments WHERE movement_id = ?',
                    (movement_id,)).fetchone()
                if linked:
                    debt_total = connection.execute(
                        'SELECT total_amount FROM accountant_debts WHERE id = ?', (linked[0],)).fetchone()[0]
                    other_paid = sum((Decimal(item[0]) for item in connection.execute(
                        'SELECT amount FROM accountant_debt_payments WHERE debt_id = ? AND movement_id != ?',
                        (linked[0], movement_id))), Decimal(0))
                    if value > Decimal(debt_total) - other_paid:
                        raise LedgerError('Выплата превышает оставшийся долг.')
                    connection.execute('UPDATE accountant_debt_payments SET amount = ? WHERE movement_id = ?',
                                       (str(value), movement_id))
                connection.execute('UPDATE accountant_movements SET description=?, amount=?, item_code=? '
                                   'WHERE id=?', (description, str(value), item_code, movement_id))
                handover = connection.execute(
                    'SELECT amount FROM accountant_handover_days WHERE day = ?', (day.isoformat(),)).fetchone()
                cashier_amount = Decimal(handover[0]) if handover else None
                available = self.available_cash(connection, day, cashier_amount)
                if kind == 'other_expense' and available < 0:
                    raise LedgerError('Операция делает остаток отрицательным.')
                if cashier_amount is None:
                    self._check_future_balances(connection, day)
                else:
                    self._check_known_future_balances(connection, day)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def update_salary_payment(self, payment_id, day: date, amount):
        value = amount_value(amount)
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                row = connection.execute(
                    'SELECT a.amount, p.amount FROM accountant_salary_payments p '
                    'JOIN accountant_accruals a ON a.id = p.accrual_id WHERE p.id = ?',
                    (payment_id,)).fetchone()
                if row is None:
                    raise LedgerError('Выплата не найдена.')
                paid_elsewhere = sum((Decimal(item[0]) for item in connection.execute(
                    'SELECT amount FROM accountant_salary_payments WHERE accrual_id = ? AND id != '
                    '(SELECT accrual_id FROM accountant_salary_payments WHERE id = ?)',
                    (connection.execute('SELECT accrual_id FROM accountant_salary_payments WHERE id = ?',
                                        (payment_id,)).fetchone()[0], payment_id))), Decimal(0))
                if value > Decimal(row[0]) - paid_elsewhere:
                    raise LedgerError('Выплата превышает начисленную сумму.')
                connection.execute('UPDATE accountant_salary_payments SET amount = ? WHERE id = ?',
                                   (str(value), payment_id))
                handover = connection.execute(
                    'SELECT amount FROM accountant_handover_days WHERE day = ?', (day.isoformat(),)).fetchone()
                cashier_amount = Decimal(handover[0]) if handover else None
                if self.available_cash(connection, day, cashier_amount) < 0:
                    raise LedgerError('Операция делает остаток отрицательным.')
                if cashier_amount is None:
                    self._check_future_balances(connection, day)
                else:
                    self._check_known_future_balances(connection, day)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def give_procurement(self, day: date, recipient: str, purpose: str, amount,
                         *, cashier_amount: Decimal | None = None):
        recipient = required_text(recipient, 'получателя')
        purpose = required_text(purpose, 'назначение закупки')
        return self._movement(day, 'procurement_advance', f'{recipient}: {purpose}', amount,
                              cashier_amount=cashier_amount)

    def pay_salary(self, accrual_id: int, paid_day: date, amount,
                   *, cashier_amount: Decimal | None = None):
        value = amount_value(amount)
        with closing(self._open()) as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                accrual = connection.execute('SELECT work_day, amount FROM accountant_accruals WHERE id = ?',
                                             (accrual_id,)).fetchone()
                if accrual is None:
                    raise LedgerError('Начисление не найдено.')
                if paid_day.isoformat() < accrual[0]:
                    raise LedgerError('Выплата не может быть раньше рабочего дня.')
                already_paid = sum((Decimal(row[0]) for row in connection.execute(
                    'SELECT amount FROM accountant_salary_payments WHERE accrual_id = ?', (accrual_id,))),
                    Decimal(0))
                if value > Decimal(accrual[1]) - already_paid:
                    raise LedgerError('Выплата превышает оставшийся долг сотруднику.')
                available = self.available_cash(connection, paid_day, cashier_amount)
                if available < value:
                    raise LedgerError('На выбранный день недостаточно денег от кассира.')
                cursor = connection.execute('INSERT INTO accountant_salary_payments '
                                            '(accrual_id, paid_day, amount, created_at) VALUES (?, ?, ?, ?)',
                                            (accrual_id, paid_day.isoformat(), str(value), datetime.now().isoformat()))
                if cashier_amount is None:
                    self._check_future_balances(connection, paid_day)
                else:
                    self._check_known_future_balances(connection, paid_day)
                connection.commit()
                return cursor.lastrowid
            except Exception:
                connection.rollback()
                raise

    def summary(self, day: date) -> dict:
        with closing(self._open()) as connection:
            accrued_on_day = sum((Decimal(row[0]) for row in connection.execute(
                'SELECT amount FROM accountant_accruals WHERE work_day = ?', (day.isoformat(),))), Decimal(0))
            paid_on_day = sum((Decimal(row[0]) for row in connection.execute(
                'SELECT amount FROM accountant_salary_payments WHERE paid_day = ?', (day.isoformat(),))), Decimal(0))
            movements = [dict(id=row[0], type=row[1], operation='movement', description=row[2], amount=row[3],
                              day=row[4], item_code=row[5])
                         for row in connection.execute('SELECT id, kind, description, amount, day, item_code '
                                                       'FROM accountant_movements WHERE day = ? ORDER BY id',
                                                       (day.isoformat(),))]
            for row in connection.execute(
                                 'SELECT p.id, a.employee_name, a.work_day, p.amount, a.group_name '
                                 'FROM accountant_salary_payments p '
                                 'JOIN accountant_accruals a ON a.id = p.accrual_id '
                                 'WHERE p.paid_day = ? ORDER BY p.id', (day.isoformat(),)):
                code = ('salary_technical' if row[4] == 'Уборка' else
                        'salary_cashier' if row[4] == 'Касса' else 'salary_staff')
                label = ITEMS[code][1]
                movements.append(dict(id=row[0], type='salary_payment', operation='salary_payment',
                                      description=f'{label} · {row[1]} · за {row[2]}',
                                      amount=row[3], day=day.isoformat(), item_code=code))
            balance = self._cash_balance(connection, day)
            prior_balance = self._cash_balance(connection, date.fromordinal(day.toordinal() - 1))
            confirmed = connection.execute('SELECT approver, confirmed_at FROM accountant_payroll_days '
                                           'WHERE day = ?', (day.isoformat(),)).fetchone()
        accruals = self.accruals(day)
        debt = sum((Decimal(item['debt']) for item in accruals), Decimal(0))
        opening_today = sum((Decimal(item['amount']) for item in movements
                             if item['type'] == 'opening'), Decimal(0))
        received = sum((Decimal(item['amount']) for item in movements
                        if item['type'] == 'cashier_transfer'), Decimal(0))
        other_outflows = sum((Decimal(item['amount']) for item in movements
                              if item['type'] in ('other_expense', 'procurement_advance')), Decimal(0))
        cash_flow = dict(opening_balance=str(prior_balance + opening_today),
                         received_from_cashier=str(received),
                         salary_paid=str(paid_on_day),
                         other_outflows=str(other_outflows),
                         closing_balance=str(balance))
        salary_categories = {}
        for item in movements:
            if item['type'] == 'salary_payment':
                label = ITEMS[item['item_code']][1]
                salary_categories[label] = salary_categories.get(label, Decimal(0)) + Decimal(item['amount'])
        return dict(accrued_on_day=accrued_on_day, salary_paid_on_day=paid_on_day,
                    salary_debt=debt, cash_balance=balance, movements=movements,
                    cash_flow=cash_flow,
                    salary_categories={name: str(amount) for name, amount in salary_categories.items()},
                    accruals=accruals, payroll_confirmed=bool(confirmed),
                    payroll_approver=confirmed[0] if confirmed else None)

    def daily_summary(self, day: date, cashier_amount: Decimal | None, *, carry_history: bool = True,
                      carry_start: date | None = None) -> dict:
        """Verified carried cash plus today's handover less actual cash outflows."""
        if cashier_amount is not None:
            self.record_handover(day, cashier_amount)
        result = self.summary(day)
        movements = [item for item in result['movements']
                     if item['type'] in ('other_expense', 'other_receipt', 'procurement_advance', 'salary_payment')]
        with closing(self._open()) as connection:
            for entry_id, note, amount in connection.execute(
                    "SELECT id, note, amount FROM accountant_reserves WHERE kind = 'transfer' AND day = ?",
                    (day.isoformat(),)):
                movements.append(dict(id=entry_id, type='reserve_transfer', description=note,
                                      amount=amount, day=day.isoformat(), item_code=None))
        if cashier_amount is not None:
            movements.insert(0, dict(id=None, type='auto_cashier',
                                     description=f'Касса за {(day - timedelta(days=1)).strftime("%d.%m.%Y")}',
                                     amount=str(cashier_amount), day=day.isoformat(), item_code=None))
        other_receipts = sum((Decimal(item['amount']) for item in movements
                              if item['type'] == 'other_receipt'), Decimal(0))
        other = sum((Decimal(item['amount']) for item in movements
                     if item['type'] in ('other_expense', 'procurement_advance', 'reserve_transfer')), Decimal(0))
        paid = result['salary_paid_on_day']
        result['salary_recorded_on_day'] = paid + sum(
            (Decimal(item['amount']) for item in movements
             if item['type'] == 'other_expense'
             and ITEMS.get(item['item_code'], (None,))[0] == 'salary'), Decimal(0))
        with closing(self._open()) as connection:
            opening, remaining, missing, first_day = self.cash_position(connection, day, carry_start)
            if not carry_history and cashier_amount is not None:
                opening = Decimal(0)
                remaining = cashier_amount + other_receipts - self._daily_outflows(connection, day)
                missing = None
        if opening is not None and opening > 0:
            movements.insert(0, dict(id=None, type='opening',
                                     description='Остаток на начало дня',
                                     amount=str(opening), day=day.isoformat(), item_code=None))
        debts = self.debt_summary(day)
        result.update(debts)
        result['cash_opening'] = self.cash_opening()
        result['movements'] = movements
        result['cash_balance'] = remaining
        result['cash_flow'] = dict(opening_balance=str(opening) if opening is not None else None,
                                   received_from_cashier=str(cashier_amount) if cashier_amount is not None else None,
                                   other_receipts=str(other_receipts),
                                   salary_paid=str(paid), other_outflows=str(other),
                                   closing_balance=str(remaining) if remaining is not None else None,
                                   missing_day=missing, first_day=first_day)
        return result
