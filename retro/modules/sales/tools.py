"""Инструмент AI «продажи по времени»: обед сегодня, любой прошлый период, топ блюд."""
from datetime import date

from retro.modules.cashier.service import DataError

from .service import DIRECTIONS, TOP_LIMIT, make_window

SALES_BY_TIME_TOOL = {
    'name': 'get_sales_by_time',
    'description': (
        'Продажи из истории чеков iiko в базе панели за любой прошедший период и часть дня: '
        'выручка, чеки, средний чек, разбивка по часам, дням и направлениям, самые продаваемые '
        'позиции с количеством и выручкой (по умолчанию только готовые блюда и напитки). '
        'Обед — window="lunch": окно задано настройкой, оно в ответе (window.label). Другие часы — '
        'from_time/to_time «ЧЧ:ММ» по Ташкенту, по времени закрытия чека, правая граница не входит. '
        'Сегодня в истории только закрытые на сейчас чеки: partial=true, время данных — updated_at. '
        'Дни без данных перечислены в missing_days: нулём их не считай. В ответе обязательно назови '
        'окно часов и время последнего обновления.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'start': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
            'end': {'type': 'string', 'format': 'date', 'description': 'Дата YYYY-MM-DD.'},
            'window': {'type': 'string', 'enum': ['lunch']},
            'from_time': {'type': 'string', 'pattern': r'^\d{1,2}(:\d{2})?$'},
            'to_time': {'type': 'string', 'pattern': r'^\d{1,2}(:\d{2})?$'},
            'direction': {'type': 'string', 'enum': ['all', *DIRECTIONS]},
            'top': {'type': 'integer', 'minimum': 0, 'maximum': TOP_LIMIT},
            'sort': {'type': 'string', 'enum': ['quantity', 'revenue']},
            'include_non_dishes': {'type': 'boolean'},
        },
        'required': ['start', 'end'],
        'additionalProperties': False,
    },
}
ARGUMENTS = frozenset(SALES_BY_TIME_TOOL['input_schema']['properties'])


def parse_day(value, field):
    if not isinstance(value, str):
        raise DataError(f'Поле {field} должно содержать дату YYYY-MM-DD.')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DataError(f'Поле {field} должно содержать дату YYYY-MM-DD.') from None


async def sales_by_time(service, arguments):
    if (not isinstance(arguments, dict) or not {'start', 'end'} <= set(arguments)
            or set(arguments) - ARGUMENTS):
        raise DataError('Инструмент продаж получил неполные параметры.')
    start, end = parse_day(arguments['start'], 'start'), parse_day(arguments['end'], 'end')
    for key in ('from_time', 'to_time'):
        if key in arguments and not isinstance(arguments[key], str):
            raise DataError('Время задаётся строкой ЧЧ:ММ.')
    if arguments.get('window') not in (None, 'lunch'):
        raise DataError('Окно — только lunch или часы from_time/to_time.')
    top = arguments.get('top', 10)
    include = arguments.get('include_non_dishes', False)
    if not isinstance(include, bool):
        raise DataError('include_non_dishes — true или false.')
    try:
        window = make_window(arguments.get('from_time'), arguments.get('to_time'),
                             lunch=service.lunch if arguments.get('window') == 'lunch' else None)
    except ValueError as error:
        raise DataError(str(error)) from None
    return await service.summary(start, end, window=window, direction=arguments.get('direction', 'all'),
                                 top=top, sort=arguments.get('sort', 'quantity'), include_non_dishes=include)
