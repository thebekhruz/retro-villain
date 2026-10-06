"""Cashier business day and accountant receipt day are different dates."""
from datetime import date, timedelta


def receipt_day(shift_day: date) -> date:
    return shift_day + timedelta(days=1)


def cashier_day(received_day: date) -> date:
    return received_day - timedelta(days=1)


def migrate_handover_dates(connection):
    """Move legacy shift-keyed receipts once, preserving amounts and confirmations.

    DDL is outside the transaction because the Postgres adapter commits DDL.
    Updating the marker serializes concurrent workers on both databases.
    """
    from .audit import record_audit

    connection.execute('CREATE TABLE IF NOT EXISTS accountant_data_migrations '
                       '(name TEXT PRIMARY KEY, applied INTEGER NOT NULL)')
    name = 'handover_receipt_day_v1'
    connection.execute('INSERT OR IGNORE INTO accountant_data_migrations (name, applied) VALUES (?, 0)', (name,))
    connection.commit()
    connection.execute('BEGIN IMMEDIATE')
    try:
        connection.execute('UPDATE accountant_data_migrations SET applied = applied WHERE name = ?', (name,))
        if not connection.execute('SELECT applied FROM accountant_data_migrations WHERE name = ?', (name,)).fetchone()[0]:
            # Descending order prevents a receipt from overwriting its neighbour.
            rows = connection.execute("SELECT day, amount, source FROM accountant_handover_days "
                                      "WHERE source IN ('cashier', 'auto') OR confirmed_at IS NOT NULL "
                                      "ORDER BY day DESC").fetchall()
            for old_day, amount, source in rows:
                new_day = receipt_day(date.fromisoformat(old_day)).isoformat()
                if connection.execute('SELECT 1 FROM accountant_handover_days WHERE day = ?', (new_day,)).fetchone():
                    raise ValueError(f'Перенос кассы {old_day}: приход {new_day} уже записан вручную; нужна сверка.')
                connection.execute('UPDATE accountant_handover_days SET day = ? WHERE day = ?', (new_day, old_day))
                record_audit(connection, 'handover', new_day, 'receipt_day_migration',
                             dict(day=old_day, amount=amount, source=source),
                             dict(day=new_day, cashier_day=old_day, amount=amount, source=source))
            if connection.execute("SELECT 1 FROM accountant_finance_audit WHERE entity_type = 'handover' LIMIT 1").fetchone():
                record_audit(connection, 'finance_migration', name, 'apply', None, dict(receipt_dates=True))
            connection.execute('UPDATE accountant_data_migrations SET applied = 1 WHERE name = ?', (name,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
