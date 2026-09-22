import os
import re
from ipaddress import IPv4Network, IPv6Network, ip_network
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from retro.runtime import resolve_data_dir

ROOT = Path(__file__).resolve().parent.parent
IIKO_ORIGIN = 'https://retro3158.iikoweb.ru'


@dataclass(frozen=True)
class HikvisionConfig:
    base_url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    source: str = 'retro-main-entry'
    poll_seconds: int = 30
    timeout_seconds: int = 8
    verify_tls: bool = True


@dataclass(frozen=True)
class Settings:
    base_url: str = IIKO_ORIGIN
    login: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    store_id: int | None = None
    dashboard_user: str = field(default='', repr=False)
    dashboard_password: str = field(default='', repr=False)
    dashboard_panel_users: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    dashboard_allowed_network: IPv4Network | IPv6Network | None = None
    trusted_proxy_network: IPv4Network | IPv6Network | None = None
    manual_handover_only: bool = False
    claude_api_key: str = field(default='', repr=False)
    claude_model: str = ''
    director_categories: dict[str, str] = field(default_factory=dict)
    director_excluded_groups: frozenset[str] = field(default_factory=frozenset)
    booking_api_url: str = ''
    booking_api_token: str = field(default='', repr=False)
    hikvision: HikvisionConfig | None = field(default=None, repr=False)
    data_dir: Path = ROOT / 'build'
    report_retention: int = 24

    @property
    def configured(self):
        return bool(self.login and self.password and self.store_id is not None)

    @property
    def claude_configured(self):
        return bool(self.claude_api_key and self.claude_model)

    @property
    def booking_configured(self):
        return bool(self.booking_api_url and self.booking_api_token)

    @property
    def hikvision_configured(self):
        return self.hikvision is not None

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / 'build' / '.env', override=False)
        base = os.getenv('IIKO_SERVER_URL', IIKO_ORIGIN).rstrip('/')
        if base != IIKO_ORIGIN:
            raise ValueError('IIKO_SERVER_URL должен указывать на сервер Retro Milliy.')
        store = os.getenv('IIKO_STORE_ID', '').strip()
        if store and (not store.isdigit() or int(store) <= 0):
            raise ValueError('IIKO_STORE_ID должен быть положительным числом.')
        user, password = os.getenv('DASHBOARD_USER', ''), os.getenv('DASHBOARD_PASSWORD', '')
        if bool(user) != bool(password):
            raise ValueError('Для защиты укажите и DASHBOARD_USER, и DASHBOARD_PASSWORD.')
        panel_users = parse_dashboard_panel_users(os.getenv('DASHBOARD_PANEL_USERS', ''))
        network_value = os.getenv('DASHBOARD_ALLOWED_NETWORK', '').strip()
        allowed_network = ip_network(network_value, strict=False) if network_value else None
        proxy_value = os.getenv('TRUSTED_PROXY_NETWORK', '').strip()
        trusted_proxy_network = ip_network(proxy_value, strict=False) if proxy_value else None
        manual = os.getenv('ACCOUNTANT_MANUAL_HANDOVER', '').strip().casefold() in {'1', 'true', 'yes', 'да'}
        categories = parse_director_categories(os.getenv('IIKO_DIRECTOR_CATEGORIES', ''))
        excluded_groups = parse_director_excluded_groups(os.getenv('IIKO_DIRECTOR_EXCLUDED_GROUPS', ''))
        booking_url = os.getenv('BOOKING_ANALYTICS_URL', '').strip().rstrip('/')
        booking_token = os.getenv('ANALYTICS_API_TOKEN', '').strip()
        if bool(booking_url) != bool(booking_token):
            raise ValueError('BOOKING_ANALYTICS_URL и ANALYTICS_API_TOKEN задаются вместе.')
        if booking_url:
            parsed = urlsplit(booking_url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or \
                    parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError('BOOKING_ANALYTICS_URL должен быть корневым HTTPS URL без credentials/query.')
        retention_value = os.getenv('DIRECTOR_REPORT_RETENTION', '24').strip()
        if not retention_value.isdigit() or not 1 <= int(retention_value) <= 1000:
            raise ValueError('DIRECTOR_REPORT_RETENTION должен быть числом от 1 до 1000.')
        hikvision = parse_hikvision_config(os.environ)
        return cls(
            base_url=base,
            login=os.getenv('IIKO_LOGIN', ''),
            password=os.getenv('IIKO_PASSWORD', ''),
            store_id=int(store) if store else None,
            dashboard_user=user,
            dashboard_password=password,
            dashboard_panel_users=panel_users,
            dashboard_allowed_network=allowed_network,
            trusted_proxy_network=trusted_proxy_network,
            manual_handover_only=manual,
            claude_api_key=os.getenv('CLAUDE_API_KEY', ''),
            claude_model=os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6'),
            director_categories=categories,
            director_excluded_groups=excluded_groups,
            booking_api_url=booking_url,
            booking_api_token=booking_token,
            hikvision=hikvision,
            data_dir=resolve_data_dir(os.getenv('RETRO_DATA_DIR', '').strip()),
            report_retention=int(retention_value),
        )


def parse_hikvision_config(environ) -> HikvisionConfig | None:
    url = environ.get('HIKVISION_URL', '').strip().rstrip('/')
    username = environ.get('HIKVISION_USER', '').strip()
    password = environ.get('HIKVISION_PASSWORD', '')
    present = (bool(url), bool(username), bool(password))
    if not any(present):
        return None
    if not all(present):
        raise ValueError('HIKVISION_URL, HIKVISION_USER и HIKVISION_PASSWORD задаются вместе.')
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('HIKVISION_URL должен быть корневым HTTP(S) URL без credentials/query.') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or port == 0):
        raise ValueError('HIKVISION_URL должен быть корневым HTTP(S) URL без credentials/query.')
    source = environ.get('HIKVISION_SOURCE', 'retro-main-entry').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', source):
        raise ValueError('HIKVISION_SOURCE должен быть безопасным именем длиной до 64 символов.')
    poll = _bounded_int(environ.get('HIKVISION_POLL_SECONDS', '30'),
                        'HIKVISION_POLL_SECONDS', 10, 3600)
    timeout = _bounded_int(environ.get('HIKVISION_TIMEOUT_SECONDS', '8'),
                           'HIKVISION_TIMEOUT_SECONDS', 1, 60)
    verify_raw = environ.get('HIKVISION_VERIFY_TLS', 'true').strip().casefold()
    if verify_raw not in {'true', 'false', '1', '0', 'yes', 'no'}:
        raise ValueError('HIKVISION_VERIFY_TLS должен быть true или false.')
    return HikvisionConfig(
        base_url=url, username=username, password=password, source=source,
        poll_seconds=poll, timeout_seconds=timeout,
        verify_tls=verify_raw in {'true', '1', 'yes'})


def _bounded_int(value: str, name: str, minimum: int, maximum: int) -> int:
    raw = value.strip()
    if not raw.isdigit() or not minimum <= int(raw) <= maximum:
        raise ValueError(f'{name} должен быть числом от {minimum} до {maximum}.')
    return int(raw)


def parse_director_categories(value: str) -> dict[str, str]:
    allowed = {'menu', 'dessert', 'drink'}
    if not value.strip():
        return {}
    result = {}
    for pair in value.split(';'):
        if pair.count('=') != 1:
            raise ValueError('IIKO_DIRECTOR_CATEGORIES должен содержать пары «категория=тип».')
        name, kind = (part.strip() for part in pair.split('='))
        if not name or kind not in allowed or name in result:
            raise ValueError('IIKO_DIRECTOR_CATEGORIES содержит некорректную категорию.')
        result[name] = kind
    return result


def parse_dashboard_panel_users(value: str) -> dict[str, tuple[str, str]]:
    if not value.strip():
        return {}
    required_roles = {'cashier', 'accountant', 'director', 'founder'}
    allowed_roles = required_roles | {'admin'}
    result = {}
    roles = set()
    for raw_entry in value.split(';'):
        parts = [part.strip() for part in raw_entry.split(':')]
        if len(parts) != 3:
            raise ValueError('DASHBOARD_PANEL_USERS должен содержать пары user:password:role.')
        username, password, role = parts
        if (not username or not password or role not in allowed_roles or username in result
                or len(username) > 64 or len(password) > 256):
            raise ValueError('DASHBOARD_PANEL_USERS содержит некорректную учётную запись.')
        result[username] = (password, role)
        roles.add(role)
    if not required_roles.issubset(roles):
        raise ValueError('DASHBOARD_PANEL_USERS должен задавать по одному пользователю для каждой панели.')
    return result


def parse_director_excluded_groups(value: str) -> frozenset[str]:
    groups = [item.strip() for item in value.split(';') if item.strip()]
    if len(groups) != len(set(groups)):
        raise ValueError('IIKO_DIRECTOR_EXCLUDED_GROUPS содержит повторяющуюся группу.')
    return frozenset(groups)
