import logging

from retro.logging_config import log_upstream_failure


def test_logged_upstream_error_excludes_secrets(caplog):
    caplog.set_level(logging.WARNING)

    log_upstream_failure(
        'gemini', RuntimeError('GEMINI_API_KEY=secret Authorization: Bearer secret'),
        operation='analyze', request_id='req-1')

    text = caplog.text
    assert 'req-1' in text
    assert 'RuntimeError' in text
    assert 'GEMINI_API_KEY' not in text
    assert 'Authorization' not in text
    assert 'secret' not in text
