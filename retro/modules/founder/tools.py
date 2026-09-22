"""Read-only, bounded data tools exposed to the founder AI chat."""

from __future__ import annotations

import asyncio
from datetime import date

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
            'направлениях Retro/школа/банкет, динамике и сверке.'
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


class FounderChatTools:
    """Execute only the explicitly allowlisted read-only founder data queries."""

    definitions = TOOL_DEFINITIONS

    def __init__(self, app):
        self.app = app

    async def execute(self, name, arguments):
        if name == 'get_revenue_analytics':
            start, end, granularity, directions = _period(arguments, with_directions=True)
            async with self.app.state.iiko_lock:
                return await asyncio.wait_for(
                    self.app.state.iiko.load_founder_analytics(
                        start, end, granularity, directions), timeout=90)
        if name == 'get_bookings':
            start, end, granularity = _period(arguments, with_directions=False)
            raw = await asyncio.wait_for(self.app.state.bookings.load(start, end), timeout=20)
            return build_booking_analytics(raw, start, end, granularity)
        if name == 'get_employee_attendance':
            return self._attendance(arguments)
        raise DataError('Чат запросил неизвестный инструмент.')

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
        return {
            'date': day.isoformat(),
            'timezone': str(TZ),
            'source': 'Hikvision ISAPI',
            'health': snapshot.health,
            'counts': counts,
            'filter': selected_status,
            'employees': [
                {
                    'name': row.name,
                    'role': row.role,
                    'group': row.group_name,
                    'status': row.status,
                    'first_entry': (
                        row.occurred_at.astimezone(TZ).isoformat(timespec='seconds')
                        if row.occurred_at else None
                    ),
                }
                for row in selected
            ],
        }
