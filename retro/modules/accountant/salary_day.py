"""Manual daily salaries: one paid-day cell, one accrual and one cash payment."""
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal

from retro.modules.cashier.service import today_tashkent

from .audit import record_audit
from .ledger import LedgerError, amount_value, closure_row, ensure_open, lock_day, now_stamp, plain

ENTRY_START = date(2026, 10, 7)
MANUAL_STATUS = 'manual_salary'


class SalaryCellChanged(LedgerError):
    pass


def _assert_day(paid_day):
    if paid_day < ENTRY_START:
        raise LedgerError('Ручной ввод зарплаты доступен с 07.10.2026; прежние выплаты только для чтения.')
    if paid_day > today_tashkent():
        raise LedgerError('Нельзя записать зарплату за будущий день.')


def _existing(connection, employee_id, work_day, paid_day):
    accrual = connection.execute(
        'SELECT id, attendance_status, amount FROM accountant_accruals '
        'WHERE employee_id=? AND work_day=?', (employee_id, work_day.isoformat())).fetchone()
    payments = connection.execute(
        'SELECT p.id,p.accrual_id,p.paid_day,p.amount FROM accountant_salary_payments p '
        'JOIN accountant_accruals a ON a.id=p.accrual_id '
        'WHERE a.employee_id=? AND (p.paid_day=? OR a.work_day=?) ORDER BY p.id',
        (employee_id, paid_day.isoformat(), work_day.isoformat())).fetchall()
    if accrual is None:
        if payments:
            raise SalaryCellChanged('За день уже есть выплата другой смены. Проверьте журнал зарплаты.')
        return None, None, Decimal(0)
    if (accrual[1] != MANUAL_STATUS or len(payments) != 1
            or payments[0][1] != accrual[0] or payments[0][2] != paid_day.isoformat()
            or Decimal(payments[0][3]) != Decimal(accrual[2])):
        raise SalaryCellChanged('За смену уже есть другое начисление или выплата. Проверьте журнал зарплаты.')
    return accrual[0], payments[0][0], Decimal(payments[0][3])


def set_cell(finance, paid_day, employee_id, amount, expected_amount, *, cashier_amount=None):
    _assert_day(paid_day)
    work_day = paid_day - timedelta(days=1)
    value = amount_value(amount, allow_zero=True)
    expected = amount_value(expected_amount, allow_zero=True)
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            for day in (work_day, paid_day):
                lock_day(connection, day)
                ensure_open(connection, day)
            if connection.execute('SELECT 1 FROM accountant_payroll_days WHERE day=?',
                                  (work_day.isoformat(),)).fetchone():
                raise SalaryCellChanged('Смена уже подтверждена в прежней ведомости. '
                                        'Её начисления нельзя изменить в ручной таблице.')
            employee = connection.execute(
                'SELECT name,group_name,rate FROM accountant_employees WHERE id=?',
                (employee_id,)).fetchone()
            if employee is None:
                raise LedgerError('Сотрудник не найден или находится в архиве.')
            version = connection.execute(
                'SELECT name,group_name,rate,deleted FROM accountant_employee_versions '
                'WHERE employee_id=? AND effective_day<=? ORDER BY effective_day DESC LIMIT 1',
                (employee_id, work_day.isoformat())).fetchone()
            if version is None or version[3]:
                raise LedgerError('Сотрудника нет в реестре за выбранную смену.')
            accrual_id, payment_id, current = _existing(connection, employee_id, work_day, paid_day)
            if value == current:
                connection.commit()
                return dict(date=paid_day.isoformat(), employee_id=employee_id, amount=plain(value),
                            work_day=work_day.isoformat(), editable=True, changed=False)
            if expected != current:
                raise SalaryCellChanged('Сумма уже изменилась. Обновите таблицу и повторите.')
            if value > current and connection.execute(
                    "SELECT 1 FROM accountant_movements WHERE day=? AND kind='other_expense' "
                    "AND item_code IN ('salary_cashier','salary_staff','salary_technical','salary_carryover')",
                    (paid_day.isoformat(),)).fetchone():
                raise SalaryCellChanged('За день выдачи уже записана общая зарплата без сотрудников. '
                                        'Сверьте её в журнале перед вводом по людям.')
            # Only the increase spends additional cash; decreases return cash.
            if value > current:
                finance._require_cash(connection, paid_day, value - current, cashier_amount)
            old_accrual = finance._row_dict(connection, 'accountant_accruals', accrual_id) if accrual_id else None
            old_payment = finance._row_dict(connection, 'accountant_salary_payments', payment_id) if payment_id else None
            if value == 0:
                connection.execute('DELETE FROM accountant_salary_payments WHERE id=?', (payment_id,))
                connection.execute('DELETE FROM accountant_accruals WHERE id=?', (accrual_id,))
            elif accrual_id is None:
                accrual_id = connection.execute(
                    'INSERT INTO accountant_accruals '
                    '(work_day,employee_id,employee_name,group_name,attendance_status,rate,amount) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (work_day.isoformat(), employee_id, version[0], version[1], MANUAL_STATUS,
                     str(version[2] or '0'), plain(value))).lastrowid
                payment_id = connection.execute(
                    'INSERT INTO accountant_salary_payments (accrual_id,paid_day,amount,created_at) '
                    'VALUES (?,?,?,?)', (accrual_id, paid_day.isoformat(), plain(value), now_stamp())).lastrowid
            else:
                connection.execute('UPDATE accountant_accruals SET amount=? WHERE id=?', (plain(value), accrual_id))
                connection.execute('UPDATE accountant_salary_payments SET amount=? WHERE id=?',
                                   (plain(value), payment_id))
            for entity, table, entity_id, before in (
                    ('accrual', 'accountant_accruals', accrual_id, old_accrual),
                    ('salary_payment', 'accountant_salary_payments', payment_id, old_payment)):
                after = finance._row_dict(connection, table, entity_id) if value else None
                record_audit(connection, entity, entity_id,
                             'delete' if not value else 'create' if before is None else 'update', before, after)
            finance._check_cash_balances(connection, paid_day)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return dict(date=paid_day.isoformat(), employee_id=employee_id, amount=plain(value),
                work_day=work_day.isoformat(), editable=True, changed=True)


def month_data(finance, first, last):
    today = today_tashkent()
    days = [(first + timedelta(days=index)).isoformat() for index in range((last-first).days+1)]
    with closing(finance._open()) as connection:
        current = connection.execute(
            'SELECT id,name,role,group_name,rate FROM accountant_employees ORDER BY source_row').fetchall()
        versions = defaultdict(list)
        for row in connection.execute(
                'SELECT employee_id,effective_day,name,role,group_name,rate,deleted '
                'FROM accountant_employee_versions WHERE effective_day<=? ORDER BY effective_day',
                (last.isoformat(),)):
            versions[row[0]].append(row)
        payments = connection.execute(
            'SELECT a.employee_id,a.employee_name,a.group_name,a.rate,p.paid_day,p.amount, '
            'a.work_day,a.attendance_status,a.id,p.id,a.amount '
            'FROM accountant_salary_payments p JOIN accountant_accruals a ON a.id=p.accrual_id '
            'WHERE p.paid_day>=? AND p.paid_day<=? ORDER BY p.id',
            (first.isoformat(), last.isoformat())).fetchall()
        accruals = connection.execute(
            'SELECT id,employee_id,work_day,attendance_status,amount FROM accountant_accruals '
            'WHERE work_day>=? AND work_day<=?',
            ((first-timedelta(days=1)).isoformat(), (last-timedelta(days=1)).isoformat())).fetchall()
        all_payments = defaultdict(list)
        for row in connection.execute(
                'SELECT p.accrual_id,p.paid_day,p.amount FROM accountant_salary_payments p '
                'JOIN accountant_accruals a ON a.id=p.accrual_id WHERE a.work_day>=? AND a.work_day<=?',
                ((first-timedelta(days=1)).isoformat(), (last-timedelta(days=1)).isoformat())):
            all_payments[row[0]].append(row)
        closure = closure_row(connection)
        confirmed_days = {row[0] for row in connection.execute(
            'SELECT day FROM accountant_payroll_days WHERE day>=? AND day<=?',
            ((first-timedelta(days=1)).isoformat(), (last-timedelta(days=1)).isoformat()))}
        aggregate_days = {row[0] for row in connection.execute(
            "SELECT DISTINCT day FROM accountant_movements WHERE day>=? AND day<=? AND kind='other_expense' "
            "AND item_code IN ('salary_cashier','salary_staff','salary_technical','salary_carryover')",
            (first.isoformat(), last.isoformat()))}
    closed_through = closure[1] if closure else ''
    people = {row[0]: dict(id=row[0], name=row[1], role=row[2], group=row[3],
                           rate=str(row[4]) if row[4] is not None else None,
                           archived=False, cells={}) for row in current}
    amounts, cell_payments = defaultdict(Decimal), defaultdict(list)
    for row in payments:
        employee_id, name, group, rate, paid_day, amount = row[:6]
        if employee_id not in people:
            historic = versions.get(employee_id, [])
            people[employee_id] = dict(id=employee_id, name=name,
                                      role=historic[-1][3] if historic else group,
                                      group=group, rate=rate, archived=True, cells={})
        amounts[(employee_id, paid_day)] += Decimal(amount)
        cell_payments[(employee_id, paid_day)].append(row)
    earned = {(row[1], row[2]): row for row in accruals}
    for employee_id, person in people.items():
        for paid_day in days:
            work_day = (date.fromisoformat(paid_day)-timedelta(days=1)).isoformat()
            history = [row for row in versions[employee_id] if row[1] <= work_day]
            editable = (not person['archived'] and ENTRY_START.isoformat() <= paid_day <= today.isoformat()
                        and work_day > closed_through and paid_day > closed_through
                        and work_day not in confirmed_days and bool(history) and not history[-1][6])
            earned_row = earned.get((employee_id, work_day))
            paid_rows = cell_payments[(employee_id, paid_day)]
            conflict = bool(paid_rows) if earned_row is None else (
                earned_row[3] != MANUAL_STATUS or len(paid_rows) != 1
                or paid_rows[0][8] != earned_row[0] or len(all_payments[earned_row[0]]) != 1
                or Decimal(paid_rows[0][5]) != Decimal(earned_row[4]))
            if not paid_rows and paid_day in aggregate_days:
                conflict = True
            person['cells'][paid_day] = dict(amount=plain(amounts[(employee_id, paid_day)]),
                                             work_day=work_day, editable=bool(editable and not conflict))
    return dict(today=today.isoformat(), entry_start=ENTRY_START.isoformat(), days=days,
                people=list(people.values()),
                closed_through=closed_through or None)
