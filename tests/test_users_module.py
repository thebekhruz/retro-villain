"""Модуль «Пользователи» (T-445): администратор заводит людей в панель."""

import pytest

from test_panel_users import make_app, store_of
from test_phone_login import USERS, Clock, open_client
from retro.panel_users import hash_password, password_matches

ADMIN_PHONE = '+998906462540'


@pytest.fixture
def admin(tmp_path):
    """Админ из базы, вошедший по паролю; рядом — записи из переменных."""
    store_of(tmp_path).save('boss', role='admin', password_hash=hash_password('boss-pass-1'),
                            phone=ADMIN_PHONE, directions=())
    with open_client(make_app(tmp_path, dashboard_panel_users=USERS), Clock()) as client:
        assert client.post('/api/session', json={'username': 'boss', 'password': 'boss-pass-1'}).status_code == 200
        yield client


def logins(answer):
    return {row['login']: row for row in answer.json()['users']}


def test_only_admin_opens_the_module(tmp_path, admin):
    assert admin.get('/users').status_code == 200
    assert 'users' in [row['id'] for row in admin.get('/api/config').json()['modules']]
    with open_client(make_app(tmp_path, dashboard_panel_users=USERS), Clock()) as client:
        client.post('/api/session', json={'username': 'director', 'password': 'secret'})
        assert client.get('/api/users').status_code == 403
        assert client.post('/api/users', json={'login': 'x', 'role': 'admin', 'phone': '901234567'}).status_code == 403
        assert client.get('/static/users.js').status_code == 403
        assert 'users' not in [row['id'] for row in client.get('/api/config').json()['modules']]


def test_listing_shows_database_and_environment_accounts(admin):
    rows = logins(admin.get('/api/users'))
    assert rows['boss']['source'] == 'db' and rows['boss']['me'] is True
    assert rows['boss']['phone'] == '+998 90 646 25 40'
    assert rows['karina']['source'] == 'env'
    # Отпечаток пароля наружу не уходит.
    assert 'boss-pass-1' not in admin.get('/api/users').text
    assert 'pbkdf2' not in admin.get('/api/users').text


def test_admin_adds_a_worker_who_then_signs_in(admin, tmp_path):
    answer = admin.post('/api/users', json={'login': 'anvar', 'role': 'cashier', 'password': 'kassa-2026'})
    assert answer.status_code == 200
    row = logins(answer)['anvar']
    assert row['role_name'] == 'Кассир' and row['updated_by'] == 'boss'
    with open_client(make_app(tmp_path, dashboard_panel_users=USERS), Clock()) as other:
        assert other.post('/api/session', json={'username': 'anvar', 'password': 'kassa-2026'}).json()['role'] == 'cashier'


def test_sms_only_manager_with_directions(admin):
    answer = admin.post('/api/users', json={'login': 'oshpaz', 'role': 'manager', 'phone': '93 555 00 77',
                                            'directions': ['Кухня']})
    row = logins(answer)['oshpaz']
    assert row['phone'] == '+998 93 555 00 77' and row['password'] is False and row['directions'] == ['Кухня']


@pytest.mark.parametrize('body, text', [
    ({'login': 'ghost', 'role': 'cashier'}, 'войти нечем'),
    ({'login': 'anvar', 'role': 'cashier', 'password': 'short'}, 'Пароль'),
    ({'login': 'anvar', 'role': 'king', 'password': 'long-enough'}, 'Роль'),
    ({'login': 'an var', 'role': 'cashier', 'password': 'long-enough'}, 'Логин'),
    ({'login': 'anvar', 'role': 'cashier', 'phone': '123'}, 'не номер'),
    ({'login': 'anvar', 'role': 'cashier', 'phone': ADMIN_PHONE}, 'уже привязан'),
    ({'login': 'boss', 'role': 'cashier', 'password': 'long-enough'}, 'уже есть'),
    ({'login': 'karina', 'role': 'cashier', 'password': 'long-enough'}, 'перенесите'),
])
def test_invalid_accounts_are_refused_in_words(admin, body, text):
    answer = admin.post('/api/users', json=body)
    assert answer.status_code in (409, 422)
    assert text in answer.json()['detail']


def test_admin_changes_phone_password_and_disables(admin, tmp_path):
    admin.post('/api/users', json={'login': 'anvar', 'role': 'cashier', 'password': 'kassa-2026'})
    assert admin.patch('/api/users/anvar', json={'phone': '+998 90 111 22 33'}).status_code == 200
    assert admin.patch('/api/users/anvar', json={'password': 'kassa-2027'}).status_code == 200
    store = store_of(tmp_path)
    assert password_matches('kassa-2027', store.password_hash('anvar'))
    assert store.get('anvar').phone == '+998901112233'
    # Пароль снят, номер остался — вход только по SMS.
    assert logins(admin.patch('/api/users/anvar', json={'remove_password': True}))['anvar']['password'] is False
    # Без пароля и без номера запись не сохраняется.
    refused = admin.patch('/api/users/anvar', json={'phone': ''})
    assert refused.status_code == 422 and 'войти нечем' in refused.json()['detail']
    assert logins(admin.patch('/api/users/anvar', json={'active': False}))['anvar']['active'] is False


def test_disabling_closes_the_open_session_immediately(admin, tmp_path):
    admin.post('/api/users', json={'login': 'anvar', 'role': 'cashier', 'password': 'kassa-2026'})
    with open_client(make_app(tmp_path, dashboard_panel_users=USERS), Clock()) as cashier:
        cashier.post('/api/session', json={'username': 'anvar', 'password': 'kassa-2026'})
        assert cashier.get('/').status_code == 200
        admin.patch('/api/users/anvar', json={'active': False})
        cashier.app.state.accounts.forget()   # другой процесс: свой кэш, 5 секунд
        assert cashier.get('/').status_code == 303


def test_admin_cannot_lock_themselves_out(admin):
    for change in ({'active': False}, {'role': 'cashier'}):
        answer = admin.patch('/api/users/boss', json=change)
        assert answer.status_code == 409 and 'Свою учётную запись' in answer.json()['detail']
    assert admin.delete('/api/users/boss').status_code == 409
    # Свой номер и пароль менять можно.
    assert admin.patch('/api/users/boss', json={'password': 'boss-pass-2'}).status_code == 200
    assert admin.get('/api/users').status_code == 200


def test_delete_removes_the_account(admin):
    admin.post('/api/users', json={'login': 'anvar', 'role': 'cashier', 'password': 'kassa-2026'})
    answer = admin.delete('/api/users/anvar')
    assert answer.status_code == 200 and 'anvar' not in logins(answer)
    assert admin.delete('/api/users/anvar').status_code == 422


def test_environment_account_is_read_only_until_moved(admin, tmp_path):
    assert admin.patch('/api/users/karina', json={'phone': '+998 90 111 22 33'}).status_code == 409
    assert admin.delete('/api/users/karina').status_code == 409
    moved = admin.post('/api/users/karina/import')
    assert moved.status_code == 200
    row = logins(moved)['karina']
    assert row['source'] == 'db' and row['role'] == 'manager' and row['password'] is True
    # Пароль переехал отпечатком и действует.
    assert password_matches('secret', store_of(tmp_path).password_hash('karina'))
    assert admin.post('/api/users/karina/import').status_code == 422
    assert admin.patch('/api/users/karina', json={'phone': '+998 90 111 22 33'}).status_code == 200


# ── Имя для интерфейса (T-446) ──────────────────────────────────────────────

def test_admin_sets_a_display_name_shown_instead_of_the_login(admin, tmp_path):
    answer = admin.post('/api/users', json={'login': 'karina2', 'role': 'manager', 'name': '  Карина   Юсупова ',
                                            'password': 'karina-2026'})
    row = logins(answer)['karina2']
    assert row['name'] == 'Карина Юсупова' and row['label'] == 'Карина Юсупова'
    with open_client(make_app(tmp_path, dashboard_panel_users=USERS), Clock()) as manager:
        manager.post('/api/session', json={'username': 'karina2', 'password': 'karina-2026'})
        config = manager.get('/api/config').json()
        assert config['user'] == 'karina2' and config['user_name'] == 'Карина Юсупова'
        assert manager.get('/api/manager/home').json()['name'] == 'Карина Юсупова'
    # Пустое имя снимает его — экран снова показывает логин.
    row = logins(admin.patch('/api/users/karina2', json={'name': ''}))['karina2']
    assert row['name'] is None and row['label'] == 'karina2'
    # Изменение другого поля имя не трогает.
    admin.patch('/api/users/karina2', json={'name': 'Карина'})
    assert logins(admin.patch('/api/users/karina2', json={'active': False}))['karina2']['name'] == 'Карина'


def test_name_is_validated(admin):
    answer = admin.post('/api/users', json={'login': 'x1', 'role': 'cashier', 'password': 'long-enough',
                                            'name': 'А' * 81})
    assert answer.status_code == 422 and 'Имя' in answer.json()['detail']


def test_without_a_name_the_interface_uses_the_login(admin):
    assert admin.get('/api/config').json()['user_name'] == 'boss'
    # Записи из переменных имени не имеют.
    with_env = logins(admin.get('/api/users'))['karina']
    assert with_env['name'] is None and with_env['label'] == 'karina'
