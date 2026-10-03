"""Баланс Шохруха: сколько выделили, сколько потратили, какой остаток (ТЗ 02.10, п. 5).

Всё вносит бухгалтер. Выделить деньги — выдача из кассы на закуп
(`procurement_advance`, как и раньше): касса уменьшается, баланс Шоха растёт.
Расход по счёт-фактуре — запись подотчёта `shoh` с базаром и комментарием:
баланс Шоха уменьшается, касса второй раз не трогается — наличные ушли из неё
при выдаче. Остаток = выделено − расходы; та же сумма у учредителя и на
дэшборде «Финансов дня» (shokh.store.pocket_position).
"""

from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal

from .audit import record_audit
from .ledger import (DEFAULT_BAZAARS, LedgerError, amount_value, ensure_open, local_timestamp, now_stamp, plain,
                     required_text)
from .reserves import _balance, _entries, add_reserve_entry

EXPENSE_NOTE = 'Расход по счёт-фактуре'


def _bazaars(connection) -> list[str]:
    """Базары по умолчанию и добавленные бухгалтером. Умолчания в базу не
    пишутся: пустая база остаётся пустой (перенос в Postgres этого требует)."""
    names = list(DEFAULT_BAZAARS)
    for (name,) in connection.execute('SELECT name FROM accountant_bazaars ORDER BY id'):
        if name.casefold() not in {item.casefold() for item in names}:
            names.append(name)
    return names


def bazaars(finance) -> list[str]:
    with closing(finance._open()) as connection:
        return _bazaars(connection)


def add_bazaar(finance, name) -> list[str]:
    name = ' '.join(str(name or '').split())
    if not name or len(name) > 80:
        raise LedgerError('Укажите название базара (до 80 символов).')
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            known = _bazaars(connection)
            if name.casefold() in {item.casefold() for item in known}:
                raise LedgerError('Такой базар уже есть в списке.')
            connection.execute('INSERT INTO accountant_bazaars (name, created_at) VALUES (?, ?)',
                               (name, now_stamp()))
            record_audit(connection, 'bazaar', name, 'create', None, dict(name=name))
            connection.commit()
            return known + [name]
        except Exception:
            connection.rollback()
            raise


def add_expense(finance, day: date, place, amount, note='') -> int:
    place = required_text(place, 'базар')
    note = ' '.join(str(note or '').split())
    if len(note) > 160:
        raise LedgerError('Комментарий — не длиннее 160 символов.')
    amount_value(amount)
    if place not in bazaars(finance):
        raise LedgerError('Выберите базар из списка или добавьте новый.')
    try:
        return add_reserve_entry(finance, day, 'shoh', 'withdrawal', amount, note or EXPENSE_NOTE, None,
                                 place=place)
    except LedgerError as error:
        if 'превышает остаток' in str(error):
            raise LedgerError('Расход больше, чем у Шоха на руках. Сначала выделите ему деньги.') from None
        raise


def delete_expense(finance, entry_id: int, day: date) -> None:
    with closing(finance._open()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        try:
            ensure_open(connection, day)
            before = finance._row_dict(connection, 'accountant_reserves', entry_id)
            if (before is None or before['account'] != 'shoh' or before['kind'] != 'withdrawal'
                    or not before.get('place')):
                raise LedgerError('Расход не найден.')
            if before['day'] != day.isoformat():
                raise LedgerError('Нельзя изменить дату операции.')
            connection.execute('DELETE FROM accountant_reserves WHERE id = ?', (entry_id,))
            record_audit(connection, 'shoh_expense', entry_id, 'delete', before, None)
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _history(connection, first: str, last: str) -> list[dict]:
    """Все выдачи и расходы Шоха за период — для «Месячного отчёта»."""
    rows = []
    for movement_id, day, amount, description, created_at in connection.execute(
            'SELECT id, day, amount, description, created_at FROM accountant_movements '
            "WHERE day >= ? AND day <= ? AND ((kind='other_expense' AND item_code='proc_shoh') "
            # Шаблон — параметром: «%» внутри строки SQL psycopg принял бы за плейсхолдер.
            "OR (kind='procurement_advance' AND description LIKE ?)) ORDER BY day, id", (first, last, 'Шох:%')):
        rows.append(dict(id=movement_id, operation='movement', day=day, kind='give', source='accountant',
                         amount=amount, place=None, note='Выдано бухгалтером',
                         created_at=local_timestamp(created_at)))
    for give_id, day, amount, created_at in connection.execute(
            'SELECT id, day, amount, created_at FROM cashier_shokh_gives WHERE day >= ? AND day <= ? '
            'ORDER BY day, id', (first, last)):
        rows.append(dict(id=give_id, operation=None, day=day, kind='give', source='cashier', amount=amount,
                         place=None, note='Выдано из кассы кассиром', created_at=local_timestamp(created_at)))
    for entry_id, day, kind, amount, note, place, created_at in connection.execute(
            "SELECT id, day, kind, amount, note, place, created_at FROM accountant_reserves "
            "WHERE account = 'shoh' AND kind IN ('withdrawal', 'deposit') AND day >= ? AND day <= ? "
            'ORDER BY day, id', (first, last)):
        rows.append(dict(id=entry_id, operation='shoh_expense' if place else None, day=day,
                         kind='expense' if kind == 'withdrawal' else 'give', source='accountant',
                         amount=amount, place=place, note=note, created_at=local_timestamp(created_at)))
    return sorted(rows, key=lambda row: (row['day'], row['created_at'] or ''), reverse=True)


def shoh_view(finance, day: date, *, history: bool = False) -> dict:
    first = day.replace(day=1)
    with closing(finance._open()) as connection:
        entries = _entries(connection, 'shoh', day.isoformat())
        names = _bazaars(connection)
        month_rows = _history(connection, first.isoformat(), day.isoformat()) if history else None
        day_rows = _history(connection, day.isoformat(), day.isoformat())
    balance = _balance(entries)

    def total(kind, start, end=None):
        end = end or day.isoformat()
        return sum((Decimal(row['amount']) for row in entries
                    if row['kind'] == kind and start <= row['day'] <= end), Decimal(0))

    month_given, month_spent = total('deposit', first.isoformat()), total('withdrawal', first.isoformat())
    today = day.isoformat()
    given_today, spent_today = total('deposit', today), total('withdrawal', today)
    expenses_today = [dict(id=row['id'], place=row.get('place'), note=row['note'], amount=row['amount'])
                      for row in entries if row['kind'] == 'withdrawal' and row['day'] == today and row['id']]
    yesterday = (day - timedelta(days=1)).isoformat()
    return dict(demo=False, date=today, month=first.strftime('%Y-%m'),
                balance=plain(balance) if balance is not None else None,
                start=plain(balance - given_today + spent_today) if balance is not None else None,
                given_today=plain(given_today), spent_today=plain(spent_today),
                spent_yesterday=plain(total('withdrawal', yesterday, yesterday)),
                month_given=plain(month_given), month_spent=plain(month_spent),
                expenses_today=expenses_today, today=day_rows, bazaars=names,
                history=month_rows)
