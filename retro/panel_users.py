"""Учётные записи панели в базе (T-442).

Раньше логины, пароли, номера и направления жили только в переменных
окружения (DASHBOARD_PANEL_USERS, DASHBOARD_PHONE_USERS,
DASHBOARD_MANAGER_DIRECTIONS): чтобы завести человека, надо было править
Railway и перезапускать панель. Теперь источник — таблица `panel_users` в
общей базе (Postgres на бою, SQLite локально).

Переменные остаются запасным источником на время переезда: запись из базы
сильнее одноимённой из окружения, а логин, которого в базе нет, ищется в
переменных, как раньше. Перенос — `python -m retro.panel_users import-env`,
после него переменные можно снять.

В базе — только отпечаток пароля (PBKDF2 с солью). Пароль у записи может
отсутствовать: такой человек входит только по номеру и коду из SMS.
У записи из базы вход по номеру открыт любой роли — номер ей вписал
администратор. Для переменных правило прежнее: только PHONE_ROLES.
"""

import hashlib
import hmac
import re
import secrets
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock

from retro.db import as_database, table_columns
from retro.modules.manager.directions import DIRECTIONS
from retro.phone_numbers import PHONE_ROLES, normalize_phone

ROLES = ('cashier', 'accountant', 'director', 'founder', 'admin', 'shokh', 'manager')
HASH_NAME = 'pbkdf2_sha256'
HASH_ROUNDS = 200_000
MIN_PASSWORD = 8
MAX_PASSWORD = 256
# Сколько секунд процесс верит прочитанной записи. Сессия проверяется на
# каждом запросе; без кэша это лишний запрос к базе на каждый клик.
CACHE_SECONDS = 5.0
LOGIN_PATTERN = re.compile(r'[^\s:;=]{1,64}')


class PanelUserError(ValueError):
    """Отказ словами для администратора."""


@dataclass(frozen=True)
class PanelAccount:
    login: str
    role: str
    # Направления менеджера; пусто — весь ресторан.
    directions: tuple[str, ...] = ()
    phone: str | None = None
    source: str = 'db'          # db — из таблицы, env — из переменных
    active: bool = True
    has_password: bool = False
    updated_at: str | None = None
    updated_by: str | None = None


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), HASH_ROUNDS).hex()
    return f'{HASH_NAME}${HASH_ROUNDS}${salt}${digest}'


def password_matches(password: str, stored: str | None) -> bool:
    if not stored:
        # Сравнение всё равно делаем: ответ не должен выдавать, есть ли пароль.
        hashlib.pbkdf2_hmac('sha256', password.encode(), b'retro-no-password', HASH_ROUNDS)
        return False
    try:
        name, rounds, salt, digest = stored.split('$')
        expected = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    except ValueError:
        return False
    return name == HASH_NAME and hmac.compare_digest(expected, digest)


def check_login(login: str) -> str:
    login = (login or '').strip()
    if not LOGIN_PATTERN.fullmatch(login):
        raise PanelUserError('Логин — до 64 знаков без пробелов, «:», «;» и «=».')
    return login


def check_role(role: str) -> str:
    if role not in ROLES:
        raise PanelUserError('Роль — одна из: ' + ', '.join(ROLES) + '.')
    return role


def check_password(password: str) -> str:
    if not MIN_PASSWORD <= len(password or '') <= MAX_PASSWORD:
        raise PanelUserError(f'Пароль — от {MIN_PASSWORD} до {MAX_PASSWORD} знаков.')
    return password


def check_phone(raw) -> str:
    phone = normalize_phone(raw)
    if phone is None:
        raise PanelUserError(f'«{raw}» — не номер вида +998XXXXXXXXX.')
    return phone


def check_directions(raw) -> tuple[str, ...]:
    items = raw if isinstance(raw, (list, tuple)) else str(raw or '').split(',')
    directions = tuple(item.strip() for item in items if str(item).strip())
    unknown = [item for item in directions if item not in DIRECTIONS]
    if unknown or len(set(directions)) != len(directions):
        raise PanelUserError('Направления — ' + ', '.join(DIRECTIONS) + '.')
    return directions


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


_COLUMNS = 'login, role, password_hash, phone, directions, active, updated_at, updated_by'


def _account(row) -> PanelAccount:
    login, role, password_hash, phone, directions, active, updated_at, updated_by = row
    return PanelAccount(login=login, role=role, directions=tuple(filter(None, (directions or '').split(','))),
                        phone=phone or None, source='db', active=bool(active),
                        has_password=bool(password_hash), updated_at=updated_at, updated_by=updated_by)


class PanelUserStore:
    """Таблица `panel_users`: одна строка — одна учётная запись."""

    def __init__(self, target):
        self.db = as_database(target)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS panel_users (
                login TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                password_hash TEXT,
                phone TEXT UNIQUE,
                directions TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL)''')
            # Кто менял запись последним: экран «Пользователи» (T-445) или команда.
            if 'updated_by' not in table_columns(connection, 'panel_users'):
                connection.execute('ALTER TABLE panel_users ADD COLUMN updated_by TEXT')

    def all(self) -> list[PanelAccount]:
        with closing(self.db.connect()) as connection:
            rows = connection.execute(f'SELECT {_COLUMNS} FROM panel_users ORDER BY role, login').fetchall()
        return [_account(row) for row in rows]

    def get(self, login: str) -> PanelAccount | None:
        with closing(self.db.connect()) as connection:
            row = connection.execute(f'SELECT {_COLUMNS} FROM panel_users WHERE login=?', (login,)).fetchone()
        return _account(row) if row else None

    def password_hash(self, login: str) -> str | None:
        with closing(self.db.connect()) as connection:
            row = connection.execute('SELECT password_hash FROM panel_users WHERE login=?', (login,)).fetchone()
        return row[0] if row else None

    def by_phone(self, phone: str) -> PanelAccount | None:
        with closing(self.db.connect()) as connection:
            row = connection.execute(f'SELECT {_COLUMNS} FROM panel_users WHERE phone=?', (phone,)).fetchone()
        return _account(row) if row else None

    def summary(self) -> tuple[bool, bool]:
        """(есть ли записи вообще, есть ли номера у включённых).

        Выключенная запись тоже считается: иначе выключение последней
        учётной записи снимало бы с панели вход целиком."""
        with closing(self.db.connect()) as connection:
            users, phones = connection.execute(
                'SELECT COUNT(*), COUNT(CASE WHEN active=1 THEN phone END) FROM panel_users').fetchone()
        return bool(users), bool(phones)

    def save(self, login: str, *, role: str, password_hash: str | None, phone: str | None,
             directions: tuple[str, ...], active: bool = True, by: str | None = None) -> None:
        stamp = _now()
        with closing(self.db.connect()) as connection, connection:
            if phone is not None:
                owner = connection.execute('SELECT login FROM panel_users WHERE phone=? AND login<>?',
                                           (phone, login)).fetchone()
                if owner is not None:
                    raise PanelUserError(f'Номер {phone} уже привязан к «{owner[0]}».')
            connection.execute(
                'INSERT INTO panel_users (login, role, password_hash, phone, directions, active, created_at, '
                'updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(login) DO UPDATE SET '
                'role=excluded.role, password_hash=excluded.password_hash, phone=excluded.phone, '
                'directions=excluded.directions, active=excluded.active, updated_at=excluded.updated_at, '
                'updated_by=excluded.updated_by',
                (login, role, password_hash, phone, ','.join(directions), int(active), stamp, stamp, by))

    def delete(self, login: str) -> bool:
        with closing(self.db.connect()) as connection, connection:
            return connection.execute('DELETE FROM panel_users WHERE login=?', (login,)).rowcount == 1


KEEP = object()  # «не менять» для change_account


def change_account(store: PanelUserStore, login: str, *, create: bool, role=KEEP, phone=KEEP,
                   password=KEEP, directions=KEEP, active=KEEP, by: str | None = None) -> PanelAccount:
    """Завести или изменить запись по одним правилам — для экрана и команды.

    phone=None снимает номер, password=None снимает пароль (вход только по
    SMS). Направления бывают только у менеджера. Запись, которой нечем
    войти — ни пароля, ни номера, — не сохраняется."""
    login = check_login(login)
    current = store.get(login)
    if create and current is not None:
        raise PanelUserError(f'«{login}» уже есть.')
    if not create and current is None:
        raise PanelUserError(f'«{login}» нет среди учётных записей.')
    role = check_role(role) if role is not KEEP else current.role
    if phone is KEEP:
        phone = current.phone if current else None
    elif phone is not None:
        phone = check_phone(phone)
    if password is KEEP:
        password_hash = store.password_hash(login) if current else None
    elif password is None:
        password_hash = None
    else:
        password_hash = hash_password(check_password(password))
    if directions is KEEP:
        directions = current.directions if current else ()
    else:
        directions = check_directions(directions)
    if role != 'manager':
        directions = ()
    active = bool(active) if active is not KEEP else (current.active if current else True)
    if password_hash is None and phone is None:
        raise PanelUserError('Нужен пароль или номер телефона: иначе войти нечем.')
    store.save(login, role=role, password_hash=password_hash, phone=phone, directions=directions,
               active=active, by=by)
    return store.get(login)


def import_env_account(store: PanelUserStore, settings, login: str, *, by: str | None = None) -> PanelAccount:
    """Перенести одну запись из DASHBOARD_PANEL_USERS в базу: пароль —
    отпечатком, первый номер и направления — как были."""
    user = settings.dashboard_panel_users.get(login)
    if user is None:
        raise PanelUserError(f'«{login}» нет в переменных окружения.')
    if store.get(login) is not None:
        raise PanelUserError(f'«{login}» уже в базе.')
    phones = [phone for phone, owner in settings.phone_users.items() if owner == login]
    phone = phones[0] if phones and store.by_phone(phones[0]) is None else None
    store.save(login, role=user[1], password_hash=hash_password(user[0]), phone=phone,
               directions=settings.manager_directions.get(login, ()), by=by)
    return store.get(login)


class PanelAccounts:
    """Кто есть в панели: база, а за ней — переменные окружения.

    Запись из базы (даже выключенная) закрывает одноимённую из переменных:
    выключить человека в базе — значит выключить, что бы ни лежало в Railway."""

    def __init__(self, settings, store: PanelUserStore | None = None, *, clock=time.monotonic):
        self.settings, self.store, self.clock = settings, store, clock
        self._cache: dict = {}
        self._lock = Lock()

    def forget(self) -> None:
        with self._lock:
            self._cache.clear()

    def _cached(self, key, load):
        if self.store is None:
            return None
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < CACHE_SECONDS:
                return hit[1]
        value = load()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def _db(self, login: str) -> PanelAccount | None:
        return self._cached(('login', login), lambda: self.store.get(login))

    def env_logins(self) -> list[str]:
        """Логины из переменных, которых нет в базе, — ещё не перенесённые."""
        return [login for login in self.settings.dashboard_panel_users if self._db(login) is None]

    def env_account(self, login: str) -> PanelAccount | None:
        return self._env(login)

    def _env(self, login: str) -> PanelAccount | None:
        user = self.settings.dashboard_panel_users.get(login)
        if user is None:
            return None
        phones = [phone for phone, owner in self.settings.phone_users.items() if owner == login]
        return PanelAccount(login=login, role=user[1], directions=self.settings.manager_directions.get(login, ()),
                            phone=phones[0] if phones else None, source='env', has_password=True)

    def get(self, login: str | None) -> PanelAccount | None:
        """Действующая учётная запись по логину или None."""
        if not login:
            return None
        stored = self._db(login)
        if stored is not None:
            return stored if stored.active else None
        return self._env(login)

    def by_phone(self, phone: str) -> PanelAccount | None:
        """Учётная запись, которой открыт вход по этому номеру."""
        stored = self._cached(('phone', phone), lambda: self.store.by_phone(phone))
        if stored is not None:
            return stored if stored.active else None
        login = self.settings.phone_users.get(phone)
        if login is None or self._db(login) is not None:
            # Логин переехал в базу — номера к нему теперь задаются там.
            return None
        account = self._env(login)
        return account if account is not None and account.role in PHONE_ROLES else None

    def authenticate(self, login: str, password: str) -> PanelAccount | None:
        """Логин и пароль → учётная запись. Сравнение — за постоянное время."""
        stored = self._db(login) if self.store is not None else None
        if stored is not None:
            if not stored.active:
                return None
            return stored if password_matches(password, self.store.password_hash(login)) else None
        user = self.settings.dashboard_panel_users.get(login)
        if user is not None and secrets.compare_digest(password.encode(), user[0].encode()):
            return self._env(login)
        return None

    def _summary(self) -> tuple[bool, bool]:
        return self._cached('summary', self.store.summary) or (False, False)

    def configured(self) -> bool:
        """Есть ли кому входить: без этого панель открыта только с петли."""
        return bool(self.settings.dashboard_password or self.settings.dashboard_panel_users
                    or self._summary()[0])

    def phones_configured(self) -> bool:
        return bool(self.settings.phone_users) or self._summary()[1]

    def directions(self, login: str) -> tuple[str, ...]:
        account = self.get(login)
        return account.directions if account is not None and account.role == 'manager' else ()

    def session_valid(self, identity) -> bool:
        """Сессия жива, пока её учётная запись есть, включена и с той же ролью;
        открытая по номеру — пока номер ведёт на ту же запись. Общий вход
        DASHBOARD_USER (роль all) учётной записи не имеет."""
        if identity.role == 'all' and not identity.phone:
            return True
        if identity.phone:
            account = self.by_phone(identity.phone)
        else:
            account = self.get(identity.username)
        return account is not None and account.login == identity.username and account.role == identity.role


if __name__ == '__main__':
    from retro.panel_users_cli import main
    main()
