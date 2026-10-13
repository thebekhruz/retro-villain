"""Брони iiko Cloud — только чтение (T-440, даты визита предоплат).

Cloud отдаёт брони кассы: стол и дату визита. Заказы и платежи, созданные на
кассе, он не отдаёт (проверено 13.10.2026: лицензия и Transport включены, а
`order/*` и `reserve/status_by_id` пусты), поэтому бронь с предоплатой
связывается по столу открытого заказа — см. retro.modules.cashier.visits.
Клиент ничего не пишет ни в iiko, ни в Cloud.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from uuid import UUID

import httpx

from retro.modules.cashier.service import BANQUET_SECTION, TZ, DataError
from retro.report_cache import ReportCache

CLOUD_ORIGIN = 'https://api-ru.iiko.services'
TERMINAL_GROUP_ID = '169f50e5-f412-02c9-0184-bd213c340064'  # группа терминалов «Ретро»
CLOUD_READ_PATHS = frozenset({
    '/api/1/access_token',
    '/api/1/reserve/available_restaurant_sections',
    '/api/1/reserve/restaurant_sections_workload',
})
TOKEN_SECONDS = 14 * 60  # токен Cloud живёт час; берём с запасом


@dataclass(frozen=True)
class Reserve:
    table: str
    section: str
    visit_at: datetime


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise DataError('iiko Cloud не вернул корректный идентификатор.') from None


def reserves_from_workload(sections, data):
    """Брони залов Retro: стол, зал, время визита. Банкетный зал не входит в реестр."""
    if not isinstance(sections, list) or not isinstance(data, dict) or not isinstance(data.get('reserves'), list):
        raise DataError('iiko Cloud не вернул список броней.')
    tables = {}
    for section in sections:
        name = section.get('name') if isinstance(section, dict) else None
        if not isinstance(name, str) or not isinstance(section.get('tables'), list):
            raise DataError('iiko Cloud вернул неполное описание зала.')
        for table in section['tables']:
            number = table.get('number') if isinstance(table, dict) else None
            if not isinstance(number, (int, str)) or isinstance(number, bool):
                raise DataError('iiko Cloud вернул стол без номера.')
            tables[identifier(table.get('id'))] = (str(number), name.strip())
    result = []
    for reserve in data['reserves']:
        try:
            visit_at = datetime.fromisoformat(reserve['estimatedStartTime'])
            table_ids = [identifier(value) for value in reserve['tableIds']]
        except (KeyError, TypeError, ValueError):
            raise DataError('iiko Cloud вернул неполную бронь.') from None
        visit_at = visit_at.replace(tzinfo=TZ) if visit_at.tzinfo is None else visit_at.astimezone(TZ)
        for table_id in table_ids:
            if table_id not in tables:
                raise DataError('iiko Cloud не вернул стол брони.')
            number, section = tables[table_id]
            if section == BANQUET_SECTION or 'бехруз' in section.casefold():
                continue
            result.append(Reserve(number, section, visit_at))
    return sorted(set(result), key=lambda item: (item.visit_at, item.table, item.section))


class IikoCloudReserves:
    def __init__(self, settings, web, *, transport=None):
        self.settings, self.web, self.transport = settings, web, transport
        self.cache = ReportCache(concurrency=1, limit=8)
        self._http = None
        self._auth_lock = asyncio.Lock()
        self._auth_until = 0

    @property
    def configured(self):
        return bool(self.settings.configured and self.settings.iiko_cloud_api_login_id)

    async def close(self):
        await self.cache.close()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _send(self, path, body):
        if path not in CLOUD_READ_PATHS:
            raise DataError('Этот запрос iiko Cloud не разрешён: клиент только читает брони.')
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=CLOUD_ORIGIN, transport=self.transport, timeout=20,
                                           follow_redirects=False,
                                           limits=httpx.Limits(max_connections=2, max_keepalive_connections=2))
        return await self._http.post(path, json=body)

    @staticmethod
    def _json(response):
        if response.status_code in (401, 403):
            raise DataError('iiko Cloud отклонил доступ к броням.')
        if not response.is_success:
            raise DataError(f'iiko Cloud не выполнил чтение (HTTP {response.status_code}).')
        try:
            data = response.json()
        except ValueError:
            raise DataError('iiko Cloud вернул ответ не в формате JSON.') from None
        if not isinstance(data, dict) or data.get('error') or data.get('errorDescription'):
            raise DataError('iiko Cloud не подтвердил чтение броней.')
        return data

    async def _authorize(self, rejected=None):
        async with self._auth_lock:
            current = self._http.headers.get('Authorization') if self._http else None
            if current and ((rejected is None and monotonic() < self._auth_until)
                            or (rejected is not None and current != rejected)):
                return
            async with self.web._client() as client:
                info = await self.web._post(client, '/api/integration-management/api-logins/get',
                                            {'apiLoginId': identifier(self.settings.iiko_cloud_api_login_id)})
            login = info.get('apiLoginInfo')
            key = login.get('apiKey') if isinstance(login, dict) else None
            if not isinstance(key, str) or not key:
                raise DataError('iiko не выдал доступ к броням Cloud.')
            token = self._json(await self._send('/api/1/access_token', {'apiLogin': key})).get('token')
            if not isinstance(token, str) or not token:
                raise DataError('iiko Cloud не подтвердил авторизацию.')
            self._http.headers['Authorization'] = 'Bearer ' + token
            self._auth_until = monotonic() + TOKEN_SECONDS

    async def _read(self, path, body):
        await self._authorize()
        token = self._http.headers.get('Authorization')
        response = await self._send(path, body)
        if response.status_code == 401:
            await self._authorize(rejected=token)
            response = await self._send(path, body)
        return self._json(response)

    async def reserves(self, start, end):
        """Брони залов Retro с визитом в [start, end]. Кэш — пять минут."""
        if not self.configured:
            raise DataError('Чтение броней iiko Cloud не настроено.')

        async def fetch():
            sections = (await self._read('/api/1/reserve/available_restaurant_sections',
                                         {'terminalGroupIds': [TERMINAL_GROUP_ID], 'returnSchema': False})
                        ).get('restaurantSections')
            if not isinstance(sections, list) or not sections:
                raise DataError('iiko Cloud не вернул залы для броней.')
            data = await self._read('/api/1/reserve/restaurant_sections_workload', {
                'restaurantSectionIds': [identifier(section.get('id')) for section in sections],
                'dateFrom': start.isoformat() + ' 00:00:00.000',
                'dateTo': end.isoformat() + ' 23:59:59.999'})
            return tuple(reserves_from_workload(sections, data))
        return await self.cache.get(('reserves', start, end), fetch, ttl=300, timeout=40,
                                    label='iiko_cloud_reserves')
