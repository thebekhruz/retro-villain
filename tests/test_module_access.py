"""Закрытый модуль не должен открываться из меню."""

from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings


def modules(settings, credentials):
    with TestClient(create_app(settings), base_url='http://dashboard.example.com',
                    client=('203.0.113.10', 50000)) as client:
        answer = client.get('/api/config', auth=credentials)
    return {row['id']: row for row in answer.json()['modules']}


def test_other_panels_are_marked_closed_for_a_single_role():
    rows = modules(Settings(dashboard_panel_users={'d': ('secret', 'director')}), ('d', 'secret'))
    assert rows['director']['available'] is True
    for panel in ('cashier', 'accountant', 'founder'):
        assert rows[panel]['available'] is False, panel
        assert rows[panel]['reason'], panel


def test_full_access_role_keeps_every_module_available_remotely():
    rows = modules(Settings(dashboard_user='boss', dashboard_password='secret'), ('boss', 'secret'))
    for panel in ('cashier', 'accountant', 'director', 'founder'):
        assert rows[panel]['available'] is True, panel


def test_accountant_role_sees_accountant_available_remotely():
    rows = modules(
        Settings(dashboard_panel_users={'bookkeeper': ('secret', 'accountant')}),
        ('bookkeeper', 'secret'),
    )
    assert rows['accountant'] == {
        'id': 'accountant',
        'name': 'Бухгалтер',
        'path': '/accountant',
        'available': True,
        'reason': '',
    }


def test_every_dashboard_page_loads_the_menu_script():
    from pathlib import Path
    static = Path(__file__).resolve().parent.parent / 'retro' / 'static'
    for page in ('index.html', 'accountant.html', 'employees.html',
                 'director.html', 'founder.html'):
        assert 'nav.js' in (static / page).read_text(encoding='utf-8'), page
