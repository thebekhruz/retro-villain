"""Persistent, account-scoped conversation history for the founder assistant."""

import sqlite3
from pathlib import Path

from retro.db import as_database
from retro.runtime import secure_directory, secure_file
from retro.schema import migrate_schema


class FounderChatStore:
    def __init__(self, path):
        self.db = as_database(path)
        # .path остаётся для скриптов обслуживания и тестов
        self.path = self.db.path
        with self._connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS founder_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )''')
            connection.execute(
                'CREATE INDEX IF NOT EXISTS founder_chat_owner_id '
                'ON founder_chat_messages(owner,id)')
            migrate_schema(connection, 'founder')

    def _connect(self):
        return self.db.connect()

    def list(self, owner, limit=100):
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Лимит истории должен быть от 1 до 100.')
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT role,content,created_at FROM ('
                'SELECT id,role,content,created_at FROM founder_chat_messages '
                'WHERE owner=? ORDER BY id DESC LIMIT ?) ORDER BY id',
                (owner, limit),
            ).fetchall()
        return [dict(role=row[0], content=row[1], created_at=row[2]) for row in rows]

    def append_exchange(self, owner, question, answer, created_at):
        if not owner or not question.strip() or not answer.strip():
            raise ValueError('Сообщения чата не могут быть пустыми.')
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            connection.executemany(
                'INSERT INTO founder_chat_messages(owner,role,content,created_at) '
                'VALUES (?,?,?,?)',
                [(owner, 'user', question.strip(), created_at),
                 (owner, 'assistant', answer.strip(), created_at)],
            )
            connection.execute(
                'DELETE FROM founder_chat_messages WHERE owner=? AND id NOT IN ('
                'SELECT id FROM founder_chat_messages WHERE owner=? ORDER BY id DESC LIMIT 100)',
                (owner, owner),
            )

    def clear(self, owner):
        with self._connect() as connection:
            cursor = connection.execute(
                'DELETE FROM founder_chat_messages WHERE owner=?', (owner,))
        return cursor.rowcount
