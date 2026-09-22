"""In-memory dashboard sessions; restart intentionally signs everyone out."""

import secrets
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class SessionIdentity:
    username: str
    role: str


class SessionStore:
    def __init__(self):
        self._identities = {}
        self._lock = Lock()

    def create(self, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._identities[token] = SessionIdentity(username, role)
        return token

    def identity(self, token: str | None) -> SessionIdentity | None:
        if not token:
            return None
        with self._lock:
            return self._identities.get(token)

    def role(self, token: str | None) -> str | None:
        identity = self.identity(token)
        return identity.role if identity else None

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._identities.pop(token, None)
