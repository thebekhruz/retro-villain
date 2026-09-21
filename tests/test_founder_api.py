from datetime import date

from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.modules.cashier.service import DataError


class FounderIiko:
    def __init__(self, result=None, error=None):
        self.result = result or {'ok': True}
        self.error = error
        self.calls = []

    async def load_founder_analytics(self, start, end, granularity, directions):
        self.calls.append((start, end, granularity, directions))
        if self.error:
            raise self.error
        return self.result


class FounderBookings:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def load(self, start, end):
        self.calls.append((start, end))
        if self.error:
            raise self.error
        return self.result


def client_with(source):
    app = create_app(Settings())
    app.state.iiko = source
    return TestClient(app, client=('127.0.0.1', 50000))


def test_founder_api_parses_inclusive_period_granularity_and_directions():
    source = FounderIiko({'period': {'start': '2026-09-01', 'end': '2026-09-08'}})
    with client_with(source) as client:
        response = client.get('/api/founder/analytics', params={
            'start': '2026-09-01', 'end': '2026-09-08',
            'granularity': 'week', 'directions': 'retro,banquet',
        })

    assert response.status_code == 200
    assert response.json()['period']['end'] == '2026-09-08'
    assert source.calls == [
        (date(2026, 9, 1), date(2026, 9, 8), 'week', ('retro', 'banquet'))]


def test_founder_api_rejects_future_oversized_and_unknown_filters():
    source = FounderIiko()
    with client_with(source) as client:
        assert client.get('/api/founder/analytics?start=2026-01-01&end=2027-01-02').status_code == 422
        assert client.get('/api/founder/analytics?start=2099-01-01&end=2099-01-02').status_code == 422
        assert client.get('/api/founder/analytics?start=2026-09-01&end=2026-09-08&directions=retro,other').status_code == 422
        assert client.get('/api/founder/analytics?start=2026-09-08&end=2026-09-01').status_code == 422
    assert source.calls == []


def test_founder_api_does_not_replace_iiko_error_with_zeroes():
    source = FounderIiko(error=DataError('iiko недоступен'))
    with client_with(source) as client:
        response = client.get('/api/founder/analytics', params={
            'start': '2026-09-01', 'end': '2026-09-08'})

    assert response.status_code == 503
    assert response.json() == {'detail': 'iiko недоступен'}


def test_founder_page_and_module_are_exposed():
    source = FounderIiko()
    with client_with(source) as client:
        page = client.get('/founder')
        config = client.get('/api/config').json()

    assert page.status_code == 200
    assert 'Учредитель' in page.text
    assert config['modules'][-1] == {'id': 'founder', 'name': 'Учредитель', 'available': True}


def test_founder_booking_api_applies_period_and_granularity_without_directions():
    coverage = {'history_started_at': '2026-09-01T08:00:00.000Z',
                'historical_data_complete': False}
    empty = {'bookings': 0, 'guests': 0, 'unknown_guest_bookings': 0}
    submitted = {'bookings': 2, 'guests': 5, 'unknown_guest_bookings': 1}
    source = FounderBookings({
        'submitted': {
            'coverage': coverage, 'excluded_missing_date': 0, 'totals': submitted,
            'by_date': [{'value': '2026-09-01', **submitted}],
            'by_source': [{'value': 'direct', 'label': 'direct', **submitted}],
        },
        'cancelled': {
            'coverage': coverage, 'excluded_missing_date': 0, 'totals': empty,
            'by_date': [], 'by_source': [],
        },
    })
    app = create_app(Settings())
    app.state.bookings = source

    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/founder/bookings', params={
            'start': '2026-09-01', 'end': '2026-09-08', 'granularity': 'week'})

    assert response.status_code == 200
    assert response.json()['totals'] == {
        'bookings': 2, 'guests': 5, 'unknown_guest_bookings': 1, 'cancelled': 0}
    assert response.json()['series'][0]['values'] == {
        'bookings': 2, 'guests': 5, 'cancelled': 0}
    assert source.calls == [(date(2026, 9, 1), date(2026, 9, 8))]


def test_booking_api_error_is_local_and_does_not_replace_iiko_data():
    app = create_app(Settings())
    app.state.iiko = FounderIiko({'revenue_series': [{'ok': True}]})
    app.state.bookings = FounderBookings(error=DataError('бот недоступен'))

    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        booking_response = client.get(
            '/api/founder/bookings?start=2026-09-01&end=2026-09-08')
        iiko_response = client.get(
            '/api/founder/analytics?start=2026-09-01&end=2026-09-08')

    assert booking_response.status_code == 503
    assert booking_response.json() == {'detail': 'бот недоступен'}
    assert iiko_response.status_code == 200
    assert iiko_response.json() == {'revenue_series': [{'ok': True}]}
