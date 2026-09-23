import asyncio
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote

import httpx

from retro.config import IIKO_ORIGIN
from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import (
    BANQUET_SECTION, RETRO_REGISTER, DataError, build_revenue_breakdown, build_snapshot, cell, number,
)
from retro.modules.director.models import SalesRow, build_snapshot as build_director_snapshot, completed_period
from retro.modules.founder.models import (
    PaymentRow, RevenueRow, build_analytics, is_banquet_item,
)


DIRECTOR_GROUPS = ['CashRegisterName', 'RestaurantSection', 'PayTypes', 'DishName',
                   'DishGroup', 'WaiterName', 'UniqOrderId.Id']
DIRECTOR_FIELDS = ['DishAmountInt', 'DishDiscountSumInt', 'ProductCostBase.ProductCost']
IIKO_DETAIL_DIMENSIONS = (
    'OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'PayTypes',
    'DishName', 'DishGroup', 'WaiterName', 'UniqOrderId.Id', 'OperationType',
)
IIKO_DETAIL_FIELDS = (
    'DishAmountInt', 'DishDiscountSumInt', 'ProductCostBase.ProductCost',
    'ProductCostBase.OneItem', 'UniqOrderId.OrdersCount',
)
FOUNDER_OLAP_MAX_DAYS = 31
FOUNDER_OLAP_CHUNK_CONCURRENCY = 2


def date_chunks(start, end, *, max_days):
    """Yield inclusive ranges small enough for one iiko OLAP report."""
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=max_days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def director_rows_from_olap(day, rows):
    """Flatten iiko's nested grouped-table response into safe typed sale rows."""
    result = []

    def visit(row, inherited):
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку отчёта директора.')
        values = list(inherited)
        while len(values) < len(DIRECTOR_GROUPS):
            index = len(values)
            field = row.get(f'field{index}')
            if not isinstance(field, dict) or 'value' not in field:
                break
            values.append(field['value'])
        children = row.get('children')
        if isinstance(children, list) and children:
            for child in children:
                visit(child, values)
            return
        if len(values) != len(DIRECTOR_GROUPS):
            raise DataError('iiko не вернул все измерения продажи.')
        quantity, revenue, total_cost = (number(cell(row, index)) for index in range(7, 10))
        register, section, payment_type, item, category, waiter, order_id = values
        # У части продаж группа блюда в iiko пустая. Без имени такую строку
        # нельзя ни отнести к типу отчёта, ни исключить — отчёт падал целиком
        # из-за девяти тысяч сум. Даём ей имя, и дальше она настраивается как
        # любая другая группа.
        if not isinstance(category, str) or not category.strip():
            category = 'Без группы'
        result.append(SalesRow(day, register, section, payment_type, item, category,
                               quantity, revenue, total_cost, waiter, order_id))

    for row in rows:
        visit(row, [])
    return result


def cash_prepay_from_shifts(day, sales, payments, shifts):
    if not isinstance(shifts, list):
        raise DataError('iiko не вернул список кассовых смен.')
    selected = []
    for shift in shifts:
        if not isinstance(shift, dict):
            raise DataError('iiko вернул некорректную кассовую смену.')
        if shift.get('cashRegNumber') == 1 and str(shift.get('openDate', ''))[:10] == day.isoformat():
            selected.append(shift)
    if not selected and sales:
        raise DataError('iiko не вернул кассовую смену для расчёта предоплат.')
    total_received = Decimal(0)
    cash_received = Decimal(0)
    for shift in selected:
        paid = number(shift.get('payOrders'))
        cash = number(shift.get('salesCash'))
        card = number(shift.get('salesCard'))
        credit = number(shift.get('salesCredit'))
        if min(paid, cash, card, credit) < 0 or cash + card + credit != paid:
            raise DataError('iiko вернул противоречивые суммы кассовой смены.')
        total_received += paid
        cash_received += cash
    new_prepayment = total_received - sales
    cash_sales = number(payments.get('Демо', 0)) + number(payments.get('Наличные (Инкасса QR)', 0))
    cash_prepayment = cash_received - cash_sales
    if new_prepayment < 0 or not 0 <= cash_prepayment <= new_prepayment:
        raise DataError('Продажи и предоплаты iiko не совпали с кассовой сменой. Обновите отчёт.')
    return new_prepayment, cash_prepayment


def olap_body(store_id, day, groups, fields, extra_filters=()):
    return olap_range_body(store_id, day, day, groups, fields, extra_filters)


def olap_range_body(store_id, start, end, groups, fields, extra_filters=()):
    return dict(storeIds=[store_id], olapType='SALES', groupFields=groups,
                dataFields=fields, calculatedFields=[],
                filters=[dict(filterType='date_range', dateFrom=start.isoformat(),
                              dateTo=end.isoformat(), includeLeft=True, includeRight=True,
                              field='OpenDate.Typed')] + [
                    dict(field=field, filterType='value_list', dateFrom=None, dateTo=None,
                         valueMin=None, valueMax=None, valueList=['NOT_DELETED'],
                         includeLeft=True, includeRight=False, inclusiveList=True)
                    for field in ['DeletedWithWriteoff', 'OrderDeleted']] + list(extra_filters),
                includeVoidTransactions=False, includeNonBusinessPaymentTypes=False)


def founder_rows_from_olap(rows, *, payments=False, dish_filter='all'):
    if dish_filter not in {'all', 'exclude_banquet', 'banquet_only'}:
        raise ValueError('unknown founder dish filter')
    group_count = 5 if payments else 4
    result = []

    def visit(row, inherited):
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку аналитики.')
        values = list(inherited)
        while len(values) < group_count:
            field = row.get(f'field{len(values)}')
            if not isinstance(field, dict) or 'value' not in field:
                break
            values.append(field['value'])
        children = row.get('children')
        if isinstance(children, list) and children:
            for child in children:
                visit(child, values)
            return
        if len(values) != group_count:
            raise DataError('iiko не вернул все измерения аналитики.')
        try:
            day = date.fromisoformat(values[0])
        except (TypeError, ValueError):
            raise DataError('iiko вернул некорректную дату аналитики.') from None
        amount = number(cell(row, group_count))
        banquet_item = is_banquet_item(values[3])
        if dish_filter == 'exclude_banquet' and banquet_item:
            return
        if dish_filter == 'banquet_only' and not banquet_item:
            return
        if payments:
            result.append(PaymentRow(
                day, values[1], values[2], values[3], values[4], amount))
        else:
            result.append(RevenueRow(day, values[1], values[2], values[3], amount))

    if not isinstance(rows, list):
        raise DataError('iiko вернул некорректную структуру аналитики.')
    for row in rows:
        visit(row, [])
    return result


def detail_rows_from_olap(rows, dimensions, *, limit):
    """Flatten a bounded, allowlisted iiko OLAP report for founder questions."""
    records = []
    total_rows = 0
    group_count = len(dimensions)

    def visit(row, inherited):
        nonlocal total_rows
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку детального отчёта.')
        values = list(inherited)
        while len(values) < group_count:
            field = row.get(f'field{len(values)}')
            if not isinstance(field, dict) or 'value' not in field:
                break
            values.append(field['value'])
        children = row.get('children')
        if isinstance(children, list) and children:
            for child in children:
                visit(child, values)
            return
        if len(values) != group_count:
            raise DataError('iiko не вернул все измерения детального отчёта.')
        quantity, revenue, total_cost, unit_cost, orders = (
            number(cell(row, group_count + index)) for index in range(5)
        )
        total_rows += 1
        if len(records) >= limit:
            return
        records.append({
            'dimensions': dict(zip(dimensions, values, strict=True)),
            'quantity': str(quantity),
            'revenue': str(revenue),
            'product_cost_per_unit': str(unit_cost),
            'product_cost_total': str(total_cost),
            'orders': str(orders),
        })

    if not isinstance(rows, list):
        raise DataError('iiko вернул некорректную структуру детального отчёта.')
    for row in rows:
        visit(row, [])
    return records, total_rows


class IikoClient:
    def __init__(self, settings, *, transport=None, poll_delay=1):
        self.settings, self.transport, self.poll_delay = settings, transport, poll_delay

    async def load(self, day):
        if not self.settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        if self.settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        headers = {'Accept': 'application/json', 'Accept-Language': 'ru_RU',
                   'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(base_url=IIKO_ORIGIN, headers=headers,
                                        timeout=25, follow_redirects=False,
                                        transport=self.transport) as client:
                auth = await self._post(client, '/api/auth/login',
                                        dict(login=self.settings.login, password=self.settings.password))
                if not isinstance(auth.get('token'), str) or not auth['token']:
                    raise DataError('iiko не подтвердил авторизацию.')
                client.headers['Authorization'] = 'Bearer ' + auth['token']
                breakdown_rows = await self._olap(client, day,
                                                  ['CashRegisterName', 'RestaurantSection'],
                                                  ['DishDiscountSumInt'])
                breakdown = build_revenue_breakdown(breakdown_rows)
                scope = [
                    dict(field='CashRegisterName', filterType='value_list',
                         valueList=[RETRO_REGISTER], inclusiveList=True),
                    dict(field='RestaurantSection', filterType='value_list',
                         valueList=[BANQUET_SECTION], inclusiveList=False),
                    dict(field='OperationType', filterType='value_list',
                         valueList=['PAYMENT'], inclusiveList=True),
                ]
                total = await self._olap(client, day, ['OpenDate.Typed'],
                                         ['UniqOrderId.OrdersCount', 'DishDiscountSumInt'], scope)
                payments = await self._olap(client, day, ['PayTypes'],
                                            ['DishDiscountSumInt'], scope)
                snapshot = build_snapshot(day, total, payments, revenue_breakdown=breakdown)
                shifts_data = await self._post(client, '/api/cash/shift/list_period',
                                               {'dateFrom': day.isoformat(), 'dateTo': day.isoformat()})
                shifts = shifts_data.get('shifts')
                if not isinstance(shifts, list):
                    raise DataError('iiko не вернул список кассовых смен.')
                amounts = {payment.name: payment.amount for payment in snapshot.payments}
                total_prepay, cash_prepay = cash_prepay_from_shifts(day, snapshot.revenue, amounts, shifts)
                from dataclasses import replace
                return replace(snapshot, cash_prepayment=cash_prepay, new_prepayment=total_prepay)
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_cashier')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None

    async def load_director_report(self, today):
        if not self.settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        start, end = completed_period(today)
        headers = {'Accept': 'application/json', 'Accept-Language': 'ru_RU',
                   'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(base_url=IIKO_ORIGIN, headers=headers,
                                        timeout=25, follow_redirects=False,
                                        transport=self.transport) as client:
                auth = await self._post(client, '/api/auth/login',
                                        dict(login=self.settings.login, password=self.settings.password))
                if not isinstance(auth.get('token'), str) or not auth['token']:
                    raise DataError('iiko не подтвердил авторизацию.')
                client.headers['Authorization'] = 'Bearer ' + auth['token']
                rows = []
                day = start
                while day <= end:
                    rows.extend(director_rows_from_olap(
                        day, await self._olap(client, day, DIRECTOR_GROUPS, DIRECTOR_FIELDS)))
                    day += timedelta(days=1)
                return build_director_snapshot(rows, self.settings.director_categories, start, end,
                                               excluded_groups=self.settings.director_excluded_groups)
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_director')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None

    async def load_founder_analytics(self, start, end, granularity, directions):
        if not self.settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        if self.settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        headers = {'Accept': 'application/json', 'Accept-Language': 'ru_RU',
                   'Content-Type': 'application/json'}
        payment_scope = [dict(field='OperationType', filterType='value_list',
                              valueList=['PAYMENT'], inclusiveList=True)]
        try:
            async with httpx.AsyncClient(base_url=IIKO_ORIGIN, headers=headers,
                                        timeout=25, follow_redirects=False,
                                        transport=self.transport) as client:
                auth = await self._post(client, '/api/auth/login',
                                        dict(login=self.settings.login, password=self.settings.password))
                if not isinstance(auth.get('token'), str) or not auth['token']:
                    raise DataError('iiko не подтвердил авторизацию.')
                client.headers['Authorization'] = 'Bearer ' + auth['token']
                payment_groups = [
                    'OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'DishName',
                    'PayTypes',
                ]
                revenue = []
                payments = []
                chunk_limit = asyncio.Semaphore(FOUNDER_OLAP_CHUNK_CONCURRENCY)

                async def load_chunk(chunk_start, chunk_end):
                    async with chunk_limit:
                        return await asyncio.gather(
                            self._olap_range(client, chunk_start, chunk_end, payment_groups,
                                             ['DishDiscountSumInt'], payment_scope),
                            self._olap_range(client, chunk_start, chunk_end, payment_groups,
                                             ['DishDiscountSumInt']),
                        )

                chunks = list(date_chunks(start, end, max_days=FOUNDER_OLAP_MAX_DAYS))
                chunk_rows = await asyncio.gather(*(
                    load_chunk(chunk_start, chunk_end)
                    for chunk_start, chunk_end in chunks
                ))
                for regular_payment_rows, banquet_payment_rows in chunk_rows:
                    regular_payments = founder_rows_from_olap(
                        regular_payment_rows, payments=True,
                        dish_filter='exclude_banquet')
                    banquet_payments = founder_rows_from_olap(
                        banquet_payment_rows, payments=True,
                        dish_filter='banquet_only')
                    payments.extend(regular_payments)
                    payments.extend(banquet_payments)
                    revenue.extend(
                        RevenueRow(row.day, row.register, row.section, row.item, row.amount)
                        for row in (*regular_payments, *banquet_payments)
                    )
                return build_analytics(revenue, payments, start, end, granularity, directions)
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_founder')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None

    async def load_sales_details(self, start, end, dimensions, *, limit):
        """Read selected sales dimensions directly from the allowlisted iiko OLAP API."""
        if not self.settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        if self.settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        if (not dimensions or len(dimensions) > 4 or len(set(dimensions)) != len(dimensions)
                or any(value not in IIKO_DETAIL_DIMENSIONS for value in dimensions)):
            raise DataError('Выберите от одного до четырёх разрешённых измерений iiko.')
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise DataError('Лимит строк iiko должен быть от 1 до 200.')
        headers = {'Accept': 'application/json', 'Accept-Language': 'ru_RU',
                   'Content-Type': 'application/json'}
        try:
            async with httpx.AsyncClient(base_url=IIKO_ORIGIN, headers=headers,
                                        timeout=25, follow_redirects=False,
                                        transport=self.transport) as client:
                auth = await self._post(client, '/api/auth/login',
                                        dict(login=self.settings.login, password=self.settings.password))
                if not isinstance(auth.get('token'), str) or not auth['token']:
                    raise DataError('iiko не подтвердил авторизацию.')
                client.headers['Authorization'] = 'Bearer ' + auth['token']
                raw = await self._olap_range(
                    client, start, end, list(dimensions), list(IIKO_DETAIL_FIELDS))
                rows, total_rows = detail_rows_from_olap(raw, dimensions, limit=limit)
                return {
                    'source': 'iiko OLAP SALES',
                    'period': {'start': start.isoformat(), 'end': end.isoformat()},
                    'dimensions': list(dimensions),
                    'metrics': list(IIKO_DETAIL_FIELDS),
                    'rows': rows,
                    'returned_rows': len(rows),
                    'total_rows': total_rows,
                    'truncated': total_rows > len(rows),
                }
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_sales_details')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None

    async def _post(self, client, path, body, pending=False):
        response = await client.post(path, json=body)
        if pending and response.status_code == 400 and 'data not found' in response.text.lower():
            return None
        if response.status_code in (401, 403):
            raise DataError('iiko отклонил доступ. Проверьте логин и права на отчёты.')
        if not 200 <= response.status_code < 300:
            raise DataError(f'iiko не выполнил запрос (HTTP {response.status_code}).')
        try:
            data = response.json()
        except ValueError:
            raise DataError('iiko вернул ответ не в формате JSON.') from None
        if not isinstance(data, dict) or data.get('error'):
            raise DataError('iiko отклонил запрос отчёта.')
        return data

    async def _olap(self, client, day, groups, fields, extra_filters=()):
        return await self._olap_range(client, day, day, groups, fields, extra_filters)

    async def _olap_range(self, client, start, end, groups, fields, extra_filters=()):
        body = olap_range_body(self.settings.store_id, start, end, groups, fields, extra_filters)
        init = await self._post(client, '/api/olap/init', body)
        fetch_id = init.get('fetchId') or init.get('data')
        if not isinstance(fetch_id, str) or not fetch_id:
            raise DataError('iiko не вернул идентификатор отчёта.')
        for attempt in range(15):
            data = await self._post(client, '/api/olap/fetch/' + quote(fetch_id, safe='') + '/grouped-table', body, True)
            if data is not None:
                result = data.get('result')
                if not isinstance(result, dict) or not isinstance(result.get('rows'), list):
                    raise DataError('iiko вернул некорректную структуру отчёта.')
                return result['rows']
            if attempt < 14:
                await asyncio.sleep(self.poll_delay)
        raise DataError('iiko ещё не подготовил отчёт. Повторите обновление через минуту.')
