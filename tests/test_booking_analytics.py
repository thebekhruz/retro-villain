from datetime import date, datetime

from retro.modules.cashier.service import TZ
from retro.modules.founder.bookings import build_booking_analytics


def counts(bookings, guests, unknown=0):
    return {
        'bookings': bookings,
        'guests': guests,
        'unknown_guest_bookings': unknown,
    }


def test_booking_analytics_groups_submitted_guests_and_cancellations_by_week():
    coverage = {
        'history_started_at': '2026-09-01T08:00:00.000Z',
        'historical_data_complete': False,
    }
    raw = {
        'submitted': {
            'coverage': coverage,
            'excluded_missing_date': 2,
            'totals': counts(3, 7, 1),
            'by_date': [
                {'value': '2026-09-01', **counts(2, 5, 1)},
                {'value': '2026-09-07', **counts(1, 2)},
            ],
            'by_source': [
                {'value': 'direct', 'label': 'direct', **counts(2, 5, 1)},
                {'value': None, 'label': 'Источник не указан', **counts(1, 2)},
            ],
        },
        'cancelled': {
            'coverage': coverage,
            'excluded_missing_date': 2,
            'totals': counts(1, 3),
            'by_date': [{'value': '2026-09-02', **counts(1, 3)}],
            'by_source': [{'value': 'direct', 'label': 'direct', **counts(1, 3)}],
        },
    }

    result = build_booking_analytics(
        raw, date(2026, 9, 1), date(2026, 9, 8), 'week',
        now=datetime(2026, 9, 8, 12, tzinfo=TZ),
    )

    assert result['totals'] == {
        'bookings': 3,
        'guests': 7,
        'unknown_guest_bookings': 1,
        'cancelled': 1,
    }
    assert result['series'] == [
        {
            'start': '2026-09-01', 'end': '2026-09-06', 'incomplete': False,
            'values': {'bookings': 2, 'guests': 5, 'cancelled': 1},
        },
        {
            'start': '2026-09-07', 'end': '2026-09-08', 'incomplete': True,
            'values': {'bookings': 1, 'guests': 2, 'cancelled': 0},
        },
    ]
    assert result['sources'] == [
        {'name': 'direct', 'bookings': 2, 'guests': 5, 'share_percent': '66.67'},
        {'name': 'Источник не указан', 'bookings': 1, 'guests': 2, 'share_percent': '33.33'},
    ]
    assert result['coverage'] == {
        **coverage,
        'excluded_missing_date': 2,
    }
