#!/usr/bin/env python3
"""Backup, verify, migrate, and restore Retro data without printing row contents."""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from retro.maintenance import (
    MaintenanceError,
    backup_databases,
    install_verified_backup,
    verify_database,
)
from retro.runtime import secure_directory, secure_file
from retro.schema import migrate_schema


def print_check(path: Path) -> None:
    check = verify_database(path)
    print(
        f'{check.filename} size={check.size} sha256={check.sha256} '
        f'integrity={check.integrity} schema={check.schema_version}'
    )


def empty_director(path: Path) -> None:
    secure_directory(path.parent)
    with sqlite3.connect(path) as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS director_reports (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, period_start TEXT NOT NULL,
            period_end TEXT NOT NULL, snapshot_json TEXT NOT NULL, analysis_json TEXT NOT NULL,
            pdf_sha256 TEXT NOT NULL, pdf BLOB NOT NULL)''')
        migrate_schema(connection, 'director')
    secure_file(path)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)
    verify = commands.add_parser('verify')
    verify.add_argument('--source', type=Path, required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--cashier', type=Path, required=True)
    backup.add_argument('--accountant', type=Path, required=True)
    backup.add_argument('--backup-root', type=Path, required=True)
    migrate = commands.add_parser('migrate')
    migrate.add_argument('--cashier', type=Path, required=True)
    migrate.add_argument('--accountant', type=Path, required=True)
    migrate.add_argument('--destination', type=Path, required=True)
    migrate.add_argument('--backup-root', type=Path, required=True)
    migrate.add_argument('--replace', action='store_true')
    restore = commands.add_parser('restore')
    restore.add_argument('--backup', type=Path, required=True)
    restore.add_argument('--destination', type=Path, required=True)
    restore.add_argument('--replace', action='store_true')
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == 'verify':
            print_check(args.source)
            return 0
        if args.command == 'restore':
            installed = install_verified_backup(args.backup, args.destination, args.replace)
            for path in installed:
                print_check(path)
            return 0
        backup = backup_databases(
            {'cashier.sqlite3': args.cashier, 'accountant.sqlite3': args.accountant},
            args.backup_root,
            datetime.now(timezone.utc),
        )
        print(f'backup={backup}')
        if args.command == 'backup':
            return 0
        installed = install_verified_backup(backup, args.destination, args.replace)
        kinds = {'cashier.sqlite3': 'cashier', 'accountant.sqlite3': 'accountant'}
        for path in installed:
            with sqlite3.connect(path) as connection:
                migrate_schema(connection, kinds[path.name])
            print_check(path)
        director = args.destination / 'director.sqlite3'
        if not director.exists():
            empty_director(director)
        print_check(director)
        return 0
    except MaintenanceError as error:
        print(f'Ошибка: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
