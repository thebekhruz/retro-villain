"""One actual payout day, several explicitly selected employee shifts.

Uses the existing accrual/payment ledger. Selection is never an accrual; only
confirmed rows are written, atomically, with their historical rate frozen.
"""
import hashlib
import json
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from retro.accounting_period import SALARY_SHIFT_START
from . import work_period
from .audit import record_audit
from .ledger import LedgerError, amount_value, closure_row, ensure_open, lock_day, now_stamp, plain
from .payroll import PRESENT_STATUSES
from .salary_day import BATCH_STATUS, MANUAL_STATUS, _assert_day, cell_attendance


def days_between(first, last, paid_day):
    _assert_day(paid_day)
    if first < SALARY_SHIFT_START or last < first or (last-first).days > 30:
        raise LedgerError('Выберите от 1 до 31 смены в пределах текущего учёта.')
    from .salary_day import today_tashkent
    if last >= today_tashkent() or last > paid_day:
        raise LedgerError('Выберите завершённые смены не позже дня выдачи денег.')
    return [first + timedelta(days=i) for i in range((last-first).days+1)]


def _state(connection, first, last):
    """Bounded queries for the whole selection, including on Postgres."""
    employees = dict((r[0], r[1:]) for r in connection.execute(
        'SELECT id,name,role,group_name,rate FROM accountant_employees ORDER BY source_row'))
    versions, payments, extras = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in connection.execute(
            'SELECT employee_id,effective_day,name,role,group_name,rate,deleted '
            'FROM accountant_employee_versions WHERE effective_day<=? ORDER BY effective_day', (last.isoformat(),)):
        versions[row[0]].append(row[1:])
    earned = {(r[0], r[1]): r[2:] for r in connection.execute(
        'SELECT employee_id,work_day,id,amount,attendance_status,rate FROM accountant_accruals '
        'WHERE work_day>=? AND work_day<=?', (first.isoformat(), last.isoformat()))}
    for row in connection.execute(
            'SELECT a.employee_id,a.work_day,p.id,p.paid_day,p.amount FROM accountant_salary_payments p '
            'JOIN accountant_accruals a ON a.id=p.accrual_id WHERE a.work_day>=? AND a.work_day<=? ORDER BY p.id',
            (first.isoformat(), last.isoformat())):
        payments[(row[0], row[1])].append(row[2:])
    for row in connection.execute('SELECT employee_id,work_day,id FROM accountant_extra_payouts WHERE work_day>=? AND work_day<=?',
                                  (first.isoformat(), last.isoformat())):
        extras[(row[0], row[1])].append(row[2:])
    marks = {(r[0], r[1]): bool(r[2]) for r in connection.execute(
        'SELECT employee_id,work_day,present FROM hikvision_manual_absences WHERE work_day>=? AND work_day<=?',
        (first.isoformat(), last.isoformat()))}
    return dict(employees=employees, versions=versions, earned=earned, payments=payments, extras=extras,
                marks=marks, periods=work_period.periods(connection, employees), closed=closure_row(connection))


def _candidate(state, employee_id, work, paid_day, attendance):
    employee = state['employees'].get(employee_id)
    if employee is None:
        raise LedgerError('Сотрудник не найден или находится в архиве.')
    versions = [v for v in state['versions'][employee_id] if v[0] <= work.isoformat()]
    version = versions[-1] if versions else None
    person = version[1:5] if version and not version[5] else employee
    key = (employee_id, work.isoformat())
    earned, payments = state['earned'].get(key), state['payments'][key]
    rate = Decimal(earned[3] if earned else person[3] or '0')
    due = Decimal(earned[1]) if earned else rate
    paid = sum((Decimal(row[2]) for row in payments), Decimal(0))
    remaining = max(Decimal(0), due-paid)
    att = attendance.get(work, {}).get(employee_id)
    status = cell_attendance(att) if att else dict(status='unknown', time=None, source='unavailable')
    mark = state['marks'].get(key)
    if mark is not None:
        status = dict(status='manual' if mark else 'absent', time=None,
                      source='manual_present' if mark else 'manual_absent')
    period = state['periods'].get(employee_id)
    outside = work_period.outside_text(period[0], period[1], period[2], work, 'cell') if period and not work_period.in_period(work, *period[1:]) else None
    closed = state['closed']
    reason = outside or ''
    if closed and min(work, paid_day).isoformat() <= closed[1]:
        reason = 'Месяц закрыт'
    elif not reason and earned and remaining == 0:
        reason = 'Уже выдано' if paid else 'Нет начисления к выплате'
    elif not reason and not earned and status['source'] not in PRESENT_STATUSES:
        reason = 'Не пришёл' if status['status'] == 'absent' else 'Нет данных — уточните смену в «Сотрудниках»'
    elif not reason and rate <= 0:
        reason = 'Нет ставки — укажите её в «Сотрудниках»'
    extras = state['extras'][key]
    if not reason and extras:
        reason = 'Есть доп. выплата за смену — сначала сверьте её назначение'
    fingerprint = hashlib.sha256(json.dumps(
        [list(person), list(earned) if earned else None, [list(p) for p in payments],
         status, reason, [row[0] for row in extras]], sort_keys=True).encode()).hexdigest()
    return dict(employee_id=employee_id, name=person[0], role=person[1], group=person[2],
                work_day=work.isoformat(), rate=plain(rate), accrued=plain(due), paid=plain(paid),
                remaining=plain(remaining), payments=[dict(id=p[0], paid_day=p[1], amount=p[2]) for p in payments],
                attendance=status, reason=reason, editable=not bool(reason), version=fingerprint,
                accrual_id=earned[0] if earned else None)


def preview(finance, first, last, paid_day, attendance):
    days = days_between(first, last, paid_day)
    with closing(finance._open()) as connection:
        state = _state(connection, first, last)
        rows = [_candidate(state, employee, day, paid_day, attendance) for employee in state['employees'] for day in days]
    return dict(paid_day=paid_day.isoformat(), days=[d.isoformat() for d in days], rows=rows)


def _aggregate_guard(connection, paid_day):
    if connection.execute(
            "SELECT 1 FROM accountant_movements WHERE day=? AND kind='other_expense' "
            "AND item_code IN ('salary_cashier','salary_staff','salary_technical','salary_carryover')",
            (paid_day.isoformat(),)).fetchone():
        raise LedgerError('За день выдачи уже записана общая зарплата без сотрудников. '
                          'Сначала сверьте её в «Финансах дня», чтобы не задвоить расход.')


def record(finance, paid_day, items, attendance, *, cashier_amount=None, actor=None):
    if not items or len(items) > 1000:
        raise LedgerError('Выберите от 1 до 1000 выплат.')
    days_between(min(i['work_day'] for i in items), max(i['work_day'] for i in items), paid_day)
    keys = [(i['employee_id'], i['work_day']) for i in items]
    if len(set(keys)) != len(keys):
        raise LedgerError('Одна смена сотрудника выбрана дважды.')
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            for day in sorted({paid_day} | {key[1] for key in keys}):
                lock_day(connection, day)
                ensure_open(connection, day)
            for employee_id in sorted({key[0] for key in keys}):
                work_period.lock_employee(connection, employee_id)
            _aggregate_guard(connection, paid_day)
            state = _state(connection, min(k[1] for k in keys), max(k[1] for k in keys))
            ready = []
            for item in items:
                row = _candidate(state, item['employee_id'], item['work_day'], paid_day, attendance)
                if item['version'] != row['version']:
                    raise LedgerError(f"Данные {row['name']} за {item['work_day']:%d.%m} уже изменились. Обновите список смен.")
                if not row['editable']:
                    raise LedgerError(f"{row['name']}: {row['reason']}.")
                value = amount_value(item['amount'])
                if value > Decimal(row['remaining']):
                    raise LedgerError(f"{row['name']}: сумма больше остатка за смену. Доплаты вносите в «Доп. выплаты».")
                ready.append((row, value))
            total = sum((value for _, value in ready), Decimal(0))
            finance._require_cash(connection, paid_day, total, cashier_amount)
            result = []
            for row, value in ready:
                accrual_id = row['accrual_id']
                before = finance._row_dict(connection, 'accountant_accruals', accrual_id) if accrual_id else None
                if accrual_id is None:
                    accrual_id = connection.execute(
                        'INSERT INTO accountant_accruals '
                        '(work_day,employee_id,employee_name,group_name,attendance_status,rate,amount) VALUES (?,?,?,?,?,?,?)',
                        (row['work_day'], row['employee_id'], row['name'], row['group'], BATCH_STATUS,
                         row['rate'], row['accrued'])).lastrowid
                else:
                    connection.execute('UPDATE accountant_accruals SET attendance_status=? WHERE id=?',
                                       (BATCH_STATUS, accrual_id))
                record_audit(connection, 'accrual', accrual_id, 'update' if before else 'create', before,
                             finance._row_dict(connection, 'accountant_accruals', accrual_id))
                payment_id = connection.execute(
                    'INSERT INTO accountant_salary_payments (accrual_id,paid_day,amount,created_at) VALUES (?,?,?,?)',
                    (accrual_id, paid_day.isoformat(), plain(value), now_stamp())).lastrowid
                record_audit(connection, 'salary_payment', payment_id, 'create', None,
                             finance._row_dict(connection, 'accountant_salary_payments', payment_id))
                result.append(dict(id=payment_id, employee_id=row['employee_id'], name=row['name'],
                                   work_day=row['work_day'], paid_day=paid_day.isoformat(), amount=plain(value)))
            finance._check_cash_balances(connection, paid_day)
            record_audit(connection, 'salary_batch', uuid4().hex, 'create', None,
                         dict(actor=actor, paid_day=paid_day.isoformat(), payments=result, total=plain(total)))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return dict(paid_day=paid_day.isoformat(), payments=result, total=plain(total))


def update(finance, payment_id, amount, expected_amount, *, cashier_amount=None, actor=None):
    value = amount_value(amount, allow_zero=True)
    expected = amount_value(expected_amount, allow_zero=True)
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            sql = ('SELECT p.accrual_id,p.paid_day,p.amount,a.work_day,a.employee_id,a.amount,a.attendance_status '
                   'FROM accountant_salary_payments p JOIN accountant_accruals a ON a.id=p.accrual_id WHERE p.id=?')
            row = connection.execute(sql, (payment_id,)).fetchone()
            if row is None:
                raise LedgerError('Выплата не найдена. Обновите ведомость.')
            paid_day, work = date.fromisoformat(row[1]), date.fromisoformat(row[3])
            _assert_day(paid_day)
            for day in sorted({work, paid_day}):
                lock_day(connection, day)
                ensure_open(connection, day)
            work_period.lock_employee(connection, row[4])
            row = connection.execute(sql, (payment_id,)).fetchone()
            if row is None or row[6] not in (BATCH_STATUS, MANUAL_STATUS):
                raise LedgerError('Выплата уже изменилась. Обновите ведомость.')
            current = Decimal(row[2])
            if current != expected:
                raise LedgerError('Сумма уже изменилась. Обновите ведомость.')
            already = sum((Decimal(p[0]) for p in connection.execute(
                'SELECT amount FROM accountant_salary_payments WHERE accrual_id=? AND id<>?',
                (row[0], payment_id))), Decimal(0))
            manual = row[6] == MANUAL_STATUS
            if manual and (already or paid_day != work + timedelta(days=1) or current != Decimal(row[5])):
                raise LedgerError('Выплата уже изменилась. Обновите ведомость.')
            if not manual and value > Decimal(row[5])-already:
                raise LedgerError('Сумма больше остатка за смену.')
            if value > current:
                outside = work_period.guard(connection, row[4], work, 'cell')
                if outside:
                    raise LedgerError(outside)
                _aggregate_guard(connection, paid_day)
                finance._require_cash(connection, paid_day, value-current, cashier_amount)
            before = finance._row_dict(connection, 'accountant_salary_payments', payment_id)
            if value:
                connection.execute('UPDATE accountant_salary_payments SET amount=? WHERE id=?', (plain(value), payment_id))
            else:
                connection.execute('DELETE FROM accountant_salary_payments WHERE id=?', (payment_id,))
            if manual:
                # A usual next-day payment may share a cell with a deferred one.
                # Keep its original one-accrual/one-payment correction semantics.
                accrual_before = finance._row_dict(connection, 'accountant_accruals', row[0])
                if value:
                    connection.execute('UPDATE accountant_accruals SET amount=? WHERE id=?', (plain(value), row[0]))
                else:
                    connection.execute('DELETE FROM accountant_accruals WHERE id=?', (row[0],))
                record_audit(connection, 'accrual', row[0], 'update' if value else 'delete', accrual_before,
                             finance._row_dict(connection, 'accountant_accruals', row[0]) if value else None)
            record_audit(connection, 'salary_payment', payment_id, 'update' if value else 'delete', before,
                         finance._row_dict(connection, 'accountant_salary_payments', payment_id) if value else None)
            record_audit(connection, 'salary_batch_edit', payment_id, 'update', None,
                         dict(actor=actor, amount=plain(value), paid_day=row[1], work_day=row[3]))
            finance._check_cash_balances(connection, paid_day)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return dict(id=payment_id, amount=plain(value), paid_day=paid_day.isoformat())
