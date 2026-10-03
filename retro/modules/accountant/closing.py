"""Сдача отчёта дня и закрытие месяца (ТЗ 02.10, п. 2 и «Сохранить и сдать отчёт»).

Отчёт дня — снимок «начало → приход → расход → конец» на момент, когда
бухгалтер нажал «Сохранить и сдать отчёт». Сдать его можно только с
подтверждённой суммой от кассира: пока сумма не подтверждена, день не закрыт
и она не входит в остаток. День после сдачи можно поправить и сдать заново —
отчёт тогда помечен «изменён после сдачи», пока его не сдадут снова.

Закрытие месяца необратимо из интерфейса: все дни по его последний день
становятся только для чтения (ledger.ensure_open), а остаток на конец
последнего дня — началом первого числа следующего месяца (CashBook).
"""

import json
from calendar import monthrange
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal

from .audit import record_audit
from .ledger import (CashBook, LedgerError, closure_row, ensure_open, flow_json, local_timestamp,
                     now_stamp, plain, required_text)

# По этим суммам сданный отчёт сверяется с текущим днём.
REPORT_KEYS = ('opening', 'handover_counted', 'receipts', 'salary', 'monthly', 'shoh', 'other',
               'transfers', 'closing')
MONTHS = ('январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь',
          'октябрь', 'ноябрь', 'декабрь')


def spaced(value) -> str:
    """«940 000» — сумма для текста замечания, с разрядами."""
    return f'{Decimal(str(value)):,.0f}'.replace(',', '\u00a0')


def month_bounds(month: str) -> tuple[date, date]:
    try:
        first = date.fromisoformat(f'{month}-01')
    except (TypeError, ValueError):
        raise LedgerError('Укажите месяц в виде ГГГГ-ММ.') from None
    return first, first.replace(day=monthrange(first.year, first.month)[1])


def month_name(month: str) -> str:
    first, _ = month_bounds(month)
    return f'{MONTHS[first.month - 1].capitalize()} {first.year}'


def _closure_json(row) -> dict:
    month, last_day, balance, closed_at, closed_by, snapshot = row
    data = json.loads(snapshot) if snapshot else {}
    return dict(month=month, name=month_name(month), last_day=last_day, closing_balance=balance,
                closed_at=local_timestamp(closed_at), closed_by=closed_by,
                debts=data.get('debts'), remarks=data.get('remarks', []),
                totals=data.get('totals'))


def closures(finance, limit: int = 24) -> list[dict]:
    with closing(finance._open()) as connection:
        rows = connection.execute(
            'SELECT month, last_day, closing_balance, closed_at, closed_by, snapshot '
            'FROM accountant_month_closures ORDER BY last_day DESC LIMIT ?', (limit,)).fetchall()
    return [_closure_json(row) for row in rows]


def closed_state(finance, day: date) -> dict | None:
    """Закрыт ли день: метка «Закрыто · дата · кем» для экранов."""
    with closing(finance._open()) as connection:
        latest = closure_row(connection)
        if latest is None or day.isoformat() > latest[1]:
            return None
        # День закрыт либо своим месяцем, либо более поздним закрытием.
        own = connection.execute(
            'SELECT month, last_day, closing_balance, closed_at, closed_by, snapshot '
            'FROM accountant_month_closures WHERE last_day >= ? ORDER BY last_day LIMIT 1',
            (day.isoformat(),)).fetchone()
    return _closure_json(own)


def latest_closure(finance) -> dict | None:
    rows = closures(finance, limit=1)
    return rows[0] if rows else None


# ── Отчёт дня ──────────────────────────────────────────────────────────────

def _report_json(row, current: dict | None) -> dict:
    day, submitted_at, submitted_by, snapshot = row
    data = json.loads(snapshot)
    saved = data.get('flow') or {}
    changed = current is not None and any(str(saved.get(key)) != str(current.get(key)) for key in REPORT_KEYS)
    return dict(day=day, submitted_at=local_timestamp(submitted_at), submitted_by=submitted_by,
                flow=saved, debts=data.get('debts'), shoh_balance=data.get('shoh_balance'),
                checks=data.get('checks', []), changed=changed)


def day_report(finance, day: date, current_flow: dict | None = None) -> dict | None:
    """Сданный отчёт дня; `changed` — день поправили после сдачи."""
    with closing(finance._open()) as connection:
        row = connection.execute(
            'SELECT day, submitted_at, submitted_by, snapshot FROM accountant_day_reports WHERE day = ?',
            (day.isoformat(),)).fetchone()
    return _report_json(row, current_flow) if row else None


def submit_day_report(finance, day: date, by: str, *, carry_start: date | None = None,
                      debts: dict | None = None, shoh_balance=None, checks=()) -> dict:
    """«Сохранить и сдать отчёт»: снимок дня для учредителя.

    Не сдаётся, пока сумма от кассира не подтверждена (или не записана
    бухгалтером вручную) и пока остаток на конец дня не посчитан."""
    by = required_text(by, 'кто сдаёт отчёт')
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            ensure_open(connection, day)
            flow = CashBook(connection, day).flow(day, carry_start, tolerate_gaps=finance.allow_negative_cash)
            if flow['handover_status'] == 'pending':
                raise LedgerError('Подтвердите сумму, полученную от кассира: без подтверждения '
                                  'отчёт дня не сдаётся.')
            if flow['handover_status'] == 'none':
                raise LedgerError('Нет суммы от кассира за этот день. Впишите, сколько получено, '
                                  'и подтвердите — без этого отчёт не сдаётся.')
            if flow['closing'] is None:
                missing = date.fromisoformat(flow['missing']).strftime('%d.%m') if flow['missing'] else '—'
                raise LedgerError(f'Остаток на конец дня не посчитан: нет передачи кассы за {missing}.')
            snapshot = dict(flow=flow_json(flow), debts=debts, shoh_balance=shoh_balance,
                            checks=[dict(lvl=str(item.get('lvl', ''))[:8], text=str(item.get('text', ''))[:200],
                                         sub=str(item.get('sub') or '')[:200]) for item in list(checks)[:50]])
            before = connection.execute(
                'SELECT day, submitted_at, submitted_by, snapshot FROM accountant_day_reports WHERE day = ?',
                (day.isoformat(),)).fetchone()
            connection.execute(
                'INSERT INTO accountant_day_reports (day, submitted_at, submitted_by, snapshot) '
                'VALUES (?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET submitted_at=excluded.submitted_at, '
                'submitted_by=excluded.submitted_by, snapshot=excluded.snapshot',
                (day.isoformat(), now_stamp(), by, json.dumps(snapshot, ensure_ascii=False)))
            after = connection.execute(
                'SELECT day, submitted_at, submitted_by, snapshot FROM accountant_day_reports WHERE day = ?',
                (day.isoformat(),)).fetchone()
            record_audit(connection, 'day_report', day.isoformat(), 'update' if before else 'create',
                         dict(zip(('day', 'submitted_at', 'submitted_by'), before[:3])) if before else None,
                         dict(zip(('day', 'submitted_at', 'submitted_by'), after[:3])))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return _report_json(after, snapshot['flow'])


# ── Закрытие месяца ────────────────────────────────────────────────────────

def _money_sum(values) -> Decimal:
    return sum((Decimal(value) for value in values), Decimal(0))


def _debts_at(connection, last: date) -> dict:
    """Долги на конец дня: сменным (начислено − выдано) и неоплаченные расходы."""
    through = last.isoformat()
    paid = {}
    for accrual_id, amount in connection.execute(
            'SELECT p.accrual_id, p.amount FROM accountant_salary_payments p '
            'JOIN accountant_accruals a ON a.id = p.accrual_id WHERE a.work_day <= ? AND p.paid_day <= ?',
            (through, through)):
        paid[accrual_id] = paid.get(accrual_id, Decimal(0)) + Decimal(amount)
    shifts = []
    for accrual_id, work_day, name, amount in connection.execute(
            'SELECT id, work_day, employee_name, amount FROM accountant_accruals WHERE work_day <= ? '
            'ORDER BY work_day, employee_name', (through,)):
        left = Decimal(amount) - paid.get(accrual_id, Decimal(0))
        if left > 0:
            shifts.append(dict(day=work_day, name=name, debt=plain(left)))
    paid_debts = {}
    for debt_id, amount in connection.execute(
            'SELECT debt_id, amount FROM accountant_debt_payments WHERE day <= ?', (through,)):
        paid_debts[debt_id] = paid_debts.get(debt_id, Decimal(0)) + Decimal(amount)
    expenses = sum((max(Decimal(0), Decimal(total) - paid_debts.get(debt_id, Decimal(0)))
                    for debt_id, total in connection.execute(
                        'SELECT id, total_amount FROM accountant_debts WHERE day <= ?', (through,))), Decimal(0))
    return dict(salary=plain(_money_sum(row['debt'] for row in shifts)), expenses=plain(expenses),
                shifts=shifts)


def month_preview(finance, month: str, today: date, *, carry_start: date | None = None,
                  monthly_left=None) -> dict:
    """Итог перед закрытием: остаток кассы, долги, незакрытые замечания.

    Замечания закрытие не запрещают — ТЗ просит их показать и попросить
    подтверждение. Запрещают только: месяц ещё идёт, месяц уже закрыт и
    остаток на конец месяца не посчитан (без него следующий месяц не начать)."""
    first, last = month_bounds(month)
    with closing(finance._open()) as connection:
        latest = closure_row(connection)
        book = CashBook(connection, last)
        flows = []
        day = first
        while day <= last:
            flows.append(book.flow(day, carry_start, tolerate_gaps=finance.allow_negative_cash))
            day += timedelta(days=1)
        reported = {row[0] for row in connection.execute(
            'SELECT day FROM accountant_day_reports WHERE day >= ? AND day <= ?',
            (first.isoformat(), last.isoformat()))}
        debts = _debts_at(connection, last)
        from .reserves import _balance, _entries
        shoh = _balance(_entries(connection, 'shoh', last.isoformat()))
    final = flows[-1]
    # Дни учёта: с первой передачи кассы или начального остатка.
    active = [flow for flow in flows if flow['first_day'] is not None]
    remarks = []

    def remark(kind, text, days=()):
        remarks.append(dict(kind=kind, text=text, days=list(days)))

    pending = [flow['day'] for flow in active if flow['handover_status'] == 'pending']
    if pending:
        remark('pending', f'Не подтверждена сумма от кассира: {len(pending)} дн. — она не вошла в остаток',
               pending)
    absent = [flow['day'] for flow in active if flow['handover_status'] == 'none']
    if absent:
        remark('absent', f'Нет передачи кассы: {len(absent)} дн.', absent)
    negative = [flow['day'] for flow in active if flow['closing'] is not None and flow['closing'] < 0]
    if negative:
        remark('negative', f'Остаток уходил в минус: {len(negative)} дн.', negative)
    if debts['shifts']:
        days = sorted({row['day'] for row in debts['shifts']})
        remark('shifts', f'Невыданные смены: {len(debts["shifts"])} на {spaced(debts["salary"])} сум', days)
    if Decimal(debts['expenses']) > 0:
        remark('debts', f'Неоплаченные расходы: {spaced(debts["expenses"])} сум')
    unreported = [flow['day'] for flow in active if flow['day'] not in reported]
    if unreported:
        remark('reports', f'Отчёт дня не сдан: {len(unreported)} дн.', unreported)

    reason = None
    already = latest is not None and last.isoformat() <= latest[1]
    if already:
        reason = 'Месяц уже закрыт.'
    elif today < last:
        reason = f'Месяц закрывается в последний день — {last.strftime("%d.%m.%Y")}.'
    elif final['closing'] is None:
        missing = date.fromisoformat(final['missing']).strftime('%d.%m') if final['missing'] else '—'
        reason = f'Остаток на {last.strftime("%d.%m")} не посчитан: нет передачи кассы за {missing}.'
    totals = dict(handover=plain(_money_sum(flow['handover_counted'] for flow in flows)),
                  receipts=plain(_money_sum(flow['receipts'] for flow in flows)),
                  outflows=plain(_money_sum(flow['outflows'] for flow in flows)),
                  salary=plain(_money_sum(flow['salary'] for flow in flows)),
                  monthly=plain(_money_sum(flow['monthly'] for flow in flows)),
                  shoh=plain(_money_sum(flow['shoh'] for flow in flows)),
                  other=plain(_money_sum(flow['other'] for flow in flows)),
                  transfers=plain(_money_sum(flow['transfers'] for flow in flows)))
    opening = next((flow['opening'] for flow in flows if flow['opening'] is not None), None)
    return dict(month=month, name=month_name(month), first=first.isoformat(), last=last.isoformat(),
                opening=plain(opening) if opening is not None else None,
                closing_balance=plain(final['closing']) if final['closing'] is not None else None,
                debts=dict(salary=debts['salary'], expenses=debts['expenses'],
                           monthly_left=plain(Decimal(str(monthly_left))) if monthly_left is not None else None),
                # Счёт Шохруха на конец месяца: после закрытия расходы Шоха за
                # этот месяц уже не внести — сверяют до закрытия.
                shoh_balance=plain(shoh) if shoh is not None else None,
                totals=totals, remarks=remarks, can_close=reason is None, reason=reason,
                # Закрытие месяца закрывает и все дни раньше него — говорим об
                # этом, только если раньше месяца есть незакрытый учёт.
                includes_earlier=_earlier_open(finance, first, latest),
                closed=closed_state(finance, last) if already else None,
                days=[flow_json(flow) for flow in flows])


def _earlier_open(finance, first: date, latest) -> bool:
    start = finance.accounting_start()
    if start is None or start >= first:
        return False
    return latest is None or (first - timedelta(days=1)).isoformat() > latest[1]


def close_month(finance, month: str, by: str, today: date, *, carry_start: date | None = None,
                monthly_left=None) -> dict:
    by = required_text(by, 'кто закрывает месяц')
    first, last = month_bounds(month)
    preview = month_preview(finance, month, today, carry_start=carry_start, monthly_left=monthly_left)
    if not preview['can_close']:
        raise LedgerError(preview['reason'])
    snapshot = dict(opening=preview['opening'], debts=preview['debts'], totals=preview['totals'],
                    shoh_balance=preview['shoh_balance'],
                    remarks=[dict(kind=item['kind'], text=item['text']) for item in preview['remarks']])
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            latest = closure_row(connection)
            if latest is not None and last.isoformat() <= latest[1]:
                raise LedgerError('Месяц уже закрыт.')
            # Остаток пересчитываем в той же транзакции, что и запись закрытия:
            # между просмотром и подтверждением день могли поправить.
            book = CashBook(connection, last)
            final = book.flow(last, carry_start, tolerate_gaps=finance.allow_negative_cash)
            if final['closing'] is None:
                raise LedgerError(preview['reason'] or 'Остаток на конец месяца не посчитан.')
            balance = plain(final['closing'])
            connection.execute(
                'INSERT INTO accountant_month_closures '
                '(month, last_day, closing_balance, closed_at, closed_by, snapshot) VALUES (?, ?, ?, ?, ?, ?)',
                (month, last.isoformat(), balance, now_stamp(), by,
                 json.dumps(dict(snapshot, closing_balance=balance), ensure_ascii=False)))
            record_audit(connection, 'month_closure', month, 'create', None,
                         dict(month=month, last_day=last.isoformat(), closing_balance=balance, closed_by=by))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return closed_state(finance, last)


def reopen_month(finance, month: str) -> bool:
    """Снять закрытие месяца — только скриптом обслуживания, не из панели."""
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            row = connection.execute(
                'SELECT month, last_day, closing_balance, closed_at, closed_by FROM accountant_month_closures '
                'WHERE month = ?', (month,)).fetchone()
            if row is None:
                connection.rollback()
                return False
            later = connection.execute('SELECT 1 FROM accountant_month_closures WHERE last_day > ?',
                                       (row[1],)).fetchone()
            if later:
                raise LedgerError('Сначала снимите закрытие более поздних месяцев.')
            connection.execute('DELETE FROM accountant_month_closures WHERE month = ?', (month,))
            record_audit(connection, 'month_closure', month, 'delete',
                         dict(zip(('month', 'last_day', 'closing_balance', 'closed_at', 'closed_by'), row)), None)
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise


def reports_feed(finance, *, days: int = 14) -> dict:
    """Учредителю: последние сданные отчёты дня и закрытые месяцы."""
    with closing(finance._open()) as connection:
        rows = connection.execute(
            'SELECT day, submitted_at, submitted_by, snapshot FROM accountant_day_reports '
            'ORDER BY day DESC LIMIT ?', (days,)).fetchall()
        current = {}
        if rows:
            book = CashBook(connection, date.fromisoformat(rows[0][0]))
            for row in rows:
                current[row[0]] = flow_json(book.flow(date.fromisoformat(row[0]),
                                                      tolerate_gaps=finance.allow_negative_cash))
    return dict(reports=[_report_json(row, current.get(row[0])) for row in rows],
                closures=closures(finance, limit=12))
