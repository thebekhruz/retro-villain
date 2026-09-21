"""Safe application logging that never serializes upstream payloads or secrets."""

import logging


LOGGER_NAME = 'retro.upstream'


def configure_logging(level=logging.INFO):
    logger = logging.getLogger('retro')
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            '%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s'))
        logger.addHandler(handler)
    return logger


def log_safe_failure(component: str, error: Exception, *, operation: str,
                     request_id: str = '-', duration_ms: int | None = None):
    logging.getLogger(LOGGER_NAME).warning(
        'component=%s operation=%s request_id=%s duration_ms=%s error_class=%s',
        component, operation, request_id, duration_ms if duration_ms is not None else '-',
        error.__class__.__name__)


def log_upstream_failure(adapter: str, error: Exception, *, operation: str,
                         request_id: str = '-', duration_ms: int | None = None):
    log_safe_failure(adapter, error, operation=operation, request_id=request_id,
                     duration_ms=duration_ms)
