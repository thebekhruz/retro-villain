import re
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone

import httpx

from retro.async_utils import gather_reads

from retro.modules.cashier.service import DataError
from retro.logging_config import log_upstream_failure


COUNT_FIELDS = ('bookings', 'guests', 'unknown_guest_bookings')
UTM_FIELDS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')
RFC3339_UTC = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z')


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
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    async def load(self, start, end):
        if not self.settings.website_booking_api_url:
            return await self._load_single(start, end)
        sources = []
        if self.settings.booking_configured:
            sources.append(('telegram', 'Telegram', self))
        website_settings = replace(
            self.settings, booking_api_url=self.settings.website_booking_api_url,
            booking_api_token=self.settings.website_booking_api_token)
        sources.append(('website', 'Сайт', BookingAnalyticsClient(website_settings, transport=self.transport)))

        async def load_source(source):
            key, label, client = source
            try:
                raw = await client._load_single(start, end)
                if raw['submitted']['coverage'] != raw['cancelled']['coverage'] or \
                        raw['submitted']['excluded_missing_date'] != raw['cancelled']['excluded_missing_date']:
                    raise DataError('API бронирований вернул несогласованное покрытие истории.')
                return key, label, raw
            except DataError as error:
                raise DataError(f'{label}: {error}') from None

        # Fail visibly if either configured source fails: partial counts must not look complete.
        loaded = await gather_reads(*(load_source(source) for source in sources))
        return merge_summaries(loaded)

    async def _load_single(self, start, end):
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


def merge_summaries(sources):
    coverage_sources = [
        {'id': key, 'name': label, **raw['submitted']['coverage'],
         'excluded_missing_date': raw['submitted']['excluded_missing_date']}
        for key, label, raw in sources]
    coverage = {
        'history_started_at': min(row['history_started_at'] for row in coverage_sources),
        'historical_data_complete': all(row['historical_data_complete'] for row in coverage_sources),
        'sources': coverage_sources,
    }
    result = {}
    for status in ('submitted', 'cancelled'):
        merged = deepcopy(sources[0][2][status])
        merged['coverage'] = coverage
        merged['excluded_missing_date'] = sum(row['excluded_missing_date'] for row in coverage_sources)
        merged['totals'] = {field: sum(raw[status]['totals'][field] for _, _, raw in sources)
                            for field in COUNT_FIELDS}
        for field in ('by_date', 'by_status'):
            merged[field] = _merge_rows([row for _, _, raw in sources for row in raw[status][field]])
        merged['by_utm'] = {field: _merge_rows(
            [row for _, _, raw in sources for row in raw[status]['by_utm'][field]]) for field in UTM_FIELDS}
        merged['by_source'] = []
        for key, label, raw in sources:
            for row in raw[status]['by_source']:
                detail = row['label']
                if key == 'website':
                    detail = {'website': '', 'website_telegram': 'Telegram-чат бота сайта'}.get(row['value'], detail)
                merged['by_source'].append({**row, 'value': key + ':' + str(row['value']),
                                            'label': label + (' · ' + detail if detail else '')})
        result[status] = merged
    return result


def _merge_rows(rows):
    grouped = {}
    for row in rows:
        key = row['value']
        if key not in grouped:
            grouped[key] = {**row, **{field: 0 for field in COUNT_FIELDS}}
        for field in COUNT_FIELDS:
            grouped[key][field] += row[field]
    return [grouped[key] for key in sorted(grouped, key=lambda value: '' if value is None else str(value))]
