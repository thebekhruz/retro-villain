import pytest

from retro.config import Settings


def test_booking_api_config_reads_https_url_and_keeps_token_out_of_repr(monkeypatch):
    monkeypatch.setenv('BOOKING_ANALYTICS_URL', 'https://booking.example.test/')
    monkeypatch.setenv('ANALYTICS_API_TOKEN', 'top-secret-token')
    monkeypatch.setenv('BROADCAST_API_TOKEN', 'write-secret-token')

    settings = Settings.from_env()

    assert settings.booking_api_url == 'https://booking.example.test'
    assert settings.booking_api_token == 'top-secret-token'
    assert settings.booking_configured is True
    assert settings.booking_broadcast_configured is True
    assert 'top-secret-token' not in repr(settings)
    assert 'write-secret-token' not in repr(settings)


@pytest.mark.parametrize('url,token', [
    ('https://booking.example.test', ''),
    ('', 'token'),
    ('http://booking.example.test', 'token'),
    ('https://user:password@booking.example.test', 'token'),
    ('https://booking.example.test/path?token=secret', 'token'),
])
def test_booking_api_config_rejects_partial_or_unsafe_values(monkeypatch, url, token):
    monkeypatch.setenv('BOOKING_ANALYTICS_URL', url)
    monkeypatch.setenv('ANALYTICS_API_TOKEN', token)

    with pytest.raises(ValueError, match='BOOKING_ANALYTICS'):
        Settings.from_env()


def test_broadcast_token_requires_booking_url_and_must_be_distinct(monkeypatch):
    monkeypatch.setenv('BOOKING_ANALYTICS_URL', '')
    monkeypatch.setenv('ANALYTICS_API_TOKEN', '')
    monkeypatch.setenv('BROADCAST_API_TOKEN', 'write-secret')
    with pytest.raises(ValueError, match='BROADCAST_API_TOKEN'):
        Settings.from_env()

    monkeypatch.setenv('BOOKING_ANALYTICS_URL', 'https://booking.example.test')
    monkeypatch.setenv('ANALYTICS_API_TOKEN', 'same-secret')
    monkeypatch.setenv('BROADCAST_API_TOKEN', 'same-secret')
    with pytest.raises(ValueError, match='должен отличаться'):
        Settings.from_env()
