import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from retro.modules.cashier.service import DataError, TZ
from retro.modules.founder.models import GRANULARITIES, _period_start, _periods


# Метка источника приходит из бота брони сырой — так её ставит ссылка
# `t.me/<бот>?start=<метка>` (README бота retro-bots и src/lib/botLink.ts сайта).
# Без метки бот пишет `direct`: человек сам нашёл бота в Telegram.
TELEGRAM_SOURCE_LABELS = {
    'direct': 'Telegram напрямую',
    # Кнопки сайта retro-milliy.uz
    'hero': 'Telegram с сайта · первый экран',
    'nav': 'Telegram с сайта · верхнее меню',
    'booking': 'Telegram с сайта · блок «Контакты»',
    'sets': 'Telegram с сайта · сеты',
    'banquet': 'Telegram с сайта · страница банкета',
    'site': 'Telegram с сайта · кнопка Telegram',
    # Карты и площадки отзывов
    'yandex': 'Telegram из Яндекс Карт',
    '2gis': 'Telegram из 2ГИС',
    'tripadvisor': 'Telegram из TripAdvisor',
    # Instagram и печать
    'insta_bio': 'Telegram из Instagram · шапка профиля',
    'insta_story': 'Telegram из Instagram · сторис',
    'table_card': 'Telegram · визитка на столе',
}
# Гость из Instagram Direct приходит в бота по метке ig_<что>_<язык> (ig_book_ru).
INSTAGRAM_SOURCE = re.compile(r'ig_[a-z]+_(?:uz|ru|en)')
WEBSITE_SOURCE_LABELS = {
    'website': 'Сайт · форма брони',
    'website_telegram': 'Сайт · Telegram-чат бота сайта',
}
NO_SOURCE = 'Источник не указан'
SOURCE_ORIGINS = ('telegram', 'website')


def source_label(value, label=None):
    """Понятная подпись источника заявки вместо сырой метки бота.

    При двух источниках значение приходит с приставкой (`telegram:direct`,
    `website:website`), при одном — как есть: это бот брони в Telegram.
    Незнакомая метка показывается как есть, чтобы новая ссылка не пропала."""
    origin, raw = 'telegram', value
    if isinstance(value, str):
        prefix, colon, rest = value.partition(':')
        if colon and prefix in SOURCE_ORIGINS:
            origin, raw = prefix, rest
    if raw in (None, '', 'None'):
        return NO_SOURCE
    if origin == 'website':
        return WEBSITE_SOURCE_LABELS.get(raw, 'Сайт · ' + raw)
    if raw in TELEGRAM_SOURCE_LABELS:
        return TELEGRAM_SOURCE_LABELS[raw]
    if INSTAGRAM_SOURCE.fullmatch(raw):
        return 'Instagram'
    if raw.startswith('meta_'):
        return 'Реклама Meta · ' + raw[len('meta_'):]
    if label and label != raw:
        return label
    return f'Telegram · метка «{raw}»'


def build_booking_analytics(raw, start, end, granularity, *, now=None):
    if granularity not in GRANULARITIES:
        raise DataError('Неизвестная детализация бронирований.')
    submitted = raw['submitted']
    cancelled = raw['cancelled']
    if submitted['coverage'] != cancelled['coverage'] or \
            submitted['excluded_missing_date'] != cancelled['excluded_missing_date']:
        raise DataError('API бронирований вернул несогласованное покрытие истории.')

    grouped = defaultdict(lambda: {'bookings': 0, 'guests': 0, 'cancelled': 0})
    for status, payload in raw.items():
        for row in payload['by_date']:
            day = date.fromisoformat(row['value'])
            if not start <= day <= end:
                raise DataError('API бронирований вернул данные вне выбранного периода.')
            group = grouped[_period_start(day, start, granularity)]
            if status == 'submitted':
                group['bookings'] += row['bookings']
                group['guests'] += row['guests']
            else:
                group['cancelled'] += row['bookings']

    now = now or datetime.now(TZ)
    series = []
    for period_start, period_end in _periods(start, end, granularity):
        series.append({
            'start': period_start.isoformat(),
            'end': period_end.isoformat(),
            'incomplete': period_start <= now.date() <= period_end,
            'values': dict(grouped[period_start]),
        })

    # Заявка, которую гость отменил, уходит из «отправленных» в «отменённые»:
    # всего заявок — сумма обоих статусов. Источник считаем по всем заявкам,
    # гостей — по активным: отменившие не придут.
    booking_total = submitted['totals']['bookings']
    all_total = booking_total + cancelled['totals']['bookings']
    by_source = {}
    for status, payload in (('submitted', submitted), ('cancelled', cancelled)):
        for row in payload['by_source']:
            entry = by_source.setdefault(row['value'], {
                'name': source_label(row['value'], row.get('label')),
                'value': row['value'], 'all': 0, 'bookings': 0, 'cancelled': 0, 'guests': 0})
            entry['all'] += row['bookings']
            if status == 'submitted':
                entry['bookings'] += row['bookings']
                entry['guests'] += row['guests']
            else:
                entry['cancelled'] += row['bookings']
    sources = []
    for entry in sorted(by_source.values(), key=lambda item: (-item['all'], item['name'])):
        entry['share_percent'] = None if all_total == 0 else str(
            (Decimal(entry['all']) * 100 / Decimal(all_total)).quantize(
                Decimal('.01'), rounding=ROUND_HALF_UP))
        sources.append(entry)

    coverage = {
        **submitted['coverage'],
        'excluded_missing_date': submitted['excluded_missing_date'],
    }
    return {
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'granularity': granularity,
        'date_basis': 'visit',
        'updated_at': now.isoformat(),
        # bookings — активные (неотменённые) заявки, all — все: активные + отменённые.
        'totals': {
            'bookings': booking_total,
            'guests': submitted['totals']['guests'],
            'unknown_guest_bookings': submitted['totals']['unknown_guest_bookings'],
            'cancelled': cancelled['totals']['bookings'],
            'all': all_total,
        },
        'series': series,
        'sources': sources,
        'coverage': coverage,
    }
