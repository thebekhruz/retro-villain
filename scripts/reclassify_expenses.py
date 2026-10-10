#!/usr/bin/env python3
"""Перенос старых строк журнала бухгалтера в понятные статьи (ТЗ 09.10, Б-08).

По умолчанию — сухой прогон: печатает расходы, у которых в тексте
«временн…» (→ «Доп. зарплата и временный персонал») или «дивиденд…»
(→ «Дивиденды напрямую из кассы»), с днём и суммой. Ничего не меняет.

Перенос — только явно, по номерам строк, сверенных с бухгалтером, и только
после резервной копии:

  python scripts/reclassify_expenses.py --database data/accountant.sqlite3
  python scripts/reclassify_expenses.py --database data/accountant.sqlite3 \\
      --apply --id 41 --id 42 --backup-root backups

База — путь к accountant.sqlite3 или строка подключения Postgres. Для SQLite
копия — проверенный файл базы (retro.maintenance), для Postgres — таблица
accountant_movements целиком в JSON. Сумма, день и ссылка строки не меняются;
в аудите (accountant_finance_audit, action='reclassify') — было и стало.
"""

import argparse
import hashlib
import json
import sys
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retro.db import as_database, is_postgres_url  # noqa: E402
from retro.maintenance import MaintenanceError, backup_databases  # noqa: E402
from retro.modules.accountant.expense_catalog import EXTRA_SALARY_ITEM, ITEMS  # noqa: E402
from retro.modules.accountant.ledger import LedgerError  # noqa: E402
from retro.modules.accountant.reclassify import candidates, reclassify  # noqa: E402
from retro.runtime import secure_directory, secure_file  # noqa: E402


def spaced(value) -> str:
    return f'{Decimal(str(value)):,.0f}'.replace(',', ' ')


def label(code) -> str:
    return ITEMS.get(code, (None, code or '—'))[1]


def backup(db, root: Path, now: datetime) -> Path:
    """Копия перед переносом. Возвращает папку копии."""
    if not db.is_postgres:
        return backup_databases({'accountant.sqlite3': db.path}, root, now)
    folder = secure_directory(Path(root)) / now.strftime('%Y%m%dT%H%M%SZ')
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        raise MaintenanceError(f'Резервная копия {folder.name} уже существует.') from None
    with closing(db.connect()) as connection:
        cursor = connection.execute('SELECT * FROM accountant_movements ORDER BY id')
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    target = folder / 'accountant_movements.json'
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding='utf-8')
    secure_file(target)
    manifest = folder / 'manifest.json'
    manifest.write_text(json.dumps({
        'created_at': now.isoformat(), 'table': 'accountant_movements', 'rows': len(rows),
        'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, ensure_ascii=False, indent=2),
        encoding='utf-8')
    secure_file(manifest)
    return folder


def show(rows) -> None:
    if not rows:
        print('Подходящих строк нет: переносить нечего.')
        return
    for row in rows:
        day = f'{row["day"][8:10]}.{row["day"][5:7]}.{row["day"][:4]}'
        line = (f'№{row["id"]}  {day}  {spaced(row["amount"]):>13} сум  '
                f'{label(row["item_code"])} → {label(row["target"])}  «{row["description"]}»')
        print(line + (f'  [нельзя: {row["blocked"]}]' if row['blocked'] else ''))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    root.add_argument('--database', required=True, help='accountant.sqlite3 или строка подключения Postgres')
    root.add_argument('--apply', action='store_true', help='перенести строки --id (без флага — сухой прогон)')
    root.add_argument('--id', dest='ids', type=int, action='append', default=[],
                      help='номер сверенной строки из сухого прогона; можно несколько раз')
    root.add_argument('--backup-root', type=Path, help='куда положить резервную копию перед переносом')
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    # Проверка до as_database: та создала бы папку под несуществующий файл.
    if not is_postgres_url(args.database) and not Path(args.database).is_file():
        print(f'Ошибка: база не найдена: {args.database}.')
        return 1
    db = as_database(args.database)
    found = candidates(db)
    show(found)
    if not args.apply:
        print('\nСухой прогон: ничего не изменено. Перенос — после сверки с бухгалтером: '
              '--apply --id N [--id N …] --backup-root ПАПКА.')
        return 0
    if not args.ids:
        print('Ошибка: укажите --id каждой сверенной строки.')
        return 2
    if args.backup_root is None:
        print('Ошибка: без --backup-root перенос не выполняется — сначала резервная копия.')
        return 2
    by_id = {row['id']: row for row in found}
    unknown = [value for value in args.ids if value not in by_id]
    if unknown:
        print('Ошибка: этих строк нет в списке выше: ' + ', '.join(f'№{value}' for value in unknown) + '.')
        return 2
    blocked = [by_id[value] for value in args.ids if by_id[value]['blocked']]
    if blocked:
        print('Ошибка: ' + '; '.join(f'№{row["id"]} — {row["blocked"]}' for row in blocked) + '.')
        return 2
    try:
        folder = backup(db, args.backup_root, datetime.now(timezone.utc))
    except MaintenanceError as error:
        print(f'Ошибка резервной копии: {error}')
        return 1
    print(f'\nРезервная копия: {folder}')
    salary_days = set()
    for value in args.ids:
        row = by_id[value]
        try:
            after = reclassify(db, value, row['target'])
        except LedgerError as error:
            print(f'№{value}: {error}')
            return 1
        print(f'№{value}: {label(row["item_code"])} → {label(after["item_code"])}, '
              f'{spaced(after["amount"])} сум, день {after["day"]} — перенесено.')
        if after['item_code'] == EXTRA_SALARY_ITEM:
            salary_days.add(after['day'])
    if salary_days:
        # Доп. зарплата уходит из «прочих» в зарплаты: суммы дня в сданном отчёте
        # расходятся с текущими, и экран попросит сдать отчёт заново.
        print('Сданные отчёты за ' + ', '.join(sorted(salary_days))
              + ' покажут «изменён после сдачи»: зарплаты и прочие расходы дня поменялись местами, '
              'остаток тот же. Сдайте отчёт этих дней заново.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
