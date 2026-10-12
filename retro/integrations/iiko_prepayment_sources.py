"""Read existing reservations and payment records without changing iiko data.

Workload does not link reserves to POS orders. Cashless PREPAY records do not
link to orders either. A time/amount match is only a hint, never an accounting
fact, and this client never writes to the local registry or iiko.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from time import monotonic
from uuid import UUID

import httpx

from retro.logging_config import log_safe_failure
from retro.report_cache import ReportCache
from retro.modules.cashier.prepayments import report_body_range, prepayments_from_olap
from retro.modules.cashier.service import DataError, TZ, number, today_tashkent

CLOUD_ORIGIN = 'https://api-ru.iiko.services'
TERMINAL_GROUP_ID = '169f50e5-f412-02c9-0184-bd213c340064'
CLOUD_READ_PATHS = frozenset({
    '/api/1/access_token',
    '/api/1/reserve/available_restaurant_sections',
    '/api/1/reserve/restaurant_sections_workload',
})


@dataclass(frozen=True)
class PartialPayments:
    items: list


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise DataError('iiko не вернул корректный идентификатор.') from None


def text(value, maximum=160):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DataError('iiko вернул неполное описание записи.')
    return value.strip()


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
        return (result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ))
    except (ValueError, TypeError):
        raise DataError('iiko вернул некорректное время записи.') from None


def unique(items):
    found = {}
    for item in items:
        previous = found.setdefault(item['id'], item)
        if previous != item:
            raise DataError('iiko вернул разные данные для одной записи.')
    return list(found.values())


def reservations_from_workload(sections, data, start, end):
    if not isinstance(sections, list) or not isinstance(data.get('reserves'), list):
        raise DataError('iiko не вернул список броней.')
    tables = {}
    for section in sections:
        for table in section['tables']:
            tables[identifier(table['id'])] = {
                'id': identifier(table['id']), 'number': table['number'],
                'section': text(section['name']),
            }
    result = []
    for reserve in data['reserves']:
        when = stamp(reserve['estimatedStartTime'])
        if not start <= when.date() <= end:
            continue
        table_ids = [identifier(value) for value in reserve['tableIds']]
        if not table_ids or any(value not in tables for value in table_ids):
            raise DataError('iiko не вернул стол для брони.')
        result.append({
            'id': identifier(reserve['id']), 'estimated_start_time': when.isoformat(),
            'duration_minutes': int(reserve['durationInMinutes']),
            'guests_count': int(reserve['guestsCount']),
            'tables': [tables[value] for value in table_ids],
            'source': 'iiko_cloud_workload', 'order_id': None, 'link_status': 'unavailable',
        })
    return sorted(unique(result), key=lambda row: (row['estimated_start_time'], row['id']))


def payments_from_shift(data, shift_id, start, end):
    if not isinstance(data.get('data'), dict):
        raise DataError('iiko не вернул детализацию смены.')
    transactions = data['data'].get('transactions')
    if not isinstance(transactions, dict):
        raise DataError('iiko пока не предоставил детализацию смены.')
    records = transactions['cashlessRecords']
    types = data['decoration']['paymentTypes']
    if not isinstance(records, list) or not isinstance(types, dict):
        raise DataError('iiko не вернул детализацию платежей.')
    result = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('info'), dict):
            raise DataError('iiko вернул неполную запись платежа.')
        info = record['info']
        if info.get('type') != 'PREPAY':
            continue
        # creationDate is Unix seconds. Never infer cash from an absent card row.
        value = info['creationDate']
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise DataError('iiko не вернул время платежа.')
        when = datetime.fromtimestamp(value, TZ)
        if not start <= when.date() <= end:
            continue
        amount = number(info['sum'])
        if amount <= 0:
            raise DataError('iiko вернул неоднозначную сумму предоплаты.')
        type_id = identifier(info['paymentTypeId'])
        method = types.get(type_id)
        if isinstance(method, dict):
            method = method.get('name')
        result.append({
            'id': identifier(info['id']), 'shift_id': identifier(shift_id),
            'received_at': when.isoformat(), 'amount': str(amount),
            'payment_type_id': type_id, 'payment_method': text(method) if method else None,
            'source': 'iiko_web_cashless_prepay', 'order_id': None,
        })
    return unique(result)


def payment_matches(payments, entries):
    """Require a one-to-one time/amount match; even that remains unconfirmed."""
    candidates, usage = {}, {}
    for payment in payments:
        matches = []
        for entry in entries:
            if (entry.order_number and entry.order_id and entry.amount == Decimal(payment['amount'])
                    and int(stamp(entry.received_at).timestamp()) == int(stamp(payment['received_at']).timestamp())):
                match = {'order_id': identifier(entry.order_id), 'order_number': entry.order_number,
                         'transaction_id': entry.id}
                if match not in matches:
                    matches.append(match)
        candidates[payment['id']] = matches
        for match in matches:
            usage[match['transaction_id']] = usage.get(match['transaction_id'], 0) + 1
    result = []
    for payment in payments:
        matches = candidates[payment['id']]
        one_to_one = len(matches) == 1 and usage[matches[0]['transaction_id']] == 1
        result.append({**payment, 'link_status': 'unconfirmed' if one_to_one else 'ambiguous' if matches else 'unlinked',
                       'candidate_order': matches[0] if one_to_one else None})
    return result


def demo_sources(start, end):
    def part(items):
        return {'status': 'ok', 'items': items, 'checked_at': datetime.now(TZ).isoformat(), 'issue': None}
    visit = min(end, max(start, today_tashkent() + timedelta(days=3)))
    return {'start': start.isoformat(), 'end': end.isoformat(), 'links_verified': False,
            'matching_issue': None, 'reservations': part([{
                'id': 'demo-reserve', 'estimated_start_time': visit.isoformat() + 'T19:00:00+05:00',
                'duration_minutes': 180, 'guests_count': 4,
                'tables': [{'id': 'demo-table', 'number': 22, 'section': 'Ресторан'}],
                'order_id': None, 'link_status': 'unavailable', 'source': 'demo',
            }]), 'payments': part([] if start > today_tashkent() else [{
                'id': 'demo-payment', 'shift_id': 'demo-shift',
                'received_at': start.isoformat() + 'T13:30:00+05:00', 'amount': '500000',
                'payment_type_id': 'demo-type', 'payment_method': 'Click/Payme Безналичный перевод',
                'source': 'demo', 'order_id': None, 'link_status': 'unconfirmed',
                'candidate_order': {'order_number': '102', 'order_id': 'demo-order', 'transaction_id': 'demo-entry'},
            }])}


class IikoPrepaymentSources:
    def __init__(self, settings, web, *, transport=None):
        self.settings, self.web, self.transport = settings, web, transport
        self.cache = ReportCache(concurrency=2, limit=32)
        self._http = None
        self._auth_lock = asyncio.Lock()
        self._auth_until = 0

    async def close(self):
        await self.cache.close()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _cloud_send(self, path, body):
        if path not in CLOUD_READ_PATHS:
            raise DataError('Этот запрос iiko не разрешён для чтения.')
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=CLOUD_ORIGIN, transport=self.transport,
                timeout=20, follow_redirects=False,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))
        return await self._http.post(path, json=body)

    @staticmethod
    def _json(response):
        if response.status_code in (401, 403):
            raise DataError('iiko отклонил доступ к источнику.')
        if not response.is_success:
            raise DataError(f'iiko не выполнил чтение (HTTP {response.status_code}).')
        try:
            data = response.json()
        except ValueError:
            raise DataError('iiko вернул ответ не в формате JSON.') from None
        if not isinstance(data, dict) or data.get('error') or data.get('errorDescription'):
            raise DataError('iiko не подтвердил чтение источника.')
        return data

    async def _authorize(self, rejected=None):
        async with self._auth_lock:
            current = self._http.headers.get('Authorization') if self._http else None
            if current and ((rejected is None and monotonic() < self._auth_until)
                            or (rejected is not None and current != rejected)):
                return
            login_id = self.settings.iiko_cloud_api_login_id
            if not login_id:
                raise DataError('Чтение броней iiko не настроено.')
            async with self.web._client() as client:
                info = await self.web._post(client, '/api/integration-management/api-logins/get',
                                            {'apiLoginId': identifier(login_id)})
            login = info.get('apiLoginInfo')
            key = login.get('apiKey') if isinstance(login, dict) else None
            if not isinstance(key, str) or not key:
                raise DataError('iiko не предоставил доступ к броням.')
            data = self._json(await self._cloud_send('/api/1/access_token', {'apiLogin': key}))
            token = data.get('token')
            if not isinstance(token, str) or not token:
                raise DataError('iiko не подтвердил авторизацию Cloud.')
            self._http.headers['Authorization'] = 'Bearer ' + token
            self._auth_until = monotonic() + 14 * 60

    async def _cloud_read(self, path, body):
        await self._authorize()
        token = self._http.headers.get('Authorization')
        response = await self._cloud_send(path, body)
        if response.status_code == 401:
            await self._authorize(rejected=token)
            response = await self._cloud_send(path, body)
        return self._json(response)

    async def _reservations(self, start, end):
        data = await self._cloud_read('/api/1/reserve/available_restaurant_sections',
            {'terminalGroupIds': [TERMINAL_GROUP_ID], 'returnSchema': False})
        sections = data['restaurantSections']
        if not isinstance(sections, list) or not sections:
            raise DataError('iiko не предоставил залы для чтения броней.')
        data = await self._cloud_read('/api/1/reserve/restaurant_sections_workload', {
            'restaurantSectionIds': [identifier(section['id']) for section in sections],
            'dateFrom': start.isoformat() + ' 00:00:00.000',
            'dateTo': end.isoformat() + ' 23:59:59.999',
        })
        return reservations_from_workload(sections, data, start, end)

    async def _payments(self, start, end):
        if start > end:
            return []
        async with self.web._client() as client:
            data = await self.web._post(client, '/api/cash/shift/list_period', {
                # Include the previous day's shift for payments after midnight.
                'dateFrom': (start - timedelta(days=1)).isoformat(), 'dateTo': end.isoformat(),
            })
            if not isinstance(data.get('shifts'), list):
                raise DataError('iiko не вернул список кассовых смен.')
            ids = set()
            for shift in data['shifts']:
                if not isinstance(shift, dict):
                    raise DataError('iiko вернул некорректный список смен.')
                if shift.get('cashRegNumber') == 1:
                    ids.add(identifier(shift['id']))
            result, failures, completed = [], 0, 0
            for shift_id in sorted(ids):
                try:
                    path = '/api/cash/shift/details/' + shift_id
                    token = client.headers.get('Authorization')
                    response = await client.get(path)
                    if response.status_code in (401, 403):
                        await self.web._authorize(client, rejected_token=token)
                        response = await client.get(path)
                    result.extend(payments_from_shift(self._json(response), shift_id, start, end))
                    completed += 1
                except (DataError, httpx.HTTPError, KeyError, TypeError, ValueError, OverflowError) as error:
                    failures += 1
                    log_safe_failure('iiko-prepayment-sources', error, operation='shift_details')
            if failures and not completed:
                raise DataError('iiko пока не предоставил детализацию смен.')
            rows = sorted(unique(result), key=lambda row: (row['received_at'], row['id']))
            return PartialPayments(rows) if failures else rows

    async def _transactions(self, start, end):
        entries, chunk = [], start
        async with self.web._client() as client:
            while chunk <= end:
                until = min(end, chunk + timedelta(days=6))
                body = report_body_range(self.settings.store_id, chunk, until)
                body['groupFields'].append('OrderId')
                rows = await self.web._fetch_olap(client, body)
                parsed = prepayments_from_olap(rows, include_order_ids=True)
                for entry in parsed:
                    if entry.order_id is not None:
                        identifier(entry.order_id)
                entries.extend(parsed)
                chunk = until + timedelta(days=1)
        return entries

    async def _part(self, name, start, end, operation, *, refresh=False):
        async def fetch():
            items = await operation(start, end)
            partial = isinstance(items, PartialPayments)
            return {'status': 'partial' if partial else 'ok', 'items': items.items if partial else items,
                    'checked_at': datetime.now(TZ).isoformat(),
                    'issue': 'Не все смены iiko доступны. Показаны только прочитанные платежи.' if partial else None}
        try:
            if not self.settings.configured:
                raise DataError('Подключение iiko ещё не настроено.')
            return await self.cache.get((name, start, end), fetch, ttl=60, timeout=55,
                                        refresh=refresh, label='iiko_prepayment_' + name)
        except (DataError, httpx.HTTPError, TimeoutError, KeyError, TypeError, ValueError, OverflowError) as error:
            log_safe_failure('iiko-prepayment-sources', error, operation=name)
            return {'status': 'unavailable', 'items': None, 'checked_at': None,
                    'issue': {'reservations': 'Не удалось получить брони iiko.',
                              'payments': 'Не удалось получить платежи из смен iiko.',
                              'transactions': 'Не удалось проверить совпадения с заказами.'}[name]}

    async def read(self, start, end, *, refresh=False):
        if start > end or (end - start).days > 30:
            raise DataError('Выберите период не больше 31 дня.')
        until = min(end, today_tashkent())
        reservations, payments = await asyncio.gather(
            self._part('reservations', start, end, self._reservations, refresh=refresh),
            self._part('payments', start, until, self._payments, refresh=refresh))
        matching_issue = None
        if payments['items']:
            transactions = await self._part('transactions', start - timedelta(days=1), until,
                                           self._transactions, refresh=refresh)
            matching_issue = transactions['issue']
            # Never mutate the shared cached source rows with request-specific hints.
            payments = {**payments, 'items': payment_matches(payments['items'], transactions['items'] or [])}
        return {'start': start.isoformat(), 'end': end.isoformat(),
                'reservations': reservations, 'payments': payments, 'matching_issue': matching_issue,
                'links_verified': False}
