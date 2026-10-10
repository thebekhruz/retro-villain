"""Manual daily salaries: one paid-day cell, one accrual and one cash payment."""
from collections import defaultdict
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal

from retro.accounting_period import ACCOUNTING_START
from retro.modules.cashier.service import TZ, today_tashkent

from .attendance import is_late

from .audit import record_audit
from .ledger import LedgerError, amount_value, closure_row, ensure_open, lock_day, now_stamp, plain

# Прошедшие дни месяца отмечаются, как в «Зарплате · месяц»: с начала рабочего учёта
# (на 02.10 стоит начальный остаток кассы). Дни, где зарплату уже провели по-старому —
# общей суммой или подтверждённой сменой, — остаются закрытыми: двойной выплаты нет.
ENTRY_START = ACCOUNTING_START
MANUAL_STATUS = 'manual_salary'


class SalaryCellChanged(LedgerError):
    pass


def _assert_day(paid_day):
    if paid_day < ENTRY_START:
        raise LedgerError('Зарплата по сотрудникам вводится с 02.10.2026 — с начала рабочего учёта.')
    if paid_day > today_tashkent():
        raise LedgerError('Нельзя записать зарплату за будущий день.')


def _existing(connection, employee_id, work_day, paid_day):
    """Что уже записано за смену: (начисление, его выплаты, выдано в этот день, прежнее ли).

    Прежнее — начисление старой ведомости (до ручной таблицы) или пара, которая
    разошлась: его берём в ручную пару при правке клетки. Нельзя только одно —
    выплата за эту смену в другой день или выплата другой смены в этот день:
    такая правка задвоила бы деньги."""
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
        return None, [], Decimal(0), False
    own = [row for row in payments if row[1] == accrual[0]]
    if len(own) != len(payments) or any(row[2] != paid_day.isoformat() for row in own):
        raise SalaryCellChanged('За смену есть выплата в другой день. Проверьте журнал зарплаты.')
    current = sum((Decimal(row[3]) for row in own), Decimal(0))
    manual = accrual[1] == MANUAL_STATUS and len(own) == 1 and Decimal(own[0][3]) == Decimal(accrual[2])
    return accrual[0], [row[0] for row in own], current, not manual


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
                # В реестр завели позже этой смены — берём нынешние имя, группу и ставку.
                version = (employee[0], employee[1], employee[2], 0)
            accrual_id, payment_ids, current, legacy = _existing(connection, employee_id, work_day, paid_day)
            if value == current:
                connection.commit()
                return dict(date=paid_day.isoformat(), employee_id=employee_id, amount=plain(value),
                            work_day=work_day.isoformat(), editable=True, changed=False)
            if expected != current:
                raise SalaryCellChanged('Сумма уже изменилась. Обновите таблицу и повторите.')
            # Only the increase spends additional cash; decreases return cash.
            if value > current:
                finance._require_cash(connection, paid_day, value - current, cashier_amount)
            old_accrual = finance._row_dict(connection, 'accountant_accruals', accrual_id) if accrual_id else None
            if legacy:
                # Начисление прежней ведомости становится ручной парой: одна сумма, одна выплата.
                for old_id in payment_ids:
                    before = finance._row_dict(connection, 'accountant_salary_payments', old_id)
                    connection.execute('DELETE FROM accountant_salary_payments WHERE id=?', (old_id,))
                    record_audit(connection, 'salary_payment', old_id, 'delete', before, None)
                payment_id, old_payment = None, None
                if value == 0:
                    connection.execute('DELETE FROM accountant_accruals WHERE id=?', (accrual_id,))
                else:
                    connection.execute('UPDATE accountant_accruals SET attendance_status=?, amount=? WHERE id=?',
                                       (MANUAL_STATUS, plain(value), accrual_id))
                    payment_id = connection.execute(
                        'INSERT INTO accountant_salary_payments (accrual_id,paid_day,amount,created_at) '
                        'VALUES (?,?,?,?)', (accrual_id, paid_day.isoformat(), plain(value), now_stamp())).lastrowid
            else:
                payment_id = payment_ids[0] if payment_ids else None
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
            accrual_after = finance._row_dict(connection, 'accountant_accruals', accrual_id) if value else None
            record_audit(connection, 'accrual', accrual_id,
                         'delete' if not value else 'create' if old_accrual is None else 'update',
                         old_accrual, accrual_after)
            if value or old_payment is not None:
                payment_after = finance._row_dict(connection, 'accountant_salary_payments', payment_id) if value else None
                record_audit(connection, 'salary_payment', payment_id,
                             'delete' if not value else 'create' if old_payment is None else 'update',
                             old_payment, payment_after)
            finance._check_cash_balances(connection, paid_day)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return dict(date=paid_day.isoformat(), employee_id=employee_id, amount=plain(value),
                work_day=work_day.isoformat(), editable=True, changed=True)


def month_data(finance, first, last, first_entries=None):
    """Ведомость месяца. first_entries(day) — первые входы Hikvision за день
    ({сотрудник: вход}); опоздавшим за смену клетки отдаётся время входа (late)."""
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
        # Доп. выплаты (Б-05): в итоге дня выплаты и сотрудника, в клетке — нет.
        from .extra_payouts import between, temporary_ids
        extras = between(connection, first, last)
        temporary = temporary_ids(connection)
        # Общая зарплата без сотрудников в «Финансах дня»: клетки не запираем, но
        # экран предупреждает — ввод тех же денег по людям посчитает выплату дважды.
        aggregate = defaultdict(Decimal)
        for day_text, amount_text in connection.execute(
                "SELECT day, amount FROM accountant_movements WHERE day>=? AND day<=? AND kind='other_expense' "
                "AND item_code IN ('salary_cashier','salary_staff','salary_technical','salary_carryover')",
                (first.isoformat(), last.isoformat())):
            aggregate[day_text] += Decimal(amount_text)
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
    for item in extras:
        # Доп. выплату получил тот, кого уже нет в реестре: строка — по имени из записи.
        if item['employee_id'] not in people:
            historic = versions.get(item['employee_id'], [])
            people[item['employee_id']] = dict(
                id=item['employee_id'], name=item['name'], role=item['role'], group=item['group'],
                rate=str(historic[-1][5]) if historic and historic[-1][5] is not None else None,
                archived=True, cells={})
        # Временный ли — по реестру; ушедшего — по записи на день выплаты.
        if item['temporary'] and people[item['employee_id']]['archived']:
            temporary.add(item['employee_id'])
        item['editable'] = (ENTRY_START.isoformat() <= item['paid_day'] <= today.isoformat()
                            and item['paid_day'] > closed_through and item['work_day'] > closed_through)
    for employee_id in temporary & people.keys():
        people[employee_id]['temporary'] = True
    earned = {(row[1], row[2]): row for row in accruals}
    for employee_id, person in people.items():
        for paid_day in days:
            work_day = (date.fromisoformat(paid_day)-timedelta(days=1)).isoformat()
            history = [row for row in versions[employee_id] if row[1] <= work_day]
            # Все прошедшие дни с начала учёта, как в «Зарплате · месяц». Запираем только
            # то, что задвоило бы деньги: выплату этой смены в другой день или выплату
            # другой смены в этот день.
            editable = (not person['archived'] and ENTRY_START.isoformat() <= paid_day <= today.isoformat()
                        and work_day > closed_through and paid_day > closed_through)
            earned_row = earned.get((employee_id, work_day))
            paid_rows = cell_payments[(employee_id, paid_day)]
            conflict = bool(paid_rows) if earned_row is None else (
                any(row[8] != earned_row[0] for row in paid_rows)
                or any(row[1] != paid_day for row in all_payments[earned_row[0]]))
            # Ставка на день смены (по версии реестра), а не нынешняя: галочка
            # «выдано по ставке» за прошлый день платит то, что действовало тогда.
            # Завели в реестр позже смены — нынешняя ставка.
            if history and not history[-1][6]:
                rate = history[-1][5]
            else:
                rate = person['rate']
            person['cells'][paid_day] = dict(amount=plain(amounts[(employee_id, paid_day)]),
                                             work_day=work_day, editable=bool(editable and not conflict),
                                             rate=str(rate) if rate is not None else None)
    if first_entries is not None:
        # Опоздание — за смену, то есть за день до выплаты; будущие смены не смотрим.
        for paid_day in days:
            work = date.fromisoformat(paid_day) - timedelta(days=1)
            if work >= today:
                continue
            for employee_id, entry in first_entries(work).items():
                person = people.get(employee_id)
                if person is not None and is_late(entry.occurred_at):
                    person['cells'][paid_day]['late'] = entry.occurred_at.astimezone(TZ).strftime('%H:%M')
    return dict(today=today.isoformat(), entry_start=ENTRY_START.isoformat(), days=days,
                people=list(people.values()), extras=extras,
                aggregate_days=[dict(day=day, amount=plain(total)) for day, total in sorted(aggregate.items())],
                closed_through=closed_through or None)
