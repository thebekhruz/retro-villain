"""Least-privilege, read-only tools for the director AI chat."""

from retro.report_cache import load_iiko

import asyncio
from datetime import date

from retro.modules.accountant.payroll import draft_payroll
from retro.modules.cashier.service import DataError, TZ, today_tashkent
from retro.modules.founder.tools import FounderChatTools


DELEGATED_NAMES = {'get_iiko_sales_details', 'get_saved_director_reports'}
TOOL_DEFINITIONS = (
    {
        'name': 'get_director_dashboard',
        'description': (
            'Получить тот же проверенный десятидневный снимок iiko, который показан в '
            'модуле директора: выручка, блюда, маржа и официанты.'
        ),
        'input_schema': {
            'type': 'object',
            'properties': {
                'as_of': {'type': 'string', 'format': 'date',
                          'description': 'Дата YYYY-MM-DD, относительно которой строится период.'},
            },
            'required': ['as_of'],
            'additionalProperties': False,
        },
    },
    next(item for item in FounderChatTools.definitions if item['name'] == 'get_iiko_sales_details'),
    next(item for item in FounderChatTools.definitions if item['name'] == 'get_employee_attendance'),
    next(item for item in FounderChatTools.definitions if item['name'] == 'get_saved_director_reports'),
)


class DirectorChatTools:
    definitions = TOOL_DEFINITIONS

    def __init__(self, app):
        self.app = app
        self._delegate = FounderChatTools(app)

    async def execute(self, name, arguments):
        if name == 'get_director_dashboard':
            if not isinstance(arguments, dict) or set(arguments) != {'as_of'}:
                raise DataError('Инструмент директора получил неполные параметры.')
            try:
                as_of = date.fromisoformat(arguments['as_of'])
            except (TypeError, ValueError):
                raise DataError('Поле as_of должно содержать дату YYYY-MM-DD.') from None
            if as_of > today_tashkent():
                raise DataError('Будущая дата недоступна.')
            snapshot = await load_iiko(self.app.state, 'load_director_report', as_of, timeout=150)
            return snapshot.json()
        if name == 'get_employee_attendance':
            return await asyncio.to_thread(self._attendance, arguments)
        if name in DELEGATED_NAMES:
            return await self._delegate.execute(name, arguments)
        raise DataError('Чат директора запросил неизвестный инструмент.')

    def _attendance(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) != {'date', 'status'}:
            raise DataError('Инструмент посещаемости получил неполные параметры.')
        try:
            day = date.fromisoformat(arguments['date'])
        except (TypeError, ValueError):
            raise DataError('Поле date должно содержать дату YYYY-MM-DD.') from None
        today = today_tashkent()
        if day > today or (today - day).days > 366:
            raise DataError('Посещаемость доступна за сегодняшний и последние 366 дней.')
        status = arguments['status']
        allowed = {'all', 'arrived', 'on_time', 'late', 'missing', 'unlinked', 'unavailable'}
        if status not in allowed:
            raise DataError('Неизвестный статус посещаемости.')
        roster = self.app.state.accountant_roster.list()
        snapshot = self.app.state.attendance.snapshot(day, roster)
        rows = draft_payroll(day, roster, set(), snapshot.rows)
        selected = rows if status == 'all' else [
            row for row in rows
            if row.status == status or (status == 'arrived' and row.status in {'on_time', 'late'})
        ]
        counts = {value: sum(row.status == value for row in rows)
                  for value in ('on_time', 'late', 'missing', 'unlinked', 'unavailable')}
        counts['arrived'] = counts['on_time'] + counts['late']
        counts['roster'] = len(rows)
        return {
            'date': day.isoformat(), 'timezone': str(TZ), 'source': 'Hikvision ISAPI',
            'health': snapshot.health, 'counts': counts, 'filter': status,
            'employees': [
                {'employee_id': row.employee_id, 'name': row.name, 'role': row.role,
                 'group': row.group_name, 'status': row.status,
                 'first_entry': row.occurred_at.astimezone(TZ).isoformat(timespec='seconds')
                 if row.occurred_at else None}
                for row in selected
            ],
        }
