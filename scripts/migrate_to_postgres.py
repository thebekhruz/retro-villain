#!/usr/bin/env python3
"""Перенос рабочих данных из файлов SQLite в Postgres — со сверкой.

Зачем. На Railway у приложения нет диска, поэтому базы переезжают в Postgres.
Переносить деньги «на глаз» нельзя, и скрипт сам себя проверяет: сравнивает
число строк и суммы денежных колонок в источнике и приёмнике. Расхождение —
ненулевой код возврата, а не строчка в логе.

Порядок:

    # посмотреть, что есть и что поедет, ничего не меняя
    PYTHONPATH=. python scripts/migrate_to_postgres.py --target "$DATABASE_URL" \
        --accountant ~/.local/share/retro-villain/accountant.sqlite3 \
        --cashier ~/.local/share/retro-villain/cashier.sqlite3 --dry-run

    # перенести
    PYTHONPATH=. python scripts/migrate_to_postgres.py --target "$DATABASE_URL" ... --apply

Приёмник должен быть пустым: переносим один раз на чистую базу. Повторный
запуск поверх данных запрещён, чтобы не удвоить начисления.
"""
import argparse
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retro.db import Database  # noqa: E402
from retro.schema import NUMERIC_COLUMNS  # noqa: E402

# Служебные таблицы: версию схемы в приёмнике ставят сами хранилища.
SKIP_TABLES = {'retro_schema_version', 'sqlite_sequence'}

# Какие деньги сверять. Собираем из карты схемы, чтобы список не разъезжался
# с кодом: там же он используется для проверки самих баз.
MONEY_COLUMNS = {table: columns
                 for kind in NUMERIC_COLUMNS.values()
                 for table, columns in kind.items()}


def source_tables(path: Path) -> list[str]:
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [name for (name,) in rows if name not in SKIP_TABLES
            and not name.startswith('sqlite_')]


def read_table(path: Path, table: str):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
        cursor = connection.execute(f'SELECT * FROM "{table}"')
        columns = [description[0] for description in cursor.description]
        return columns, cursor.fetchall()


def money_total(rows, columns: list[str], table: str) -> Decimal:
    """Сумма денежных колонок таблицы. Деньги лежат текстом, читаем Decimal."""
    total = Decimal(0)
    for column in MONEY_COLUMNS.get(table, ()):
        if column not in columns:
            continue
        index = columns.index(column)
        for row in rows:
            value = row[index]
            if value in (None, ''):
                continue
            try:
                total += Decimal(str(value))
            except InvalidOperation:
                raise SystemExit(f'{table}.{column}: значение «{value}» не число — '
                                 'перенос остановлен, данные требуют внимания.')
    return total


def create_schema(database: Database) -> None:
    """Схему создают сами хранилища — один источник истины для DDL."""
    from retro.financial_requests import FinancialRequests
    from retro.integrations.cbu import UsdRates
    from retro.modules.accountant.hikvision import AttendanceStore
    from retro.modules.accountant.ledger import FinanceStore
    from retro.modules.accountant.roster import RosterStore
    from retro.modules.cashier.expenses import ExpenseStore
    from retro.modules.director.store import DirectorReportStore
    from retro.modules.founder.chat import FounderChatStore
    from retro.modules.founder.dividends import DividendTargetStore
    from retro.modules.shokh.store import ShokhStore

    for store in (ExpenseStore, UsdRates, RosterStore, FinanceStore, AttendanceStore,
                  ShokhStore, DividendTargetStore, FounderChatStore, DirectorReportStore,
                  FinancialRequests):
        store(database)


def target_counts(database: Database, tables) -> dict:
    result = {}
    with database.cursor() as connection:
        for table in tables:
            result[table] = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    return result


def copy_table(connection, table: str, columns: list[str], rows) -> None:
    if not rows:
        return
    names = ', '.join(f'"{column}"' for column in columns)
    marks = ', '.join('?' for _ in columns)
    connection.executemany(f'INSERT INTO "{table}" ({names}) VALUES ({marks})',
                           [tuple(row) for row in rows])


def fix_sequence(connection, table: str, columns: list[str]) -> None:
    """Сдвинуть счётчик id за перенесённые строки.

    Без этого первая же новая запись столкнулась бы с существующим номером:
    номера мы переносим как есть, а последовательность осталась на нуле.
    """
    if 'id' not in columns:
        return
    connection.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
        f'COALESCE((SELECT MAX(id) FROM "{table}"), 0) + 1, false)')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', required=True, help='строка подключения Postgres')
    for name in ('cashier', 'accountant', 'director', 'founder', 'financial-requests'):
        parser.add_argument(f'--{name}', type=Path, help=f'файл {name}.sqlite3')
    parser.add_argument('--apply', action='store_true', help='выполнить перенос')
    parser.add_argument('--dry-run', action='store_true', help='только показать, что поедет')
    args = parser.parse_args()

    if args.apply == args.dry_run:
        parser.error('Укажите либо --dry-run, либо --apply.')

    sources = {name: getattr(args, name.replace('-', '_'))
               for name in ('cashier', 'accountant', 'director', 'founder', 'financial-requests')}
    sources = {name: path for name, path in sources.items() if path}
    if not sources:
        parser.error('Не указан ни один файл источника.')
    for name, path in sources.items():
        if not path.exists():
            parser.error(f'{name}: файла нет — {path}')

    database = Database(args.target)
    if not database.is_postgres:
        parser.error('--target должен быть строкой подключения Postgres.')

    # Что есть в источниках
    plan = []
    for name, path in sources.items():
        for table in source_tables(path):
            columns, rows = read_table(path, table)
            plan.append(dict(source=name, table=table, columns=columns, rows=rows,
                             count=len(rows), money=money_total(rows, columns, table)))

    print(f'Источников: {len(sources)}; таблиц с данными: '
          f'{sum(1 for item in plan if item["count"])} из {len(plan)}\n')
    print(f'{"таблица":38} {"строк":>8}  {"сумма денег":>18}  источник')
    for item in sorted(plan, key=lambda row: (-row['count'], row['table'])):
        money = f'{item["money"]:,.2f}'.replace(',', ' ') if item['table'] in MONEY_COLUMNS else '—'
        print(f'{item["table"]:38} {item["count"]:>8}  {money:>18}  {item["source"]}')
    total_rows = sum(item['count'] for item in plan)
    print(f'\nВсего строк к переносу: {total_rows}')

    if args.dry_run:
        print('\nЭто --dry-run: ничего не изменено.')
        return 0

    create_schema(database)
    tables = [item['table'] for item in plan]
    existing = target_counts(database, tables)
    busy = {table: count for table, count in existing.items() if count}
    if busy:
        print('\nПриёмник не пуст, перенос отменён. Иначе начисления удвоились бы.')
        for table, count in busy.items():
            print(f'  {table}: {count} строк')
        return 1

    with database.cursor() as connection:
        try:
            connection.execute('BEGIN')
            for item in plan:
                copy_table(connection, item['table'], item['columns'], item['rows'])
                fix_sequence(connection, item['table'], item['columns'])
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    # Сверка: строки и деньги должны совпасть
    print('\nСверка:')
    problems = []
    with database.cursor() as connection:
        for item in plan:
            table = item['table']
            count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count != item['count']:
                problems.append(f'{table}: строк {item["count"]} → {count}')
                continue
            if table in MONEY_COLUMNS:
                moved = Decimal(0)
                for column in MONEY_COLUMNS[table]:
                    if column not in item['columns']:
                        continue
                    for (value,) in connection.execute(
                            f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'):
                        if value != '':
                            moved += Decimal(str(value))
                if moved != item['money']:
                    problems.append(f'{table}: деньги {item["money"]} → {moved}')
                    continue
            if item['count']:
                print(f'  ✓ {table}: {count} строк'
                      + (f', {item["money"]} сум' if table in MONEY_COLUMNS else ''))

    if problems:
        print('\nРАСХОЖДЕНИЯ — приёмник использовать нельзя:')
        for line in problems:
            print('  ✗', line)
        return 1
    print(f'\n✓ Перенесено {total_rows} строк, суммы совпали.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
