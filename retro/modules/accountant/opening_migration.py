"""Start October 13 with the balances from the paper ledgers (Timur, October 14).

Бухгалтер — «остаток на 13.10.2026» из книги Лины, Шох — «остаток на 12.10.2026»
из книги Шохруха (12-го закупа не было). Сейф в сумах и долларах в книгах не
ведётся — ноль, как и на прежних границах.

The cutover date is immutable: a future accounting boundary must not silently
reuse this migration or its amounts. Existing operations remain unchanged.
"""
from contextlib import closing
from datetime import date
from retro.accounting_period import ACCOUNTING_START
from .audit import record_audit
from .ledger import now_stamp

CUTOVER = date(2026, 10, 13)
MIGRATION = 'confirmed_october_13_opening_2026_v1'
NOTE = 'Остаток на 13.10.2026 из бумажных книг по указанию Timur от 14.10.2026'
CASH_OPENING = '27235000'
RESERVE_OPENINGS = {'dividends': '0', 'usd': '0', 'shoh': '8012000'}


def apply_october_opening(finance):
    if ACCOUNTING_START != CUTOVER:
        raise RuntimeError('Accounting start changed: define a new opening migration.')
    with closing(finance._open()) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS accountant_data_migrations '
                           '(name TEXT PRIMARY KEY, applied INTEGER NOT NULL)')
        connection.execute('INSERT OR IGNORE INTO accountant_data_migrations (name, applied) VALUES (?, 0)', (MIGRATION,))
        connection.commit()
        connection.execute('BEGIN IMMEDIATE')
        try:
            connection.execute('UPDATE accountant_data_migrations SET applied=applied WHERE name=?', (MIGRATION,))
            if connection.execute('SELECT applied FROM accountant_data_migrations WHERE name=?', (MIGRATION,)).fetchone()[0]:
                connection.commit()
                return
            day, stamp = CUTOVER.isoformat(), now_stamp()
            before = finance._row_dict(connection, 'accountant_working_cash_opening', 1)
            connection.execute('INSERT INTO accountant_working_cash_opening (id,day,amount,note,created_at) '
                               'VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                               'day=excluded.day, amount=excluded.amount, note=excluded.note, created_at=excluded.created_at',
                               (day, CASH_OPENING, NOTE, stamp))
            record_audit(connection, 'cash_opening', day, 'confirmed_opening_migration', before,
                         dict(day=day, amount=CASH_OPENING, note=NOTE))
            # Archive the old working cash anchor in the audit above. Reserve
            # openings before Oct 13 stay in their original rows for audit.
            rows = connection.execute("SELECT id FROM accountant_reserves "
                                      "WHERE account IN ('dividends','usd','shoh') "
                                      "AND kind='opening' AND day>=?", (day,)).fetchall()
            for (entry_id,) in rows:
                old = finance._row_dict(connection, 'accountant_reserves', entry_id)
                record_audit(connection, 'reserve', entry_id, 'opening_migration_replace', old, None)
                connection.execute('DELETE FROM accountant_reserves WHERE id=?', (entry_id,))
            for account, amount in RESERVE_OPENINGS.items():
                cursor = connection.execute('INSERT INTO accountant_reserves (day,account,kind,amount,note,created_at) '
                                            "VALUES (?,?,'opening',?,?,?)", (day, account, amount, NOTE, stamp))
                record_audit(connection, 'reserve', cursor.lastrowid, 'confirmed_opening_migration', None,
                             dict(day=day, account=account, kind='opening', amount=amount, note=NOTE))
            connection.execute('UPDATE accountant_data_migrations SET applied=1 WHERE name=?', (MIGRATION,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
