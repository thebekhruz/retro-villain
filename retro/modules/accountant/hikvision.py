"""Persistent first-entry attendance derived from read-only Hikvision events."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from retro.integrations.hikvision import HikvisionEvent
from retro.runtime import secure_directory, secure_file

from .payroll import AttendanceRow
from .roster import Employee


TZ = ZoneInfo('Asia/Tashkent')
LATE_AFTER = time(10, 0)


@dataclass(frozen=True)
class FirstEntry:
    employee_id: int
    employee_no: str
    occurred_at: datetime


@dataclass(frozen=True)
class SyncState:
    source: str
    cursor_at: datetime | None
    covered_from: datetime | None
    covered_through: datetime | None
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True)
class AttendanceSnapshot:
    rows: tuple[AttendanceRow, ...]
    complete: bool
    health: dict


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError('Hikvision timestamp must include a timezone.')
    return value.astimezone(TZ).isoformat(timespec='seconds')


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class AttendanceStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _open(self):
        secure_directory(self.path.parent)
        connection = sqlite3.connect(self.path, timeout=10)
        secure_file(self.path)
        connection.execute('''CREATE TABLE IF NOT EXISTS hikvision_events (
            source TEXT NOT NULL,
            serial_no TEXT NOT NULL,
            employee_no TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            received_at TEXT NOT NULL,
            PRIMARY KEY (source, serial_no)
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS hikvision_first_entries (
            work_day TEXT NOT NULL,
            employee_id INTEGER NOT NULL,
            employee_no TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            source TEXT NOT NULL,
            serial_no TEXT NOT NULL,
            PRIMARY KEY (work_day, employee_id)
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS hikvision_sync_state (
            source TEXT PRIMARY KEY,
            cursor_at TEXT,
            covered_from TEXT,
            covered_through TEXT,
            last_attempt_at TEXT,
            last_success_at TEXT,
            last_error_code TEXT
        )''')
        return connection

    def ingest(self, event: HikvisionEvent, employee_id: int | None,
               *, received_at: datetime | None = None) -> bool:
        occurred_at = _iso(event.occurred_at)
        received = _iso(received_at or datetime.now(TZ))
        work_day = event.occurred_at.astimezone(TZ).date().isoformat()
        with closing(self._open()) as connection, connection:
            inserted = connection.execute(
                'INSERT OR IGNORE INTO hikvision_events '
                '(source,serial_no,employee_no,occurred_at,received_at) VALUES (?,?,?,?,?)',
                (event.source, event.serial_no, event.employee_no, occurred_at, received)).rowcount
            if employee_id is not None:
                connection.execute('''INSERT INTO hikvision_first_entries
                    (work_day,employee_id,employee_no,occurred_at,source,serial_no)
                    VALUES (?,?,?,?,?,?)
                    ON CONFLICT(work_day,employee_id) DO UPDATE SET
                      employee_no=excluded.employee_no,
                      occurred_at=excluded.occurred_at,
                      source=excluded.source,
                      serial_no=excluded.serial_no
                    WHERE excluded.occurred_at < hikvision_first_entries.occurred_at''',
                    (work_day, employee_id, event.employee_no, occurred_at,
                     event.source, event.serial_no))
        return bool(inserted)

    def reconcile_links(self, employee_ids: dict[str, int]) -> int:
        """Build first entries for events stored before their employees were linked."""
        if not employee_ids:
            return 0
        reconciled = 0
        values = tuple(employee_ids)
        with closing(self._open()) as connection, connection:
            for offset in range(0, len(values), 500):
                batch = values[offset:offset + 500]
                placeholders = ','.join('?' for _ in batch)
                rows = connection.execute(
                    'SELECT source,serial_no,employee_no,occurred_at FROM hikvision_events '
                    f'WHERE employee_no IN ({placeholders})', batch).fetchall()
                for source, serial_no, employee_no, occurred_at in rows:
                    work_day = datetime.fromisoformat(occurred_at).astimezone(TZ).date().isoformat()
                    connection.execute('''INSERT INTO hikvision_first_entries
                        (work_day,employee_id,employee_no,occurred_at,source,serial_no)
                        VALUES (?,?,?,?,?,?)
                        ON CONFLICT(work_day,employee_id) DO UPDATE SET
                          employee_no=excluded.employee_no,
                          occurred_at=excluded.occurred_at,
                          source=excluded.source,
                          serial_no=excluded.serial_no
                        WHERE excluded.occurred_at < hikvision_first_entries.occurred_at''',
                                       (work_day, employee_ids[employee_no], employee_no,
                                        occurred_at, source, serial_no))
                    reconciled += 1
        return reconciled

    def first_entries(self, day: date) -> dict[int, FirstEntry]:
        with closing(self._open()) as connection:
            rows = connection.execute(
                'SELECT employee_id,employee_no,occurred_at FROM hikvision_first_entries '
                'WHERE work_day = ?', (day.isoformat(),)).fetchall()
        return {row[0]: FirstEntry(row[0], row[1], datetime.fromisoformat(row[2])) for row in rows}

    def event_count(self) -> int:
        with closing(self._open()) as connection:
            return connection.execute('SELECT COUNT(*) FROM hikvision_events').fetchone()[0]

    def record_attempt(self, source: str, at: datetime):
        with closing(self._open()) as connection, connection:
            connection.execute('''INSERT INTO hikvision_sync_state(source,last_attempt_at)
                VALUES (?,?) ON CONFLICT(source) DO UPDATE SET last_attempt_at=excluded.last_attempt_at''',
                               (source, _iso(at)))

    def record_failure(self, source: str, *, at: datetime, code: str):
        if code not in {'not_configured', 'network', 'timeout', 'unauthorized',
                        'invalid_response', 'device_error', 'internal'}:
            code = 'internal'
        with closing(self._open()) as connection, connection:
            connection.execute('''INSERT INTO hikvision_sync_state
                (source,last_attempt_at,last_error_code) VALUES (?,?,?)
                ON CONFLICT(source) DO UPDATE SET
                  last_attempt_at=excluded.last_attempt_at,
                  last_error_code=excluded.last_error_code''',
                               (source, _iso(at), code))

    def record_success(self, source: str, *, at: datetime, cursor_at: datetime,
                       covered_from: datetime, covered_through: datetime):
        state = self.sync_state(source)
        merged_from = min(filter(None, (state.covered_from, covered_from)), default=covered_from)
        merged_through = max(filter(None, (state.covered_through, covered_through)),
                             default=covered_through)
        with closing(self._open()) as connection, connection:
            connection.execute('''INSERT INTO hikvision_sync_state
                (source,cursor_at,covered_from,covered_through,last_attempt_at,last_success_at,last_error_code)
                VALUES (?,?,?,?,?,?,NULL)
                ON CONFLICT(source) DO UPDATE SET
                  cursor_at=excluded.cursor_at,
                  covered_from=excluded.covered_from,
                  covered_through=excluded.covered_through,
                  last_attempt_at=excluded.last_attempt_at,
                  last_success_at=excluded.last_success_at,
                  last_error_code=NULL''',
                               (source, _iso(cursor_at), _iso(merged_from), _iso(merged_through),
                                _iso(at), _iso(at)))

    def sync_state(self, source: str) -> SyncState:
        with closing(self._open()) as connection:
            row = connection.execute(
                'SELECT source,cursor_at,covered_from,covered_through,last_attempt_at,'
                'last_success_at,last_error_code FROM hikvision_sync_state WHERE source = ?',
                (source,)).fetchone()
        if row is None:
            return SyncState(source, None, None, None, None, None, None)
        return SyncState(row[0], *(_datetime(value) for value in row[1:6]), row[6])


class AttendanceService:
    def __init__(self, store: AttendanceStore, *, source: str,
                 enabled: bool, poll_seconds: int):
        self.store = store
        self.source = source
        self.enabled = enabled
        self.poll_seconds = poll_seconds

    def snapshot(self, day: date, employees: list[Employee], *,
                 now: datetime | None = None) -> AttendanceSnapshot:
        now = now or datetime.now(TZ)
        if now.tzinfo is None:
            raise ValueError('Current time must include a timezone.')
        now = now.astimezone(TZ)
        entries = self.store.first_entries(day)
        state = self.store.sync_state(self.source)
        start = datetime.combine(day, time.min, TZ)
        end = start + timedelta(days=1)
        complete = bool(day < now.date() and state.covered_from is not None
                        and state.covered_through is not None
                        and state.covered_from <= start and state.covered_through >= end)
        rows = []
        for employee in employees:
            entry = entries.get(employee.id)
            if entry is not None:
                local_time = entry.occurred_at.astimezone(TZ).time().replace(tzinfo=None)
                status = 'late' if local_time > LATE_AFTER else 'on_time'
                rows.append(AttendanceRow(employee.id, status, entry.occurred_at))
            elif employee.hikvision_id is None:
                rows.append(AttendanceRow(employee.id, 'unlinked', None))
            else:
                rows.append(AttendanceRow(employee.id, 'missing' if complete else 'unavailable', None))
        return AttendanceSnapshot(tuple(rows), complete, self._health(state, now, complete))

    def _health(self, state: SyncState, now: datetime, complete: bool) -> dict:
        if not self.enabled:
            status = 'not_configured'
        elif state.last_success_at is None:
            status = state.last_error_code or 'starting'
        elif state.last_error_code and state.last_attempt_at and \
                state.last_attempt_at > state.last_success_at:
            status = state.last_error_code
        elif now - state.last_success_at > timedelta(seconds=max(60, self.poll_seconds * 2)):
            status = 'stale'
        else:
            status = 'ok'
        return {
            'status': status,
            'last_success': _iso(state.last_success_at),
            'covered_from': _iso(state.covered_from),
            'covered_through': _iso(state.covered_through),
            'complete': complete,
        }
