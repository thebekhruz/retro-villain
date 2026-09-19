"""Local employee roster imported from the approved payroll worksheet."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from retro.runtime import secure_directory, secure_file


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


def parse_money(value, *, allow_zero=True) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Сумма должна быть числом.') from None
    if (not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero)
            or amount > Decimal('1000000000000') or amount.as_tuple().exponent < -2):
        raise ValueError('Сумма должна быть неотрицательной, не более 1 трлн сум и с точностью до тиына.')
    return amount


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


@dataclass(frozen=True)
class MonthlyEmployee:
    id: int
    external_key: str | None
    name: str
    role: str
    salary: Decimal
    schedule: str
    card: Decimal
    cash: Decimal
    advances: Decimal
    remaining: Decimal

    def json(self):
        return dict(id=self.id, external_key=self.external_key, name=self.name, role=self.role,
                    salary=str(self.salary), schedule=self.schedule, card=str(self.card),
                    cash=str(self.cash), advances=str(self.advances), remaining=str(self.remaining))


class RosterStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self):
        secure_directory(self.path.parent)
        connection = sqlite3.connect(self.path, timeout=10)
        secure_file(self.path)
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
        connection.execute('''CREATE TABLE IF NOT EXISTS accountant_monthly_employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_key TEXT UNIQUE,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            salary TEXT NOT NULL,
            schedule TEXT NOT NULL DEFAULT '',
            card TEXT NOT NULL DEFAULT '0',
            cash TEXT NOT NULL DEFAULT '0',
            advances TEXT NOT NULL DEFAULT '0',
            remaining TEXT NOT NULL DEFAULT '0'
        )''')
        columns = {row[1] for row in connection.execute(
            'PRAGMA table_info(accountant_monthly_employees)')}
        if 'external_key' not in columns:
            connection.execute('ALTER TABLE accountant_monthly_employees ADD COLUMN external_key TEXT')
            connection.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS accountant_monthly_external_key '
                'ON accountant_monthly_employees(external_key) WHERE external_key IS NOT NULL')
        return connection

    def import_xlsx(self, source: Path, *, replace: bool = False) -> dict[str, int]:
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
                if replace:
                    source_rows = {row[0] for row in rows}
                    connection.execute('DELETE FROM accountant_employees WHERE source_row NOT IN (%s)' %
                                       ','.join('?' for _ in source_rows), tuple(source_rows))
                for row in rows:
                    cursor = connection.execute('SELECT id FROM accountant_employees WHERE source_row = ?', (row[0],))
                    if cursor.fetchone() is None:
                        connection.execute(
                            'INSERT INTO accountant_employees '
                            '(source_row, name, role, group_name, rate) VALUES (?, ?, ?, ?, ?)', row)
                        imported += 1
                    else:
                        if replace:
                            connection.execute('UPDATE accountant_employees SET name=?, role=?, group_name=?, rate=? '
                                               'WHERE source_row=?', (row[1], row[2], row[3], row[4], row[0]))
                            imported += 1
                        else:
                            existing += 1
        return {'imported': imported, 'existing': existing}

    def list(self) -> list[Employee]:
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT id, source_row, name, role, group_name, rate, hikvision_id '
                                      'FROM accountant_employees ORDER BY source_row').fetchall()
        return [Employee(*row[:5], Decimal(row[5]) if row[5] is not None else None, row[6]) for row in rows]

    def monthly_total(self) -> Decimal:
        """Return only salaries explicitly stored in the monthly payroll register."""
        return sum((person.salary for person in self.list_monthly()), Decimal(0))

    def list_monthly(self) -> list[MonthlyEmployee]:
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, external_key, name, role, salary, schedule, card, cash, advances, remaining '
                'FROM accountant_monthly_employees ORDER BY role, name, id').fetchall()
        return [MonthlyEmployee(row[0], row[1], row[2], row[3], Decimal(row[4]), row[5],
                                Decimal(row[6]), Decimal(row[7]), Decimal(row[8]), Decimal(row[9]))
                for row in rows]

    @staticmethod
    def _monthly_values(*, name, role, salary, schedule='', card='0', cash='0', advances='0',
                        remaining='0'):
        name = name.strip() if isinstance(name, str) else ''
        role = role.strip() if isinstance(role, str) else ''
        schedule = schedule.strip() if isinstance(schedule, str) else ''
        if not name or len(name) > 160 or not role or len(role) > 80:
            raise ValueError('Укажите корректные имя и должность.')
        if len(schedule) > 160:
            raise ValueError('График должен быть не длиннее 160 символов.')
        money = {field: parse_money(value) for field, value in {
            'salary': salary, 'card': card, 'cash': cash,
            'advances': advances, 'remaining': remaining,
        }.items()}
        return name, role, schedule, money

    def add_monthly(self, *, name: str, role: str, salary: str, schedule: str = '',
                    card: str = '0', cash: str = '0', advances: str = '0',
                    remaining: str = '0', external_key: str | None = None) -> MonthlyEmployee:
        name, role, schedule, money = self._monthly_values(
            name=name, role=role, salary=salary, schedule=schedule, card=card, cash=cash,
            advances=advances, remaining=remaining)
        key = external_key.strip() if isinstance(external_key, str) and external_key.strip() else None
        with closing(self._open()) as connection, connection:
            try:
                employee_id = connection.execute(
                    'INSERT INTO accountant_monthly_employees '
                    '(external_key,name,role,salary,schedule,card,cash,advances,remaining) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (key, name, role, str(money['salary']), schedule, str(money['card']),
                     str(money['cash']), str(money['advances']), str(money['remaining']))).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError('Сотрудник с таким внешним ключом уже существует.') from None
        return next(item for item in self.list_monthly() if item.id == employee_id)

    def update_monthly(self, employee_id: int, *, name: str, role: str, salary: str,
                       schedule: str = '', card: str = '0', cash: str = '0',
                       advances: str = '0', remaining: str = '0') -> MonthlyEmployee:
        name, role, schedule, money = self._monthly_values(
            name=name, role=role, salary=salary, schedule=schedule, card=card, cash=cash,
            advances=advances, remaining=remaining)
        with closing(self._open()) as connection, connection:
            changed = connection.execute(
                'UPDATE accountant_monthly_employees SET name=?,role=?,salary=?,schedule=?,card=?,cash=?, '
                'advances=?,remaining=? WHERE id=?',
                (name, role, str(money['salary']), schedule, str(money['card']), str(money['cash']),
                 str(money['advances']), str(money['remaining']), employee_id)).rowcount
        if not changed:
            raise ValueError('Сотрудник не найден.')
        return next(item for item in self.list_monthly() if item.id == employee_id)

    def delete_monthly(self, employee_id: int):
        with closing(self._open()) as connection, connection:
            deleted = connection.execute(
                'DELETE FROM accountant_monthly_employees WHERE id = ?', (employee_id,)).rowcount
        if not deleted:
            raise ValueError('Сотрудник не найден.')

    def add(self, *, name: str, role: str, rate: str | None, group_name: str) -> Employee:
        name = name.strip()
        role = role.strip()
        if not name or len(name) > 160:
            raise ValueError('Укажите корректное имя сотрудника.')
        if not role or len(role) > 80:
            raise ValueError('Укажите корректную должность.')
        if group_name not in set(GROUPS.values()) | {'Кухня'}:
            raise ValueError('Неизвестная группа.')
        parsed_rate = parse_rate(rate)
        with closing(self._open()) as connection, connection:
            source_row = connection.execute('SELECT COALESCE(MAX(source_row), 0) + 1 '
                                            'FROM accountant_employees').fetchone()[0]
            employee_id = connection.execute(
                'INSERT INTO accountant_employees (source_row, name, role, group_name, rate) '
                'VALUES (?, ?, ?, ?, ?)',
                (source_row, name, role, group_name,
                 str(parsed_rate) if parsed_rate is not None else None)).lastrowid
        return next(person for person in self.list() if person.id == employee_id)

    def delete(self, employee_id: int):
        with closing(self._open()) as connection, connection:
            deleted = connection.execute('DELETE FROM accountant_employees WHERE id = ?',
                                         (employee_id,)).rowcount
        if not deleted:
            raise ValueError('Сотрудник не найден.')

    def update(self, employee_id: int, *, name: str | None = None, role: str | None = None,
               rate: str | None,
               group_name: str | None = None, reason: str) -> Employee:
        if not reason.strip():
            raise ValueError('Укажите причину изменения.')
        with closing(self._open()) as connection:
            existing = connection.execute('SELECT name, role FROM accountant_employees WHERE id = ?',
                                          (employee_id,)).fetchone()
        if existing is None:
            raise ValueError('Сотрудник не найден.')
        name = (name or existing[0]).strip()
        role = (role or existing[1]).strip()
        if not name or len(name) > 160:
            raise ValueError('Укажите корректное имя сотрудника.')
        if not role or len(role) > 80:
            raise ValueError('Укажите корректную должность.')
        derived_group = group_name or group_for(role)
        if derived_group not in set(GROUPS.values()) | {'Кухня'}:
            raise ValueError('Неизвестная группа.')
        parsed_rate = parse_rate(rate)
        with closing(self._open()) as connection:
            with connection:
                old = connection.execute('SELECT name, role, rate, group_name FROM accountant_employees WHERE id = ?',
                                         (employee_id,)).fetchone()
                if old is None:
                    raise ValueError('Сотрудник не найден.')
                connection.execute('UPDATE accountant_employees SET name = ?, role = ?, rate = ?, group_name = ? '
                                   'WHERE id = ?', (name, role,
                                   str(parsed_rate) if parsed_rate is not None else None,
                                   derived_group, employee_id))
                connection.execute('INSERT INTO accountant_roster_audit '
                                   '(employee_id, changed_at, reason, old_rate, new_rate, old_group, new_group) '
                                   'VALUES (?, ?, ?, ?, ?, ?, ?)',
                                   (employee_id, datetime.now().isoformat(), reason.strip(), old[2],
                                    str(parsed_rate) if parsed_rate is not None else None, old[3], derived_group))
        return next(person for person in self.list() if person.id == employee_id)
