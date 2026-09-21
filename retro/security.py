"""Request-boundary helpers shared by the application middleware."""

from ipaddress import ip_address
from urllib.parse import urlsplit


MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


def client_address(request, settings):
    if request.client is None:
        raise ValueError('Адрес клиента не распознан.')
    peer = ip_address(request.client.host)
    forwarded = request.headers.get('x-forwarded-for')
    if forwarded is None or settings.trusted_proxy_network is None or peer not in settings.trusted_proxy_network:
        return peer
    if ',' in forwarded or not forwarded.strip():
        raise ValueError('Прокси передал некорректный адрес клиента.')
    return ip_address(forwarded.strip())


def is_finance_path(path: str) -> bool:
    return (path == '/accountant' or path.startswith('/accountant/')
            or path.startswith('/api/accountant/')
            or path == '/api/director/attendance')


def is_local_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.casefold() == 'localhost':
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _origin_tuple(value: str):
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('Источник запроса не распознан.')
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        raise ValueError('Источник запроса не распознан.') from None
    return parsed.scheme, parsed.hostname.casefold(), port


def effective_scheme(request) -> str:
    """Схема, которую видит браузер.

    Сервер стоит за обратным прокси хостинга и разговаривает с ним по http,
    поэтому request.url.scheme всегда http. Браузер при этом пришёл по https
    и присылает такой Origin. Сравнивать с ним схему соединения нельзя:
    иначе собственная форма входа выглядит как запрос с чужого сайта.
    Подменить заголовок можно, но домен в Origin подделать нельзя, а защита
    от межсайтовых запросов держится именно на домене."""
    forwarded = request.headers.get('x-forwarded-proto', '')
    first = forwarded.split(',')[0].strip().casefold()
    if first in ('http', 'https'):
        return first
    return request.url.scheme


def validate_mutation_origin(request) -> None:
    if request.method not in MUTATING_METHODS:
        return
    origin = request.headers.get('origin')
    if origin is None:
        return
    base = urlsplit(str(request.base_url))
    expected = '%s://%s' % (effective_scheme(request), base.netloc)
    if origin == 'null' or _origin_tuple(origin) != _origin_tuple(expected):
        raise ValueError('Запрос с другого источника отклонён.')
