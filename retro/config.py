import os
from ipaddress import IPv4Network, IPv6Network, ip_network
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from retro.runtime import resolve_data_dir

ROOT = Path(__file__).resolve().parent.parent
IIKO_ORIGIN = 'https://retro3158.iikoweb.ru'


@dataclass(frozen=True)
class Settings:
    base_url: str = IIKO_ORIGIN
    login: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    store_id: int | None = None
    dashboard_user: str = field(default='', repr=False)
    dashboard_password: str = field(default='', repr=False)
    dashboard_allowed_network: IPv4Network | IPv6Network | None = None
    manual_handover_only: bool = False
    gemini_api_key: str = field(default='', repr=False)
    gemini_model: str = ''
    director_categories: dict[str, str] = field(default_factory=dict)
    director_excluded_groups: frozenset[str] = field(default_factory=frozenset)
    booking_api_url: str = ''
    booking_api_token: str = field(default='', repr=False)
    data_dir: Path = ROOT / 'build'
    report_retention: int = 24

    @property
    def configured(self):
        return bool(self.login and self.password and self.store_id is not None)

    @property
    def gemini_configured(self):
        return bool(self.gemini_api_key and self.gemini_model)

    @property
    def booking_configured(self):
        return bool(self.booking_api_url and self.booking_api_token)

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
        network_value = os.getenv('DASHBOARD_ALLOWED_NETWORK', '').strip()
        allowed_network = ip_network(network_value, strict=False) if network_value else None
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
        return cls(
            base_url=base,
            login=os.getenv('IIKO_LOGIN', ''),
            password=os.getenv('IIKO_PASSWORD', ''),
            store_id=int(store) if store else None,
            dashboard_user=user,
            dashboard_password=password,
            dashboard_allowed_network=allowed_network,
            manual_handover_only=manual,
            gemini_api_key=os.getenv('GEMINI_API_KEY', ''),
            gemini_model=os.getenv('GEMINI_MODEL', 'gemini-2.5-flash'),
            director_categories=categories,
            director_excluded_groups=excluded_groups,
            booking_api_url=booking_url,
            booking_api_token=booking_token,
            data_dir=resolve_data_dir(os.getenv('RETRO_DATA_DIR', '').strip()),
            report_retention=int(retention_value),
        )


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


def parse_director_excluded_groups(value: str) -> frozenset[str]:
    groups = [item.strip() for item in value.split(';') if item.strip()]
    if len(groups) != len(set(groups)):
        raise ValueError('IIKO_DIRECTOR_EXCLUDED_GROUPS содержит повторяющуюся группу.')
    return frozenset(groups)
