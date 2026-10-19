"""Перезапуск сервера и выкат не должны выгонять смену из панели."""

import json
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

from retro.sessions import SessionStore, stamp


def rows(path):
    import sqlite3
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute('SELECT fingerprint, username, role, phone, started FROM panel_sessions').fetchall()


def test_session_survives_a_restart(tmp_path):
    # Деплой перезапускает процесс несколько раз в день. Официант в зале не
    # должен получать экран входа посреди смены.
    path = tmp_path / 'sessions.sqlite3'
    token = SessionStore(path).create('anvar', 'cashier')
    after_restart = SessionStore(path)
    assert after_restart.identity(token).username == 'anvar'
    assert after_restart.role(token) == 'cashier'


def test_logging_out_closes_the_session_everywhere(tmp_path):
    path = tmp_path / 'sessions.sqlite3'
    store = SessionStore(path)
    token = store.create('anvar', 'cashier')
    store.delete(token)
    assert SessionStore(path).identity(token) is None


def test_the_database_never_holds_the_token_itself(tmp_path):
    # База может утечь. По ней не должно быть возможности войти, поэтому
    # храним отпечаток, а не сам ключ.
    path = tmp_path / 'sessions.sqlite3'
    token = SessionStore(path).create('anvar', 'cashier')
    assert token not in repr(rows(path))
    assert token.encode() not in path.read_bytes()


def test_old_sessions_do_not_live_forever(tmp_path):
    # Забытый вход на общем телефоне не должен открывать кассу через год.
    path = tmp_path / 'sessions.sqlite3'
    store = SessionStore(path)
    token = store.create('anvar', 'cashier')
    import sqlite3
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('UPDATE panel_sessions SET started = ?',
                           (stamp(datetime.now(timezone.utc) - timedelta(days=31)),))
    assert SessionStore(path).identity(token) is None
    assert rows(path) == []  # истёкшая строка удалена


def test_new_login_sweeps_expired_sessions(tmp_path):
    path = tmp_path / 'sessions.sqlite3'
    store = SessionStore(path)
    store.create('old', 'cashier')
    import sqlite3
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('UPDATE panel_sessions SET started = ?',
                           (stamp(datetime.now(timezone.utc) - timedelta(days=40)),))
    store.create('anvar', 'cashier')
    assert [row[1] for row in rows(path)] == ['anvar']


def test_store_without_a_database_still_works():
    # Тесты и локальный запуск обходятся без диска.
    store = SessionStore()
    token = store.create('anvar', 'cashier')
    assert store.role(token) == 'cashier'
    store.delete(token)
    assert store.role(token) is None


@pytest.mark.parametrize('token', [None, '', 'чужой'])
def test_unknown_token_gives_nothing(tmp_path, token):
    store = SessionStore(tmp_path / 'sessions.sqlite3')
    store.create('anvar', 'cashier')
    assert store.identity(token) is None


def test_database_file_is_readable_only_by_the_server(tmp_path):
    path = tmp_path / 'sessions.sqlite3'
    SessionStore(path).create('anvar', 'cashier')
    assert path.stat().st_mode & 0o077 == 0


def test_phone_session_keeps_the_number(tmp_path):
    path = tmp_path / 'sessions.sqlite3'
    token = SessionStore(path).create('karina', 'manager', phone='+998901234567')
    assert SessionStore(path).identity(token).phone == '+998901234567'


def test_live_sessions_move_from_the_old_file_once(tmp_path):
    # Сервер со своим диском: сессии из sessions.json переезжают в базу, файл
    # убирается, истёкшие и битые строки не переносятся.
    legacy = tmp_path / 'sessions.json'
    old = SessionStore()
    from retro.sessions import fingerprint
    now = datetime.now(timezone.utc)
    legacy.write_text(json.dumps({
        fingerprint('live'): dict(username='anvar', role='cashier', started=now.isoformat()),
        fingerprint('stale'): dict(username='old', role='cashier', started=(now - timedelta(days=31)).isoformat()),
        'broken': dict(username='x'),
    }), encoding='utf-8')
    store = SessionStore(tmp_path / 'sessions.sqlite3', legacy=legacy)
    assert store.role('live') == 'cashier'
    assert store.identity('stale') is None
    assert not legacy.exists()
    assert old.role('live') is None


def test_a_broken_old_file_does_not_stop_the_panel(tmp_path):
    legacy = tmp_path / 'sessions.json'
    legacy.write_text('{ не json', encoding='utf-8')
    store = SessionStore(tmp_path / 'sessions.sqlite3', legacy=legacy)
    assert store.role(store.create('anvar', 'cashier')) == 'cashier'


def test_the_panel_keeps_people_signed_in_across_a_deploy(tmp_path):
    # Выкат на хостинге — новый контейнер: каталог данных пустой, база та же.
    # Cookie из прежнего входа обязана открывать модуль.
    from fastapi.testclient import TestClient

    from legacy_app import create_app
    from retro.config import Settings

    users = {'director': ('test-password', 'director')}
    database = str(tmp_path / 'shared.sqlite3')  # на хостинге — DATABASE_URL Postgres

    def app(container):
        return create_app(Settings(dashboard_panel_users=users, data_dir=tmp_path / container,
                                   database_url=database))

    with TestClient(app('container-1'), client=('127.0.0.1', 50000), base_url='http://127.0.0.1',
                    follow_redirects=False) as client:
        login = client.post('/api/session', json={'username': 'director', 'password': 'test-password'})
        cookie = client.cookies['retro_session']
    # Вход по паролю живёт столько же, сколько сессия, а не 12 часов.
    assert 'Max-Age=2592000' in login.headers['set-cookie']

    with TestClient(app('container-2'), client=('127.0.0.1', 50000), base_url='http://127.0.0.1',
                    follow_redirects=False) as restarted:
        restarted.cookies.set('retro_session', cookie)
        page = restarted.get('/director')

    assert page.status_code == 200
