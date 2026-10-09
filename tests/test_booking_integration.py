import asyncio
from datetime import date

import httpx
import pytest

from retro.config import Settings
from retro.integrations.bookings import BookingAnalyticsClient
from retro.modules.cashier.service import DataError
from retro.modules.founder.bookings import build_booking_analytics


def summary(status):
    cancelled = status == 'cancelled'
    totals = {'bookings': 1 if cancelled else 2, 'guests': 3 if cancelled else 5,
              'unknown_guest_bookings': 0 if cancelled else 1}
    row = {'value': '2026-09-19', **totals}
    return {
        'date_basis': 'visit',
        'timezone': 'Asia/Tashkent',
        'from': '2026-09-19',
        'to': '2026-09-20',
        'coverage': {
            'history_started_at': '2026-09-19T08:00:00.000Z',
            'historical_data_complete': False,
        },
        'excluded_missing_date': 0,
        'totals': totals,
        'by_date': [row],
        'by_status': [{'value': status, **totals}],
        'by_source': [{'value': 'direct', 'label': 'direct', **totals}],
        'by_utm': {
            name: [{'value': None, 'label': 'Источник не указан', **totals}]
            for name in ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')
        },
    }


def test_booking_client_requests_submitted_and_cancelled_summaries_server_to_server():
    requests = []

    def handler(request):
        requests.append(request)
        status = request.url.params['status']
        return httpx.Response(200, json=summary(status))

    settings = Settings(booking_api_url='https://booking.example.test',
                        booking_api_token='secret')
    client = BookingAnalyticsClient(settings, transport=httpx.MockTransport(handler))

    result = asyncio.run(client.load(date(2026, 9, 19), date(2026, 9, 20)))

    assert result['submitted']['totals'] == {
        'bookings': 2, 'guests': 5, 'unknown_guest_bookings': 1}
    assert result['cancelled']['totals']['bookings'] == 1
    assert [request.url.path for request in requests] == [
        '/analytics/summary', '/analytics/summary']
    assert [request.url.params['status'] for request in requests] == [
        'submitted', 'cancelled']
    assert all(request.url.params['from'] == '2026-09-19' for request in requests)
    assert all(request.url.params['to'] == '2026-09-20' for request in requests)
    assert all(request.url.params['date_basis'] == 'visit' for request in requests)
    assert all(request.headers['Authorization'] == 'Bearer secret' for request in requests)


@pytest.mark.parametrize('mutate', [
    lambda payload: payload['totals'].__setitem__('bookings', -1),
    lambda payload: payload['totals'].__setitem__('unknown_guest_bookings', 3),
    lambda payload: payload.__setitem__('by_date', 'not-a-list'),
    lambda payload: payload['coverage'].__setitem__('historical_data_complete', 'false'),
    lambda payload: payload['coverage'].__setitem__('history_started_at', 'not-a-date'),
    lambda payload: payload['coverage'].__setitem__('history_started_at', '20260919T080000Z'),
    lambda payload: payload.__setitem__('timezone', 'UTC'),
    lambda payload: payload['by_status'][0].__setitem__('value', 'cancelled')
    if payload['by_status'][0]['value'] == 'submitted' else None,
    lambda payload: payload['by_source'].append({
        'value': payload['by_source'][0]['value'], 'label': 'duplicate',
        'bookings': 0, 'guests': 0, 'unknown_guest_bookings': 0}),
    lambda payload: payload['by_source'][0].__setitem__('value', []),
])
def test_booking_client_rejects_malformed_summary_instead_of_showing_zeroes(mutate):
    def handler(request):
        payload = summary(request.url.params['status'])
        mutate(payload)
        return httpx.Response(200, json=payload)

    settings = Settings(booking_api_url='https://booking.example.test',
                        booking_api_token='secret')
    client = BookingAnalyticsClient(settings, transport=httpx.MockTransport(handler))

    with pytest.raises(DataError, match='некорректный ответ'):
        asyncio.run(client.load(date(2026, 9, 19), date(2026, 9, 20)))


def test_booking_client_accepts_empty_status_breakdown_for_empty_summary():
    def handler(request):
        payload = summary(request.url.params['status'])
        zero = {'bookings': 0, 'guests': 0, 'unknown_guest_bookings': 0}
        payload['totals'] = zero
        payload['by_date'] = []
        payload['by_status'] = []
        payload['by_source'] = []
        payload['by_utm'] = {name: [] for name in payload['by_utm']}
        return httpx.Response(200, json=payload)

    settings = Settings(booking_api_url='https://booking.example.test',
                        booking_api_token='secret')
    client = BookingAnalyticsClient(settings, transport=httpx.MockTransport(handler))

    result = asyncio.run(client.load(date(2026, 9, 19), date(2026, 9, 20)))

    assert result['submitted']['totals']['bookings'] == 0
    assert result['cancelled']['by_status'] == []


def multi_source_settings():
    return Settings(booking_api_url='https://booking.example.test', booking_api_token='telegram-secret',
                    website_booking_api_url='https://website.example.test', website_booking_api_token='website-secret')


def test_two_sources_sum_once_and_preserve_source_labels_and_history():
    requests = []

    def handler(request):
        requests.append(request)
        payload = summary(request.url.params['status'])
        if request.url.host == 'website.example.test':
            payload['coverage']['history_started_at'] = '2026-09-20T00:00:00.000Z'
            payload['by_source'][0].update(value='website', label='website')
        return httpx.Response(200, json=payload)

    client = BookingAnalyticsClient(multi_source_settings(), transport=httpx.MockTransport(handler))
    raw = asyncio.run(client.load(date(2026, 9, 19), date(2026, 9, 20)))
    result = build_booking_analytics(raw, date(2026, 9, 19), date(2026, 9, 20), 'day')
    assert len(requests) == 4
    for request in requests:
        expected = 'website-secret' if request.url.host == 'website.example.test' else 'telegram-secret'
        assert request.headers['Authorization'] == 'Bearer ' + expected
    assert result['totals'] == {'bookings': 4, 'guests': 10, 'unknown_guest_bookings': 2, 'cancelled': 2}
    assert {r['name'] for r in result['sources']} == {'Telegram · direct', 'Сайт'}
    assert [s['name'] for s in result['coverage']['sources']] == ['Telegram', 'Сайт']
    assert result['coverage']['sources'][1]['history_started_at'] == '2026-09-20T00:00:00.000Z'
    assert result['series'][0]['values'] == {'bookings': 4, 'guests': 10, 'cancelled': 2}
    assert 'secret' not in str(result)


@pytest.mark.parametrize('broken_host,label', [('website.example.test', 'Сайт'), ('booking.example.test', 'Telegram')])
def test_one_source_failure_never_returns_a_partial_total(broken_host, label):
    def handler(request):
        if request.url.host == broken_host:
            return httpx.Response(503, json={'error': 'source_unavailable'})
        return httpx.Response(200, json=summary(request.url.params['status']))
    client = BookingAnalyticsClient(multi_source_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(DataError, match=label):
        asyncio.run(client.load(date(2026, 9, 19), date(2026, 9, 20)))


def test_inconsistent_source_history_is_detected_before_merging():
    def handler(request):
        payload = summary(request.url.params['status'])
        if request.url.host == 'website.example.test' and request.url.params['status'] == 'cancelled':
            payload['excluded_missing_date'] = 1
        return httpx.Response(200, json=payload)
    client = BookingAnalyticsClient(multi_source_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(DataError, match='Сайт:.*несогласованное'):
        asyncio.run(client.load(date(2026, 9, 19), date(2026, 9, 20)))
