import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4


class DirectorReportStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS director_reports (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, period_start TEXT NOT NULL,
                period_end TEXT NOT NULL, snapshot_json TEXT NOT NULL, analysis_json TEXT NOT NULL,
                pdf_sha256 TEXT NOT NULL, pdf BLOB NOT NULL)''')

    def _connect(self):
        return sqlite3.connect(self.path)

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
