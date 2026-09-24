"""Read-only, bounded data tools exposed to the founder AI chat."""

from __future__ import annotations

from retro.report_cache import load_iiko

import asyncio
from datetime import date
from types import SimpleNamespace

from fastapi import HTTPException

from retro.integrations.iiko import IIKO_DETAIL_DIMENSIONS
from retro.modules.accountant.payroll import draft_payroll
from retro.modules.cashier.service import DataError, TZ, today_tashkent
from retro.modules.founder.bookings import build_booking_analytics
from retro.modules.founder.models import DIRECTIONS, GRANULARITIES


ATTENDANCE_STATUSES = {
    'all', 'arrived', 'on_time', 'late', 'missing', 'unlinked', 'unavailable',
}

TOOL_DEFINITIONS = (
    {
        'name': 'get_revenue_analytics',
        'description': (
            'Получить проверенную аналитику iiko по выручке и способам оплаты за '
            'включительный период. Используй для вопросов о выручке, оплатах, долях, '
            'направлениях Retro/школа/банкет, динамике и сверке. '
            'sales_bridge разделяет оплаты продаж и зачёт авансов; это не поступления '
            'кассовой смены и не остаток наличных. Не называй продажи суммой к передаче.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'start': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
                'end': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
                'granularity': {'type': 'string', 'enum': list(GRANULARITIES)},
                'directions': {
                    'type': 'array',
                    'items': {'type': 'string', 'enum': list(DIRECTIONS)},
                    'minItems': 1,
                    'uniqueItems': True,
                },
            },
            'required': ['start', 'end', 'granularity', 'directions'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_bookings',
        'description': (
            'Получить бронирования за включительный период: количество броней и гостей, '
            'отмены, источники и покрытие истории.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'start': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
                'end': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
                'granularity': {'type': 'string', 'enum': list(GRANULARITIES)},
            },
            'required': ['start', 'end', 'granularity'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_employee_attendance',
        'description': (
            'Получить фактический первый вход сотрудников из Hikvision за конкретный день. '
            'Поддерживает опоздавших, пришедших вовремя, всех пришедших, отсутствующих, '
            'непривязанных и сотрудников с недоступным статусом. Никогда не считай '
            'unavailable или unlinked отсутствием.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'date': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
                'status': {'type': 'string', 'enum': sorted(ATTENDANCE_STATUSES)},
            },
            'required': ['date', 'status'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_iiko_sales_details',
        'description': (
            'Напрямую запросить детальный read-only OLAP-отчёт iiko по продажам. '
            'Доступны даты, кассы, отделения, способы оплаты, блюда, группы блюд, '
            'официанты, заказы и типы операций; метрики включают количество, выручку, '
            'себестоимость и число заказов. Выбирай только нужные измерения и узкий период. '
            'При PayTypes/OperationType себестоимость недоступна из-за повторов в iiko. '
            'Число заказов и себестоимость единицы нельзя складывать. Усечённая выдача '
            'не подходит для общих итогов или полного рейтинга. Для продолжения передай '
            'next_offset как offset и revision из первой страницы; не смешивай версии отчёта.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'start': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
                'end': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
                'dimensions': {
                    'type': 'array',
                    'items': {'type': 'string', 'enum': list(IIKO_DETAIL_DIMENSIONS)},
                    'minItems': 1, 'maxItems': 4, 'uniqueItems': True,
                },
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                'offset': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
                'revision': {'type': 'string'},
            },
            'required': ['start', 'end', 'dimensions', 'limit'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_cashier_day',
        'description': (
            'Получить все доступные данные кассира за день: live-снимок iiko, способы '
            'оплаты, чеки, предоплаты, разбивку выручки, ручные расходы и поступления, '
            'курс и остаток USD.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'date': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
            },
            'required': ['date'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_accounting_day',
        'description': (
            'Получить полный read-only снимок бухгалтерии за день: сотрудники, ставки, '
            'начисления, выплаты, долги, движения денег, остатки, резервы, месячные '
            'сотрудники, кассовую передачу и посещаемость.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'date': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
            },
            'required': ['date'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'get_saved_director_reports',
        'description': (
            'Получить список сохранённых отчётов директора либо полный отчёт по его id, '
            'включая снимок iiko и ранее сформированный анализ.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'report_id': {'type': 'string', 'minLength': 1, 'maxLength': 64},
            },
            'additionalProperties': False,
        },
    },
)


def _parse_date(value, field):
    if not isinstance(value, str):
        raise DataError(f'Поле {field} должно содержать дату YYYY-MM-DD.')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DataError(f'Поле {field} должно содержать дату YYYY-MM-DD.') from None


def _period(arguments, *, with_directions):
    required = {'start', 'end', 'granularity'} | ({'directions'} if with_directions else set())
    if not isinstance(arguments, dict) or set(arguments) != required:
        raise DataError('Инструмент получил неполные параметры периода.')
    start = _parse_date(arguments['start'], 'start')
    end = _parse_date(arguments['end'], 'end')
    today = today_tashkent()
    if start > end:
        raise DataError('Дата начала должна быть не позже даты конца.')
    if end > today:
        raise DataError('Будущие даты недоступны.')
    if (end - start).days >= 366:
        raise DataError('Период не может быть длиннее 366 дней.')
    granularity = arguments['granularity']
    if granularity not in GRANULARITIES:
        raise DataError('Неизвестная детализация.')
    if granularity == 'day' and (end - start).days >= 62:
        raise DataError('Для периода длиннее 62 дней выберите детализацию week или month.')
    if not with_directions:
        return start, end, granularity
    directions = arguments['directions']
    if (not isinstance(directions, list) or not directions
            or any(not isinstance(item, str) for item in directions)
            or len(set(directions)) != len(directions)
            or any(item not in DIRECTIONS for item in directions)):
        raise DataError('Выберите направления retro, school или banquet без повторов.')
    return start, end, granularity, tuple(directions)


def _single_day(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {'date'}:
        raise DataError('Инструмент получил неполные параметры даты.')
    day = _parse_date(arguments['date'], 'date')
    if day > today_tashkent():
        raise DataError('Будущие даты недоступны.')
    return day


def _tool_request(app):
    return SimpleNamespace(app=app, state=SimpleNamespace(request_id='founder-chat-tool'))


class FounderChatTools:
    """Execute only the explicitly allowlisted read-only founder data queries."""

    definitions = TOOL_DEFINITIONS

    def __init__(self, app):
        self.app = app

    async def execute(self, name, arguments):
        if name == 'get_revenue_analytics':
            start, end, granularity, directions = _period(arguments, with_directions=True)
            return await load_iiko(self.app.state, 'load_founder_analytics',
                                   start, end, granularity, directions, timeout=180)
        if name == 'get_bookings':
            start, end, granularity = _period(arguments, with_directions=False)
            raw = await asyncio.wait_for(self.app.state.bookings.load(start, end), timeout=20)
            return build_booking_analytics(raw, start, end, granularity)
        if name == 'get_employee_attendance':
            return await asyncio.to_thread(self._attendance, arguments)
        if name == 'get_iiko_sales_details':
            if (not isinstance(arguments, dict)
                    or not {'start', 'end', 'dimensions', 'limit'} <= set(arguments)
                    or set(arguments) - {'start', 'end', 'dimensions', 'limit', 'offset', 'revision'}):
                raise DataError('Инструмент iiko получил неполные параметры.')
            start = _parse_date(arguments['start'], 'start')
            end = _parse_date(arguments['end'], 'end')
            if start > end:
                raise DataError('Дата начала должна быть не позже даты конца.')
            if end > today_tashkent():
                raise DataError('Будущие даты недоступны.')
            if (end - start).days >= 31:
                raise DataError('Детальный отчёт iiko доступен максимум за 31 день.')
            dimensions = arguments['dimensions']
            limit = arguments['limit']
            if (not isinstance(dimensions, list) or not dimensions
                    or len(dimensions) > 4 or len(set(dimensions)) != len(dimensions)
                    or any(not isinstance(item, str) or item not in IIKO_DETAIL_DIMENSIONS
                           for item in dimensions)):
                raise DataError('Выберите от одного до четырёх разрешённых измерений iiko без повторов.')
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
                raise DataError('Лимит строк iiko должен быть от 1 до 200.')
            return await load_iiko(self.app.state, 'load_sales_details',
                                   start, end, tuple(dimensions), limit=limit,
                                   **{key: arguments[key] for key in ('offset', 'revision') if key in arguments})
        if name == 'get_cashier_day':
            return await self._cashier_day(arguments)
        if name == 'get_accounting_day':
            return await self._accounting_day(arguments)
        if name == 'get_saved_director_reports':
            return await asyncio.to_thread(self._director_reports, arguments)
        raise DataError('Чат запросил неизвестный инструмент.')

    async def _cashier_day(self, arguments):
        day = _single_day(arguments)
        from retro.modules.cashier.routes import day_report
        try:
            result = await day_report(_tool_request(self.app), day, False)
        except HTTPException as error:
            raise DataError(str(error.detail)) from None
        try:
            usd_rate = (await self.app.state.usd_rates.get(day)).json()
        except DataError as error:
            usd_rate = {'error': str(error)}
        expenses = await asyncio.to_thread(self.app.state.expenses.list, day)
        receipts = await asyncio.to_thread(self.app.state.expenses.list_receipts, day)
        return {
            **result,
            'expenses': [item.json() for item in expenses],
            'expense_total': str(sum((item.amount for item in expenses), 0)),
            'receipts': [item.json() for item in receipts],
            'receipt_total': str(sum((item.amount for item in receipts), 0)),
            'usd_rate': usd_rate,
            'usd_balance': await asyncio.to_thread(self.app.state.usd_rates.balance, day),
        }

    async def _accounting_day(self, arguments):
        day = _single_day(arguments)
        from retro.modules.accountant.routes import day_view
        try:
            return await day_view(_tool_request(self.app), day)
        except HTTPException as error:
            raise DataError(str(error.detail)) from None

    def _director_reports(self, arguments):
        if not isinstance(arguments, dict) or not set(arguments) <= {'report_id'}:
            raise DataError('Инструмент отчётов директора получил неверные параметры.')
        report_id = arguments.get('report_id')
        if report_id is None:
            return {'reports': self.app.state.director_store.list_metadata()}
        if not isinstance(report_id, str) or not report_id or len(report_id) > 64:
            raise DataError('Укажите корректный id отчёта директора.')
        report = self.app.state.director_store.get(report_id)
        if report is None:
            raise DataError('Отчёт директора не найден.')
        return report

    def _attendance(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) != {'date', 'status'}:
            raise DataError('Инструмент посещаемости получил неполные параметры.')
        day = _parse_date(arguments['date'], 'date')
        today = today_tashkent()
        if day > today:
            raise DataError('Будущая посещаемость недоступна.')
        if (today - day).days > 366:
            raise DataError('Посещаемость доступна максимум за последние 366 дней.')
        selected_status = arguments['status']
        if not isinstance(selected_status, str) or selected_status not in ATTENDANCE_STATUSES:
            raise DataError('Неизвестный статус посещаемости.')

        roster = self.app.state.accountant_roster.list()
        snapshot = self.app.state.attendance.snapshot(day, roster)
        rows = draft_payroll(day, roster, set(), snapshot.rows)
        counts = {
            status: sum(row.status == status for row in rows)
            for status in ('on_time', 'late', 'missing', 'unlinked', 'unavailable')
        }
        counts['arrived'] = counts['on_time'] + counts['late']
        counts['roster'] = len(rows)
        if selected_status == 'all':
            selected = rows
        elif selected_status == 'arrived':
            selected = [row for row in rows if row.status in ('on_time', 'late')]
        else:
            selected = [row for row in rows if row.status == selected_status]
        by_id = {employee.id: employee for employee in roster}
        return {
            'date': day.isoformat(),
            'timezone': str(TZ),
            'source': 'Hikvision ISAPI',
            'health': snapshot.health,
            'counts': counts,
            'filter': selected_status,
            'employees': [{
                **row.json(),
                'first_entry': (
                    row.occurred_at.astimezone(TZ).isoformat(timespec='seconds')
                    if row.occurred_at else None
                ),
                'hikvision_registered': by_id[row.employee_id].hikvision_id is not None,
            } for row in selected],
        }
