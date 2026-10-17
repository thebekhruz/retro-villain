"""Журнал рассылок гостям (ТЗ 16.10, п. 5.3).

Сама рассылка живёт в booking-боте, у нас — только запись о том, что ушло
через эту панель: кто отправил, когда, какой текст, скольким гостям, и
последний известный итог (отправлено / заблокировали бота / ошибок). Итог
обновляется, когда экран опрашивает статус. По каждому гостю бот отдаёт
только общие счётчики, поэтому и журнал хранит только их.

`kind` различает ручные и автоматические сообщения: сегодня отправка только
ручная, `auto` оставлен для будущей автоотправки, чтобы журнал не пришлось
переделывать. Повтор с тем же operation_id (двойное нажатие, обрыв связи) —
та же рассылка у бота и та же строка здесь.
"""
from contextlib import closing

from retro.db import as_database

KINDS = ('manual', 'auto')
TERMINAL = ('completed', 'failed', 'interrupted')
JOURNAL_LIMIT = 30
# Хранить больше, чем показывает экран, незачем: это журнал, а не архив бота.
KEPT = 500
ROW = ('operation_id', 'job_id', 'kind', 'sent_by', 'sent_at', 'text', 'recipients',
       'status', 'audience', 'sent', 'blocked', 'failed', 'status_at')


def _count(job, field):
    value = job.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class BroadcastJournal:
    def __init__(self, path):
        self.db = as_database(path)
        self.path = self.db.path
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS founder_broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'manual' CHECK(kind IN ('manual','auto')),
                sent_by TEXT NOT NULL DEFAULT '',
                sent_at TEXT NOT NULL,
                text TEXT NOT NULL,
                recipients INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '',
                audience INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                status_at TEXT
            )''')
            connection.execute(
                'CREATE INDEX IF NOT EXISTS founder_broadcasts_job ON founder_broadcasts(job_id)')

    def record(self, operation_id, job, *, text, recipients, sent_by, at, kind='manual'):
        """Рассылку принял бот — пишем строку. Повтор того же operation_id
        строку не множит, а лишь освежает итог."""
        if kind not in KINDS:
            raise ValueError('Неизвестный вид рассылки.')
        if not operation_id or not str(text).strip():
            raise ValueError('Запись журнала рассылок неполна.')
        with closing(self.db.connect()) as connection, connection:
            connection.execute(
                'INSERT OR IGNORE INTO founder_broadcasts(operation_id,job_id,kind,sent_by,sent_at,'
                'text,recipients) VALUES (?,?,?,?,?,?,?)',
                (str(operation_id), str(job.get('id') or ''), kind, sent_by or '', at,
                 str(text).strip(), int(recipients)))
            connection.execute(
                'DELETE FROM founder_broadcasts WHERE id NOT IN ('
                'SELECT id FROM founder_broadcasts ORDER BY id DESC LIMIT ?)', (KEPT,))
        self.update_status(job, at, operation_id=operation_id)

    def update_status(self, job, at, *, operation_id=None):
        """Последний известный итог из ответа бота; строки нет — ничего не делаем."""
        job_id = str(job.get('id') or '')
        if not job_id and not operation_id:
            return 0
        with closing(self.db.connect()) as connection, connection:
            cursor = connection.execute(
                'UPDATE founder_broadcasts SET status=?,audience=?,sent=?,blocked=?,failed=?,'
                'status_at=? WHERE job_id=? OR operation_id=?',
                (str(job.get('status') or ''), _count(job, 'audience'), _count(job, 'sent'),
                 _count(job, 'blocked'), _count(job, 'failed'), at, job_id,
                 str(operation_id or job_id)))
        return cursor.rowcount

    def list(self, limit=JOURNAL_LIMIT):
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Лимит журнала должен быть от 1 до 100.')
        with closing(self.db.connect()) as connection, connection:
            rows = connection.execute(
                'SELECT ' + ','.join(ROW) + ' FROM founder_broadcasts ORDER BY id DESC LIMIT ?',
                (limit,)).fetchall()
        return [dict(zip(ROW, row)) for row in rows]
