"""Роль видит только свой модуль: в меню, в разметке и в статике (T-383)."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from retro.app import STATIC_PANELS, create_app, static_panels
from retro.config import Settings

STATIC = Path(__file__).resolve().parent.parent / 'retro' / 'static'
ROLES = ('cashier', 'accountant', 'director', 'founder', 'shokh')
USERS = {role: ('secret', role) for role in ROLES} | {'boss': ('secret', 'admin')}
# Страница → панель, которой она принадлежит.
PAGES = {'index.html': 'cashier', 'accountant.html': 'accountant', 'employees.html': 'accountant',
         'payroll.html': 'accountant', 'director.html': 'director', 'director-app.html': 'director',
         'founder.html': 'founder', 'founder-cabinet.html': 'founder', 'shokh.html': 'shokh'}
PATHS = {'cashier': '/', 'accountant': '/accountant', 'director': '/director',
         'founder': '/founder', 'shokh': '/shokh'}


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(dashboard_panel_users=USERS, data_dir=tmp_path))
    with TestClient(app, base_url='http://dashboard.example.com',
                    client=('203.0.113.10', 50000)) as test_client:
        yield test_client


def module_ids(client, user):
    answer = client.get('/api/config', auth=(user, 'secret'))
    assert answer.status_code == 200
    return [row['id'] for row in answer.json()['modules']]


@pytest.mark.parametrize('role', ROLES)
def test_single_role_gets_only_its_own_module(client, role):
    assert module_ids(client, role) == [role]


def test_full_access_sees_every_module_including_purchase(client):
    assert module_ids(client, 'boss') == list(ROLES)


def test_legacy_shared_login_sees_every_module(tmp_path):
    app = create_app(Settings(dashboard_user='boss', dashboard_password='secret', data_dir=tmp_path))
    with TestClient(app, base_url='http://dashboard.example.com',
                    client=('203.0.113.10', 50000)) as test_client:
        answer = test_client.get('/api/config', auth=('boss', 'secret'))
    assert [row['id'] for row in answer.json()['modules']] == list(ROLES)


def test_config_rows_carry_no_lock_reason(client):
    answer = client.get('/api/config', auth=('accountant', 'secret')).json()
    assert answer['modules'] == [
        {'id': 'accountant', 'name': 'Бухгалтер', 'path': '/accountant', 'available': True}]


@pytest.mark.parametrize('page, panel', PAGES.items())
def test_page_markup_links_no_foreign_module(page, panel):
    """В исходнике страницы нет ссылок на чужие модули — даже если скрипт не выполнится."""
    text = (STATIC / page).read_text(encoding='utf-8')
    hrefs = set(re.findall(r'href="(/[a-z]*)(?:/[^"]*)?"', text))
    foreign = {PATHS[other] for other in ROLES if other != panel}
    assert not hrefs & foreign, (page, hrefs & foreign)


@pytest.mark.parametrize('role', ROLES)
def test_foreign_panel_static_is_forbidden(client, role):
    for name, panels in STATIC_PANELS.items():
        answer = client.get('/static/' + name, auth=(role, 'secret'))
        expected = 200 if role in panels else 403
        assert answer.status_code == expected, (role, name, answer.status_code)


def test_named_holes_from_the_task_are_closed(client):
    for name in ('accountant.html', 'founder-cabinet.html', 'director-app.js'):
        assert client.get('/static/' + name, auth=('cashier', 'secret')).status_code == 403, name
    assert client.get('/static/accountant.html', auth=('accountant', 'secret')).status_code == 200
    assert client.get('/static/founder-cabinet.html', auth=('founder', 'secret')).status_code == 200
    assert client.get('/static/director-app.js', auth=('director', 'secret')).status_code == 200


@pytest.mark.parametrize('path', ['/static/a/../accountant.html', '/static/Accountant.html',
                                  '/static/ACCOUNTANT.JS'])
def test_path_tricks_do_not_open_a_foreign_panel(client, path):
    assert client.get(path, auth=('cashier', 'secret')).status_code == 403


@pytest.mark.parametrize('role', ROLES + ('boss',))
def test_shared_static_is_open_to_every_signed_in_role(client, role):
    for name in ('style.css', 'mobile.css', 'nav.js', 'i18n.js', 'i18n-uz.js', 'logout.js',
                 'frontend-state.js', 'financial-write.js', 'favicon.svg'):
        assert client.get('/static/' + name, auth=(role, 'secret')).status_code == 200, (role, name)


def test_admin_opens_every_panel_static(client):
    for name in STATIC_PANELS:
        assert client.get('/static/' + name, auth=('boss', 'secret')).status_code == 200, name


def test_shokh_is_locked_out_of_accountant_routes(client):
    for path in ('/api/accountant/day', '/api/accountant/shokh/purchases'):
        assert client.get(path, auth=('shokh', 'secret')).status_code == 403, path
    answer = client.post('/api/accountant/shokh/purchases/1/accept', auth=('shokh', 'secret'))
    assert answer.status_code == 403
    assert client.get('/shokh', auth=('shokh', 'secret')).status_code == 200
    assert client.get('/api/shokh/home', auth=('shokh', 'secret')).status_code != 403


def test_anonymous_gets_401_on_panel_static_and_login_redirect_on_pages(client):
    assert client.get('/static/accountant.html').status_code == 401
    assert client.get('/static/app.js').status_code == 401
    answer = client.get('/accountant', follow_redirects=False)
    assert answer.status_code == 303 and answer.headers['location'] == '/login'


def test_unlisted_static_file_stays_shared():
    assert static_panels('/static/style.css') is None
    assert static_panels('/static/no-such-file.js') is None


def test_static_table_matches_what_pages_really_load():
    """Файл модуля, подключённый страницей, есть в таблице с панелью этой страницы;
    файл, который грузят все панели, остаётся общим. Новый файл не забыть."""
    loaded: dict[str, set[str]] = {}
    for page, panel in PAGES.items():
        loaded.setdefault(page, set()).add(panel)
        for name in re.findall(r'/static/([A-Za-z0-9._-]+)', (STATIC / page).read_text(encoding='utf-8')):
            loaded.setdefault(name, set()).add(panel)
    every_panel = set(ROLES)
    for name, panels in loaded.items():
        if name in STATIC_PANELS:
            assert panels <= STATIC_PANELS[name], (name, panels)
        else:
            assert len(panels) >= 4, f'{name} грузят только {panels} — впишите его в STATIC_PANELS'
    assert set(STATIC_PANELS) <= set(loaded) | {p.name for p in STATIC.iterdir()}
    assert every_panel


def test_every_dashboard_page_loads_the_menu_script():
    for page in ('index.html', 'accountant.html', 'employees.html', 'payroll.html',
                 'director.html', 'founder.html', 'director-app.html', 'founder-cabinet.html'):
        assert 'nav.js' in (STATIC / page).read_text(encoding='utf-8'), page
