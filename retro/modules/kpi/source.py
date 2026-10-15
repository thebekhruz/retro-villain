"""Чтение продаж по официантам из OLAP iiko (только чтение, те же вызовы, что у отчёта директора).

Три отчёта на окно до 7 дней: строки «день × касса × отделение × официант × чек × льгота ×
безналичный тип × карточка», типы оплаты чека (признак Яндекса и Узума) и удаления
(запрос без фильтров NOT_DELETED: иначе удалённых строк не видно). Себестоимость в
сгруппированных строках OLAP — итог строки, не цена порции (сверено с ./iiko.sh dish-sales).
"""
import asyncio
from datetime import date, timedelta
from urllib.parse import quote

import httpx

from retro.config import IIKO_ORIGIN
from retro.integrations.iiko import olap_range_body
from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import DataError

from .rules import Line, Order

LINE_GROUPS = ['OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'WaiterName', 'UniqOrderId.Id',
               'OrderDiscount.Type', 'NonCashPaymentType', 'DishId', 'DishName']
LINE_FIELDS = ['DishAmountInt', 'DishDiscountSumInt', 'ProductCostBase.ProductCost']
PAY_GROUPS = ['OpenDate.Typed', 'UniqOrderId.Id', 'PayTypes']
PAY_FIELDS = ['DishDiscountSumInt']
VOID_GROUPS = ['OpenDate.Typed', 'UniqOrderId.Id', 'DeletedWithWriteoff', 'OrderDeleted']
VOID_FIELDS = ['DishAmountInt']
MARGIN_GROUPS = ['DishId']
MARGIN_FIELDS = ['DishAmountInt', 'DishDiscountSumInt', 'ProductCostBase.ProductCost']
CHUNK_DAYS = 7
NOT_DELETED_LINE = 'Не удалено'
NOT_DELETED_ORDER = 'Заказ не удален'
HEADERS = {'Accept': 'application/json', 'Accept-Language': 'ru_RU', 'Content-Type': 'application/json'}


def flatten(rows, groups, fields):
    """Вложенная grouped-table → плоские словари {измерение: значение, поле: число}."""
    out = []
    count = len(groups)

    def visit(row, inherited):
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку отчёта KPI.')
        values = list(inherited)
        while len(values) < count:
            cell = row.get(f'field{len(values)}')
            if not isinstance(cell, dict) or 'value' not in cell:
                break
            values.append(cell['value'])
        children = row.get('children')
        if isinstance(children, list) and children:
            for child in children:
                visit(child, values)
            return
        if len(values) != count:
            raise DataError('iiko не вернул все измерения отчёта KPI.')
        record = dict(zip(groups, values))
        for index, name in enumerate(fields):
            cell = row.get(f'field{count + index}')
            value = cell.get('value') if isinstance(cell, dict) else None
            try:
                record[name] = float(value) if value not in (None, '') else 0.0
            except (TypeError, ValueError):
                record[name] = 0.0
        out.append(record)

    if not isinstance(rows, list):
        raise DataError('iiko вернул некорректную структуру отчёта KPI.')
    for row in rows:
        visit(row, [])
    return out


def chunks(start: date, end: date, days=CHUNK_DAYS):
    current = start
    while current <= end:
        last = min(end, current + timedelta(days=days - 1))
        yield current, last
        current = last + timedelta(days=1)


def void_body(store_id, start, end):
    body = olap_range_body(store_id, start, end, VOID_GROUPS, VOID_FIELDS)
    body['filters'] = [item for item in body['filters'] if item.get('filterType') == 'date_range']
    body['includeVoidTransactions'] = True
    return body


class KpiSource:
    """Выгрузка чеков и опорной маржи. `iiko` — IikoClient панели: его авторизация и ретраи."""

    def __init__(self, iiko):
        self.iiko = iiko

    def _ready(self):
        settings = self.iiko.settings
        if not settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        if settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        return settings

    async def _session(self):
        settings = self._ready()
        client = httpx.AsyncClient(base_url=IIKO_ORIGIN, headers=HEADERS, timeout=60, follow_redirects=False,
                                   transport=self.iiko.transport)
        try:
            auth = await self.iiko._post(client, '/api/auth/login',
                                         dict(login=settings.login, password=settings.password))
            if not isinstance(auth.get('token'), str) or not auth['token']:
                raise DataError('iiko не подтвердил авторизацию.')
            client.headers['Authorization'] = 'Bearer ' + auth['token']
        except BaseException:
            await client.aclose()
            raise
        return client

    async def _voids(self, client, start, end):
        body = void_body(self.iiko.settings.store_id, start, end)
        init = await self.iiko._post(client, '/api/olap/init', body)
        fetch_id = init.get('fetchId') or init.get('data')
        if not isinstance(fetch_id, str) or not fetch_id:
            raise DataError('iiko не вернул идентификатор отчёта.')
        for attempt in range(15):
            data = await self.iiko._post(client, '/api/olap/fetch/' + quote(fetch_id, safe='') + '/grouped-table',
                                         body, True)
            if data is not None:
                result = data.get('result')
                if not isinstance(result, dict) or not isinstance(result.get('rows'), list):
                    raise DataError('iiko вернул некорректную структуру отчёта.')
                return flatten(result['rows'], VOID_GROUPS, VOID_FIELDS)
            if attempt < 14:
                await asyncio.sleep(self.iiko.poll_delay)
        raise DataError('iiko ещё не подготовил отчёт. Повторите обновление через минуту.')

    async def orders(self, start: date, end: date) -> list:
        """Чеки за окно: строки, типы оплаты и удаления, собранные в Order."""
        if end < start:
            return []
        try:
            client = await self._session()
            try:
                windows = list(chunks(start, end))
                answers = await asyncio.gather(*(
                    asyncio.gather(
                        self.iiko._olap_range(client, first, last, LINE_GROUPS, LINE_FIELDS),
                        self.iiko._olap_range(client, first, last, PAY_GROUPS, PAY_FIELDS),
                        self._voids(client, first, last))
                    for first, last in windows))
            finally:
                await client.aclose()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_kpi_orders')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None
        orders = {}
        pay_types, deleted_lines, deleted_orders = {}, set(), set()
        for lines_raw, pays_raw, voids_raw in answers:
            for row in flatten(pays_raw, PAY_GROUPS, PAY_FIELDS):
                if row['PayTypes']:
                    pay_types.setdefault(row['UniqOrderId.Id'], set()).add(row['PayTypes'])
            for row in voids_raw:
                if row['OrderDeleted'] != NOT_DELETED_ORDER:
                    deleted_orders.add(row['UniqOrderId.Id'])
                elif row['DeletedWithWriteoff'] != NOT_DELETED_LINE:
                    deleted_lines.add(row['UniqOrderId.Id'])
            for row in flatten(lines_raw, LINE_GROUPS, LINE_FIELDS):
                order_id = row['UniqOrderId.Id']
                try:
                    day = date.fromisoformat(str(row['OpenDate.Typed'])[:10])
                except ValueError:
                    raise DataError('iiko вернул некорректную дату чека.') from None
                item = orders.setdefault(order_id, dict(
                    id=order_id, day=day, register=row['CashRegisterName'] or '', section=row['RestaurantSection'] or '',
                    waiters=set(), lines=[], discounts=set(), noncash=set()))
                item['waiters'].add((row['WaiterName'] or '').strip())
                if row['OrderDiscount.Type']:
                    item['discounts'].add(row['OrderDiscount.Type'])
                if row['NonCashPaymentType']:
                    item['noncash'].add(row['NonCashPaymentType'])
                item['lines'].append(Line(row['DishId'] or '', row['DishName'] or '', row['DishAmountInt'],
                                          row['DishDiscountSumInt'], row['ProductCostBase.ProductCost']))
        return [Order(id=item['id'], day=item['day'], register=item['register'], section=item['section'],
                      waiters=frozenset(item['waiters']), lines=tuple(item['lines']),
                      discount_types=frozenset(item['discounts']), noncash_types=frozenset(item['noncash']),
                      pay_types=frozenset(pay_types.get(item['id'], ())),
                      deleted_lines=item['id'] in deleted_lines, order_deleted=item['id'] in deleted_orders)
                for item in orders.values()]

    async def margins(self, start: date, end: date) -> dict:
        """Карточка → (порций, выручка после льгот, себестоимость) за окно: опорная маржа на порцию."""
        try:
            client = await self._session()
            try:
                answers = await asyncio.gather(*(
                    self.iiko._olap_range(client, first, last, MARGIN_GROUPS, MARGIN_FIELDS)
                    for first, last in chunks(start, end, 14)))
            finally:
                await client.aclose()
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_kpi_margins')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None
        totals = {}
        for raw in answers:
            for row in flatten(raw, MARGIN_GROUPS, MARGIN_FIELDS):
                item = totals.setdefault(row['DishId'], [0.0, 0.0, 0.0])
                item[0] += row['DishAmountInt']
                item[1] += row['DishDiscountSumInt']
                item[2] += row['ProductCostBase.ProductCost']
        return {dish_id: tuple(values) for dish_id, values in totals.items()}


def margin_per_unit(totals: dict, dishes) -> float | None:
    quantity = sum(totals.get(dish_id, (0, 0, 0))[0] for dish_id, _ in dishes)
    if quantity <= 0:
        return None
    revenue = sum(totals.get(dish_id, (0, 0, 0))[1] for dish_id, _ in dishes)
    cost = sum(totals.get(dish_id, (0, 0, 0))[2] for dish_id, _ in dishes)
    return (revenue - cost) / quantity
