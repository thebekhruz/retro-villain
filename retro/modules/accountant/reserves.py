"""Dated subsidiary ledgers. Moving cash to the safe is not an owner payout."""
from collections import defaultdict
from contextlib import closing, nullcontext
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby
from retro.accounting_period import ACCOUNTING_START, accounting_range_start, period_start
from retro.request_reads import call as cached_call

from .ledger import LedgerError, amount_value, ensure_open, now_stamp, required_text
from .audit import record_audit

SHIFT_SALARY_CODES = {'salary_cashier', 'salary_staff', 'salary_technical', 'salary_carryover'}


def is_monthly_salary(item_code):
    return item_code == 'salary_monthly' or (
        isinstance(item_code, str) and item_code.startswith('salary_')
        and item_code not in SHIFT_SALARY_CODES)


# День «до начала учёта» для подразумеваемого нулевого остатка Шоха.
BEFORE_ALL = '0001-01-01'


def _entries(connection, account, through=None, *, since=None):
    # Границы периода уходят в SQL: выдачи Шоху лежат в общей таблице движений,
    # и без них этот запрос читал её целиком на каждый экран. Фильтр по
    # питону ниже остаётся — он же отсекает приход из кассы кассира.
    if since is None:
        since = period_start(date.fromisoformat(through)) if through else date.min
    first = since.isoformat()
    last = through if through is not None else date.max.isoformat()
    rows = [dict(id=r[0], day=r[1], kind=r[2], amount=r[3], note=r[4], place=r[5]) for r in connection.execute(
        'SELECT id, day, kind, amount, note, place FROM accountant_reserves '
        'WHERE account = ? AND day >= ? AND day <= ? ORDER BY day, id',
        (account, first, last))]
    if account == 'shoh':
        rows += [dict(id=None, day=r[0], kind='deposit', amount=r[1], note=r[2]) for r in connection.execute(
            "SELECT day, amount, description FROM accountant_movements WHERE day >= ? AND day <= ? AND ("
            "(kind='other_expense' AND item_code='proc_shoh') OR "
            "(kind='procurement_advance' AND description LIKE 'Шох:%'))", (first, last))]
    # Приход из кассы кассира: выдачи Шоху (`shoh`) и доллары в сейф (`usd`).
    # Деньги бухгалтера они не трогают — см. modules/cashier/till.py.
    from retro.modules.cashier.till import reserve_rows
    rows += reserve_rows(connection, account)
    rows = [r for r in rows if r['day'] >= first
            and (through is None or r['day'] <= through)]
    # Шох начинает с нуля в каждом периоде; архивный остаток не переносится.
    if account == 'shoh':
        for start, end in ((date.min, ACCOUNTING_START), (ACCOUNTING_START, date.max)):
            if start < since or (through is not None and start.isoformat() > through):
                continue
            if not any(r['kind'] == 'opening' and start.isoformat() <= r['day'] < end.isoformat() for r in rows):
                rows.append(dict(id=None, day=start.isoformat(), kind='opening', amount='0',
                                 note='Счёт Шоха с нуля', place=None, implicit=True))
    return sorted(rows, key=lambda r: r['day'])


def first_negative_day(rows):
    """Первый день, в который остаток резерва уходит в минус, или None.

    Раньше это считалось так: для каждого дня — полный пересчёт всех строк не
    позже него, то есть квадрат от числа дней, и на каждой записи в резерв.
    Один проход по дням даёт тот же ответ: остаток накапливается, а рабочий
    период начинает счёт заново — ровно как `_balance`, который у рабочего дня
    отбрасывает архивные строки. Пока начального остатка в периоде нет,
    остаток неизвестен, и минуса в нём быть не может.
    """
    periods = defaultdict(list)
    for row in rows:
        periods[period_start(date.fromisoformat(row['day']))].append(row)
    for period_rows in periods.values():
        total, opened = Decimal(0), False
        period_rows.sort(key=lambda row: row['day'])
        for day, same_day in groupby(period_rows, key=lambda row: row['day']):
            for row in same_day:
                opened = opened or row['kind'] == 'opening'
                total += Decimal(row['amount']) * (-1 if row['kind'] == 'withdrawal' else 1)
            if opened and total < 0:
                return day
    return None


def _balance(rows):
    if rows:
        start = period_start(date.fromisoformat(max(r['day'] for r in rows))).isoformat()
        rows = [r for r in rows if r['day'] >= start]
    if not any(r['kind'] == 'opening' for r in rows):
        return None
    return sum((Decimal(r['amount']) * (-1 if r['kind'] == 'withdrawal' else 1) for r in rows), Decimal(0))


def add_reserve_entry(store, day, account, kind, amount, note, cashier_amount, *, existing_connection=None,
                      place=None):
    if kind == 'opening' and day > ACCOUNTING_START:
        raise LedgerError('Начальный остаток нужно указать на 02.10.2026 — первый день учёта.')
    allowed = {'dividends': {'opening', 'transfer', 'withdrawal'},
               'usd': {'opening', 'deposit', 'withdrawal'}, 'shoh': {'opening', 'withdrawal'}}
    if account not in allowed or kind not in allowed[account]:
        raise LedgerError('Выберите допустимую операцию и счёт.')
    value = amount_value(amount, allow_zero=kind == 'opening')
    note = required_text(note, 'основание операции')
    with (nullcontext(existing_connection) if existing_connection else closing(store._open())) as connection:
        if existing_connection is None:
            connection.execute('BEGIN IMMEDIATE')
        try:
            ensure_open(connection, day)
            rows = [r for r in _entries(connection, account, since=period_start(day)) if not r.get('implicit')]
            if kind == 'opening':
                if any(r['kind'] == 'opening' for r in rows):
                    raise LedgerError('Начальный остаток уже указан.')
                if any(r['day'] < day.isoformat() for r in rows):
                    raise LedgerError('Начальный остаток должен быть не позже первой операции.')
            elif account != 'shoh' and not any(r['kind'] == 'opening' and r['day'] <= day.isoformat() for r in rows):
                raise LedgerError('Сначала укажите подтверждённый начальный остаток на этот день или раньше.')
            if kind == 'transfer':
                if cashier_amount is None:
                    raise LedgerError('Нет данных кассира для перевода в сейф.')
                if store.available_cash(connection, day, cashier_amount) < value:
                    raise LedgerError('Недостаточно денег от кассира для перевода в сейф.')
            cursor = connection.execute(
                'INSERT INTO accountant_reserves (day, account, kind, amount, note, created_at, place) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (day.isoformat(), account, kind, str(value), note, now_stamp(), place))
            after = store._row_dict(connection, 'accountant_reserves', cursor.lastrowid)
            record_audit(connection, 'reserve', cursor.lastrowid, 'create', None, after)
            if first_negative_day(_entries(connection, account, since=period_start(day))):
                raise LedgerError('Операция превышает остаток на этот или последующий день.')
            if kind == 'transfer' and cashier_amount is not None:
                store._check_known_future_balances(connection, day)
            if existing_connection is None:
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
                ensure_open(connection, day)
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
        plan = cached_call(('monthly_plan', month), lambda: connection.execute(
            'SELECT amount, note FROM accountant_monthly_plans WHERE month = ?', (month,)).fetchone())
        paid = sum((Decimal(r[0]) for r in connection.execute(
            'SELECT amount, item_code FROM accountant_movements WHERE day >= ? AND day <= ? '
            "AND kind = 'other_expense'", (accounting_range_start(day.replace(day=1), day).isoformat(),
                                         day.isoformat())) if is_monthly_salary(r[1])), Decimal(0))
        result['monthly'] = dict(month=month, plan=plan[0] if plan else None, paid=str(paid),
                                 balance=str(Decimal(plan[0]) - paid) if plan else None)
    return result
