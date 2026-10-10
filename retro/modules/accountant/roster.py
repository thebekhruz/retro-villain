"""Local employee roster imported from the approved payroll worksheet."""

# Аннотации не вычисляются при импорте: ниже в классе есть метод list(),
# и на Python 3.12 подпись «-> list[MonthlyEmployee]» бралась бы за него,
# а не за встроенный тип. На 3.14 аннотации ленивые и это не всплывает,
# поэтому локально всё работало, а на сервере приложение не поднималось.
from __future__ import annotations

import base64
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from retro.db import as_database, table_columns, table_exists
from retro.request_reads import once
from retro.runtime import secure_directory, secure_file
from retro.integrations.hikvision import HikvisionPerson
from retro.modules.cashier.service import TZ, today_tashkent

from .names import device_name_matches, person_name, role_name


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

HIKVISION_ID = re.compile(r'[0-9A-Za-z_-]+')
# Номер для Hikvision выдаём сами: следующий после самого большого известного.
# Длинные числа (номера карт и т. п.) в счёт не берём.
EMPLOYEE_NO_DIGITS = 9


def unique_violation(error: Exception) -> bool:
    """Нарушение уникальности — одинаково для SQLite и Postgres."""
    return 'unique' in str(error).lower() or 'integrity' in type(error).__name__.lower()


class HikvisionIdTaken(ValueError):
    """Номер в Hikvision уже привязан к другому сотруднику."""

    def __init__(self, employee_no: str, owner: str | None):
        self.employee_no, self.owner = employee_no, owner
        super().__init__(f'ID {employee_no} в Hikvision уже привязан к сотруднику «{owner}».' if owner
                         else f'ID {employee_no} в Hikvision уже привязан к другому сотруднику.')


UNASSIGNED_ROLE = 'Должность не указана'
UNASSIGNED_GROUP = 'Не распределено'


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


# Фото сотрудника: снимок с телефона менеджера, уже ужатый экраном.
PHOTO_TYPES = ('image/jpeg', 'image/png')
MAX_PHOTO_BYTES = 2 * 1024 * 1024


class DeviceNamesakes(ValueError):
    """На устройстве несколько непривязанных людей с этим именем (или тёзка
    есть и в реестре): чей номер — решает бухгалтер, сами не выбираем."""


def normalized_hikvision_name(value: str) -> str:
    # Payroll exports and Hikvision may store the same full name in a
    # different order (surname first vs. given name first).  Sorting complete
    # whitespace-delimited parts keeps the match strict while ignoring order;
    # the two-sided uniqueness checks below still reject duplicate names.
    return ' '.join(sorted(value.casefold().split()))


@dataclass(frozen=True)
class Employee:
    id: int
    source_row: int
    name: str
    role: str
    group_name: str
    rate: Decimal | None
    hikvision_id: str | None
    # «Нет в Hikvision · отмечать вручную»: человек проходит мимо турникета
    # (охрана, уборка). Привязку к устройству такой сотрудник не получает, и
    # его день отмечает бухгалтер, как у любого непривязанного.
    manual_attendance: bool = False
    # С какого дня действует ручная отметка (день включения флага). До него
    # «был» по умолчанию не ставится: новый человек не получает оплату за дни,
    # когда его ещё не было. None — флаг стоял раньше, чем появилась дата:
    # такие сотрудники «был» по умолчанию во все дни, как и прежде.
    manual_since: date | None = None
    # Кабинет менеджера (ТЗ 09.10, М-01): сменный или временный, направление
    # менеджера и кто завёл карточку. У старых карточек — «сменный» и пусто.
    employment_type: str = 'shift'
    direction: str | None = None
    created_by: str | None = None

    def json(self):
        return dict(id=self.id, name=self.name, role=self.role, group=self.group_name,
                    rate=str(self.rate) if self.rate is not None else None,
                    hikvision_registered=self.hikvision_id is not None,
                    # Номер на устройстве — для поля «ID в Hikvision» в «Сотрудниках».
                    hikvision_id=self.hikvision_id,
                    manual_attendance=self.manual_attendance,
                    manual_since=self.manual_since.isoformat() if self.manual_since else None,
                    employment_type=self.employment_type, direction=self.direction,
                    created_by=self.created_by)


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
    # «⊘ Hik»: окладник не проходит турникет. На выплаты не влияет — это
    # пометка для экранов (1a, 2b, 6a), как у сменных «отмечать вручную».
    no_hikvision: bool = False

    def json(self):
        return dict(id=self.id, external_key=self.external_key, name=self.name, role=self.role,
                    salary=str(self.salary), schedule=self.schedule, card=str(self.card),
                    cash=str(self.cash), advances=str(self.advances), remaining=str(self.remaining),
                    no_hikvision=self.no_hikvision,
                    accounting_basis='manual_current_register',
                    warning='Ручной текущий реестр: поля выплат и остатка не сверены с движениями; месяц не задан.')


class RosterStore:
    def __init__(self, path: Path):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        self._initialize()

    def _open(self):
        return self.db.connect()

    def _initialize(self):
        with closing(self._open()) as connection, connection:
            connection.execute('PRAGMA journal_mode=WAL')
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
            employee_columns = table_columns(connection, 'accountant_employees')
            if 'manual_attendance' not in employee_columns:
                connection.execute('ALTER TABLE accountant_employees '
                                   'ADD COLUMN manual_attendance INTEGER NOT NULL DEFAULT 0')
            if 'manual_since' not in employee_columns:
                # У уже отмеченных вручную дата остаётся пустой — «всегда»:
                # вчерашняя смена у них по-прежнему выдаётся как «был».
                connection.execute('ALTER TABLE accountant_employees ADD COLUMN manual_since TEXT')
            # Кабинет менеджера (ТЗ 09.10, М-01…М-03). Колонки только
            # добавляются: старые карточки становятся «сменными» без менеджера,
            # их выплаты, посещаемость и привязки остаются как были.
            # employment_type/direction/created_by/created_at/request_key —
            # из первой версии кабинета, где менеджер сам заводил карточки;
            # теперь карточки заводит только бухгалтер, и эти колонки больше не
            # пишутся, но остаются (на проде они уже могут быть).
            # hikvision_employee_no — номер для устройства, выданный до
            # отправки: повтор шлёт тот же номер. hikvision_id появляется,
            # только когда устройство подтвердило.
            # face_* — отправка лица на устройство: человек и его лицо уходят
            # отдельными запросами, и сбой лица не отменяет добавленного человека.
            for column, declaration in (
                    ('employment_type', "TEXT NOT NULL DEFAULT 'shift'"),
                    ('direction', 'TEXT'), ('created_by', 'TEXT'), ('created_at', 'TEXT'),
                    ('request_key', 'TEXT'), ('hikvision_employee_no', 'TEXT'),
                    ('hikvision_state', 'TEXT'), ('hikvision_error', 'TEXT'),
                    ('hikvision_synced_at', 'TEXT'), ('face_state', 'TEXT'), ('face_error', 'TEXT'),
                    ('face_synced_at', 'TEXT')):
                if column not in employee_columns:
                    connection.execute(f'ALTER TABLE accountant_employees ADD COLUMN {column} {declaration}')
            # Фото сотрудника — своей таблицей, чтобы списки реестра не тащили
            # снимки. Base64-текстом: одинаково в SQLite и Postgres, без
            # BLOB/BYTEA. updated_at — версия снимка для адреса ?v= и для
            # отметки «лицо отправлено» именно этого снимка.
            connection.execute('''CREATE TABLE IF NOT EXISTS accountant_employee_photos (
                employee_id INTEGER PRIMARY KEY,
                mime TEXT NOT NULL,
                data TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT
            )''')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS accountant_employee_request_key '
                               'ON accountant_employees(request_key) WHERE request_key IS NOT NULL')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS accountant_employee_hikvision_no '
                               'ON accountant_employees(hikvision_employee_no) '
                               'WHERE hikvision_employee_no IS NOT NULL')
            columns = table_columns(connection, 'accountant_monthly_employees')
            if 'external_key' not in columns:
                connection.execute('ALTER TABLE accountant_monthly_employees ADD COLUMN external_key TEXT')
                connection.execute(
                    'CREATE UNIQUE INDEX IF NOT EXISTS accountant_monthly_external_key '
                    'ON accountant_monthly_employees(external_key) WHERE external_key IS NOT NULL')
            if 'no_hikvision' not in columns:
                connection.execute('ALTER TABLE accountant_monthly_employees '
                                   'ADD COLUMN no_hikvision INTEGER NOT NULL DEFAULT 0')
            # Удаление окладника = архив: строка остаётся, чтобы выплаты
            # прошлых дней и ведомость показывали имя, а не «удалён · №».
            if 'archived' not in columns:
                connection.execute('ALTER TABLE accountant_monthly_employees '
                                   'ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
            # История реестра (1a): кроме ставки и группы — что сделали, кто и
            # с чем (добавил, удалил в архив, «без Hikvision», имя/должность),
            # и чей это сотрудник: сменный (shift) или на окладе (monthly).
            audit_columns = table_columns(connection, 'accountant_roster_audit')
            for column in ('action', 'changed_by', 'kind', 'details'):
                if column not in audit_columns:
                    connection.execute(f'ALTER TABLE accountant_roster_audit ADD COLUMN {column} TEXT')
            # Preserve values used for previous calendar days. The initial version
            # is a baseline, not a reconstruction of changes before this migration.
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS accountant_employee_versions (
                    employee_id INTEGER NOT NULL, effective_day TEXT NOT NULL,
                    source_row INTEGER NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
                    group_name TEXT NOT NULL, rate TEXT, hikvision_id TEXT,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(employee_id, effective_day)
                );
                INSERT OR IGNORE INTO accountant_employee_versions
                    SELECT id,'0001-01-01',source_row,name,role,group_name,rate,hikvision_id,0
                    FROM accountant_employees WHERE id NOT IN
                        (SELECT employee_id FROM accountant_employee_versions);
                -- Историю ставок ведёт сам код (_stamp_version), а не триггеры:
                -- они были на диалекте SQLite и в Postgres не переносятся, а
                -- один денежный инвариант не должен жить в двух вариантах.
                -- У существующих баз триггеры сносим, иначе история писалась бы
                -- дважды.
                DROP TRIGGER IF EXISTS accountant_employee_insert_history;
                DROP TRIGGER IF EXISTS accountant_employee_update_history;
                DROP TRIGGER IF EXISTS accountant_employee_delete_history;
            ''')


    # ── История ставок ─────────────────────────────────────────────────────
    # `list(day)` читает версию с наибольшим effective_day <= день, поэтому
    # каждая правка реестра обязана оставить снимок на день правки. Раньше это
    # делали три триггера SQLite; теперь — этот метод, вызываемый из всех путей
    # записи в одной с ними транзакции.

    def _stamp_version(self, connection, employee_id: int, *, deleted: bool = False,
                       day: str | None = None) -> None:
        """Записать текущее состояние сотрудника в историю на указанный день."""
        effective = day or today_tashkent().isoformat()
        connection.execute(
            'INSERT INTO accountant_employee_versions '
            '(employee_id, effective_day, source_row, name, role, group_name, rate, '
            ' hikvision_id, deleted) '
            'SELECT id, ?, source_row, name, role, group_name, rate, hikvision_id, ? '
            'FROM accountant_employees WHERE id = ? '
            'ON CONFLICT(employee_id, effective_day) DO UPDATE SET '
            'source_row=excluded.source_row, name=excluded.name, role=excluded.role, '
            'group_name=excluded.group_name, rate=excluded.rate, '
            'hikvision_id=excluded.hikvision_id, deleted=excluded.deleted',
            (effective, 1 if deleted else 0, employee_id))

    @staticmethod
    def _audit(connection, employee_id: int, *, action: str, reason: str, old_rate=None, new_rate=None,
               old_group: str = '', new_group: str = '', by: str | None = None, kind: str = 'shift',
               details: str = '') -> None:
        """Строка истории реестра: кто, когда, что сделал, было → стало."""
        connection.execute(
            'INSERT INTO accountant_roster_audit (employee_id, changed_at, reason, old_rate, new_rate, '
            'old_group, new_group, action, changed_by, kind, details) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (employee_id, datetime.now(TZ).isoformat(timespec='seconds'), reason,
             str(old_rate) if old_rate is not None else None,
             str(new_rate) if new_rate is not None else None,
             old_group or '', new_group or '', action, by, kind, details))

    def history(self, employee_id: int, *, kind: str = 'shift') -> list[dict]:
        """История сотрудника, новые записи сверху. Старые строки (до
        колонки kind) — правки ставки сменных."""
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT changed_at, reason, old_rate, new_rate, old_group, new_group, action, changed_by, details '
                'FROM accountant_roster_audit WHERE employee_id = ? AND COALESCE(kind, ?) = ? '
                'ORDER BY id DESC', (employee_id, 'shift', kind)).fetchall()
        return [dict(changed_at=row[0], reason=row[1], old_rate=row[2], new_rate=row[3],
                     old_group=row[4] or None, new_group=row[5] or None, action=row[6] or 'update',
                     changed_by=row[7], details=row[8] or '') for row in rows]

    def import_xlsx(self, source: Path, *, replace: bool = False) -> dict[str, int]:
        workbook = load_workbook(source, read_only=True, data_only=True)
        try:
            if 'ЗП' not in workbook.sheetnames:
                raise ValueError('В файле нет листа «ЗП».')
            sheet = workbook['ЗП']
            rows = []
            for cells in sheet.iter_rows(min_row=5, min_col=1, max_col=4):
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
                    placeholders = ','.join('?' for _ in source_rows)
                    # Снимок «удалён» снимаем до удаления, пока строки ещё есть.
                    for (gone_id,) in connection.execute(
                            'SELECT id FROM accountant_employees WHERE source_row NOT IN (%s)'
                            % placeholders, tuple(source_rows)).fetchall():
                        self._stamp_version(connection, gone_id, deleted=True)
                    connection.execute('DELETE FROM accountant_employees WHERE source_row NOT IN (%s)' %
                                       placeholders, tuple(source_rows))
                    connection.execute('DELETE FROM accountant_employee_photos WHERE employee_id NOT IN '
                                       '(SELECT id FROM accountant_employees)')
                for row in rows:
                    cursor = connection.execute(
                        'SELECT id,name FROM accountant_employees WHERE source_row = ?',
                        (row[0],))
                    current = cursor.fetchone()
                    if current is None:
                        new_id = connection.execute(
                            'INSERT INTO accountant_employees '
                            '(source_row, name, role, group_name, rate) VALUES (?, ?, ?, ?, ?)',
                            row).lastrowid
                        # Импорт задаёт исходное состояние, поэтому версия с
                        # начала времён: прошлые дни должны видеть сотрудника.
                        self._stamp_version(connection, new_id, day='0001-01-01')
                        imported += 1
                    else:
                        if replace:
                            preserve_link = (normalized_hikvision_name(current[1]) ==
                                             normalized_hikvision_name(row[1]))
                            if not preserve_link:
                                raise ValueError('Строка импорта относится к другому сотруднику. '
                                                 'Нельзя заменять человека под прежним ID.')
                            connection.execute(
                                'UPDATE accountant_employees SET name=?, role=?, group_name=?, rate=?, '
                                'hikvision_id=CASE WHEN ? THEN hikvision_id ELSE NULL END '
                                'WHERE source_row=?',
                                (row[1], row[2], row[3], row[4], preserve_link, row[0]))
                            self._stamp_version(connection, current[0])
                            imported += 1
                        else:
                            existing += 1
        return {'imported': imported, 'existing': existing}

    @once
    def list(self, day=None) -> list[Employee]:
        with closing(self._open()) as connection:
            if day is None:
                rows = connection.execute('SELECT id, source_row, name, role, group_name, rate, hikvision_id, '
                                          'manual_attendance, manual_since, employment_type, direction, '
                                          'created_by FROM accountant_employees ORDER BY source_row').fetchall()
            else:
                rows = connection.execute('''
                    SELECT employee_id,source_row,name,role,group_name,rate,
                        COALESCE(v.hikvision_id,
                            (SELECT e.hikvision_id FROM accountant_employees e WHERE e.id=v.employee_id),
                            (SELECT h.hikvision_id FROM accountant_employee_versions h
                             WHERE h.employee_id=v.employee_id AND h.hikvision_id IS NOT NULL
                             ORDER BY h.effective_day DESC LIMIT 1)),
                        COALESCE((SELECT e.manual_attendance FROM accountant_employees e
                                  WHERE e.id=v.employee_id), 0),
                        (SELECT e.manual_since FROM accountant_employees e WHERE e.id=v.employee_id),
                        (SELECT e.employment_type FROM accountant_employees e WHERE e.id=v.employee_id),
                        (SELECT e.direction FROM accountant_employees e WHERE e.id=v.employee_id),
                        (SELECT e.created_by FROM accountant_employees e WHERE e.id=v.employee_id)
                    FROM accountant_employee_versions v WHERE deleted=0 AND effective_day=(
                        SELECT MAX(effective_day) FROM accountant_employee_versions h
                        WHERE h.employee_id=v.employee_id AND h.effective_day<=?)
                    ORDER BY source_row
                ''', (day.isoformat(),)).fetchall()
        # Ручная отметка сильнее привязки: турникет такого человека не видит,
        # и его вход не должен ни засчитываться, ни считаться прогулом.
        return [Employee(*row[:5], Decimal(row[5]) if row[5] is not None else None,
                         None if row[7] else row[6], bool(row[7]),
                         date.fromisoformat(row[8]) if row[7] and row[8] else None,
                         row[9] or 'shift', row[10], row[11]) for row in rows]

    def set_manual_attendance(self, employee_id: int, manual: bool, *, by: str | None = None) -> Employee:
        """Включить или снять ручную отметку.

        Включение запоминает день (по Ташкенту): «был» по умолчанию ставится
        только с него. Повторное включение дату не сдвигает, снятие — стирает.
        """
        since = today_tashkent().isoformat()
        with closing(self._open()) as connection, connection:
            before = connection.execute('SELECT manual_attendance, rate, group_name FROM accountant_employees '
                                        'WHERE id = ?', (employee_id,)).fetchone()
            # В SET справа — значения до правки: дата ставится только при
            # переходе «выкл → вкл».
            changed = connection.execute(
                'UPDATE accountant_employees SET manual_attendance = ?, manual_since = CASE '
                'WHEN ? = 0 THEN NULL WHEN manual_attendance = 1 THEN manual_since ELSE ? END '
                'WHERE id = ?',
                (1 if manual else 0, 1 if manual else 0, since, employee_id)).rowcount
            if changed:
                self._stamp_version(connection, employee_id)
                if bool(before[0]) != manual:
                    self._audit(connection, employee_id, action='manual', by=by,
                                reason=('Нет в Hikvision · отмечать вручную' if manual
                                        else 'Снова по Hikvision'),
                                old_rate=before[1], new_rate=before[1],
                                old_group=before[2], new_group=before[2])
        if not changed:
            raise ValueError('Сотрудник не найден.')
        return next(person for person in self.list() if person.id == employee_id)

    def set_hikvision_id(self, employee_id: int, value: str | None, *, by: str | None = None) -> tuple[str | None, str | None]:
        """Привязать сотрудника к устройству вручную: его номер в Hikvision
        (employeeNo). Пусто — снять привязку. Номер уникален: занятый другим
        сотрудником — HikvisionIdTaken (409 с именем владельца).

        Номер — не свойство дня, а то, кто этот человек на устройстве, поэтому
        он же записывается и в историю версий: иначе прошлые дни смотрели бы на
        старый номер. Возвращает (было, стало)."""
        new = (value or '').strip() or None
        if new is not None and (len(new) > 32 or not HIKVISION_ID.fullmatch(new)):
            raise ValueError('ID в Hikvision — номер сотрудника на устройстве: цифры и латиница, до 32 знаков.')
        with closing(self._open()) as connection, connection:
            row = connection.execute('SELECT hikvision_id, rate, group_name FROM accountant_employees WHERE id = ?',
                                     (employee_id,)).fetchone()
            if row is None:
                raise ValueError('Сотрудник не найден.')
            old = row[0]
            if old == new:
                return old, new
            if new is not None:
                # Номер, выданный менеджером под отправку, тоже занят: под ним на
                # устройстве другой человек (ТЗ 09.10, М-03).
                owner = connection.execute('SELECT id, name FROM accountant_employees WHERE id <> ? AND '
                                           '(hikvision_id = ? OR hikvision_employee_no = ?)',
                                           (employee_id, new, new)).fetchone()
                if owner is not None:
                    raise HikvisionIdTaken(new, owner[1])
            try:
                connection.execute('UPDATE accountant_employees SET hikvision_id = ? WHERE id = ?', (new, employee_id))
            except Exception as error:
                # Гонка двух привязок: уникальность держит сама база (SQLite и Postgres).
                if 'unique' in str(error).lower() or 'integrity' in type(error).__name__.lower():
                    raise HikvisionIdTaken(new, None) from None
                raise
            connection.execute('UPDATE accountant_employee_versions SET hikvision_id = ? WHERE employee_id = ?',
                               (new, employee_id))
            self._stamp_version(connection, employee_id)
            self._audit(connection, employee_id, action='hikvision', by=by,
                        reason='Привязка к Hikvision' if new else 'Привязка к Hikvision снята',
                        old_rate=row[1], new_rate=row[1], old_group=row[2], new_group=row[2],
                        details=f'ID в Hikvision: {old or "—"} → {new or "—"}')
        return old, new

    def link_hikvision_people(self, people: tuple[HikvisionPerson, ...]) -> dict[str, int]:
        """Link only two-sided unique exact normalized names; never overwrite IDs."""
        unique_people = {person.employee_no: person for person in people if person.employee_no}
        employees = self.list()
        already_ids = {employee.hikvision_id for employee in employees if employee.hikvision_id}
        # Карточки, отправленные из кабинета менеджера, ждут свой номер: по
        # имени их не привязываем, а их номер ни к кому другому не прикрепляем
        # (ТЗ 09.10, М-03).
        reserved = self._reserved_numbers()
        already_ids |= set(reserved)
        unlinked_by_name: dict[str, list[Employee]] = {}
        for employee in employees:
            if employee.hikvision_id is None and not employee.manual_attendance \
                    and employee.id not in {row[0] for row in reserved.values()}:
                key = normalized_hikvision_name(employee.name)
                if key:
                    unlinked_by_name.setdefault(key, []).append(employee)
        people_by_name: dict[str, list[HikvisionPerson]] = {}
        for person in unique_people.values():
            if person.employee_no in already_ids:
                continue
            key = normalized_hikvision_name(person.name or '')
            if key:
                people_by_name.setdefault(key, []).append(person)

        result = dict(people=len(unique_people), linked=0, already_linked=0,
                      ambiguous=0, unmatched=0)
        with closing(self._open()) as connection, connection:
            for person in unique_people.values():
                if person.employee_no in reserved:
                    # Номер выдан под отправку, а ответ устройства мы потеряли:
                    # человек там есть — значит, отправка дошла.
                    employee_id, name = reserved[person.employee_no]
                    if device_name_matches(person.name, name) and \
                            self._confirm_number(connection, employee_id, person.employee_no):
                        result['linked'] += 1
                    else:
                        result['ambiguous'] += 1
                    continue
                if person.employee_no in already_ids:
                    result['already_linked'] += 1
                    continue
                key = normalized_hikvision_name(person.name or '')
                candidates = unlinked_by_name.get(key, []) if key else []
                device_candidates = people_by_name.get(key, []) if key else []
                if key and (len(candidates) > 1 or len(device_candidates) > 1):
                    result['ambiguous'] += 1
                    continue
                if not key or not candidates:
                    result['unmatched'] += 1
                    continue
                try:
                    changed = connection.execute(
                        'UPDATE accountant_employees SET hikvision_id = ? '
                        'WHERE id = ? AND hikvision_id IS NULL',
                        (person.employee_no, candidates[0].id)).rowcount
                except sqlite3.IntegrityError:
                    changed = 0
                if changed:
                    self._stamp_version(connection, candidates[0].id)
                    result['linked'] += 1
                    already_ids.add(person.employee_no)
                else:
                    result['ambiguous'] += 1
        return result

    def import_hikvision_people(self, people: tuple[HikvisionPerson, ...]) -> dict[str, int]:
        """Create missing device people with explicit placeholders and link by employeeNo.

        This is intentionally separate from background polling: importing people changes the
        accountant roster and must only be triggered by an authenticated operator.
        """
        unique_people = {person.employee_no: person for person in people if person.employee_no}
        people_by_name: dict[str, list[HikvisionPerson]] = {}
        for person in unique_people.values():
            key = normalized_hikvision_name(person.name or '')
            if key:
                people_by_name.setdefault(key, []).append(person)

        result = dict(people=len(unique_people), created=0, linked=0,
                      already_linked=0, ambiguous=0)
        with closing(self._open()) as connection, connection:
            rows = connection.execute(
                'SELECT id,name,COALESCE(hikvision_id,hikvision_employee_no) FROM accountant_employees '
                'ORDER BY source_row').fetchall()
            by_name: dict[str, list[tuple[int, str | None]]] = {}
            linked_ids = set()
            # Номер, выданный менеджером, считается занятым: иначе импорт завёл
            # бы под ним вторую карточку («Должность не указана»).
            for employee_id, name, hikvision_id in rows:
                key = normalized_hikvision_name(name)
                if key:
                    by_name.setdefault(key, []).append((employee_id, hikvision_id))
                if hikvision_id:
                    linked_ids.add(hikvision_id)

            next_source_row = connection.execute(
                'SELECT COALESCE(MAX(source_row), 0) + 1 FROM accountant_employees').fetchone()[0]
            for person in unique_people.values():
                if person.employee_no in linked_ids:
                    result['already_linked'] += 1
                    continue
                key = normalized_hikvision_name(person.name or '')
                device_matches = people_by_name.get(key, []) if key else []
                roster_matches = by_name.get(key, []) if key else []
                if not key or len(device_matches) != 1 or len(roster_matches) > 1:
                    result['ambiguous'] += 1
                    continue
                if roster_matches:
                    employee_id, current_link = roster_matches[0]
                    if current_link:
                        result['ambiguous'] += 1
                        continue
                    connection.execute(
                        'UPDATE accountant_employees SET hikvision_id=? WHERE id=?',
                        (person.employee_no, employee_id))
                    self._stamp_version(connection, employee_id)
                    linked_ids.add(person.employee_no)
                    result['linked'] += 1
                    continue
                # last_insert_rowid() есть только в SQLite; слой отдаёт номер
                # через lastrowid на обоих диалектах.
                new_id = connection.execute(
                    'INSERT INTO accountant_employees '
                    '(source_row,name,role,group_name,rate,hikvision_id) VALUES (?,?,?,?,NULL,?)',
                    (next_source_row, person.name.strip(), UNASSIGNED_ROLE,
                     UNASSIGNED_GROUP, person.employee_no)).lastrowid
                self._stamp_version(connection, new_id, day='0001-01-01')
                by_name[key] = [(new_id, person.employee_no)]
                linked_ids.add(person.employee_no)
                next_source_row += 1
                result['created'] += 1
        return result

    def monthly_total(self) -> Decimal:
        """Return only salaries explicitly stored in the monthly payroll register."""
        return sum((person.salary for person in self.list_monthly()), Decimal(0))
    @once
    def list_monthly(self, *, archived: bool = False) -> list[MonthlyEmployee]:
        """Окладники в реестре; archived=True — удалённые (в архиве)."""
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT id, external_key, name, role, salary, schedule, card, cash, advances, remaining, '
                'no_hikvision FROM accountant_monthly_employees WHERE archived = ? ORDER BY role, name, id',
                (1 if archived else 0,)).fetchall()
        return [MonthlyEmployee(row[0], row[1], row[2], row[3], Decimal(row[4]), row[5],
                                Decimal(row[6]), Decimal(row[7]), Decimal(row[8]), Decimal(row[9]),
                                bool(row[10]))
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

    @staticmethod
    def _changed_cyrillic(before, name: str, role: str) -> tuple[str, str]:
        """Кириллица — только у изменённых имени и должности (ТЗ 09.10, М-04)."""
        return (person_name(name) if name != before[0] else name,
                role_name(role) if role != before[1] else role)

    def add_monthly(self, *, name: str, role: str, salary: str, schedule: str = '',
                    card: str = '0', cash: str = '0', advances: str = '0',
                    remaining: str = '0', external_key: str | None = None,
                    no_hikvision: bool | None = None, by: str | None = None,
                    cyrillic: bool = False) -> MonthlyEmployee:
        name, role, schedule, money = self._monthly_values(
            name=name, role=role, salary=salary, schedule=schedule, card=card, cash=cash,
            advances=advances, remaining=remaining)
        if cyrillic:
            name, role = person_name(name), role_name(role)
        key = external_key.strip() if isinstance(external_key, str) and external_key.strip() else None
        with closing(self._open()) as connection, connection:
            try:
                employee_id = connection.execute(
                    'INSERT INTO accountant_monthly_employees '
                    '(external_key,name,role,salary,schedule,card,cash,advances,remaining,no_hikvision) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (key, name, role, str(money['salary']), schedule, str(money['card']),
                     str(money['cash']), str(money['advances']), str(money['remaining']),
                     1 if no_hikvision else 0)).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError('Сотрудник с таким внешним ключом уже существует.') from None
            self._audit(connection, employee_id, action='create', kind='monthly', by=by,
                        reason='Добавлен на оклад', new_rate=money['salary'],
                        details='Без Hikvision' if no_hikvision else '')
        return next(item for item in self.list_monthly() if item.id == employee_id)

    def update_monthly(self, employee_id: int, *, name: str, role: str, salary: str,
                       schedule: str = '', card: str = '0', cash: str = '0',
                       advances: str = '0', remaining: str = '0', no_hikvision: bool | None = None,
                       by: str | None = None, reason: str = '', cyrillic: bool = False) -> MonthlyEmployee:
        name, role, schedule, money = self._monthly_values(
            name=name, role=role, salary=salary, schedule=schedule, card=card, cash=cash,
            advances=advances, remaining=remaining)
        with closing(self._open()) as connection, connection:
            before = connection.execute(
                'SELECT name, role, salary, no_hikvision FROM accountant_monthly_employees WHERE id=? AND archived=0',
                (employee_id,)).fetchone()
            if before is not None and cyrillic:
                name, role = self._changed_cyrillic(before, name, role)
            flag = bool(before[3]) if before and no_hikvision is None else bool(no_hikvision)
            changed = connection.execute(
                'UPDATE accountant_monthly_employees SET name=?,role=?,salary=?,schedule=?,card=?,cash=?, '
                'advances=?,remaining=?,no_hikvision=? WHERE id=? AND archived=0',
                (name, role, str(money['salary']), schedule, str(money['card']), str(money['cash']),
                 str(money['advances']), str(money['remaining']), 1 if flag else 0, employee_id)).rowcount
            if changed:
                notes = [f'{label}: {old} → {new}' for label, old, new in
                         (('Имя', before[0], name), ('Должность', before[1], role)) if old != new]
                if bool(before[3]) != flag:
                    notes.append('Без Hikvision' if flag else 'По Hikvision')
                if notes or Decimal(before[2]) != money['salary']:
                    self._audit(connection, employee_id, action='update', kind='monthly', by=by,
                                reason=reason.strip() or 'Изменение оклада', old_rate=before[2],
                                new_rate=money['salary'], details='; '.join(notes))
        if not changed:
            raise ValueError('Сотрудник не найден.')
        return next(item for item in self.list_monthly() if item.id == employee_id)

    def update_monthly_basics(self, employee_id: int, *, name: str, role: str,
                              salary: str, by: str | None = None,
                              reason: str = 'Изменено директором', cyrillic: bool = False) -> MonthlyEmployee:
        """Имя, должность и оклад без касания выплат, которые ведёт бухгалтер."""
        name, role, _, money = self._monthly_values(name=name, role=role, salary=salary)
        with closing(self._open()) as connection, connection:
            before = connection.execute(
                'SELECT name, role, salary FROM accountant_monthly_employees WHERE id=? AND archived=0',
                (employee_id,)).fetchone()
            if before is not None and cyrillic:
                name, role = self._changed_cyrillic(before, name, role)
            changed = connection.execute(
                'UPDATE accountant_monthly_employees SET name=?,role=?,salary=? WHERE id=? AND archived=0',
                (name, role, str(money['salary']), employee_id)).rowcount
            if changed:
                notes = [f'{label}: {old} → {new}' for label, old, new in
                         (('Имя', before[0], name), ('Должность', before[1], role)) if old != new]
                if notes or Decimal(before[2]) != money['salary']:
                    self._audit(connection, employee_id, action='update', kind='monthly', by=by,
                                reason=reason, old_rate=before[2], new_rate=money['salary'],
                                details='; '.join(notes))
        if not changed:
            raise ValueError('Сотрудник не найден.')
        return next(item for item in self.list_monthly() if item.id == employee_id)

    def delete_monthly(self, employee_id: int, *, by: str | None = None):
        with closing(self._open()) as connection, connection:
            before = connection.execute('SELECT salary FROM accountant_monthly_employees '
                                        'WHERE id = ? AND archived = 0', (employee_id,)).fetchone()
            if before is not None:
                self._audit(connection, employee_id, action='delete', kind='monthly', by=by,
                            reason='Удалён из реестра', old_rate=before[0])
            deleted = connection.execute(
                'UPDATE accountant_monthly_employees SET archived = 1 WHERE id = ? AND archived = 0',
                (employee_id,)).rowcount
        if not deleted:
            raise ValueError('Сотрудник не найден.')

    def add(self, *, name: str, role: str, rate: str | None, group_name: str,
            by: str | None = None, cyrillic: bool = False) -> Employee:
        """cyrillic=True — ввод с экрана: имя и должность только кириллицей
        (ТЗ 09.10, М-04). Импорт и обслуживание пишут как есть."""
        name = name.strip()
        role = role.strip()
        if not name or len(name) > 160:
            raise ValueError('Укажите корректное имя сотрудника.')
        if not role or len(role) > 80:
            raise ValueError('Укажите корректную должность.')
        if cyrillic:
            name, role = person_name(name), role_name(role)
        if group_name not in set(GROUPS.values()) | {'Кухня', UNASSIGNED_GROUP}:
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
            # Новый сотрудник действует с начала времён: иначе прошлые дни его
            # не увидят, а начисления за них уже закрыты.
            self._stamp_version(connection, employee_id, day='0001-01-01')
            self._audit(connection, employee_id, action='create', by=by, reason='Добавлен в реестр',
                        new_rate=parsed_rate, new_group=group_name)
        return next(person for person in self.list() if person.id == employee_id)

    def delete(self, employee_id: int, *, by: str | None = None):
        """Удаление = архив: в сегодняшних и будущих списках сотрудника нет,
        прошлые дни (версии реестра) и начисления остаются как были."""
        with closing(self._open()) as connection, connection:
            before = connection.execute('SELECT rate, group_name FROM accountant_employees WHERE id = ?',
                                        (employee_id,)).fetchone()
            if before is not None:
                self._audit(connection, employee_id, action='delete', by=by,
                            reason='Удалён из реестра: прошлые дни сохранены',
                            old_rate=before[0], old_group=before[1])
            # Снимок снимаем до удаления: после него строки уже нет.
            self._stamp_version(connection, employee_id, deleted=True)
            deleted = connection.execute('DELETE FROM accountant_employees WHERE id = ?',
                                         (employee_id,)).rowcount
            connection.execute('DELETE FROM accountant_employee_photos WHERE employee_id = ?', (employee_id,))
        if not deleted:
            raise ValueError('Сотрудник не найден.')

    def update(self, employee_id: int, *, name: str | None = None, role: str | None = None,
               rate: str | None,
               group_name: str | None = None, reason: str, by: str | None = None,
               cyrillic: bool = False) -> Employee:
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
        # Кириллицу проверяем только у нового ввода: старое имя из выгрузки
        # или Hikvision остаётся как было, пока его не переименуют.
        if cyrillic:
            name, role = self._changed_cyrillic(existing, name, role)
        derived_group = group_name or group_for(role)
        if derived_group not in set(GROUPS.values()) | {'Кухня', UNASSIGNED_GROUP}:
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
                self._stamp_version(connection, employee_id)
                if old[2] is None and parsed_rate is not None:
                    # Ставки не было — её впервые задали. Дни без ставки ждали её,
                    # поэтому она действует и на них: пустая ставка в истории
                    # заменяется новой. Начисленные дни не меняются — их суммы
                    # заморожены в начислениях. Смена уже заданной ставки
                    # по-прежнему действует только с сегодняшнего дня.
                    connection.execute(
                        'UPDATE accountant_employee_versions SET rate = ? '
                        'WHERE employee_id = ? AND rate IS NULL',
                        (str(parsed_rate), employee_id))
                notes = [f'{label}: {before} → {after}' for label, before, after in
                         (('Имя', old[0], name), ('Должность', old[1], role)) if before != after]
                self._audit(connection, employee_id, action='update', by=by, reason=reason.strip(),
                            old_rate=old[2], new_rate=parsed_rate, old_group=old[3],
                            new_group=derived_group, details='; '.join(notes))
        return next(person for person in self.list() if person.id == employee_id)

    # ── Кабинет менеджера (ТЗ 09.10, М-01…М-03) ────────────────────────────
    # Карточки заводит бухгалтер. Менеджер выбирает человека своего
    # направления, фотографирует, и система отправляет в Hikvision человека
    # (если его там ещё нет) и его лицо. Номер для устройства выдаётся и
    # записывается ДО отправки, поэтому повтор после обрыва связи шлёт тот же
    # номер, а не заводит второго человека. hikvision_id (привязка, по
    # которой идут проходы) появляется только после подтверждения устройства.

    def _reserved_numbers(self) -> dict[str, tuple[int, str]]:
        """Номера, выданные под отправку, но ещё не подтверждённые устройством."""
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT hikvision_employee_no, id, name FROM accountant_employees '
                'WHERE hikvision_employee_no IS NOT NULL AND hikvision_id IS NULL').fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}

    @staticmethod
    def _known_numbers(connection) -> set[str]:
        known = set()
        for (value,) in connection.execute(
                'SELECT hikvision_id FROM accountant_employees WHERE hikvision_id IS NOT NULL '
                'UNION SELECT hikvision_employee_no FROM accountant_employees '
                'WHERE hikvision_employee_no IS NOT NULL '
                'UNION SELECT hikvision_id FROM accountant_employee_versions WHERE hikvision_id IS NOT NULL'):
            known.add(value)
        # Проходы людей, которых нет в реестре, тоже держат свой номер.
        if table_exists(connection, 'hikvision_events'):
            known.update(row[0] for row in connection.execute(
                'SELECT DISTINCT employee_no FROM hikvision_events'))
        return known

    def _next_employee_no(self, connection, also: set[str] = frozenset()) -> str:
        numbers = [int(value) for value in self._known_numbers(connection) | set(also)
                   if value and value.isdigit() and len(value) <= EMPLOYEE_NO_DIGITS]
        return str(max(numbers, default=0) + 1)

    def reserve_employee_no(self, employee_id: int, device_people: tuple[HikvisionPerson, ...]) -> str:
        """Номер для устройства сотруднику без привязки — до отправки, чтобы
        повтор шёл с тем же номером. Уже выданный номер не меняется.

        Человек с этим именем уже есть на устройстве и ни к кому не привязан —
        берём его номер, второго не заводим (сверка та же, что у опроса:
        имя без регистра и порядка слов, уникальное с обеих сторон). Таких
        несколько или тёзка есть и в реестре — DeviceNamesakes. Иначе —
        новый номер выше всех известных и всех номеров на устройстве."""
        on_device = {person.employee_no for person in device_people if person.employee_no}
        for _ in range(5):
            try:
                with closing(self._open()) as connection, connection:
                    if not self.db.is_postgres:
                        connection.execute('BEGIN IMMEDIATE')
                    row = connection.execute(
                        'SELECT name, hikvision_id, hikvision_employee_no FROM accountant_employees WHERE id = ?',
                        (employee_id,)).fetchone()
                    if row is None:
                        raise ValueError('Сотрудник не найден.')
                    if row[1] or row[2]:
                        return row[1] or row[2]
                    key = normalized_hikvision_name(row[0])
                    taken = {value for (value,) in connection.execute(
                        'SELECT hikvision_id FROM accountant_employees WHERE hikvision_id IS NOT NULL '
                        'UNION SELECT hikvision_employee_no FROM accountant_employees '
                        'WHERE hikvision_employee_no IS NOT NULL')}
                    candidates = {person.employee_no for person in device_people
                                  if person.employee_no and person.employee_no not in taken
                                  and normalized_hikvision_name(person.name or '') == key}
                    namesakes = [other for other, in connection.execute(
                        'SELECT name FROM accountant_employees WHERE id <> ? AND hikvision_id IS NULL '
                        'AND manual_attendance = 0', (employee_id,))
                        if normalized_hikvision_name(other) == key]
                    if len(candidates) > 1 or (candidates and namesakes):
                        raise DeviceNamesakes(key)
                    number = candidates.pop() if candidates else self._next_employee_no(connection, on_device)
                    connection.execute('UPDATE accountant_employees SET hikvision_employee_no = ? '
                                       'WHERE id = ? AND hikvision_id IS NULL AND hikvision_employee_no IS NULL',
                                       (number, employee_id))
                return number
            except Exception as error:
                if not unique_violation(error):
                    raise
                # Номер в ту же секунду выдали другому — берём следующий.
        raise ValueError('Не удалось выдать номер для Hikvision.')

    def manager_card(self, employee_id: int) -> dict | None:
        rows = self.manager_cards(employee_id=employee_id)
        return rows[0] if rows else None

    def manager_cards(self, *, employee_id: int | None = None) -> list[dict]:
        """Поля карточки для кабинета менеджера — без ставок и выплат. От фото
        только версия (photo_updated_at): сам снимок читает photo()."""
        query = ('SELECT e.id, e.name, e.role, e.group_name, e.hikvision_id, e.manual_attendance, '
                 'e.employment_type, e.hikvision_employee_no, e.hikvision_state, e.hikvision_error, '
                 'e.hikvision_synced_at, e.face_state, e.face_error, e.face_synced_at, p.updated_at '
                 'FROM accountant_employees e LEFT JOIN accountant_employee_photos p ON p.employee_id = e.id')
        params = ()
        if employee_id is not None:
            query += ' WHERE e.id = ?'
            params = (employee_id,)
        with closing(self._open()) as connection:
            rows = connection.execute(query + ' ORDER BY e.source_row', params).fetchall()
        return [dict(id=row[0], name=row[1], role=row[2], group=row[3], hikvision_id=row[4],
                     manual_attendance=bool(row[5]), employment_type=row[6] or 'shift',
                     hikvision_employee_no=row[7], hikvision_state=row[8], hikvision_error=row[9],
                     hikvision_synced_at=row[10], face_state=row[11], face_error=row[12],
                     face_synced_at=row[13], photo_updated_at=row[14]) for row in rows]

    def set_photo(self, employee_id: int, mime: str, content: bytes, *, by: str | None = None) -> str:
        """Новое фото сотрудника. Лицо на устройстве после этого снова «ждёт
        отправки»: на нём старый снимок. Возвращает версию снимка."""
        if mime not in PHOTO_TYPES or not content or len(content) > MAX_PHOTO_BYTES:
            raise ValueError('Нужна фотография JPEG или PNG до 2 МБ.')
        now = datetime.now(TZ)
        with closing(self._open()) as connection, connection:
            exists = connection.execute('SELECT 1 FROM accountant_employees WHERE id = ?',
                                        (employee_id,)).fetchone()
            if exists is None:
                raise ValueError('Сотрудник не найден.')
            before = connection.execute('SELECT updated_at FROM accountant_employee_photos WHERE employee_id = ?',
                                        (employee_id,)).fetchone()
            replaced = before is not None
            # Версия только растёт: два снимка в одну миллисекунду не делят
            # ни адрес ?v=, ни отметку «лицо отправлено».
            if replaced and now <= datetime.fromisoformat(before[0]):
                now = datetime.fromisoformat(before[0]) + timedelta(milliseconds=1)
            stamp = now.isoformat(timespec='milliseconds')
            connection.execute(
                'INSERT INTO accountant_employee_photos (employee_id, mime, data, updated_at, updated_by) '
                'VALUES (?, ?, ?, ?, ?) ON CONFLICT(employee_id) DO UPDATE SET mime = excluded.mime, '
                'data = excluded.data, updated_at = excluded.updated_at, updated_by = excluded.updated_by',
                (employee_id, mime, base64.b64encode(content).decode('ascii'), stamp, by))
            connection.execute("UPDATE accountant_employees SET face_state = 'pending', face_error = NULL "
                               'WHERE id = ?', (employee_id,))
            self._audit(connection, employee_id, action='update', by=by,
                        reason='Фото заменено' if replaced else 'Добавлено фото')
        return stamp

    def photo(self, employee_id: int) -> tuple[bytes, str, str] | None:
        """(снимок, MIME, версия) или None, если фото нет."""
        with closing(self._open()) as connection:
            row = connection.execute('SELECT data, mime, updated_at FROM accountant_employee_photos '
                                     'WHERE employee_id = ?', (employee_id,)).fetchone()
        if row is None:
            return None
        return base64.b64decode(row[0]), row[1], row[2]

    def record_face(self, employee_id: int, state: str, error: str | None, *, version: str,
                    by: str | None = None) -> bool:
        """Итог отправки лица — только для того снимка, что ушёл (version).
        Пока шла отправка, менеджер мог загрузить новое фото: тогда отметка
        не ставится, и новое лицо по-прежнему ждёт отправки."""
        stamp = datetime.now(TZ).isoformat(timespec='seconds')
        with closing(self._open()) as connection, connection:
            updated = connection.execute(
                'UPDATE accountant_employees SET face_state = ?, face_error = ?, face_synced_at = ? '
                'WHERE id = ? AND EXISTS (SELECT 1 FROM accountant_employee_photos '
                'WHERE employee_id = ? AND updated_at = ?)',
                (state, error, stamp, employee_id, employee_id, version)).rowcount
            if updated and state == 'sent':
                self._audit(connection, employee_id, action='hikvision', by=by,
                            reason='Фото добавлено в Hikvision')
        return bool(updated)

    def reassign_employee_no(self, employee_id: int, taken: set[str], *, by: str | None = None) -> str:
        """Номер на устройстве занят чужим человеком — выдать новый, выше
        занятых. Только пока отправка не подтверждена."""
        for _ in range(5):
            try:
                with closing(self._open()) as connection, connection:
                    row = connection.execute(
                        'SELECT hikvision_employee_no, hikvision_id FROM accountant_employees WHERE id = ?',
                        (employee_id,)).fetchone()
                    if row is None:
                        raise ValueError('Сотрудник не найден.')
                    if row[1] is not None:
                        return row[1]
                    number = self._next_employee_no(connection, set(taken) | {row[0] or ''})
                    connection.execute('UPDATE accountant_employees SET hikvision_employee_no = ? '
                                       'WHERE id = ? AND hikvision_id IS NULL', (number, employee_id))
                    self._audit(connection, employee_id, action='hikvision', by=by,
                                reason='Номер на устройстве занят другим человеком',
                                details=f'Номер для Hikvision: {row[0] or "—"} → {number}')
                return number
            except Exception as error:
                if not unique_violation(error):
                    raise
        raise ValueError('Не удалось выдать номер для Hikvision.')

    def record_hikvision_attempt(self, employee_id: int, state: str, error: str | None) -> None:
        """Итог отправки, не ставший успехом: «ожидает» или «ошибка» с причиной."""
        with closing(self._open()) as connection, connection:
            connection.execute(
                'UPDATE accountant_employees SET hikvision_state = ?, hikvision_error = ?, '
                'hikvision_synced_at = ? WHERE id = ? AND hikvision_id IS NULL',
                (state, error, datetime.now(TZ).isoformat(timespec='seconds'), employee_id))

    def confirm_hikvision_number(self, employee_id: int, employee_no: str, *, by: str | None = None) -> bool:
        """Устройство подтвердило человека под номером: это и есть привязка.
        False — номер тем временем привязали к другому сотруднику."""
        with closing(self._open()) as connection, connection:
            return self._confirm_number(connection, employee_id, employee_no, by=by)

    def _confirm_number(self, connection, employee_id: int, employee_no: str, *, by: str | None = None) -> bool:
        row = connection.execute('SELECT hikvision_id, rate, group_name FROM accountant_employees WHERE id = ?',
                                 (employee_id,)).fetchone()
        if row is None:
            return False
        stamp = datetime.now(TZ).isoformat(timespec='seconds')
        if row[0] == employee_no:
            connection.execute("UPDATE accountant_employees SET hikvision_state = 'sent', hikvision_error = NULL "
                               'WHERE id = ?', (employee_id,))
            return True
        owner = connection.execute('SELECT name FROM accountant_employees WHERE hikvision_id = ? AND id <> ?',
                                   (employee_no, employee_id)).fetchone()
        if row[0] is not None or owner is not None:
            connection.execute("UPDATE accountant_employees SET hikvision_state = 'error', "
                               "hikvision_error = 'number_linked', hikvision_synced_at = ? WHERE id = ?",
                               (stamp, employee_id))
            return False
        connection.execute(
            "UPDATE accountant_employees SET hikvision_id = ?, hikvision_employee_no = ?, "
            "hikvision_state = 'sent', hikvision_error = NULL, hikvision_synced_at = ? WHERE id = ?",
            (employee_no, employee_no, stamp, employee_id))
        connection.execute('UPDATE accountant_employee_versions SET hikvision_id = ? WHERE employee_id = ?',
                           (employee_no, employee_id))
        self._stamp_version(connection, employee_id)
        self._audit(connection, employee_id, action='hikvision', by=by, reason='Добавлен в Hikvision',
                    old_rate=row[1], new_rate=row[1], old_group=row[2], new_group=row[2],
                    details=f'ID в Hikvision: — → {employee_no}')
        return True
