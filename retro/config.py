import os
from ipaddress import IPv4Network, IPv6Network, ip_network
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

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

    @property
    def configured(self):
        return bool(self.login and self.password and self.store_id is not None)


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
        return cls(base, os.getenv('IIKO_LOGIN', ''), os.getenv('IIKO_PASSWORD', ''),
                   int(store) if store else None, user, password, allowed_network)
