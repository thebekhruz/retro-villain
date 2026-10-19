"""Сессии панели переживают перезапуск сервера и выкат.

Раньше они жили только в памяти процесса, а потом — в файле `sessions.json`
в каталоге данных. На Railway данные лежат в Postgres, а каталог данных —
внутри контейнера: каждый выкат начинался с пустого каталога и выбрасывал
всех на экран входа (T-515). Поэтому сессии живут в той же базе, что и всё
остальное: в Postgres на хостинге, в SQLite-файле локально.

В базе хранится не сам ключ из cookie, а его отпечаток: если база утечёт,
войти по ней не получится, как и по украденной базе паролей.

Сессия, открытая по номеру телефона (ТЗ 09.10, М-05), помнит номер: по нему
приложение проверяет, что номер всё ещё привязан к той же учётной записи.
"""

import hashlib
import json
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from retro.db import as_database

LIFETIME = timedelta(days=30)


@dataclass(frozen=True)
class SessionIdentity:
    username: str
    role: str
    # Номер, по которому открыта сессия; None — вход по логину и паролю.
    phone: str | None = None


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def stamp(moment: datetime) -> str:
    """Время в UTC одной длины: строки в базе сравниваются как время."""
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')


class SessionStore:
    """`target` — база (путь к SQLite, строка Postgres или `Database`);
    None — только в памяти (тесты и локальный запуск без диска).

    `legacy` — прежний `sessions.json`: живые сессии из него переносятся в
    базу один раз, чтобы выкат с этим кодом не выкинул тех, у кого файл
    сохранился (сервер со своим диском)."""

    def __init__(self, target=None, lifetime: timedelta = LIFETIME, *, legacy: Path | None = None):
        self._lifetime = lifetime
        self._lock = Lock()
        self._memory = {}
        self.db = as_database(target) if target is not None else None
        if self.db is not None:
            with closing(self.db.connect()) as connection, connection:
                connection.executescript('''
                    CREATE TABLE IF NOT EXISTS panel_sessions (
                        fingerprint TEXT PRIMARY KEY,
                        username TEXT NOT NULL,
                        role TEXT NOT NULL,
                        phone TEXT,
                        started TEXT NOT NULL
                    );
                ''')
            if legacy is not None:
                self._import_legacy(Path(legacy))

    @property
    def lifetime(self) -> timedelta:
        return self._lifetime

    def create(self, username: str, role: str, *, phone: str | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self._put(fingerprint(token), SessionIdentity(username, role, phone), datetime.now(timezone.utc))
        return token

    def identity(self, token: str | None) -> SessionIdentity | None:
        if not token:
            return None
        mark = fingerprint(token)
        row = self._get(mark)
        if row is None:
            return None
        identity, started = row
        if self._expired(started):
            self._drop(mark)
            return None
        return identity

    def role(self, token: str | None) -> str | None:
        identity = self.identity(token)
        return identity.role if identity else None

    def delete(self, token: str | None) -> None:
        if token:
            self._drop(fingerprint(token))

    def _expired(self, started: datetime) -> bool:
        return datetime.now(timezone.utc) - started >= self._lifetime

    # ── Хранение ───────────────────────────────────────────────────────────

    def _put(self, mark: str, identity: SessionIdentity, started: datetime) -> None:
        if self.db is None:
            with self._lock:
                self._memory[mark] = (identity, started)
            return
        cutoff = stamp(datetime.now(timezone.utc) - self._lifetime)
        with closing(self.db.connect()) as connection, connection:
            connection.execute(
                'INSERT INTO panel_sessions (fingerprint, username, role, phone, started) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(fingerprint) DO NOTHING',
                (mark, identity.username, identity.role, identity.phone, stamp(started)))
            # Истёкшие сессии убираем при каждом новом входе: таблица не растёт.
            connection.execute('DELETE FROM panel_sessions WHERE started < ?', (cutoff,))

    def _get(self, mark: str):
        if self.db is None:
            with self._lock:
                return self._memory.get(mark)
        with closing(self.db.connect()) as connection:
            row = connection.execute(
                'SELECT username, role, phone, started FROM panel_sessions WHERE fingerprint = ?',
                (mark,)).fetchone()
        if row is None:
            return None
        try:
            started = datetime.fromisoformat(row[3])
        except (TypeError, ValueError):
            return None
        return SessionIdentity(row[0], row[1], row[2]), started

    def _drop(self, mark: str) -> None:
        if self.db is None:
            with self._lock:
                self._memory.pop(mark, None)
            return
        with closing(self.db.connect()) as connection, connection:
            connection.execute('DELETE FROM panel_sessions WHERE fingerprint = ?', (mark,))

    def _import_legacy(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            rows = saved.items()
        except (OSError, ValueError, AttributeError):
            # Панель важнее сессий: оборванный файл стоит смены входов, а
            # не упавшего сервера.
            rows = ()
        for mark, row in rows:
            try:
                started = datetime.fromisoformat(row['started'])
                identity = SessionIdentity(row['username'], row['role'], row.get('phone'))
            except (TypeError, KeyError, ValueError):
                continue
            if not self._expired(started):
                self._put(mark, identity, started)
        # Перенесли — файл больше не нужен: второй раз те же сессии (и уже
        # закрытые выходом) из него не вернутся.
        path.unlink(missing_ok=True)
