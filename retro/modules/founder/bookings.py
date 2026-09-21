from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from retro.modules.cashier.service import DataError, TZ
from retro.modules.founder.models import GRANULARITIES, _period_start, _periods


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

    booking_total = submitted['totals']['bookings']
    sources = []
    for row in sorted(submitted['by_source'], key=lambda item: (-item['bookings'], item['label'])):
        share = None if booking_total == 0 else str(
            (Decimal(row['bookings']) * 100 / Decimal(booking_total)).quantize(
                Decimal('.01'), rounding=ROUND_HALF_UP))
        sources.append({
            'name': row['label'],
            'bookings': row['bookings'],
            'guests': row['guests'],
            'share_percent': share,
        })

    coverage = {
        **submitted['coverage'],
        'excluded_missing_date': submitted['excluded_missing_date'],
    }
    return {
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'granularity': granularity,
        'date_basis': 'visit',
        'updated_at': now.isoformat(),
        'totals': {
            'bookings': booking_total,
            'guests': submitted['totals']['guests'],
            'unknown_guest_bookings': submitted['totals']['unknown_guest_bookings'],
            'cancelled': cancelled['totals']['bookings'],
        },
        'series': series,
        'sources': sources,
        'coverage': coverage,
    }
