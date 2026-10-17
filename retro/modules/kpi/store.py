"""Периоды KPI и привязка логинов iiko к сотрудникам реестра. Postgres и SQLite через retro.db."""
import json
from contextlib import closing
from datetime import date, datetime

from retro.db import as_database, table_columns
from retro.modules.cashier.service import TZ

from .rules import KpiError, PILOT_START, default_settings, month_bounds, next_bounds, week_bounds

# Ключи словаря периода — как в API («start», «end»); в SQL даты зовутся start_day/end_day,
# потому что `end` — зарезервированное слово Postgres (в SQLite проходило, CI на Postgres падал).
COLUMNS = ('id', 'kind', 'start', 'end', 'status', 'settings_json', 'snapshot_json', 'created_at', 'created_by',
           'closed_at', 'closed_by', 'payouts_json')
SQL_COLUMNS = tuple({'start': 'start_day', 'end': 'end_day'}.get(name, name) for name in COLUMNS)


def now_text():
    return datetime.now(TZ).isoformat(timespec='seconds')


class KpiStore:
    def __init__(self, path):
        self.db = as_database(path)
        with closing(self._open()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS kpi_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, start_day TEXT NOT NULL, end_day TEXT NOT NULL,
                status TEXT NOT NULL, settings_json TEXT NOT NULL, snapshot_json TEXT, created_at TEXT NOT NULL,
                created_by TEXT, closed_at TEXT, closed_by TEXT, payouts_json TEXT)''')
            if 'end' in table_columns(connection, 'kpi_periods'):
                # Базы, созданные до переименования (локальные SQLite): колонки start/end → start_day/end_day.
                connection.execute('ALTER TABLE kpi_periods RENAME COLUMN "start" TO start_day')
                connection.execute('ALTER TABLE kpi_periods RENAME COLUMN "end" TO end_day')
            connection.execute('CREATE INDEX IF NOT EXISTS kpi_periods_start ON kpi_periods(start_day, id)')
            connection.execute('''CREATE TABLE IF NOT EXISTS kpi_waiters (
                waiter TEXT PRIMARY KEY, employee_id INTEGER, active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL)''')

    def _open(self):
        return self.db.connect()

    @staticmethod
    def _row(row):
        item = dict(zip(COLUMNS, row))
        item['settings'] = json.loads(item.pop('settings_json'))
        snapshot, payouts = item.pop('snapshot_json'), item.pop('payouts_json')
        item['snapshot'] = json.loads(snapshot) if snapshot else None
        item['payouts'] = json.loads(payouts) if payouts else None
        return item

    def list(self):
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT ' + ', '.join(SQL_COLUMNS) + ' FROM kpi_periods ORDER BY start_day DESC, id DESC').fetchall()
        return [self._row(row) for row in rows]

    def get(self, period_id):
        with closing(self._open()) as connection:
            row = connection.execute('SELECT ' + ', '.join(SQL_COLUMNS) + ' FROM kpi_periods WHERE id = ?', (period_id,)).fetchone()
        return self._row(row) if row else None

    def create(self, kind, start: date, end: date, settings, *, by=None):
        if kind not in ('week', 'month'):
            raise KpiError('Период — неделя или месяц.')
        if end < start:
            raise KpiError('Конец периода раньше начала.')
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            clash = connection.execute(
                "SELECT id FROM kpi_periods WHERE status = 'open' AND NOT (end_day < ? OR start_day > ?)",
                (start.isoformat(), end.isoformat())).fetchone()
            if clash:
                raise KpiError('На эти даты уже есть открытый период.')
            cursor = connection.execute(
                'INSERT INTO kpi_periods (kind, start_day, end_day, status, settings_json, created_at, created_by) '
                'VALUES (?,?,?,?,?,?,?)',
                (kind, start.isoformat(), end.isoformat(), 'open',
                 json.dumps(settings, ensure_ascii=False, sort_keys=True), now_text(), by))
            period_id = cursor.lastrowid
        return self.get(period_id)

    def current(self, today: date):
        """Открытый период, в который попадает день, иначе ближайший открытый впереди, иначе последний."""
        periods = self.list()
        open_periods = [p for p in periods if p['status'] == 'open']
        for period in open_periods:
            if period['start'] <= today.isoformat() <= period['end']:
                return period
        upcoming = [p for p in open_periods if p['start'] > today.isoformat()]
        if upcoming:
            return min(upcoming, key=lambda p: p['start'])
        return periods[0] if periods else None

    def ensure_current(self, today: date, *, by=None):
        """Пилот: если периодов нет, заводится неделя с понедельника 19.10.2026 (или текущая, если позже)."""
        period = self.current(today)
        if period is not None:
            return period
        start, end = week_bounds(max(today, PILOT_START))
        return self.create('week', start, end, default_settings(), by=by)

    def update_settings(self, period_id, settings, *, by=None, kind=None, start=None, end=None):
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT status, kind, start_day, end_day FROM kpi_periods WHERE id = ?', (period_id,)).fetchone()
            if row is None:
                raise KpiError('Период не найден.')
            if row[0] != 'open':
                raise KpiError('Период закрыт: его настройки больше не меняются.')
            kind = kind or row[1]
            start = (start or date.fromisoformat(row[2])).isoformat()
            end = (end or date.fromisoformat(row[3])).isoformat()
            if end < start:
                raise KpiError('Конец периода раньше начала.')
            clash = connection.execute(
                "SELECT id FROM kpi_periods WHERE status = 'open' AND id <> ? AND NOT (end_day < ? OR start_day > ?)",
                (period_id, start, end)).fetchone()
            if clash:
                raise KpiError('На эти даты уже есть другой открытый период.')
            connection.execute('UPDATE kpi_periods SET settings_json = ?, kind = ?, start_day = ?, end_day = ? WHERE id = ?',
                               (json.dumps(settings, ensure_ascii=False, sort_keys=True), kind, start, end, period_id))
        return self.get(period_id)

    def close(self, period_id, snapshot, *, by=None):
        """Снимок фиксируется, следующий период той же длины открывается с теми же настройками."""
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT status, kind, end_day, settings_json FROM kpi_periods WHERE id = ?',
                                     (period_id,)).fetchone()
            if row is None:
                raise KpiError('Период не найден.')
            if row[0] != 'open':
                raise KpiError('Период уже закрыт.')
            connection.execute('UPDATE kpi_periods SET status = ?, snapshot_json = ?, closed_at = ?, closed_by = ? WHERE id = ?',
                               ('closed', json.dumps(snapshot, ensure_ascii=False, sort_keys=True), now_text(), by, period_id))
            start, end = next_bounds(row[1], date.fromisoformat(row[2]))
            exists = connection.execute("SELECT id FROM kpi_periods WHERE status = 'open' AND NOT (end_day < ? OR start_day > ?)",
                                        (start.isoformat(), end.isoformat())).fetchone()
            if not exists:
                connection.execute(
                    'INSERT INTO kpi_periods (kind, start_day, end_day, status, settings_json, created_at, created_by) '
                    'VALUES (?,?,?,?,?,?,?)', (row[1], start.isoformat(), end.isoformat(), 'open', row[3], now_text(), by))
        return self.get(period_id)

    def set_payouts(self, period_id, payouts):
        with closing(self._open()) as connection, connection:
            connection.execute('UPDATE kpi_periods SET payouts_json = ? WHERE id = ?',
                               (json.dumps(payouts, ensure_ascii=False, sort_keys=True), period_id))

    # ── логины iiko → сотрудники ──
    def links(self) -> dict:
        with closing(self._open()) as connection:
            rows = connection.execute('SELECT waiter, employee_id, active FROM kpi_waiters').fetchall()
        return {row[0]: dict(employee_id=row[1], active=bool(row[2])) for row in rows}

    def set_link(self, waiter: str, employee_id, *, active=True):
        waiter = (waiter or '').strip()
        if not waiter:
            raise KpiError('Нужен логин официанта из iiko.')
        with closing(self._open()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM kpi_waiters WHERE waiter = ?', (waiter,))
            connection.execute('INSERT INTO kpi_waiters (waiter, employee_id, active, updated_at) VALUES (?,?,?,?)',
                               (waiter, employee_id, 1 if active else 0, now_text()))

    def delete_link(self, waiter: str):
        """Снять ручную отметку: логин снова решает реестр официантов (T-469)."""
        with closing(self._open()) as connection, connection:
            connection.execute('DELETE FROM kpi_waiters WHERE waiter = ?', ((waiter or '').strip(),))
