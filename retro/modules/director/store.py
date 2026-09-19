import hashlib
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

    def _connect(self):
        connection = sqlite3.connect(self.path)
        secure_file(self.path)
        return connection

    def create(self, snapshot, analysis, pdf, created_at):
        report_id = uuid4().hex
        with self._connect() as connection:
            connection.execute('INSERT INTO director_reports VALUES (?,?,?,?,?,?,?,?)',
                               (report_id, created_at, snapshot['period_start'], snapshot['period_end'],
                                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                                json.dumps(analysis, ensure_ascii=False, sort_keys=True),
                                hashlib.sha256(pdf).hexdigest(), pdf))
        return report_id

    def get_pdf(self, report_id):
        with self._connect() as connection:
            row = connection.execute('SELECT pdf FROM director_reports WHERE id=?', (report_id,)).fetchone()
        return row[0] if row else None

    def get(self, report_id):
        with self._connect() as connection:
            row = connection.execute('SELECT id,created_at,period_start,period_end,snapshot_json,analysis_json,pdf_sha256 FROM director_reports WHERE id=?', (report_id,)).fetchone()
        return self._row(row) if row else None

    def list(self):
        with self._connect() as connection:
            rows = connection.execute('SELECT id,created_at,period_start,period_end,snapshot_json,analysis_json,pdf_sha256 FROM director_reports ORDER BY created_at DESC').fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row):
        return dict(id=row[0], created_at=row[1], period_start=row[2], period_end=row[3],
                    snapshot=json.loads(row[4]), analysis=json.loads(row[5]), pdf_sha256=row[6])
