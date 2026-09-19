from datetime import date

import httpx

from retro.modules.cashier.service import DataError


COUNT_FIELDS = ('bookings', 'guests', 'unknown_guest_bookings')
UTM_FIELDS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')


def _invalid():
    raise DataError('API бронирований вернул некорректный ответ.')


def _validate_counts(value):
    if not isinstance(value, dict):
        _invalid()
    for field in COUNT_FIELDS:
        count = value.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _invalid()


def _validate_breakdown(value, totals, *, kind):
    if not isinstance(value, list):
        _invalid()
    sums = {field: 0 for field in COUNT_FIELDS}
    for row in value:
        _validate_counts(row)
        key = row.get('value')
        if kind == 'date':
            try:
                date.fromisoformat(key)
            except (TypeError, ValueError):
                _invalid()
        elif key is not None and not isinstance(key, str):
            _invalid()
        if kind in ('source', 'utm') and not isinstance(row.get('label'), str):
            _invalid()
        for field in COUNT_FIELDS:
            sums[field] += row[field]
    if sums != {field: totals[field] for field in COUNT_FIELDS}:
        _invalid()


def validate_summary(payload, start, end):
    if not isinstance(payload, dict) or payload.get('date_basis') != 'visit' or \
            payload.get('timezone') != 'Asia/Tashkent' or payload.get('from') != start.isoformat() or \
            payload.get('to') != end.isoformat():
        _invalid()
    coverage = payload.get('coverage')
    if not isinstance(coverage, dict) or not isinstance(coverage.get('history_started_at'), str) or \
            not isinstance(coverage.get('historical_data_complete'), bool):
        _invalid()
    excluded = payload.get('excluded_missing_date')
    if isinstance(excluded, bool) or not isinstance(excluded, int) or excluded < 0:
        _invalid()
    totals = payload.get('totals')
    _validate_counts(totals)
    _validate_breakdown(payload.get('by_date'), totals, kind='date')
    _validate_breakdown(payload.get('by_status'), totals, kind='status')
    _validate_breakdown(payload.get('by_source'), totals, kind='source')
    by_utm = payload.get('by_utm')
    if not isinstance(by_utm, dict) or set(by_utm) != set(UTM_FIELDS):
        _invalid()
    for field in UTM_FIELDS:
        _validate_breakdown(by_utm[field], totals, kind='utm')
    return payload


class BookingAnalyticsClient:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    async def load(self, start, end):
        if not self.settings.booking_configured:
            raise DataError('API бронирований ещё не настроен.')
        headers = {
            'Accept': 'application/json',
            'Authorization': 'Bearer ' + self.settings.booking_api_token,
        }
        params = {
            'from': start.isoformat(),
            'to': end.isoformat(),
            'date_basis': 'visit',
        }
        try:
            async with httpx.AsyncClient(
                    base_url=self.settings.booking_api_url,
                    headers=headers,
                    timeout=15,
                    follow_redirects=False,
                    transport=self.transport,
            ) as client:
                result = {}
                for status in ('submitted', 'cancelled'):
                    response = await client.get('/analytics/summary', params={**params, 'status': status})
                    if response.status_code in (401, 403):
                        raise DataError('API бронирований отклонил доступ.')
                    if not 200 <= response.status_code < 300:
                        raise DataError('API бронирований недоступен.')
                    try:
                        payload = response.json()
                    except ValueError:
                        raise DataError('API бронирований вернул некорректный ответ.') from None
                    result[status] = validate_summary(payload, start, end)
                return result
        except (httpx.HTTPError, TimeoutError):
            raise DataError('Не удалось связаться с API бронирований.') from None
