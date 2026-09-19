"""Numbered, transactional schema metadata and legacy-value validation."""

import sqlite3
from decimal import Decimal, InvalidOperation


class SchemaError(RuntimeError):
    pass


CURRENT_SCHEMAS = {'cashier': 1, 'accountant': 1, 'director': 1}

NUMERIC_COLUMNS = {
    'cashier': {
        'cashier_expenses': ('amount',),
        'cashier_receipts': ('amount',),
        'cashier_usd_balances': ('amount',),
        'cashier_usd_rates': ('official_rate', 'restaurant_rate'),
    },
    'accountant': {
        'accountant_accruals': ('rate', 'amount'),
        'accountant_salary_payments': ('amount',),
        'accountant_movements': ('amount',),
        'accountant_reserves': ('amount',),
        'accountant_monthly_plans': ('amount',),
        'accountant_handover_days': ('amount',),
        'accountant_cash_opening': ('amount',),
        'accountant_debts': ('total_amount',),
        'accountant_debt_payments': ('amount',),
        'accountant_employees': ('rate',),
        'accountant_monthly_employees': ('salary', 'card', 'cash', 'advances', 'remaining'),
    },
    'director': {},
}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _validate_numeric_rows(connection: sqlite3.Connection, database_kind: str) -> None:
    for table, configured_columns in NUMERIC_COLUMNS[database_kind].items():
        if not _table_exists(connection, table):
            continue
        existing = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        columns = [column for column in configured_columns if column in existing]
        if not columns:
            continue
        selected = ', '.join(f'"{column}"' for column in columns)
        for row in connection.execute(f'SELECT rowid, {selected} FROM "{table}"'):
            row_id, *values = row
            for column, raw in zip(columns, values, strict=True):
                if raw is None:
                    continue
                try:
                    value = Decimal(str(raw))
                except (InvalidOperation, ValueError):
                    raise SchemaError(f'Некорректное число: {table} row {row_id}, поле {column}.') from None
                if not value.is_finite() or value.as_tuple().exponent < -2:
                    raise SchemaError(f'Некорректное число: {table} row {row_id}, поле {column}.')


def migrate_schema(connection: sqlite3.Connection, database_kind: str) -> int:
    if database_kind not in CURRENT_SCHEMAS:
        raise SchemaError(f'Неизвестный тип базы: {database_kind}.')
    target = CURRENT_SCHEMAS[database_kind]
    connection.execute('BEGIN IMMEDIATE')
    try:
        if _table_exists(connection, 'retro_schema_version'):
            row = connection.execute(
                'SELECT version FROM retro_schema_version WHERE database_kind=?',
                (database_kind,),
            ).fetchone()
            if row and row[0] > target:
                raise SchemaError('Версия базы новее этой версии приложения.')
            if row and row[0] == target:
                connection.commit()
                return target
        _validate_numeric_rows(connection, database_kind)
        connection.execute(
            'CREATE TABLE IF NOT EXISTS retro_schema_version ('
            'database_kind TEXT PRIMARY KEY, version INTEGER NOT NULL)'
        )
        connection.execute(
            'INSERT INTO retro_schema_version(database_kind,version) VALUES (?,?) '
            'ON CONFLICT(database_kind) DO UPDATE SET version=excluded.version',
            (database_kind, target),
        )
        connection.commit()
        return target
    except Exception:
        connection.rollback()
        raise
