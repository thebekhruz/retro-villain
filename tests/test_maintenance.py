import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from retro.maintenance import (
    MaintenanceError,
    backup_databases,
    file_sha256,
    install_verified_backup,
    verify_database,
)
from retro.schema import CURRENT_SCHEMAS, migrate_schema


FIXED_NOW = datetime(2026, 9, 19, 12, 30, tzinfo=timezone.utc)


def sqlite_file(path, schema, row=None):
    with sqlite3.connect(path) as connection:
        connection.execute(schema)
        if row is not None:
            connection.execute('INSERT INTO values_table(value) VALUES (?)', row)
    return path


def test_backup_uses_sqlite_api_and_writes_verified_manifest(tmp_path):
    source = sqlite_file(
        tmp_path / 'source.sqlite3',
        'CREATE TABLE values_table(value TEXT)',
        ('ok',),
    )

    result = backup_databases(
        {'cashier.sqlite3': source},
        tmp_path / 'backups',
        FIXED_NOW,
    )

    manifest = json.loads((result / 'manifest.json').read_text())
    copied = result / 'cashier.sqlite3'
    assert manifest['created_at'] == '2026-09-19T12:30:00+00:00'
    assert manifest['databases'][0]['integrity'] == 'ok'
    assert manifest['databases'][0]['sha256'] == file_sha256(copied)
    assert copied.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(copied) as connection:
        assert connection.execute('SELECT value FROM values_table').fetchone() == ('ok',)


def test_install_refuses_nonempty_destination_without_replace(tmp_path):
    source = sqlite_file(tmp_path / 'source.sqlite3', 'CREATE TABLE values_table(value TEXT)')
    backup = backup_databases({'cashier.sqlite3': source}, tmp_path / 'backups', FIXED_NOW)
    destination = tmp_path / 'data'
    destination.mkdir()
    (destination / 'cashier.sqlite3').write_bytes(b'existing')

    with pytest.raises(MaintenanceError, match='уже существует'):
        install_verified_backup(backup, destination)

    assert (destination / 'cashier.sqlite3').read_bytes() == b'existing'


def test_install_verified_backup_is_atomic_and_owner_only(tmp_path):
    source = sqlite_file(tmp_path / 'source.sqlite3', 'CREATE TABLE values_table(value TEXT)', ('ok',))
    backup = backup_databases({'cashier.sqlite3': source}, tmp_path / 'backups', FIXED_NOW)
    destination = tmp_path / 'data'

    installed = install_verified_backup(backup, destination)

    assert installed == [destination / 'cashier.sqlite3']
    assert verify_database(installed[0]).integrity == 'ok'
    assert installed[0].stat().st_mode & 0o777 == 0o600


def test_legacy_schema_migrates_once(tmp_path):
    database = tmp_path / 'accountant.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE accountant_movements(id INTEGER PRIMARY KEY, amount TEXT NOT NULL)')
        connection.execute('INSERT INTO accountant_movements(amount) VALUES (?)', ('125.50',))

    with sqlite3.connect(database) as connection:
        assert migrate_schema(connection, 'accountant') == CURRENT_SCHEMAS['accountant']
        assert migrate_schema(connection, 'accountant') == CURRENT_SCHEMAS['accountant']
        assert connection.execute('SELECT version FROM retro_schema_version').fetchone() == (
            CURRENT_SCHEMAS['accountant'],
        )
        assert Decimal(connection.execute('SELECT amount FROM accountant_movements').fetchone()[0]) == Decimal('125.50')


def test_invalid_legacy_numeric_row_blocks_schema_migration(tmp_path):
    database = tmp_path / 'accountant.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE accountant_movements(id INTEGER PRIMARY KEY, amount TEXT NOT NULL)')
        connection.execute('INSERT INTO accountant_movements(amount) VALUES (?)', ('bad',))

    with sqlite3.connect(database) as connection:
        with pytest.raises(MaintenanceError, match='accountant_movements.*1'):
            migrate_schema(connection, 'accountant')
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retro_schema_version'"
        ).fetchone() is None
