"""One-time confirmed balances at the close of October 1, carried into October 2.

These are opening capital, never receipts. Do not rewrite them on subsequent
starts or import historical movements into the working period.
"""
from contextlib import closing
from retro.accounting_period import ACCOUNTING_START
from .audit import record_audit
from .ledger import now_stamp

MIGRATION = 'confirmed_october_opening_2026_v1'
CASH_OPENING = '3875000'
SHOH_OPENING = '15639000'
NOTE = 'Подтверждённый остаток на 01.10.2026, перенесён на начало 02.10.2026'


def apply_october_opening(finance):
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
            day, stamp = ACCOUNTING_START.isoformat(), now_stamp()
            before = finance._row_dict(connection, 'accountant_working_cash_opening', 1)
            connection.execute('INSERT INTO accountant_working_cash_opening (id,day,amount,note,created_at) '
                               'VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                               'day=excluded.day, amount=excluded.amount, note=excluded.note, created_at=excluded.created_at',
                               (day, CASH_OPENING, NOTE, stamp))
            record_audit(connection, 'cash_opening', day, 'confirmed_opening_migration', before,
                         dict(day=day, amount=CASH_OPENING, note=NOTE))
            # Replace working opening entries only; preserve operations and archive.
            rows = connection.execute("SELECT id FROM accountant_reserves WHERE account='shoh' "
                                      "AND kind='opening' AND day>=?", (day,)).fetchall()
            for (entry_id,) in rows:
                old = finance._row_dict(connection, 'accountant_reserves', entry_id)
                record_audit(connection, 'reserve', entry_id, 'opening_migration_replace', old, None)
                connection.execute('DELETE FROM accountant_reserves WHERE id=?', (entry_id,))
            cursor = connection.execute('INSERT INTO accountant_reserves (day,account,kind,amount,note,created_at) '
                                        "VALUES (?,'shoh','opening',?,?,?)", (day, SHOH_OPENING, NOTE, stamp))
            record_audit(connection, 'reserve', cursor.lastrowid, 'confirmed_opening_migration', None,
                         dict(day=day, account='shoh', kind='opening', amount=SHOH_OPENING, note=NOTE))
            connection.execute('UPDATE accountant_data_migrations SET applied=1 WHERE name=?', (MIGRATION,))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
