"""Local employee roster imported from the approved payroll worksheet."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook


GROUPS = {
    'менеджер': 'Управление',
    'хостес': 'Встреча гостей',
    'официант': 'Обслуживание зала',
    'ранер': 'Обслуживание зала',
    'бармен': 'Бар',
    'няня': 'Присмотр за детьми',
    'техперсонал': 'Уборка',
    'охрана': 'Охрана',
}


def group_for(role: str) -> str:
    normalized = ' '.join(role.casefold().split())
    if normalized.startswith('повар') or normalized == 'кондитер':
        return 'Кухня'
    if normalized not in GROUPS:
        raise ValueError(f'Неизвестная должность в реестре: {role}')
    return GROUPS[normalized]


def parse_rate(value) -> Decimal | None:
    if value is None or value == '':
        return None
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Дневная ставка должна быть числом.') from None
    if not rate.is_finite() or rate <= 0 or rate.as_tuple().exponent < -2:
        raise ValueError('Дневная ставка должна быть положительной суммой.')
    return rate


@dataclass(frozen=True)
class Employee:
    id: int
    source_row: int
    name: str
    role: str
    group_name: str
    rate: Decimal | None
    hikvision_id: str | None

    def json(self):
        return dict(id=self.id, name=self.name, role=self.role, group=self.group_name,
                    rate=str(self.rate) if self.rate is not None else None,
                    hikvision_registered=self.hikvision_id is not None)


class RosterStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute('''CREATE TABLE IF NOT EXISTS accountant_employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_row INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            group_name TEXT NOT NULL,
            rate TEXT,
            hikvision_id TEXT UNIQUE
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS accountant_roster_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            changed_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            old_rate TEXT,
            new_rate TEXT,
            old_group TEXT NOT NULL,
            new_group TEXT NOT NULL
        )''')
        return connection

    def import_xlsx(self, source: Path) -> dict[str, int]:
        workbook = load_workbook(source, read_only=True, data_only=True)
        try:
            if 'ЗП' not in workbook.sheetnames:
                raise ValueError('В файле нет листа «ЗП».')
            sheet = workbook['ЗП']
            rows = []
            for cells in sheet.iter_rows(min_row=5, max_row=78, min_col=1, max_col=4):
                number, name, role, raw_rate = (cell.value for cell in cells)
                if not isinstance(number, int) or not isinstance(name, str) or not name.strip():
                    continue
                if not isinstance(role, str):
                    raise ValueError(f'Строка {cells[0].row}: у сотрудника нет должности.')
                rows.append((cells[0].row, name.strip(), role.strip(), group_for(role),
                             str(parse_rate(raw_rate)) if raw_rate is not None else None))
        finally:
            workbook.close()
        imported = existing = 0
        with closing(self._open()) as connection:
            with connection:
                for row in rows:
                    cursor = connection.execute(
                        'INSERT OR IGNORE INTO accountant_employees '
                        '(source_row, name, role, group_name, rate) VALUES (?, ?, ?, ?, ?)', row)
                    if cursor.rowcount:
                        imported += 1
                    else:
                        existing += 1
        return {'imported': imported, 'existing': existing}

    def list(self) -> list[Employee]:
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT id, source_row, name, role, group_name, rate, hikvision_id '
                                      'FROM accountant_employees ORDER BY source_row').fetchall()
        return [Employee(*row[:5], Decimal(row[5]) if row[5] is not None else None, row[6]) for row in rows]

    def update(self, employee_id: int, *, rate: str | None, group_name: str, reason: str) -> Employee:
        if not reason.strip():
            raise ValueError('Укажите причину изменения.')
        if group_name not in set(GROUPS.values()) | {'Кухня'}:
            raise ValueError('Неизвестная группа.')
        parsed_rate = parse_rate(rate)
        with closing(self._open()) as connection:
            with connection:
                old = connection.execute('SELECT rate, group_name FROM accountant_employees WHERE id = ?',
                                         (employee_id,)).fetchone()
                if old is None:
                    raise ValueError('Сотрудник не найден.')
                connection.execute('UPDATE accountant_employees SET rate = ?, group_name = ? WHERE id = ?',
                                   (str(parsed_rate) if parsed_rate is not None else None, group_name, employee_id))
                connection.execute('INSERT INTO accountant_roster_audit '
                                   '(employee_id, changed_at, reason, old_rate, new_rate, old_group, new_group) '
                                   'VALUES (?, ?, ?, ?, ?, ?, ?)',
                                   (employee_id, datetime.now().isoformat(), reason.strip(), old[0],
                                    str(parsed_rate) if parsed_rate is not None else None, old[1], group_name))
        return next(person for person in self.list() if person.id == employee_id)
