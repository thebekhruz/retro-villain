import re

import httpx

from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import DataError


JOB_ID = re.compile(r'[A-Za-z0-9_-]{16,80}')
STATUSES = {'queued', 'running', 'completed', 'failed', 'interrupted'}


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
    return {'subscribers': subscribers, 'profiles': profiles}


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
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    def _client(self):
        if not self.settings.booking_broadcast_configured:
            raise DataError('Рассылка через booking-бот ещё не настроена.')
        return httpx.AsyncClient(
            base_url=self.settings.booking_api_url,
            headers={
                'Accept': 'application/json',
                'Authorization': 'Bearer ' + self.settings.booking_broadcast_token,
            },
            timeout=15,
            follow_redirects=False,
            transport=self.transport,
        )

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
                raise BroadcastConflict('Ключ этой рассылки уже использован для другого текста.')
            raise BroadcastConflict('Рассылку сейчас нельзя запустить.')
        if not 200 <= response.status_code < 300:
            raise DataError('API рассылок недоступен.')

    async def audience(self):
        try:
            async with self._client() as client:
                response = await client.get('/admin/broadcast/audience')
                self._raise(response)
                return validate_audience(response.json())
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='audience')
            raise DataError('Не удалось связаться с API рассылок.') from None

    async def start(self, operation_id, text):
        try:
            async with self._client() as client:
                response = await client.post(
                    '/admin/broadcast/jobs',
                    headers={'Idempotency-Key': operation_id},
                    json={'text': text},
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
        try:
            async with self._client() as client:
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
