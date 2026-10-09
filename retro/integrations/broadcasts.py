import re

import httpx

from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import DataError


JOB_ID = re.compile(r'[A-Za-z0-9_-]{16,80}')
RECIPIENT_ID = re.compile(r'[A-Za-z0-9_-]{20,64}')
STATUSES = {'queued', 'running', 'completed', 'failed', 'interrupted'}
# Экран опрашивает статус рассылки до её конца: пяти секунд httpx по
# умолчанию не хватает даже на один интервал опроса.
BROADCAST_KEEPALIVE = 60


class BroadcastConflict(DataError):
    pass


def _invalid():
    raise DataError('API рассылок вернул некорректный ответ.')


def validate_audience(payload):
    if not isinstance(payload, dict):
        _invalid()
    subscribers, profiles = payload.get('subscribers'), payload.get('profiles')
    if (isinstance(subscribers, bool) or not isinstance(subscribers, int) or subscribers < 0
            or isinstance(profiles, bool) or not isinstance(profiles, int) or profiles < subscribers):
        _invalid()
    recipients = payload.get('recipients')
    if not isinstance(recipients, list) or len(recipients) != subscribers:
        _invalid()
    cleaned, seen = [], set()
    for recipient in recipients:
        if not isinstance(recipient, dict) or set(recipient) != {'id', 'name', 'phone'}:
            _invalid()
        recipient_id, name, phone = recipient['id'], recipient['name'], recipient['phone']
        if (not isinstance(recipient_id, str) or not RECIPIENT_ID.fullmatch(recipient_id)
                or recipient_id in seen or not isinstance(name, str) or not name.strip()
                or len(name) > 80 or not isinstance(phone, str) or not phone
                or len(phone) > 20):
            _invalid()
        seen.add(recipient_id)
        cleaned.append({'id': recipient_id, 'name': name.strip(), 'phone': phone})
    return {'subscribers': subscribers, 'profiles': profiles, 'recipients': cleaned}


def validate_job(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('id'), str) or \
            not JOB_ID.fullmatch(payload['id']) or payload.get('status') not in STATUSES:
        _invalid()
    counts = {}
    for field in ('audience', 'sent', 'blocked', 'failed'):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _invalid()
        counts[field] = value
    if counts['sent'] + counts['blocked'] + counts['failed'] > counts['audience']:
        _invalid()
    for field in ('created_at', 'started_at', 'finished_at'):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            _invalid()
    return {key: payload.get(key) for key in (
        'id', 'status', 'audience', 'sent', 'blocked', 'failed',
        'created_at', 'started_at', 'finished_at')}


class BookingBroadcastClient:
    """Клиент API рассылок. Соединение переживает запрос.

    `status()` экран опрашивает до самого конца рассылки, поэтому новый пул и
    TLS-хендшейк на каждый опрос — чистая потеря: один клиент на приложение
    держит соединение живым (BROADCAST_KEEPALIVE) и закрывается с приложением.
    """

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self._http = None

    def _client(self):
        if not self.settings.booking_broadcast_configured:
            raise DataError('Рассылка через booking-бот ещё не настроена.')
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.settings.booking_api_url,
                headers={
                    'Accept': 'application/json',
                    'Authorization': 'Bearer ' + self.settings.booking_broadcast_token,
                },
                timeout=15,
                follow_redirects=False,
                transport=self.transport,
                limits=httpx.Limits(keepalive_expiry=BROADCAST_KEEPALIVE),
            )
        return self._http

    async def close(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @staticmethod
    def _raise(response):
        if response.status_code in (401, 403):
            raise DataError('API рассылок отклонил доступ.')
        if response.status_code == 409:
            try:
                code = response.json().get('error')
            except ValueError:
                code = None
            if code == 'broadcast_in_progress':
                raise BroadcastConflict('Другая рассылка уже выполняется.')
            if code == 'idempotency_conflict':
                raise BroadcastConflict('Ключ этой рассылки уже использован для других параметров.')
            if code == 'recipients_changed':
                raise BroadcastConflict('Список получателей изменился. Обновите его и выберите снова.')
            raise BroadcastConflict('Рассылку сейчас нельзя запустить.')
        if not 200 <= response.status_code < 300:
            raise DataError('API рассылок недоступен.')

    async def audience(self):
        client = self._client()
        try:
            response = await client.get('/admin/broadcast/audience')
            self._raise(response)
            return validate_audience(response.json())
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='audience')
            raise DataError('Не удалось связаться с API рассылок.') from None

    async def start(self, operation_id, text, recipient_ids):
        if (not isinstance(recipient_ids, list) or not recipient_ids
                or len(recipient_ids) > 2000 or len(set(recipient_ids)) != len(recipient_ids)
                or any(not isinstance(value, str) or not RECIPIENT_ID.fullmatch(value)
                       for value in recipient_ids)):
            raise DataError('Выберите хотя бы одного корректного получателя.')
        client = self._client()
        try:
            response = await client.post(
                '/admin/broadcast/jobs',
                headers={'Idempotency-Key': operation_id},
                json={'text': text, 'recipient_ids': recipient_ids},
            )
            self._raise(response)
            return validate_job(response.json())
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='start')
            raise DataError('Не удалось связаться с API рассылок.') from None

    async def status(self, operation_id):
        if not JOB_ID.fullmatch(operation_id):
            raise DataError('Некорректный идентификатор рассылки.')
        client = self._client()
        try:
            response = await client.get('/admin/broadcast/jobs/' + operation_id)
            if response.status_code == 404:
                raise DataError('Рассылка не найдена.')
            self._raise(response)
            return validate_job(response.json())
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='status')
            raise DataError('Не удалось связаться с API рассылок.') from None
