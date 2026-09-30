"""CashBook (T-400): остаток из одной выборки совпадает с посуточными запросами.

Эталон ниже — прежний `cash_position`, который читал базу на каждый день. Книга
читает строки один раз и отбирает их в памяти; на данных с дырами, якорем,
выплатами, переводами в сейф и приходами ответы обязаны совпасть до копейки.
"""
import random
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal

from retro.modules.accountant.ledger import CashBook, FinanceStore


def reference_position(connection, day, start_day=None, *, current_amount=None, tolerate_gaps=False):
    rows = connection.execute('SELECT day, amount FROM accountant_handover_days '
                              'WHERE day <= ? ORDER BY day', (day.isoformat(),)).fetchall()
    if current_amount is not None:
        rows = sorted([row for row in rows if row[0] != day.isoformat()] + [(day.isoformat(), str(current_amount))])
    if start_day is not None:
        rows = [row for row in rows if row[0] >= start_day.isoformat()]
    anchor = connection.execute('SELECT day,amount FROM accountant_cash_opening WHERE id=1').fetchone()
    if anchor and day.isoformat() >= anchor[0]:
        rows = [row for row in rows if row[0] >= anchor[0]]
    if not rows:
        return None, None, day.isoformat(), None
    first = date.fromisoformat(anchor[0] if anchor and day.isoformat() >= anchor[0] else rows[0][0])
    expected = first
    for recorded, _ in rows:
        if date.fromisoformat(recorded) != expected and not tolerate_gaps:
            return None, None, expected.isoformat(), first.isoformat()
        expected = date.fromordinal(date.fromisoformat(recorded).toordinal() + 1)
    if expected < day and not tolerate_gaps:
        return None, None, expected.isoformat(), first.isoformat()
    opening = Decimal(anchor[1]) if anchor and first.isoformat() == anchor[0] else Decimal(0)
    opening += sum((Decimal(v) for r, v in rows if r < day.isoformat()), Decimal(0))
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
    receipts = sum((Decimal(r[0]) for r in connection.execute(
        "SELECT amount FROM accountant_movements WHERE day = ? AND kind = 'other_receipt'",
        (day.isoformat(),))), Decimal(0))
    return (opening, opening + Decimal(rows[-1][1]) + receipts - FinanceStore._daily_outflows(connection, day),
            None, first.isoformat())


FIRST = date(2026, 8, 20)
DAYS = [FIRST + timedelta(days=offset) for offset in range(45)]


def seeded(tmp_path, seed, *, anchor):
    path = tmp_path / f'ledger-{seed}.sqlite3'
    FinanceStore(path)
    rng = random.Random(seed)
    stamp = '2026-09-30T00:00:00'
    with closing(sqlite3.connect(path)) as connection, connection:
        gap = rng.choice(DAYS[10:30])
        for day in DAYS[:40]:
            if day != gap:
                connection.execute('INSERT INTO accountant_handover_days (day, amount, checked_at) VALUES (?,?,?)',
                                   (day.isoformat(), str(rng.randrange(0, 9_000_000, 500)), stamp))
        if anchor:
            connection.execute('INSERT INTO accountant_cash_opening (id, day, amount, note, created_at) VALUES (1,?,?,?,?)',
                               (DAYS[5].isoformat(), '1500000', 'остаток', stamp))
        for index in range(120):
            day = rng.choice(DAYS).isoformat()
            amount = str(rng.randrange(1000, 900_000, 250))
            table = rng.choice(('movement', 'salary', 'transfer', 'reserve'))
            if table == 'movement':
                kind = rng.choice(('other_expense', 'procurement_advance', 'other_receipt', 'supplier_transfer'))
                connection.execute('INSERT INTO accountant_movements (day, kind, description, amount, reference, '
                                   'created_at) VALUES (?,?,?,?,?,?)', (day, kind, 'x', amount, f'r{index}', stamp))
            elif table == 'salary':
                connection.execute('INSERT INTO accountant_salary_payments (accrual_id, paid_day, amount, '
                                   'created_at) VALUES (?,?,?,?)', (index, day, amount, stamp))
            else:
                kind = 'transfer' if table == 'transfer' else rng.choice(('deposit', 'withdrawal'))
                connection.execute('INSERT INTO accountant_reserves (day, account, kind, amount, note, created_at) '
                                   'VALUES (?,?,?,?,?,?)', (day, 'dividends', kind, amount, 'x', stamp))
    return path


def test_book_matches_per_day_queries_on_every_day(tmp_path):
    for seed in range(6):
        path = seeded(tmp_path, seed, anchor=seed % 2 == 0)
        with closing(sqlite3.connect(path)) as connection:
            book = CashBook(connection, DAYS[-1])
            for day in DAYS:
                for kwargs in (dict(), dict(tolerate_gaps=True), dict(start_day=DAYS[3]),
                               dict(current_amount=Decimal('777000'))):
                    assert book.position(day, **kwargs) == reference_position(connection, day, **kwargs), \
                        (seed, day, kwargs)


def test_negative_days_match_the_reference_and_read_the_ledger_once(tmp_path, monkeypatch):
    import retro.modules.accountant.ledger as ledger
    path = seeded(tmp_path, 1, anchor=True)
    store = FinanceStore(path)
    with closing(sqlite3.connect(path)) as connection:
        expected = [dict(day=day.isoformat(), balance=str(reference_position(connection, day)[1]))
                    for day in DAYS if (reference_position(connection, day)[1] or 0) < 0]
    assert expected, 'данные теста должны давать минусовые дни'
    books = []
    monkeypatch.setattr(ledger, 'CashBook', lambda *a: books.append(a) or CashBook(*a))
    assert store.negative_cash_days(DAYS[0], DAYS[-1]) == expected
    assert len(books) == 1
