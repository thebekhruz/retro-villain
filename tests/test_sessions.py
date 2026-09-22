"""Перезапуск сервера не должен выгонять смену из панели."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from retro.sessions import SessionStore


def test_session_survives_a_restart(tmp_path):
    # Деплой перезапускает процесс несколько раз в день. Официант в зале не
    # должен получать экран входа посреди смены.
    path = tmp_path / 'sessions.json'
    token = SessionStore(path).create('anvar', 'cashier')
    after_restart = SessionStore(path)
    assert after_restart.identity(token).username == 'anvar'
    assert after_restart.role(token) == 'cashier'


def test_logging_out_closes_the_session_everywhere(tmp_path):
    path = tmp_path / 'sessions.json'
    store = SessionStore(path)
    token = store.create('anvar', 'cashier')
    store.delete(token)
    assert SessionStore(path).identity(token) is None


def test_the_file_never_holds_the_token_itself(tmp_path):
    # Файл лежит на диске сервера. Если он утечёт, по нему не должно быть
    # возможности войти, поэтому храним отпечаток, а не сам ключ.
    path = tmp_path / 'sessions.json'
    token = SessionStore(path).create('anvar', 'cashier')
    assert token not in path.read_text(encoding='utf-8')


def test_old_sessions_do_not_live_forever(tmp_path):
    # Забытый вход на общем телефоне не должен открывать кассу через год.
    path = tmp_path / 'sessions.json'
    token = SessionStore(path).create('anvar', 'cashier')
    saved = json.loads(path.read_text(encoding='utf-8'))
    old = datetime.now(timezone.utc) - timedelta(days=31)
    for row in saved.values():
        row['started'] = old.isoformat()
    path.write_text(json.dumps(saved), encoding='utf-8')
    assert SessionStore(path).identity(token) is None


def test_a_broken_file_does_not_stop_the_panel(tmp_path):
    # Диск может оборваться на середине записи. Панель важнее сессий:
    # хуже пустого входа только панель, которая вовсе не поднялась.
    path = tmp_path / 'sessions.json'
    path.write_text('{ не json', encoding='utf-8')
    store = SessionStore(path)
    token = store.create('anvar', 'cashier')
    assert store.role(token) == 'cashier'


def test_store_without_a_file_still_works():
    # Тесты и локальный запуск обходятся без диска.
    store = SessionStore()
    assert store.role(store.create('anvar', 'cashier')) == 'cashier'


@pytest.mark.parametrize('token', [None, '', 'чужой'])
def test_unknown_token_gives_nothing(tmp_path, token):
    store = SessionStore(tmp_path / 'sessions.json')
    store.create('anvar', 'cashier')
    assert store.identity(token) is None


def test_file_is_readable_only_by_the_server(tmp_path):
    path = tmp_path / 'sessions.json'
    SessionStore(path).create('anvar', 'cashier')
    assert path.stat().st_mode & 0o077 == 0


def test_saved_file_keeps_the_role(tmp_path):
    path = tmp_path / 'sessions.json'
    SessionStore(path).create('anvar', 'cashier')
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert [row['role'] for row in saved.values()] == ['cashier']


def test_the_panel_keeps_people_signed_in_across_a_deploy(tmp_path):
    # Деплой — это новый процесс с тем же томом. Проверяем целиком, через
    # приложение: cookie из прежнего входа обязана открывать модуль.
    from fastapi.testclient import TestClient

    from retro.app import create_app
    from retro.config import Settings

    users = {'director': ('test-password', 'director')}
    settings = Settings(dashboard_panel_users=users, data_dir=tmp_path)
    with TestClient(create_app(settings), client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1', follow_redirects=False) as client:
        client.post('/api/session', json={'username': 'director', 'password': 'test-password'})
        cookie = client.cookies['retro_session']

    with TestClient(create_app(Settings(dashboard_panel_users=users, data_dir=tmp_path)),
                    client=('127.0.0.1', 50000), base_url='http://127.0.0.1',
                    follow_redirects=False) as restarted:
        restarted.cookies.set('retro_session', cookie)
        page = restarted.get('/director')

    assert page.status_code == 200
