"""Панель как приложение на экране телефона (T-451).

Манифест и иконки браузер запрашивает без кук: Chrome берёт манифест в режиме
credentials=omit, iOS — значок при «На экран Домой». Поэтому они открыты без
входа, а вся прочая статика по-прежнему закрыта."""

import json
import re
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from retro.app import PUBLIC_PATHS, create_app
from retro.config import Settings

STATIC = Path(__file__).resolve().parent.parent / 'retro' / 'static'
USERS = {'kassa': ('secret', 'cashier'), 'karina': ('secret', 'manager'),
         'dir': ('secret', 'director'), 'boss': ('secret', 'admin')}
PAGES = sorted(path.name for path in STATIC.glob('*.html'))
ICONS = ('/static/icon-192.png', '/static/icon-512.png',
         '/static/icon-maskable-192.png', '/static/icon-maskable-512.png')


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(dashboard_panel_users=USERS, data_dir=tmp_path))
    with TestClient(app, base_url='http://dashboard.example.com',
                    client=('203.0.113.10', 50000)) as test_client:
        yield test_client


def png_header(body: bytes) -> tuple[int, int, int]:
    """Ширина, высота и тип цвета PNG (2 — RGB без прозрачности, 6 — RGBA)."""
    assert body[:8] == b'\x89PNG\r\n\x1a\n'
    width, height, _depth, color = struct.unpack('>IIBB', body[16:26])
    return width, height, color


def manifest(client) -> dict:
    answer = client.get('/manifest.webmanifest')
    assert answer.status_code == 200
    assert answer.headers['content-type'] == 'application/manifest+json'
    return json.loads(answer.content)


def test_manifest_is_served_without_login_as_manifest_json(client):
    data = manifest(client)
    assert data['name'] == 'Retro Milliy'
    assert data['short_name'] and len(data['short_name']) <= 12
    assert data['display'] == 'standalone'
    assert data['scope'] == '/'
    assert data['lang'] == 'ru'
    assert data['theme_color'] == data['background_color'] == '#143e35'


def test_start_url_is_the_login_page_that_sends_each_role_home(client):
    """С иконки открывается /login: без сессии — форма входа, с сессией —
    сервер сам ведёт в модуль своей роли (кассира на «/» менеджер не пустил бы)."""
    assert manifest(client)['start_url'] == '/login'
    assert client.get('/login').status_code == 200
    for user, home in (('kassa', '/'), ('karina', '/manager'), ('dir', '/director'), ('boss', '/')):
        session = client.post('/api/session', json={'username': user, 'password': 'secret'})
        assert session.status_code == 200
        answer = client.get('/login', follow_redirects=False)
        assert answer.status_code == 303 and answer.headers['location'] == home, user
        client.cookies.clear()


def test_manifest_icons_are_open_png_of_the_declared_size(client):
    icons = manifest(client)['icons']
    assert {icon['src'] for icon in icons} == set(ICONS)
    purposes = {(icon['sizes'], icon['purpose']) for icon in icons}
    assert {('192x192', 'any'), ('512x512', 'any'), ('512x512', 'maskable')} <= purposes
    for icon in icons:
        answer = client.get(icon['src'])
        assert answer.status_code == 200, icon['src']
        assert answer.headers['content-type'] == 'image/png'
        width, height, color = png_header(answer.content)
        assert f'{width}x{height}' == icon['sizes'], icon['src']
        if icon['purpose'] == 'maskable':
            # Маскируемую иконку система обрезает по своей форме: углы должны быть залиты.
            assert color == 2, icon['src']


def test_apple_touch_icon_is_open_opaque_180(client):
    answer = client.get('/static/apple-touch-icon.png')
    assert answer.status_code == 200
    # iOS заливает прозрачное чёрным: у значка нет альфа-канала вовсе.
    assert png_header(answer.content) == (180, 180, 2)


def test_only_the_manifest_and_icons_were_opened(client):
    """Открыты ровно манифест и файлы иконок; прочая статика и страницы — за входом."""
    assert {'/manifest.webmanifest', *ICONS} <= PUBLIC_PATHS
    login = (STATIC / 'login.html').read_text(encoding='utf-8')
    for path in PUBLIC_PATHS:
        # Открытая статика — только то, что грузит экран входа, и иконки манифеста.
        if path.startswith('/static/'):
            assert f'"{path}"' in login or path in ICONS, path
    for path in ('/static/style.css', '/static/app.js', '/static/nav.js', '/static/manager.js',
                 '/static/manifest.webmanifest', '/static/index.html', '/static/manager.html'):
        assert client.get(path).status_code == 401, path
    for path in ('/', '/manager', '/accountant', '/director'):
        answer = client.get(path, follow_redirects=False)
        assert answer.status_code == 303 and answer.headers['location'] == '/login', path


def test_manifest_is_not_versioned_or_cached_for_a_year(client):
    """Адрес манифеста у установленного значка не меняется: страница не
    дописывает ему ?v=<хэш>, как остальной статике."""
    page = client.get('/login').text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    assert 'immutable' not in client.get('/manifest.webmanifest').headers['cache-control']


@pytest.mark.parametrize('page', PAGES)
def test_every_page_can_be_added_to_the_home_screen(page):
    head = (STATIC / page).read_text(encoding='utf-8').split('</head>', 1)[0]
    for tag in ('<link rel="manifest" href="/manifest.webmanifest">',
                '<meta name="theme-color" content="#143e35">',
                '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">',
                '<meta name="mobile-web-app-capable" content="yes">',
                '<meta name="apple-mobile-web-app-capable" content="yes">',
                '<meta name="apple-mobile-web-app-title" content="Retro Milliy">',
                # default: на любой iOS шапка встаёт под строкой состояния, а не под чёлку.
                '<meta name="apple-mobile-web-app-status-bar-style" content="default">'):
        assert tag in head, (page, tag)
    assert len(re.findall(r'rel="manifest"', head)) == 1, page


def test_no_service_worker_caches_money_data():
    """Панель денежная: данные только живые. Service worker не нужен для
    установки, и его нет — значит, нет и офлайн-копий отчётов."""
    for path in STATIC.iterdir():
        if path.suffix in ('.js', '.html'):
            assert 'serviceWorker' not in path.read_text(encoding='utf-8'), path.name
