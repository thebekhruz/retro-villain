import hashlib
from collections import OrderedDict
from time import monotonic
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from retro.runtime import secure_directory, secure_file


class DirectorReportStore:
    def __init__(self, path):
        self.path = Path(path)
        secure_directory(self.path.parent)
        with self._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS director_reports (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, period_start TEXT NOT NULL,
                period_end TEXT NOT NULL, snapshot_json TEXT NOT NULL, analysis_json TEXT NOT NULL,
                pdf_sha256 TEXT NOT NULL, pdf BLOB NOT NULL)''')
            connection.execute(
                'DELETE FROM director_reports WHERE rowid NOT IN '
                '(SELECT MAX(rowid) FROM director_reports GROUP BY period_start, period_end)')
            connection.execute(
                'CREATE UNIQUE INDEX IF NOT EXISTS director_report_period '
                'ON director_reports(period_start, period_end)')

    def _connect(self):
        connection = sqlite3.connect(self.path)
        secure_file(self.path)
        return connection

    def create(self, snapshot, analysis, pdf, created_at):
        return self.create_or_replace(snapshot, analysis, pdf, created_at, 1000)

    def create_or_replace(self, snapshot, analysis, pdf, created_at, retention):
        if not isinstance(retention, int) or retention < 1:
            raise ValueError('Срок хранения отчётов должен быть положительным числом.')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            existing = connection.execute(
                'SELECT id FROM director_reports WHERE period_start=? AND period_end=?',
                (snapshot['period_start'], snapshot['period_end'])).fetchone()
            report_id = existing[0] if existing else uuid4().hex
            values = (created_at, json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                      json.dumps(analysis, ensure_ascii=False, sort_keys=True),
                      hashlib.sha256(pdf).hexdigest(), pdf, report_id)
            if existing:
                connection.execute(
                    'UPDATE director_reports SET created_at=?,snapshot_json=?,analysis_json=?,pdf_sha256=?,pdf=? '
                    'WHERE id=?', values)
            else:
                connection.execute(
                    'INSERT INTO director_reports '
                    '(created_at,snapshot_json,analysis_json,pdf_sha256,pdf,id,period_start,period_end) '
                    'VALUES (?,?,?,?,?,?,?,?)',
                    values[:-1] + (report_id, snapshot['period_start'], snapshot['period_end']))
            keep = [row[0] for row in connection.execute(
                'SELECT id FROM director_reports ORDER BY period_end DESC, created_at DESC, id DESC '
                'LIMIT ?', (retention,))]
            connection.execute(
                'DELETE FROM director_reports WHERE id NOT IN (%s)' % ','.join('?' for _ in keep), keep)
        return report_id

    def get_pdf(self, report_id):
        with self._connect() as connection:
            row = connection.execute('SELECT pdf FROM director_reports WHERE id=?', (report_id,)).fetchone()
        return row[0] if row else None

    def get(self, report_id):
        with self._connect() as connection:
            row = connection.execute('SELECT id,created_at,period_start,period_end,snapshot_json,analysis_json,pdf_sha256 FROM director_reports WHERE id=?', (report_id,)).fetchone()
        return self._row(row) if row else None

    def get_for_period(self, period_start, period_end):
        with self._connect() as connection:
            row = connection.execute(
                'SELECT id,created_at,period_start,period_end,snapshot_json,analysis_json,pdf_sha256 '
                'FROM director_reports WHERE period_start=? AND period_end=?',
                (period_start, period_end),
            ).fetchone()
        return self._row(row) if row else None

    def list(self):
        with self._connect() as connection:
            rows = connection.execute('SELECT id,created_at,period_start,period_end,snapshot_json,analysis_json,pdf_sha256 FROM director_reports ORDER BY created_at DESC').fetchall()
        return [self._row(row) for row in rows]

    def list_metadata(self):
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT id,created_at,period_start,period_end,analysis_json,pdf_sha256 '
                'FROM director_reports ORDER BY period_end DESC, created_at DESC, id DESC').fetchall()
        result = []
        for row in rows:
            analysis = json.loads(row[4])
            result.append(dict(id=row[0], created_at=row[1], period_start=row[2], period_end=row[3],
                               analysis_summary=analysis.get('summary', ''), pdf_sha256=row[5]))
        return result

    @staticmethod
    def _row(row):
        return dict(id=row[0], created_at=row[1], period_start=row[2], period_end=row[3],
                    snapshot=json.loads(row[4]), analysis=json.loads(row[5]), pdf_sha256=row[6])


class PeriodCache:
    """Готовый отчёт за период, чтобы каждый заход не пересобирал его в iiko.

    Закрытые дни в iiko почти не меняются, а сборка периода занимает секунды:
    без кеша переключение между модулями каждый раз ждало заново. Кнопка
    «Обновить» кеш обходит.
    """

    def __init__(self, ttl=900, limit=16):
        self.ttl, self.limit = ttl, limit
        self.entries = OrderedDict()

    def get(self, key):
        entry = self.entries.get(key)
        if entry is None:
            return None
        created, value = entry
        if monotonic() - created > self.ttl:
            del self.entries[key]
            return None
        return value

    def put(self, key, value):
        self.entries[key] = (monotonic(), value)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)
