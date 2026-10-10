"""Вход по номеру телефона и одноразовому SMS-коду (ТЗ 09.10, М-05).

Ввёл номер → пришёл код → подтвердил → кабинет открыт в сохранённой сессии.
Номер находит уже заведённую учётную запись панели (DASHBOARD_PHONE_USERS →
логин из DASHBOARD_PANEL_USERS) и никогда не создаёт новую: заводить людей
для входа — отдельное действие администратора, не менеджера.

Правила кода:
* 6 цифр, живёт 5 минут, одноразовый; новый код отменяет прежний;
* в базе — только соль и отпечаток (PBKDF2), сравнение за постоянное время;
* не больше 5 попыток ввода на код;
* новый код — не раньше чем через 60 с после прошлого, не больше 5 SMS на
  номер в час и 20 запросов с одного адреса в час (неизвестные номера тоже
  считаются — так перебирать номера дорого);
* неизвестному номеру SMS не отправляем: баланс Eskiz платный.

Почему неизвестный номер получает прямой ответ «номер не подключён», а не
обтекаемое «если номер есть, код придёт». Входящих по номеру — несколько
менеджеров. Обтекаемый ответ оставил бы человека с опечаткой в настройках
ждать SMS, которой не будет, и звонить «код не приходит». Цена прямого
ответа — можно проверить, подключён ли конкретный номер; но с одного адреса
это 20 номеров в час, а знание номера ничего не открывает: код всё равно
приходит только на сам телефон.

Хранилище — таблица в общей базе (Postgres на бою, SQLite локально): лимиты
и выданные коды переживают выкат, а несколько запросов подряд не
обгоняют друг друга (BEGIN IMMEDIATE / advisory-блокировка).
"""

import asyncio
import hashlib
import hmac
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from retro.config import SMS_TEMPLATE
from retro.db import PostgresConnection, as_database
from retro.integrations.sms import render_message
from retro.logging_config import log_safe_failure
from retro.phone_numbers import PHONE_ROLES, normalize_phone

CODE_LENGTH = 6
CODE_LIFETIME = timedelta(minutes=5)
MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
PHONE_SENDS_PER_HOUR = 5
ADDRESS_REQUESTS_PER_HOUR = 20
WINDOW = timedelta(hours=1)
KEEP_ROWS = timedelta(days=2)
HASH_ROUNDS = 120_000
# Ключ advisory-блокировки в Postgres: «PHON» — запросы кода по одному.
PHONE_LOCK = 0x50484F4E

# Состояния строки: unknown — запрос неизвестного номера (только для лимита
# на адрес, сам номер не храним); sending → sent → used; failed — SMS не ушла;
# replaced — после неё отправлен новый код.
ACTIVE = 'sent'
UNKNOWN = 'Этот номер не подключён к панели. Обратитесь к администратору.'


class PhoneLoginError(Exception):
    """Отказ словами для человека: status — HTTP, code — что случилось."""

    def __init__(self, status: int, code: str, message: str, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra
        super().__init__(code)

    def payload(self) -> dict:
        return dict(detail=self.message, code=self.code, **self.extra)


@dataclass(frozen=True)
class CodeSent:
    phone: str
    sent: bool          # False — код уже отправлен меньше минуты назад, новый не шлём
    resend_in: int      # секунд до «Отправить ещё раз»
    expires_in: int


@dataclass(frozen=True)
class PhoneAccount:
    phone: str
    login: str
    role: str


def _stamp(moment: datetime) -> str:
    # Одна длина и UTC: строки сравниваются как время.
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc)


def _seconds(delta: timedelta) -> int:
    # Округляем вверх: «через 0 с» при оставшихся 0,4 с — ложь.
    return max(0, -int(-delta.total_seconds() // 1))


def _minutes_text(seconds: int) -> str:
    return f'{max(1, -(-seconds // 60))} мин'


def code_fingerprint(phone: str, code: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256', f'{phone}:{code}'.encode(), bytes.fromhex(salt),
                               HASH_ROUNDS).hex()


def new_code() -> str:
    return f'{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}'


def phone_account(settings, phone: str) -> PhoneAccount | None:
    """Учётная запись по подтверждаемому номеру — только уже заведённая."""
    login = settings.phone_users.get(phone)
    user = settings.dashboard_panel_users.get(login) if login else None
    if user is None or user[1] not in PHONE_ROLES:
        return None
    return PhoneAccount(phone=phone, login=login, role=user[1])


def phone_session_valid(identity, settings) -> bool:
    """Сессия, открытая по номеру, жива, пока номер привязан к той же учётной
    записи: сняли номер или учётную запись из настроек — после перезапуска
    кабинет закрывается, не дожидаясь конца 30 дней."""
    account = phone_account(settings, identity.phone)
    return account is not None and account.login == identity.username and account.role == identity.role


class PhoneLoginStore:
    def __init__(self, target):
        self.db = as_database(target)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS phone_login_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                state TEXT NOT NULL,
                salt TEXT,
                code_hash TEXT,
                expires_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                used_at TEXT)''')
            connection.execute('CREATE INDEX IF NOT EXISTS phone_login_codes_phone '
                               'ON phone_login_codes(phone, requested_at)')
            connection.execute('CREATE INDEX IF NOT EXISTS phone_login_codes_address '
                               'ON phone_login_codes(address, requested_at)')

    def _begin(self, connection):
        connection.execute('BEGIN IMMEDIATE')
        if isinstance(connection, PostgresConnection):
            connection.execute('SELECT pg_advisory_xact_lock(CAST(? AS BIGINT))', (PHONE_LOCK,))

    def reserve(self, *, phone: str | None, address: str, now: datetime, salt: str | None,
                code_hash: str | None):
        """Проверить лимиты и записать запрос. phone=None — неизвестный номер.

        Возвращает ('limit', причина, секунд до повтора) / ('unknown', None) /
        ('active', секунд до повтора, секунд жизни кода) — прежний код ещё
        действует и минута не прошла / ('new', id строки)."""
        hour_ago = _stamp(now - WINDOW)
        with closing(self.db.connect()) as connection, connection:
            self._begin(connection)
            connection.execute('DELETE FROM phone_login_codes WHERE requested_at < ?',
                               (_stamp(now - KEEP_ROWS),))
            from_address = connection.execute(
                'SELECT requested_at FROM phone_login_codes WHERE address=? AND requested_at >= ? '
                'ORDER BY requested_at', (address, hour_ago)).fetchall()
            if len(from_address) >= ADDRESS_REQUESTS_PER_HOUR:
                return 'limit', 'address_limit', _seconds(_parse(from_address[0][0]) + WINDOW - now)
            if phone is None:
                connection.execute(
                    "INSERT INTO phone_login_codes(phone,address,requested_at,state) VALUES ('',?,?,'unknown')",
                    (address, _stamp(now)))
                return 'unknown', None
            last = connection.execute(
                'SELECT id,requested_at,state,expires_at,attempts FROM phone_login_codes '
                "WHERE phone=? AND state IN ('sending','sent') ORDER BY requested_at DESC, id DESC",
                (phone,)).fetchone()
            if last is not None and _parse(last[1]) + RESEND_AFTER > now:
                wait = _seconds(_parse(last[1]) + RESEND_AFTER - now)
                if last[2] == ACTIVE and _parse(last[3]) > now and last[4] < MAX_ATTEMPTS:
                    return 'active', wait, _seconds(_parse(last[3]) - now)
                return 'limit', 'cooldown', wait
            sends = connection.execute(
                'SELECT requested_at FROM phone_login_codes WHERE phone=? AND requested_at >= ? '
                'ORDER BY requested_at', (phone, hour_ago)).fetchall()
            if len(sends) >= PHONE_SENDS_PER_HOUR:
                return 'limit', 'phone_limit', _seconds(_parse(sends[0][0]) + WINDOW - now)
            row_id = self.db.insert_returning_id(
                connection,
                'INSERT INTO phone_login_codes(phone,address,requested_at,state,salt,code_hash,expires_at) '
                "VALUES (?,?,?,'sending',?,?,?)",
                (phone, address, _stamp(now), salt, code_hash, _stamp(now + CODE_LIFETIME)))
            return 'new', row_id

    def mark_sent(self, row_id: int, phone: str) -> None:
        with closing(self.db.connect()) as connection, connection:
            # Новый код отменяет прежние: действует последний пришедший.
            connection.execute("UPDATE phone_login_codes SET state='replaced' "
                               "WHERE phone=? AND state='sent' AND id<>?", (phone, row_id))
            connection.execute("UPDATE phone_login_codes SET state='sent' WHERE id=? AND state='sending'",
                               (row_id,))

    def mark_failed(self, row_id: int) -> None:
        with closing(self.db.connect()) as connection, connection:
            connection.execute("UPDATE phone_login_codes SET state='failed' WHERE id=? AND state='sending'",
                               (row_id,))

    def active(self, phone: str):
        """Последний отправленный код номера: (id, соль, отпечаток, срок, попыток)."""
        with closing(self.db.connect()) as connection:
            return connection.execute(
                'SELECT id,salt,code_hash,expires_at,attempts FROM phone_login_codes '
                "WHERE phone=? AND state='sent' ORDER BY requested_at DESC, id DESC", (phone,)).fetchone()

    def take_attempt(self, row_id: int) -> int | None:
        """Занять попытку до сравнения: параллельные вводы не обойдут лимит.
        Возвращает номер попытки или None, если попыток нет / код уже не действует."""
        with closing(self.db.connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE phone_login_codes SET attempts=attempts+1 "
                "WHERE id=? AND state='sent' AND attempts < ?", (row_id, MAX_ATTEMPTS))
            if cursor.rowcount != 1:
                return None
            return connection.execute('SELECT attempts FROM phone_login_codes WHERE id=?',
                                      (row_id,)).fetchone()[0]

    def use(self, row_id: int, now: datetime) -> bool:
        """Код одноразовый: сессию выдаёт только первый, кто его погасил."""
        with closing(self.db.connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE phone_login_codes SET state='used', used_at=? WHERE id=? AND state='sent'",
                (_stamp(now), row_id))
            return cursor.rowcount == 1


class PhoneLogin:
    def __init__(self, store: PhoneLoginStore, settings, sender, *, clock=None):
        self.store, self.settings, self.sender = store, settings, sender
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def enabled(self) -> bool:
        return bool(self.settings.phone_users) and self.sender is not None

    def _phone(self, raw) -> str:
        phone = normalize_phone(raw)
        if phone is None:
            raise PhoneLoginError(422, 'bad_phone', 'Введите номер полностью: +998 и 9 цифр.')
        return phone

    def _off(self):
        if not self.enabled:
            raise PhoneLoginError(503, 'off', 'Вход по SMS пока не подключён. Войдите по логину и паролю.')

    async def request_code(self, raw_phone, address: str) -> CodeSent:
        self._off()
        phone = self._phone(raw_phone)
        now = self.clock()
        account = phone_account(self.settings, phone)
        code = new_code()
        salt = secrets.token_hex(16)
        # Отпечаток считаем заранее и вне цикла событий: PBKDF2 — десятки
        # миллисекунд, и он не должен держать ни блокировку базы, ни сервер.
        fingerprint = (await asyncio.to_thread(code_fingerprint, phone, code, salt)) if account else None
        result = await asyncio.to_thread(
            self.store.reserve, phone=phone if account else None, address=address, now=now,
            salt=salt, code_hash=fingerprint)
        if result[0] == 'limit':
            _, reason, wait = result
            if reason == 'cooldown':
                message = f'Новый код можно запросить через {wait} с.'
            elif reason == 'phone_limit':
                message = f'Слишком много SMS на этот номер. Попробуйте через {_minutes_text(wait)}.'
            else:
                message = f'Слишком много запросов кода с этого устройства. Попробуйте через {_minutes_text(wait)}.'
            raise PhoneLoginError(429, reason, message, retry_after=wait)
        if result[0] == 'unknown':
            raise PhoneLoginError(404, 'unknown', UNKNOWN)
        if result[0] == 'active':
            _, wait, expires = result
            return CodeSent(phone=phone, sent=False, resend_in=wait, expires_in=expires)
        row_id = result[1]
        try:
            template = self.settings.sms.template if self.settings.sms else SMS_TEMPLATE
            await self.sender.send(phone, render_message(template, code))
        except Exception as error:
            # Сессии нет, код не действует; в лог — только категория сбоя.
            await asyncio.to_thread(self.store.mark_failed, row_id)
            log_safe_failure('sms', error, operation='send_code:' + str(getattr(error, 'code', 'unexpected')))
            raise PhoneLoginError(502, 'not_sent', 'SMS не отправлено, попробуйте позже.') from None
        await asyncio.to_thread(self.store.mark_sent, row_id, phone)
        return CodeSent(phone=phone, sent=True, resend_in=_seconds(RESEND_AFTER),
                        expires_in=_seconds(CODE_LIFETIME))

    def verify(self, raw_phone, raw_code) -> PhoneAccount:
        self._off()
        phone = self._phone(raw_phone)
        code = str(raw_code or '').strip()
        if len(code) != CODE_LENGTH or not code.isdigit() or not code.isascii():
            raise PhoneLoginError(422, 'bad_code', f'Введите {CODE_LENGTH} цифр из SMS.')
        now = self.clock()
        row = self.store.active(phone)
        if row is None:
            raise PhoneLoginError(400, 'no_code', 'Сначала запросите код.')
        row_id, salt, stored, expires_at, attempts = row
        if _parse(expires_at) <= now:
            raise PhoneLoginError(410, 'expired', 'Код истёк — запросите новый.')
        if attempts >= MAX_ATTEMPTS:
            raise PhoneLoginError(429, 'attempts', 'Попытки закончились — запросите новый код.',
                                  attempts_left=0)
        attempt = self.store.take_attempt(row_id)
        if attempt is None:
            raise PhoneLoginError(429, 'attempts', 'Попытки закончились — запросите новый код.',
                                  attempts_left=0)
        if not hmac.compare_digest(code_fingerprint(phone, code, salt), stored):
            left = MAX_ATTEMPTS - attempt
            if left <= 0:
                raise PhoneLoginError(401, 'wrong', 'Неверный код. Попытки закончились — запросите новый код.',
                                      attempts_left=0)
            raise PhoneLoginError(401, 'wrong', f'Неверный код. Осталось попыток: {left}.', attempts_left=left)
        account = phone_account(self.settings, phone)
        if account is None:
            raise PhoneLoginError(404, 'unknown', UNKNOWN)
        if not self.store.use(row_id, now):
            raise PhoneLoginError(400, 'no_code', 'Сначала запросите код.')
        return account

    async def close(self):
        close = getattr(self.sender, 'close', None)
        if close is not None:
            await close()
