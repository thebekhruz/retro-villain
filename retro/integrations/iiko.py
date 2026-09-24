import asyncio
import json
import hashlib
import logging
from contextlib import asynccontextmanager
from time import monotonic
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import quote

import httpx

from retro.async_utils import gather_reads

from retro.report_cache import ReportCache, refresh_source
from retro.config import IIKO_ORIGIN
from retro.logging_config import log_upstream_failure
from retro.modules.cashier.service import (
    BANQUET_SECTION, RETRO_REGISTER, DataError, build_revenue_breakdown, build_snapshot, cell, number,
)
from retro.modules.director.models import (
    SalesRow, build_snapshot as build_director_snapshot, completed_period, payment_total,
)
from retro.modules.founder.models import (
    PaymentRow, RevenueRow, build_analytics, is_banquet_item,
)


DIRECTOR_GROUPS = ['CashRegisterName', 'RestaurantSection', 'PayTypes', 'DishName',
                   'DishGroup', 'WaiterName', 'UniqOrderId.Id']
DIRECTOR_COST_GROUPS = [field for field in DIRECTOR_GROUPS if field != 'PayTypes']
DIRECTOR_DETAIL_GROUPS = DIRECTOR_GROUPS + ['NonCashPaymentType']
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


def director_rows_from_olap(day, rows, *, split_payments=True, payment_details=False):
    """Flatten iiko's nested grouped-table response into safe typed sale rows."""
    result = []
    groups = DIRECTOR_GROUPS if split_payments else DIRECTOR_COST_GROUPS
    if payment_details:
        groups = DIRECTOR_DETAIL_GROUPS
    if day is None:
        groups = ['OpenDate.Typed', *groups]

    def visit(row, inherited):
        if not isinstance(row, dict):
            raise DataError('iiko вернул некорректную строку отчёта директора.')
        values = list(inherited)
        while len(values) < len(groups):
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
        if len(values) != len(groups):
            raise DataError('iiko не вернул все измерения продажи.')
        quantity, revenue, total_cost = (
            number(cell(row, index)) for index in range(len(groups), len(groups) + 3))
        row_day = day
        if row_day is None:
            try:
                row_day = date.fromisoformat(values.pop(0))
            except (ValueError, TypeError):
                raise DataError('iiko вернул некорректную дату продажи.') from None
        if not split_payments:
            values.insert(2, '')
        purpose = values.pop() if payment_details else ''
        register, section, payment_type, item, category, waiter, order_id = values
        # У части продаж группа блюда в iiko пустая. Без имени такую строку
        # нельзя ни отнести к типу отчёта, ни исключить — отчёт падал целиком
        # из-за девяти тысяч сум. Даём ей имя, и дальше она настраивается как
        # любая другая группа.
        if not isinstance(category, str) or not category.strip():
            category = 'Без группы'
        result.append(SalesRow(row_day, register, section, payment_type, item, category,
                               quantity, revenue, total_cost, waiter, order_id, purpose or ''))

    for row in rows:
        visit(row, [])
    return result


def reconcile_director_costs(payment_rows, cost_rows):
    """Allocate unsplit iiko costs; PayTypes repeats cost for mixed payments.

    Quantity is apportioned by iiko between payment types (to 3 decimals).
    Normalize those weights, including zero-revenue dishes, and keep the
    remainder on the largest share so the authoritative cost is conserved.
    Never deduplicate by amount: equal costs can belong to different orders.
    """
    def key(row):
        return (row.day, row.register, row.section, row.item, row.category,
                row.waiter, row.order_id)

    mismatch = 'Детализация оплат и себестоимости iiko не совпала. Обновите данные.'
    grouped = defaultdict(list)
    for row in payment_rows:
        grouped[key(row)].append(row)
    costs = {}
    for row in cost_rows:
        row_key = key(row)
        if row_key in costs:
            raise DataError(mismatch)
        costs[row_key] = row
    if grouped.keys() != costs.keys():
        raise DataError(mismatch)
    result = []
    for row_key, parts in grouped.items():
        source = costs[row_key]
        quantity = sum((row.quantity for row in parts), Decimal(0))
        revenue = sum((row.revenue for row in parts), Decimal(0))
        if (source.quantity * source.cost < 0
                or any(row.quantity * source.quantity < 0 for row in parts)
                or abs(quantity - source.quantity) > Decimal('.001') * len(parts)
                or abs(revenue - source.revenue) > Decimal('.01') * len(parts)
                or (not quantity and source.cost)):
            raise DataError(mismatch)
        # Largest share receives the remainder, including Decimal division dust.
        parts = sorted(parts, key=lambda row: abs(row.quantity))
        remaining = source.cost
        for index, row in enumerate(parts):
            if index == len(parts) - 1:
                cost = remaining
            else:
                cost = source.cost * row.quantity / quantity if quantity else Decimal(0)
                remaining -= cost
            result.append(replace(
                row, cost=cost,
                quantity=row.quantity + (source.quantity - quantity if index == len(parts) - 1 else 0),
                revenue=row.revenue + (source.revenue - revenue if index == len(parts) - 1 else 0)))
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


def detail_rows_from_olap(rows, dimensions, *, limit, offset=0):
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
        if total_rows <= offset or len(records) >= limit:
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
        self._http = None
        self._auth_lock = asyncio.Lock()
        self._auth_until = 0
        self._olap_cache = ReportCache(concurrency=4, limit=16, max_weight=12_000_000,
            weigh=lambda rows: len(json.dumps(rows, ensure_ascii=False).encode()))

    @asynccontextmanager
    async def _client(self):
        if self.settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=IIKO_ORIGIN,
                headers={'Accept': 'application/json', 'Accept-Language': 'ru_RU',
                         'Content-Type': 'application/json'},
                timeout=25, follow_redirects=False, transport=self.transport,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))
        await self._authorize(self._http)
        yield self._http

    async def _authorize(self, client, rejected_token=None):
        async with self._auth_lock:
            current = client.headers.get('Authorization')
            if current and ((rejected_token is None and monotonic() < self._auth_until)
                            or (rejected_token is not None and current != rejected_token)):
                return
            auth = await self._post(client, '/api/auth/login',
                dict(login=self.settings.login, password=self.settings.password))
            if not isinstance(auth.get('token'), str) or not auth['token']:
                raise DataError('iiko не подтвердил авторизацию.')
            client.headers['Authorization'] = 'Bearer ' + auth['token']
            self._auth_until = monotonic() + 15 * 60

    async def close(self):
        await self._olap_cache.close()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def load(self, day):
        if not self.settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        if self.settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        fresh_token = refresh_source.set(True)
        try:
            async with self._client() as client:
                scope = [
                    dict(field='CashRegisterName', filterType='value_list',
                         valueList=[RETRO_REGISTER], inclusiveList=True),
                    dict(field='RestaurantSection', filterType='value_list',
                         valueList=[BANQUET_SECTION], inclusiveList=False),
                    dict(field='OperationType', filterType='value_list',
                         valueList=['PAYMENT'], inclusiveList=True),
                ]
                breakdown_rows, total, payments, shift_payments, shifts_data = await gather_reads(
                    self._olap(client, day, ['CashRegisterName', 'RestaurantSection'],
                               ['DishDiscountSumInt']),
                    self._olap(client, day, ['OpenDate.Typed'],
                               ['UniqOrderId.OrdersCount', 'DishDiscountSumInt'], scope),
                    self._olap(client, day, ['PayTypes'], ['DishDiscountSumInt'], scope),
                    # A shift covers the entire register, including the banquet
                    # section. Subtract an equally scoped PAYMENT report, never
                    # the narrower cashier sales card.
                    self._olap(client, day, ['PayTypes'], ['DishDiscountSumInt'], [scope[0], scope[2]]),
                    self._post(client, '/api/cash/shift/list_period',
                               {'dateFrom': day.isoformat(), 'dateTo': day.isoformat()}),
                )
                breakdown = build_revenue_breakdown(breakdown_rows)
                snapshot = build_snapshot(day, total, payments, revenue_breakdown=breakdown)
                shifts = shifts_data.get('shifts')
                if not isinstance(shifts, list):
                    raise DataError('iiko не вернул список кассовых смен.')
                amounts = defaultdict(Decimal)
                for row in shift_payments:
                    amounts[cell(row, 0)] += number(cell(row, 1))
                total_prepay, cash_prepay = cash_prepay_from_shifts(
                    day, sum(amounts.values(), Decimal(0)), amounts, shifts)
                from dataclasses import replace
                register_sales = sum(amounts.values(), Decimal(0))
                return replace(snapshot, cash_prepayment=cash_prepay, new_prepayment=total_prepay,
                               register_payment_sales=register_sales,
                               register_received_total=register_sales + total_prepay)
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_cashier')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None
        finally:
            refresh_source.reset(fresh_token)

    async def load_director_report(self, today):
        if not self.settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        start, end = completed_period(today)
        try:
            async with self._client() as client:
                payment_groups = [
                    'OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'DishName',
                    'PayTypes',
                ]
                payment_scope = [dict(field='OperationType', filterType='value_list',
                                      valueList=['PAYMENT'], inclusiveList=True)]
                payment_rows, cost_rows, regular_rows, banquet_rows = await gather_reads(
                    self._olap_range(client, start, end, ['OpenDate.Typed', *DIRECTOR_DETAIL_GROUPS],
                                     DIRECTOR_FIELDS),
                    self._olap_range(client, start, end, ['OpenDate.Typed', *DIRECTOR_COST_GROUPS],
                                     DIRECTOR_FIELDS),
                    self._olap_range(client, start, end, payment_groups,
                                     ['DishDiscountSumInt'], payment_scope),
                    self._olap_range(client, start, end, payment_groups,
                                     ['DishDiscountSumInt']),
                )
                rows = reconcile_director_costs(
                    director_rows_from_olap(None, payment_rows, payment_details=True),
                    director_rows_from_olap(None, cost_rows, split_payments=False))
                payments = founder_rows_from_olap(
                    regular_rows, payments=True, dish_filter='exclude_banquet')
                payments.extend(founder_rows_from_olap(
                    banquet_rows, payments=True, dish_filter='banquet_only'))
                yandex_revenue = payment_total(payments, 'Яндекс Еда')
                return build_director_snapshot(rows, self.settings.director_categories, start, end,
                                               excluded_groups=self.settings.director_excluded_groups,
                                               yandex_revenue=yandex_revenue)
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_director')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None

    async def load_founder_analytics(self, start, end, granularity, directions):
        if not self.settings.configured:
            raise DataError('Подключение iiko ещё не настроено. Нужен файл build/.env.')
        if self.settings.base_url != IIKO_ORIGIN:
            raise DataError('Разрешён только сервер Retro Milliy.')
        payment_scope = [dict(field='OperationType', filterType='value_list',
                              valueList=['PAYMENT'], inclusiveList=True)]
        try:
            async with self._client() as client:
                payment_groups = [
                    'OpenDate.Typed', 'CashRegisterName', 'RestaurantSection', 'DishName',
                    'PayTypes',
                ]
                revenue_groups = payment_groups[:-1]
                revenue = []
                full_revenue = []
                payments = []
                chunk_limit = asyncio.Semaphore(FOUNDER_OLAP_CHUNK_CONCURRENCY)

                async def load_chunk(chunk_start, chunk_end):
                    async with chunk_limit:
                        return await gather_reads(
                            self._olap_range(client, chunk_start, chunk_end, payment_groups,
                                             ['DishDiscountSumInt'], payment_scope),
                            self._olap_range(client, chunk_start, chunk_end, payment_groups,
                                             ['DishDiscountSumInt']),
                            self._olap_range(client, chunk_start, chunk_end, revenue_groups,
                                             ['DishDiscountSumInt'], payment_scope),
                            self._olap_range(client, chunk_start, chunk_end, revenue_groups,
                                             ['DishDiscountSumInt']),
                        )

                chunks = list(date_chunks(start, end, max_days=FOUNDER_OLAP_MAX_DAYS))
                chunk_rows = await gather_reads(*(
                    load_chunk(chunk_start, chunk_end)
                    for chunk_start, chunk_end in chunks
                ))
                for regular_payment_rows, banquet_payment_rows, regular_revenue, banquet_revenue in chunk_rows:
                    regular_payments = founder_rows_from_olap(
                        regular_payment_rows, payments=True,
                        dish_filter='exclude_banquet')
                    banquet_payments = founder_rows_from_olap(
                        banquet_payment_rows, payments=True,
                        dish_filter='banquet_only')
                    payments.extend(regular_payments)
                    payments.extend(banquet_payments)
                    full_revenue.extend(founder_rows_from_olap(banquet_revenue))
                    revenue.extend(founder_rows_from_olap(regular_revenue, dish_filter='exclude_banquet'))
                    revenue.extend(founder_rows_from_olap(banquet_revenue, dish_filter='banquet_only'))
                result = build_analytics(revenue, payments, start, end, granularity, directions)
                from retro.modules.founder.models import classify_direction
                sales = {name: Decimal(0) for name in ('retro', 'school', 'banquet')}
                excluded = Decimal(0)
                for row in full_revenue:
                    group = classify_direction(row.register, row.section, row.item)
                    if group is None:
                        excluded += row.amount
                    else:
                        sales[group] += row.amount
                result['sales_totals'] = {key: str(value) for key, value in sales.items()}
                result['scope_excluded_revenue'] = str(excluded)
                result['calculation_version'] = '2026-09-24'
                result['scope_note'] = (
                    'График: обычные заказы — операция «Оплата», без зачтённых авансов; '
                    'банкет — все операции блюд с меткой «БЕХРУЗ». '
                    'Это согласованная выборка, а не все продажи или поступления денег. '
                    'Сверка — с отдельным отчётом без разбивки по типам оплаты.')
                return result
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_founder')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None

    async def load_sales_details(self, start, end, dimensions, *, limit, offset=0, revision=None):
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
        if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= 100000:
            raise DataError('Некорректное смещение страницы iiko.')
        if offset and not revision:
            raise DataError('Для следующей страницы укажите revision первой страницы.')
        try:
            async with self._client() as client:
                raw = await self._olap_range(
                    client, start, end, list(dimensions), list(IIKO_DETAIL_FIELDS))
                report_revision = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
                if revision is not None and revision != report_revision:
                    raise DataError('Отчёт iiko изменился между страницами. Начните чтение заново.')
                rows, total_rows = detail_rows_from_olap(raw, dimensions, limit=limit, offset=offset)
                unsafe_cost = 'PayTypes' in dimensions or 'OperationType' in dimensions
                if unsafe_cost:
                    for row in rows:
                        row['product_cost_total'] = None
                return {
                    'source': 'iiko OLAP SALES',
                    'period': {'start': start.isoformat(), 'end': end.isoformat()},
                    'dimensions': list(dimensions),
                    'metrics': list(IIKO_DETAIL_FIELDS),
                    'rows': rows,
                    'returned_rows': len(rows),
                    'total_rows': total_rows,
                    'truncated': offset > 0 or total_rows > len(rows),
                    'offset': offset, 'revision': report_revision,
                    'next_offset': offset + len(rows) if offset + len(rows) < total_rows else None,
                    'cost_additive': not unsafe_cost,
                    'warnings': ([
                        'Себестоимость скрыта: iiko повторяет её при разделении по оплатам/операциям. '
                        'Запросите себестоимость без этих измерений.'
                    ] if unsafe_cost else []) + ([
                        'Выдача усечена; нельзя вычислять итоги и полный рейтинг по этим строкам.'
                    ] if offset > 0 or total_rows > len(rows) else []),
                    'scope': 'Все продажи источника; фильтры направлений и исключения меню не применены.',
                    'non_additive_metrics': ['product_cost_per_unit', 'orders'],
                }
        except (httpx.HTTPError, TimeoutError) as error:
            log_upstream_failure('iiko', error, operation='load_sales_details')
            raise DataError('Не удалось связаться с iiko. Попробуйте обновить данные позже.') from None

    async def _post(self, client, path, body, pending=False):
        sent_token = client.headers.get('Authorization')
        response = await client.post(path, json=body)
        if response.status_code in (401, 403) and path != '/api/auth/login':
            await self._authorize(client, rejected_token=sent_token)
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
        key = json.dumps(body, sort_keys=True, ensure_ascii=False)
        return await self._olap_cache.get(
            key, lambda: self._fetch_olap(client, body), ttl=60, timeout=90,
            refresh=refresh_source.get(), label="iiko_olap")

    async def _fetch_olap(self, client, body):
        started = monotonic()
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
                logging.getLogger('retro.performance').info(
                    'operation=iiko_fetch attempts=%d duration_ms=%d',
                    attempt + 1, (monotonic() - started) * 1000)
                return result['rows']
            if attempt < 14:
                await asyncio.sleep(self.poll_delay)
        raise DataError('iiko ещё не подготовил отчёт. Повторите обновление через минуту.')
