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
