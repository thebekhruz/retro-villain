from dataclasses import replace

import pytest

from retro.config import HikvisionConfig, Settings


HIK_ENV = {
    'HIKVISION_URL': 'https://203.0.113.10:8443',
    'HIKVISION_USER': 'retro-reader',
    'HIKVISION_PASSWORD': 'not-a-real-secret',
    'HIKVISION_SOURCE': 'retro-main-entry',
    'HIKVISION_POLL_SECONDS': '30',
    'HIKVISION_TIMEOUT_SECONDS': '8',
    'HIKVISION_VERIFY_TLS': 'true',
}


def test_hikvision_config_is_optional(monkeypatch):
    for name in HIK_ENV:
        monkeypatch.delenv(name, raising=False)

    settings = Settings.from_env()

    assert settings.hikvision is None
    assert settings.hikvision_configured is False


def test_hikvision_config_reads_one_device_without_exposing_password(monkeypatch):
    for name, value in HIK_ENV.items():
        monkeypatch.setenv(name, value)

    config = Settings.from_env().hikvision

    assert config == HikvisionConfig(
        base_url='https://203.0.113.10:8443', username='retro-reader',
        password='not-a-real-secret', source='retro-main-entry',
        poll_seconds=30, timeout_seconds=8, verify_tls=True)
    assert 'not-a-real-secret' not in repr(config)


@pytest.mark.parametrize('missing', ['HIKVISION_URL', 'HIKVISION_USER', 'HIKVISION_PASSWORD'])
def test_hikvision_connection_values_are_all_or_none(monkeypatch, missing):
    for name, value in HIK_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing)

    with pytest.raises(ValueError, match='HIKVISION_URL, HIKVISION_USER и HIKVISION_PASSWORD'):
        Settings.from_env()


@pytest.mark.parametrize('url', [
    'ftp://203.0.113.10',
    'https://user:secret@203.0.113.10',
    'https://203.0.113.10/path',
    'https://203.0.113.10?secret=yes',
    'https://203.0.113.10#fragment',
    'https://',
])
def test_hikvision_url_must_be_a_safe_http_root(monkeypatch, url):
    for name, value in HIK_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('HIKVISION_URL', url)

    with pytest.raises(ValueError, match='HIKVISION_URL'):
        Settings.from_env()


@pytest.mark.parametrize(('name', 'value'), [
    ('HIKVISION_POLL_SECONDS', '9'),
    ('HIKVISION_POLL_SECONDS', '3601'),
    ('HIKVISION_TIMEOUT_SECONDS', '0'),
    ('HIKVISION_TIMEOUT_SECONDS', '61'),
    ('HIKVISION_VERIFY_TLS', 'sometimes'),
    ('HIKVISION_SOURCE', 'bad/source'),
])
def test_hikvision_operational_values_are_bounded(monkeypatch, name, value):
    for env_name, env_value in HIK_ENV.items():
        monkeypatch.setenv(env_name, env_value)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings.from_env()


def test_settings_repr_does_not_expose_hikvision_secret():
    settings = Settings(hikvision=HikvisionConfig(
        base_url='https://203.0.113.10', username='reader', password='super-secret',
        source='entry', poll_seconds=30, timeout_seconds=8, verify_tls=True))

    assert 'super-secret' not in repr(settings)
    assert replace(settings).hikvision.password == 'super-secret'
