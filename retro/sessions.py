"""Сессии панели переживают перезапуск сервера.

Раньше они жили только в памяти процесса, и каждый деплой выбрасывал всех
на экран входа: за один день это случалось по несколько раз, а смена в зале
ничего про деплой не знает. Поэтому список лежит на диске — на том же
томе, что и базы модулей.

В файле хранится не сам ключ из cookie, а его отпечаток: если файл утечёт,
войти по нему не получится, как и по украденной базе паролей.
"""

import hashlib
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

LIFETIME = timedelta(days=30)


@dataclass(frozen=True)
class SessionIdentity:
    username: str
    role: str


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


class SessionStore:
    def __init__(self, path: Path | None = None, lifetime: timedelta = LIFETIME):
        self._path = Path(path) if path else None
        self._lifetime = lifetime
        self._identities = {}
        self._lock = Lock()
        self._load()

    def create(self, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._identities[fingerprint(token)] = (
                SessionIdentity(username, role), datetime.now(timezone.utc))
            self._save()
        return token

    def identity(self, token: str | None) -> SessionIdentity | None:
        if not token:
            return None
        with self._lock:
            row = self._identities.get(fingerprint(token))
            if row is None:
                return None
            if self._expired(row[1]):
                del self._identities[fingerprint(token)]
                self._save()
                return None
            return row[0]

    def role(self, token: str | None) -> str | None:
        identity = self.identity(token)
        return identity.role if identity else None

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            if self._identities.pop(fingerprint(token), None) is not None:
                self._save()

    def _expired(self, started: datetime) -> bool:
        return datetime.now(timezone.utc) - started >= self._lifetime

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            saved = json.loads(self._path.read_text(encoding='utf-8'))
            rows = saved.items()
        except (OSError, ValueError, AttributeError):
            # Панель важнее сессий: оборванный файл стоит смены входов, а
            # не упавшего сервера.
            return
        for mark, row in rows:
            try:
                started = datetime.fromisoformat(row['started'])
                identity = SessionIdentity(row['username'], row['role'])
            except (TypeError, KeyError, ValueError):
                continue
            if not self._expired(started):
                self._identities[mark] = (identity, started)

    def _save(self) -> None:
        if self._path is None:
            return
        saved = {mark: dict(username=identity.username, role=identity.role,
                            started=started.isoformat())
                 for mark, (identity, started) in self._identities.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Пишем через временный файл: оборванная запись не должна оставить
        # вместо списка сессий половину строки.
        handle, temporary = tempfile.mkstemp(dir=self._path.parent, prefix='.sessions-')
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as file:
                json.dump(saved, file, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
