import re
from datetime import date, datetime, timezone

import httpx

from retro.async_utils import gather_reads

from retro.modules.cashier.service import DataError
from retro.logging_config import log_upstream_failure


COUNT_FIELDS = ('bookings', 'guests', 'unknown_guest_bookings')
UTM_FIELDS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')
RFC3339_UTC = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z')
# Пять секунд httpx по умолчанию не переживают даже паузу между двумя
# экранами учредителя; минута простоя оставляет соединение живым.
BOOKING_KEEPALIVE = 60


def _invalid():
    raise DataError('API бронирований вернул некорректный ответ.')


def _validate_counts(value):
    if not isinstance(value, dict):
        _invalid()
    for field in COUNT_FIELDS:
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _invalid()
    if value['unknown_guest_bookings'] > value['bookings']:
        _invalid()


def _validate_breakdown(value, totals, *, kind, start=None, end=None):
    if not isinstance(value, list):
        _invalid()
    sums = {field: 0 for field in COUNT_FIELDS}
    seen = set()
    for row in value:
        _validate_counts(row)
        key = row.get('value')
        if kind == 'date':
            try:
                day = date.fromisoformat(key)
            except (TypeError, ValueError):
                _invalid()
            if day.isoformat() != key or not start <= day <= end:
                _invalid()
        elif key is not None and not isinstance(key, str):
            _invalid()
        if key in seen:
            _invalid()
        seen.add(key)
        if kind in ('source', 'utm') and not isinstance(row.get('label'), str):
            _invalid()
        for field in COUNT_FIELDS:
            sums[field] += row[field]
    if sums != {field: totals[field] for field in COUNT_FIELDS}:
        _invalid()


def _validate_coverage(value):
    if not isinstance(value, dict) or not isinstance(value.get('history_started_at'), str) or \
            not isinstance(value.get('historical_data_complete'), bool):
        _invalid()
    timestamp = value['history_started_at']
    if not RFC3339_UTC.fullmatch(timestamp):
        _invalid()
    try:
        parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError:
        _invalid()
    if not timestamp.endswith('Z') or parsed.tzinfo != timezone.utc:
        _invalid()


def validate_summary(payload, start, end, expected_status):
    if not isinstance(payload, dict) or payload.get('date_basis') != 'visit' or \
            payload.get('timezone') != 'Asia/Tashkent' or payload.get('from') != start.isoformat() or \
            payload.get('to') != end.isoformat():
        _invalid()
    _validate_coverage(payload.get('coverage'))
    excluded = payload.get('excluded_missing_date')
    if isinstance(excluded, bool) or not isinstance(excluded, int) or excluded < 0:
        _invalid()
    totals = payload.get('totals')
    _validate_counts(totals)
    _validate_breakdown(payload.get('by_date'), totals, kind='date', start=start, end=end)
    _validate_breakdown(payload.get('by_status'), totals, kind='status')
    status_rows = payload['by_status']
    if totals['bookings'] == 0 and status_rows:
        _invalid()
    if totals['bookings'] > 0 and (len(status_rows) != 1 or status_rows[0]['value'] != expected_status):
        _invalid()
    _validate_breakdown(payload.get('by_source'), totals, kind='source')
    by_utm = payload.get('by_utm')
    if not isinstance(by_utm, dict) or set(by_utm) != set(UTM_FIELDS):
        _invalid()
    for field in UTM_FIELDS:
        _validate_breakdown(by_utm[field], totals, kind='utm')
    return payload


class BookingAnalyticsClient:
    """Клиент API бронирований. Соединение переживает запрос.

    Сводку открывают с экрана учредителя подряд по разным периодам, и каждый
    запрос — два чтения. Свой `AsyncClient` на вызов означал новый пул и новый
    TLS-хендшейк на каждое из них; один клиент на приложение держит
    соединение живым (BOOKING_KEEPALIVE) и закрывается вместе с приложением.
    """

    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport
        self._http = None

    def _client(self):
        if not self.settings.booking_configured:
            raise DataError('API бронирований ещё не настроен.')
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.settings.booking_api_url,
                headers={
                    'Accept': 'application/json',
                    'Authorization': 'Bearer ' + self.settings.booking_api_token,
                },
                timeout=15,
                follow_redirects=False,
                transport=self.transport,
                limits=httpx.Limits(keepalive_expiry=BOOKING_KEEPALIVE),
            )
        return self._http

    async def close(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def load(self, start, end):
        client = self._client()
        params = {
            'from': start.isoformat(),
            'to': end.isoformat(),
            'date_basis': 'visit',
        }
        try:
            result = {}
            statuses = ('submitted', 'cancelled')
            responses = await gather_reads(*(
                client.get('/analytics/summary', params={**params, 'status': status})
                for status in statuses))
            for status, response in zip(statuses, responses, strict=True):
                if response.status_code in (401, 403):
                    raise DataError('API бронирований отклонил доступ.')
                if not 200 <= response.status_code < 300:
                    raise DataError('API бронирований недоступен.')
                try:
                    payload = response.json()
                except ValueError:
                    raise DataError('API бронирований вернул некорректный ответ.') from None
                result[status] = validate_summary(payload, start, end, status)
            return result
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('bookings', error, operation='load_summary')
            raise DataError('Не удалось связаться с API бронирований.') from None
