import re

import httpx

from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import DataError


JOB_ID = re.compile(r'[A-Za-z0-9_-]{16,80}')
RECIPIENT_ID = re.compile(r'[A-Za-z0-9_-]{20,64}')
CURSOR = re.compile(r'[A-Za-z0-9_-]{1,512}')
STATUSES = {'queued', 'running', 'completed', 'failed', 'interrupted'}
# Откуда запущено задание (T-503): панель, команда /broadcast в боте или
# сообщение по расписанию. У заданий старого формата источника нет.
SOURCES = {'dashboard', 'tg_admin', 'auto'}
# Журнал рассылок по видам: ручные — из панели и командой в боте.
HISTORY_KINDS = {'all': None, 'manual': 'dashboard,tg_admin', 'auto': 'auto'}
DELIVERY_STATUSES = {'pending', 'sending', 'sent', 'blocked', 'failed'}
AUTHOR_LIMIT = 80
# Экран опрашивает статус рассылки до её конца: пяти секунд httpx по
# умолчанию не хватает даже на один интервал опроса.
BROADCAST_KEEPALIVE = 60


class BroadcastConflict(DataError):
    pass


class BroadcastNotFound(DataError):
    pass


def _invalid():
    raise DataError('API рассылок вернул некорректный ответ.')


def mask_phone(value):
    """Телефон гостя маской «••• 45 67», как в аудитории бота.

    Бот и так отдаёт маску; маскируем ещё раз здесь, чтобы полный номер не
    дошёл до браузера, даже если бот когда-нибудь пришлёт его целиком."""
    digits = re.sub(r'\D', '', value or '')[-4:]
    return f'••• {digits[:2]} {digits[2:]}' if len(digits) == 4 else '•••'


def clean_author(value):
    """Имя для журнала рассылок: бот принимает от 1 до 80 символов."""
    text = ' '.join(str(value or '').split())[:AUTHOR_LIMIT].strip()
    return text or None


def _count(value, *, default=None):
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _invalid()
    return value


def _optional_text(value, limit):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        _invalid()
    return value


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
        cleaned.append({'id': recipient_id, 'name': name.strip(), 'phone': mask_phone(phone)})
    return {'subscribers': subscribers, 'profiles': profiles, 'recipients': cleaned}


def validate_job(payload):
    """Задание рассылки. Поля журнала (T-503) необязательны: бот прежней
    версии их не присылает, и такой ответ остаётся корректным."""
    if not isinstance(payload, dict) or not isinstance(payload.get('id'), str) or \
            not JOB_ID.fullmatch(payload['id']) or payload.get('status') not in STATUSES:
        _invalid()
    counts = {field: _count(payload.get(field)) for field in ('audience', 'sent', 'blocked', 'failed')}
    done = counts['sent'] + counts['blocked'] + counts['failed']
    if done > counts['audience']:
        _invalid()
    # «В очереди» — кому ещё не отправляли; старый бот этого поля не знает.
    pending = _count(payload.get('pending'), default=counts['audience'] - done)
    if pending > counts['audience'] - done:
        _invalid()
    for field in ('created_at', 'started_at', 'finished_at', 'resumed_at', 'throttled_until'):
        _optional_text(payload.get(field), 64)
    source = payload.get('source')
    if source is not None and source not in SOURCES:
        _invalid()
    manual = payload.get('manual')
    if manual is not None and not isinstance(manual, bool):
        _invalid()
    if source is not None:
        manual = source != 'auto'
    for field in ('recipients_known', 'resumable'):
        if payload.get(field) is not None and not isinstance(payload[field], bool):
            _invalid()
    author, resumed_by = (_optional_text(payload.get(field), AUTHOR_LIMIT) for field in ('author', 'resumed_by'))
    return {
        **{key: payload.get(key) for key in (
            'id', 'status', 'created_at', 'started_at', 'finished_at', 'resumed_at', 'throttled_until')},
        **counts,
        'pending': pending,
        'source': source,
        'manual': manual,
        'author': author,
        'text': _optional_text(payload.get('text'), 4096),
        'recipients_known': payload.get('recipients_known') is True,
        # Дослать можно только то, что бот сам назвал досылаемым и где есть кому.
        'resumable': payload.get('resumable') is True and pending > 0,
        'resume_count': _count(payload.get('resume_count'), default=0),
        'resumed_by': resumed_by,
    }


def validate_jobs_page(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('jobs'), list) or len(payload['jobs']) > 100:
        _invalid()
    jobs = [validate_job(job) for job in payload['jobs']]
    if len({job['id'] for job in jobs}) != len(jobs):
        _invalid()
    total = _count(payload.get('total'))
    cursor = payload.get('next_cursor')
    if total < len(jobs) or (cursor is not None and (not isinstance(cursor, str) or not CURSOR.fullmatch(cursor))):
        _invalid()
    return {'supported': True, 'jobs': jobs, 'total': total, 'next_cursor': cursor}


def validate_deliveries(payload, job_id):
    """Получатели одного задания. Телефон — только маской."""
    if not isinstance(payload, dict) or payload.get('job_id') != job_id \
            or not isinstance(payload.get('recipients_known'), bool) \
            or not isinstance(payload.get('recipients'), list) or len(payload['recipients']) > 500:
        _invalid()
    total = _count(payload.get('total'))
    next_offset = payload.get('next_offset')
    if next_offset is not None:
        _count(next_offset)
    recipients = []
    for row in payload['recipients']:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) \
                or not RECIPIENT_ID.fullmatch(row['id']) or row.get('status') not in DELIVERY_STATUSES:
            _invalid()
        name = _optional_text(row.get('name'), 80)
        phone = _optional_text(row.get('phone'), 20)
        recipients.append({
            'id': row['id'],
            'name': (name or '').strip(),
            'phone': mask_phone(phone),
            'status': row['status'],
            'error': _optional_text(row.get('error'), 200),
            'at': _optional_text(row.get('at'), 64),
        })
    if total < len(recipients):
        _invalid()
    return {'job_id': job_id, 'recipients_known': payload['recipients_known'], 'total': total,
            'recipients': recipients, 'next_offset': next_offset}


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
    async def _get(client, path, **kwargs):
        """Чтение с одним повтором на обрыве соединения из пула.

        Бот на Node закрывает простаивающее соединение через пять секунд, пул
        держит его минуту: первый запрос после паузы может уйти в уже
        закрытое соединение. Чтение повторять безопасно; запуск и досылку
        здесь не повторяем."""
        try:
            return await client.get(path, **kwargs)
        except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
            return await client.get(path, **kwargs)

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
            if code == 'not_resumable':
                raise BroadcastConflict('Эту рассылку дослать нельзя: бот не сохранил её получателей.')
            raise BroadcastConflict('Рассылку сейчас нельзя запустить.')
        if not 200 <= response.status_code < 300:
            raise DataError('API рассылок недоступен.')

    async def audience(self):
        client = self._client()
        try:
            response = await self._get(client, '/admin/broadcast/audience')
            self._raise(response)
            return validate_audience(response.json())
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='audience')
            raise DataError('Не удалось связаться с API рассылок.') from None

    async def start(self, operation_id, text, recipient_ids, author=None):
        if (not isinstance(recipient_ids, list) or not recipient_ids
                or len(recipient_ids) > 2000 or len(set(recipient_ids)) != len(recipient_ids)
                or any(not isinstance(value, str) or not RECIPIENT_ID.fullmatch(value)
                       for value in recipient_ids)):
            raise DataError('Выберите хотя бы одного корректного получателя.')
        client = self._client()
        # Источник и автор попадают в журнал бота; бот прежней версии лишние
        # поля пропускает. Ключ повтора от них не зависит: текст и получатели.
        body = {'text': text, 'recipient_ids': recipient_ids, 'source': 'dashboard'}
        if clean_author(author):
            body['author'] = clean_author(author)
        try:
            response = await client.post(
                '/admin/broadcast/jobs',
                headers={'Idempotency-Key': operation_id},
                json=body,
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
            response = await self._get(client, '/admin/broadcast/jobs/' + operation_id)
            if response.status_code == 404:
                raise BroadcastNotFound('Рассылка не найдена.')
            self._raise(response)
            return validate_job(response.json())
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='status')
            raise DataError('Не удалось связаться с API рассылок.') from None

    async def list(self, kind='all', cursor=None, limit=20):
        """Журнал рассылок, новые сверху (T-503).

        Бот без журнала отвечает на этот адрес 404 (или 405: прежде здесь был
        только запуск). Это не ошибка: `supported` false, экран так и пишет."""
        if kind not in HISTORY_KINDS or isinstance(limit, bool) or not isinstance(limit, int) \
                or not 1 <= limit <= 100 or (cursor is not None and not CURSOR.fullmatch(cursor)):
            raise DataError('Некорректный запрос журнала рассылок.')
        params = {'limit': str(limit)}
        if HISTORY_KINDS[kind]:
            params['source'] = HISTORY_KINDS[kind]
        if cursor:
            params['cursor'] = cursor
        client = self._client()
        try:
            response = await self._get(client, '/admin/broadcast/jobs', params=params)
            if response.status_code in (404, 405):
                return {'supported': False, 'jobs': [], 'total': 0, 'next_cursor': None}
            if response.status_code == 400:
                raise DataError('Журнал рассылок обновился. Откройте его заново.')
            self._raise(response)
            return validate_jobs_page(response.json())
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='list')
            raise DataError('Не удалось связаться с API рассылок.') from None

    async def deliveries(self, job_id, statuses=None, offset=0, limit=100):
        """Получатели задания и их статусы; `statuses` — подмножество DELIVERY_STATUSES."""
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise DataError('Некорректный идентификатор рассылки.')
        statuses = list(statuses or ())
        if any(status not in DELIVERY_STATUSES for status in statuses) or len(set(statuses)) != len(statuses) \
                or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000 \
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise DataError('Некорректный запрос получателей рассылки.')
        params = {'limit': str(limit), 'offset': str(offset)}
        if statuses:
            params['status'] = ','.join(statuses)
        client = self._client()
        try:
            response = await self._get(client, f'/admin/broadcast/jobs/{job_id}/deliveries', params=params)
            if response.status_code == 404:
                raise BroadcastNotFound('Рассылка не найдена: бот хранит последние сто.')
            self._raise(response)
            return validate_deliveries(response.json(), job_id)
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='deliveries')
            raise DataError('Не удалось связаться с API рассылок.') from None

    async def resume(self, job_id, author=None):
        """Дослать тем, кому сообщение ещё не уходило.

        Бот шлёт только получателям «в очереди»: получившим, заблокировавшим
        бота и тем, чья доставка не подтверждена, повторно ничего не уходит,
        поэтому повторный вызов безопасен. 202 — досылка идёт, 200 — некому."""
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise DataError('Некорректный идентификатор рассылки.')
        client = self._client()
        body = {'author': clean_author(author)} if clean_author(author) else {}
        try:
            response = await client.post(f'/admin/broadcast/jobs/{job_id}/resume', json=body)
            if response.status_code == 404:
                raise BroadcastNotFound('Рассылка не найдена: бот хранит последние сто.')
            self._raise(response)
            return {'job': validate_job(response.json()), 'started': response.status_code == 202}
        except ValueError:
            _invalid()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('broadcasts', error, operation='resume')
            raise DataError('Не удалось связаться с API рассылок.') from None
