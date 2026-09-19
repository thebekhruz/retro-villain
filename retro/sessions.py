"""In-memory dashboard sessions; restart intentionally signs everyone out."""

import secrets
from threading import Lock


class SessionStore:
    def __init__(self):
        self._roles = {}
        self._lock = Lock()

    def create(self, role: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._roles[token] = role
        return token

    def role(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            return self._roles.get(token)

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._roles.pop(token, None)
