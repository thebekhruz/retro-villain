from datetime import date, datetime

from retro.modules.cashier.service import TZ
from retro.modules.founder.bookings import build_booking_analytics, source_label


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

    # bookings — активные заявки, all — все: активные и отменённые (ТЗ 16.10, п. 5.3).
    assert result['totals'] == {
        'bookings': 3,
        'guests': 7,
        'unknown_guest_bookings': 1,
        'cancelled': 1,
        'all': 4,
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
    # Источник считается по всем заявкам, гости — по активным; доля — от всех заявок.
    assert result['sources'] == [
        {'name': 'Telegram напрямую', 'value': 'direct', 'all': 3, 'bookings': 2, 'cancelled': 1,
         'guests': 5, 'share_percent': '75.00'},
        {'name': 'Источник не указан', 'value': None, 'all': 1, 'bookings': 1, 'cancelled': 0,
         'guests': 2, 'share_percent': '25.00'},
    ]
    assert result['coverage'] == {
        **coverage,
        'excluded_missing_date': 2,
    }


def test_raw_bot_labels_get_readable_names():
    # Метки ссылок `t.me/<бот>?start=<метка>` — README бота и кнопки сайта.
    assert source_label('direct') == 'Telegram напрямую'
    assert source_label('hero') == 'Telegram с сайта · первый экран'
    assert source_label('telegram:nav') == 'Telegram с сайта · верхнее меню'
    assert source_label('telegram:site') == 'Telegram с сайта · кнопка Telegram'
    for language in ('ru', 'uz', 'en'):
        assert source_label('ig_book_' + language) == 'Instagram'
    assert source_label('yandex') == 'Telegram из Яндекс Карт'
    assert source_label('meta_grill4') == 'Реклама Meta · grill4'
    assert source_label('website:website') == 'Сайт · форма брони'
    assert source_label('website:website_telegram') == 'Сайт · Telegram-чат бота сайта'
    assert source_label(None, 'Источник не указан') == 'Источник не указан'
    assert source_label('telegram:None') == 'Источник не указан'
    # Новая метка не пропадает: видна как есть.
    assert source_label('flyer_oct') == 'Telegram · метка «flyer_oct»'
