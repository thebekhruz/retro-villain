"""Уведомления в браузер на телефоне и компьютере (Web Push).

Стандартный путь без сторонних сервисов: браузер сам выдаёт адрес своей
службы доставки (Chrome и Edge на Android и ПК — FCM/WNS, Firefox — Mozilla,
Safari на Mac и iPhone — Apple), а сервер шлёт туда зашифрованное сообщение.

* Ключи VAPID (RFC 8292) — пара P-256. Берутся из VAPID_PRIVATE_KEY, а без
  неё создаются один раз и хранятся в базе: на Railway работает без ручной
  настройки, и подписки переживают выкат.
* Шифрование — aes128gcm (RFC 8291 + RFC 8188): ключ устройства и общий
  секрет приходят вместе с подпиской.
* Safari (Mac, iPhone/iPad 16.4+) требует `sub` в виде mailto: или https:,
  срок токена не дальше суток и показ уведомления на каждый push — всё это
  соблюдено здесь и в sw.js. На iPhone подписка возможна только из значка
  на экране «Домой» — это уже забота страницы (push.js).

Подписки — в таблице `push_subscriptions`: адрес службы, ключи устройства,
кто подписал и когда. Служба ответила 404/410 — устройство отписалось или
сбросило данные, подписку удаляем.
"""
import asyncio
import base64
import json
import os
import time
from contextlib import closing
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from retro.db import as_database

# Службы доставки браузеров. Чужой адрес не принимаем: иначе сервер стал бы
# слать POST куда угодно по просьбе страницы.
PUSH_HOSTS = ('fcm.googleapis.com', 'android.googleapis.com', 'push.services.mozilla.com',
              'push.apple.com', 'notify.windows.com')
# Запас под заголовок шифрования: служба принимает до 4096 байт тела.
MAX_PAYLOAD = 3000
RECORD_SIZE = 4096
TOKEN_HOURS = 12  # Apple принимает токен не дольше 24 часов
DEFAULT_SUBJECT = 'mailto:push@retro-milliy.uz'


class PushError(Exception):
    """Служба доставки отказала. `code` — короткий код без адресов и ключей."""

    def __init__(self, code: str, *, gone: bool = False):
        super().__init__(code)
        self.code, self.gone = code, gone


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def unb64u(text: str) -> bytes:
    text = str(text).strip()
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def push_host_allowed(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    host = (parts.hostname or '').lower()
    return (parts.scheme == 'https' and not parts.username and not parts.password and
            any(host == allowed or host.endswith('.' + allowed) for allowed in PUSH_HOSTS))


def valid_keys(p256dh: str, auth: str) -> bool:
    """Ключ устройства — точка P-256 в несжатом виде, секрет — 16 байт."""
    try:
        receiver, secret = unb64u(p256dh), unb64u(auth)
        if len(receiver) != 65 or receiver[0] != 4 or len(secret) != 16:
            return False
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver)
    except ValueError:
        return False
    return True


def public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


# ── VAPID ────────────────────────────────────────────────────────────────────

class VapidKeys:
    """Ключ сервера: приватная часть — 32 байта base64url, как у web-push."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey):
        self.private_key = private_key
        self.public_key = b64u(public_bytes(private_key.public_key()))

    @classmethod
    def generate(cls) -> 'VapidKeys':
        return cls(ec.generate_private_key(ec.SECP256R1()))

    @classmethod
    def from_private(cls, text: str) -> 'VapidKeys':
        raw = unb64u(text)
        if len(raw) != 32:
            raise ValueError('VAPID_PRIVATE_KEY — 32 байта в base64url (python -m retro.web_push).')
        return cls(ec.derive_private_key(int.from_bytes(raw, 'big'), ec.SECP256R1()))

    @property
    def private_text(self) -> str:
        return b64u(self.private_key.private_numbers().private_value.to_bytes(32, 'big'))

    def authorization(self, endpoint: str, subject: str, *, now: float | None = None) -> str:
        """Заголовок Authorization: подписанный токен для службы этого адреса."""
        parts = urlsplit(endpoint)
        claims = {'aud': f'{parts.scheme}://{parts.netloc}',
                  'exp': int(now if now is not None else time.time()) + TOKEN_HOURS * 3600,
                  'sub': subject}
        head = b64u(json.dumps({'typ': 'JWT', 'alg': 'ES256'}, separators=(',', ':')).encode())
        body = b64u(json.dumps(claims, separators=(',', ':')).encode())
        signing = f'{head}.{body}'.encode('ascii')
        r, s = decode_dss_signature(self.private_key.sign(signing, ec.ECDSA(hashes.SHA256())))
        token = f'{head}.{body}.{b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}'
        return f'vapid t={token}, k={self.public_key}'


# ── Шифрование (RFC 8291) ────────────────────────────────────────────────────

def _hkdf(secret: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(secret)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """Тело запроса aes128gcm: заголовок (соль, размер записи, ключ сервера) и
    одна зашифрованная запись. Соль и ключ — новые на каждое сообщение."""
    receiver = unb64u(p256dh)
    secret = unb64u(auth)
    if len(receiver) != 65 or receiver[0] != 4 or len(secret) != 16:
        raise PushError('bad_keys', gone=True)
    try:
        receiver_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver)
    except ValueError:
        raise PushError('bad_keys', gone=True) from None
    server_key = server_key or ec.generate_private_key(ec.SECP256R1())
    sender = public_bytes(server_key.public_key())
    shared = server_key.exchange(ec.ECDH(), receiver_key)
    ikm = _hkdf(shared, secret, b'WebPush: info\x00' + receiver + sender, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(ikm, salt, b'Content-Encoding: aes128gcm\x00', 16)
    nonce = _hkdf(ikm, salt, b'Content-Encoding: nonce\x00', 12)
    # Одна запись: полезная нагрузка и разделитель последней записи 0x02.
    cipher = AESGCM(cek).encrypt(nonce, payload + b'\x02', None)
    return salt + RECORD_SIZE.to_bytes(4, 'big') + bytes([len(sender)]) + sender + cipher


# ── Отправка ─────────────────────────────────────────────────────────────────

class PushSender:
    def __init__(self, keys: VapidKeys, subject: str, *, transport=None):
        self.keys, self.subject = keys, subject
        self.http = httpx.AsyncClient(timeout=15, transport=transport)

    async def send(self, subscription: dict, message: dict, *, ttl: int = 12 * 3600,
                   urgency: str = 'high') -> None:
        """Одно уведомление одному устройству. Ошибка — PushError с кодом;
        `gone` — подписка больше не действует и её пора удалить."""
        endpoint = subscription['endpoint']
        if not push_host_allowed(endpoint):
            raise PushError('bad_endpoint', gone=True)
        payload = json.dumps(message, ensure_ascii=False, separators=(',', ':')).encode()
        if len(payload) > MAX_PAYLOAD:
            raise PushError('too_large')
        body = encrypt(payload, subscription['p256dh'], subscription['auth'])
        headers = {'Authorization': self.keys.authorization(endpoint, self.subject),
                   'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream',
                   'TTL': str(ttl), 'Urgency': urgency}
        try:
            response = await self.http.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as error:
            raise PushError('network:' + type(error).__name__) from None
        if response.status_code in (200, 201, 202):
            return
        if response.status_code in (404, 410):
            raise PushError(f'http_{response.status_code}', gone=True)
        reason = ''
        try:
            reason = str(response.json().get('reason') or '')[:40]  # Apple: BadJwtToken и т.п.
        except ValueError:
            pass
        raise PushError(f'http_{response.status_code}' + (f':{reason}' if reason else ''))

    async def close(self):
        await self.http.aclose()


# ── Подписки и ключи в базе ──────────────────────────────────────────────────

class PushStore:
    COLUMNS = ('endpoint', 'p256dh', 'auth', 'owner', 'role', 'user_agent', 'created_at',
               'last_sent_at', 'last_error')

    def __init__(self, database):
        self.db = as_database(database)
        with closing(self.db.connect()) as connection, connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
                owner TEXT NOT NULL, role TEXT, user_agent TEXT, created_at TEXT NOT NULL,
                last_sent_at TEXT, last_error TEXT)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS push_vapid (
                name TEXT PRIMARY KEY, private_key TEXT NOT NULL, created_at TEXT NOT NULL)''')

    def vapid(self, now: datetime) -> VapidKeys:
        """Ключ сервера из базы; первый процесс его создаёт, остальные читают тот же."""
        candidate = VapidKeys.generate()
        with closing(self.db.connect()) as connection, connection:
            connection.execute('INSERT OR IGNORE INTO push_vapid (name, private_key, created_at) VALUES (?, ?, ?)',
                               ('default', candidate.private_text, now.isoformat()))
            row = connection.execute("SELECT private_key FROM push_vapid WHERE name = 'default'").fetchone()
        return VapidKeys.from_private(row[0])

    def save(self, subscription: dict, *, owner: str, role: str | None, user_agent: str, now: datetime):
        with closing(self.db.connect()) as connection, connection:
            connection.execute('DELETE FROM push_subscriptions WHERE endpoint = ?', (subscription['endpoint'],))
            connection.execute(
                'INSERT INTO push_subscriptions (endpoint, p256dh, auth, owner, role, user_agent, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (subscription['endpoint'], subscription['p256dh'], subscription['auth'], owner, role,
                 user_agent[:300], now.isoformat()))

    def get(self, endpoint: str) -> dict | None:
        with closing(self.db.connect()) as connection:
            row = connection.execute(f'SELECT {", ".join(self.COLUMNS)} FROM push_subscriptions WHERE endpoint = ?',
                                     (endpoint,)).fetchone()
        return dict(zip(self.COLUMNS, row)) if row else None

    def all(self) -> list[dict]:
        with closing(self.db.connect()) as connection:
            rows = connection.execute(
                f'SELECT {", ".join(self.COLUMNS)} FROM push_subscriptions ORDER BY created_at').fetchall()
        return [dict(zip(self.COLUMNS, row)) for row in rows]

    def remove(self, endpoint: str) -> bool:
        with closing(self.db.connect()) as connection, connection:
            return connection.execute('DELETE FROM push_subscriptions WHERE endpoint = ?', (endpoint,)).rowcount > 0

    def mark(self, endpoint: str, now: datetime, error: str | None = None):
        with closing(self.db.connect()) as connection, connection:
            if error is None:
                connection.execute('UPDATE push_subscriptions SET last_sent_at = ?, last_error = NULL '
                                   'WHERE endpoint = ?', (now.isoformat(), endpoint))
            else:
                connection.execute('UPDATE push_subscriptions SET last_error = ? WHERE endpoint = ?',
                                   (error[:80], endpoint))


async def broadcast(sender: PushSender, store: PushStore, message: dict, now: datetime,
                    subscriptions: list[dict] | None = None) -> dict:
    """Одно уведомление на все подписанные устройства. Отписавшиеся удаляются."""
    subscriptions = store.all() if subscriptions is None else subscriptions
    result = dict(sent=0, failed=0, removed=0, errors=[])

    async def one(subscription):
        try:
            await sender.send(subscription, message)
        except PushError as error:
            if error.gone:
                await asyncio.to_thread(store.remove, subscription['endpoint'])
                result['removed'] += 1
            else:
                await asyncio.to_thread(store.mark, subscription['endpoint'], now, error.code)
                result['failed'] += 1
                result['errors'].append(error.code)
            return
        await asyncio.to_thread(store.mark, subscription['endpoint'], now)
        result['sent'] += 1

    await asyncio.gather(*(one(subscription) for subscription in subscriptions))
    return result


if __name__ == '__main__':
    # Новый ключ для VAPID_PRIVATE_KEY: python -m retro.web_push
    keys = VapidKeys.generate()
    print(f'VAPID_PRIVATE_KEY={keys.private_text}')
    print(f'# публичный ключ (для справки): {keys.public_key}')
