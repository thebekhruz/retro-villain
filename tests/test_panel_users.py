"""Учётные записи панели в базе (T-442): вход, приоритет над переменными,
выключение, направления менеджера и команда управления."""

import io
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from legacy_app import create_app
from test_accountant_design_parity import POSTGRES_URL
from test_phone_login import USERS, FakeSms, Clock, ask, confirm, open_client
from retro.config import Settings, SmsConfig
from retro.panel_users import PanelAccounts, PanelUserStore, hash_password, password_matches
from retro.panel_users_cli import run

ADMIN_PHONE = '+998906462540'


def settings(tmp_path, **extra):
    values = dict(data_dir=tmp_path, sms=SmsConfig(provider='console'))
    values.update(extra)
    return Settings(**values)


def make_app(tmp_path, sms=None, **extra):
    return create_app(settings(tmp_path, **extra), expense_db_path=tmp_path / 'cashier.sqlite3',
                      accountant_db_path=tmp_path / 'accountant.sqlite3',
                      director_db_path=tmp_path / 'director.sqlite3',
                      founder_db_path=tmp_path / 'founder.sqlite3', sms_client=sms or FakeSms())


def store_of(tmp_path):
    return PanelUserStore(tmp_path / 'panel-users.sqlite3')


def cli(tmp_path, *argv, password='new-secret-1', **extra):
    out = io.StringIO()
    code = run(list(argv), settings=settings(tmp_path, **extra), store=store_of(tmp_path), out=out,
               password_source=lambda: password)
    return code, out.getvalue()


def sign_in(client, login, password):
    return client.post('/api/session', json={'username': login, 'password': password})


# ── Хранилище ───────────────────────────────────────────────────────────────

def test_password_is_kept_only_as_a_salted_fingerprint(tmp_path):
    stored = hash_password('kassa-2026')
    assert 'kassa-2026' not in stored and stored != hash_password('kassa-2026')
    assert password_matches('kassa-2026', stored)
    assert not password_matches('kassa-2027', stored)
    assert not password_matches('anything', None)


def test_one_number_belongs_to_one_account(tmp_path):
    store = store_of(tmp_path)
    store.save('boss', role='admin', password_hash=None, phone=ADMIN_PHONE, directions=())
    with pytest.raises(ValueError, match='уже привязан к «boss»'):
        store.save('other', role='manager', password_hash=None, phone=ADMIN_PHONE, directions=())


# ── Вход ────────────────────────────────────────────────────────────────────

def test_account_from_the_database_signs_in_without_any_environment(tmp_path):
    store_of(tmp_path).save('anvar', role='cashier', password_hash=hash_password('kassa-2026'),
                            phone=None, directions=())
    with open_client(make_app(tmp_path), Clock()) as client:
        # Учётные записи есть только в базе — панель всё равно закрыта входом.
        assert client.get('/').status_code == 303
        assert sign_in(client, 'anvar', 'kassa-2027').status_code == 401
        login = sign_in(client, 'anvar', 'kassa-2026')
        assert login.status_code == 200
        assert login.json() == {'role': 'cashier', 'path': '/'}
        assert client.get('/accountant').status_code == 403


def test_admin_with_only_a_number_enters_by_sms(tmp_path):
    sms = FakeSms()
    store_of(tmp_path).save('boss', role='admin', password_hash=None, phone=ADMIN_PHONE, directions=())
    with open_client(make_app(tmp_path, sms), Clock()) as client:
        assert 'data-phone-login="on"' in client.get('/login').text
        assert ask(client, '+998 90 646 25 40').status_code == 200
        login = confirm(client, sms.code(), ADMIN_PHONE)
        assert login.json() == {'role': 'admin', 'path': '/'}
        assert client.get('/accountant').status_code == 200
        client.cookies.clear()
        # Пароля у записи нет — по логину не войти никаким паролем.
        assert sign_in(client, 'boss', 'any-password').status_code == 401


def test_database_row_overrides_the_environment(tmp_path):
    store_of(tmp_path).save('anvar', role='accountant', password_hash=hash_password('new-pass-1'),
                            phone=None, directions=())
    users = {**USERS, 'anvar': ('old-pass', 'cashier')}
    with open_client(make_app(tmp_path, dashboard_panel_users=users), Clock()) as client:
        assert sign_in(client, 'anvar', 'old-pass').status_code == 401
        assert sign_in(client, 'anvar', 'new-pass-1').json()['role'] == 'accountant'
        # Остальные по-прежнему входят из переменных.
        assert sign_in(client, 'director', 'secret').json()['role'] == 'director'


def test_disabling_the_account_closes_its_open_session(tmp_path):
    store = store_of(tmp_path)
    store.save('anvar', role='cashier', password_hash=hash_password('kassa-2026'), phone=None, directions=())
    with open_client(make_app(tmp_path), Clock()) as client:
        sign_in(client, 'anvar', 'kassa-2026')
        assert client.get('/').status_code == 200
        store.save('anvar', role='cashier', password_hash=store.password_hash('anvar'), phone=None,
                   directions=(), active=False)
        client.app.state.accounts.forget()
        assert client.get('/').status_code == 303
        assert sign_in(client, 'anvar', 'kassa-2026').status_code == 401


def test_changed_role_closes_the_session_too(tmp_path):
    store = store_of(tmp_path)
    store.save('anvar', role='cashier', password_hash=hash_password('kassa-2026'), phone=None, directions=())
    with open_client(make_app(tmp_path), Clock()) as client:
        sign_in(client, 'anvar', 'kassa-2026')
        store.save('anvar', role='accountant', password_hash=store.password_hash('anvar'), phone=None,
                   directions=())
        client.app.state.accounts.forget()
        assert client.get('/').status_code == 303


def test_manager_directions_come_from_the_database(tmp_path):
    store_of(tmp_path).save('oshxona', role='manager', password_hash=hash_password('kitchen-1'),
                            phone=None, directions=('Кухня',))
    with open_client(make_app(tmp_path), Clock()) as client:
        sign_in(client, 'oshxona', 'kitchen-1')
        home = client.get('/api/manager/home').json()
        assert home['login'] == 'oshxona'
    accounts = PanelAccounts(settings(tmp_path), store_of(tmp_path))
    assert accounts.directions('oshxona') == ('Кухня',)


def test_environment_number_of_a_login_moved_to_the_database_is_ignored(tmp_path):
    store_of(tmp_path).save('karina', role='manager', password_hash=hash_password('karina-1'),
                            phone=None, directions=())
    accounts = PanelAccounts(settings(tmp_path, dashboard_panel_users=USERS,
                                      phone_users={'+998901234567': 'karina'}), store_of(tmp_path))
    assert accounts.by_phone('+998901234567') is None


# ── Команда управления ──────────────────────────────────────────────────────

def test_cli_adds_the_admin_by_number_and_lists_it(tmp_path):
    code, out = cli(tmp_path, 'add', 'boss', '--role', 'admin', '--phone', '90 646 25 40')
    assert code == 0 and ADMIN_PHONE in out and 'только SMS' in out
    code, out = cli(tmp_path, 'list')
    assert 'boss' in out and 'admin' in out


def test_cli_refuses_an_account_nobody_can_enter(tmp_path, capsys):
    code, _ = cli(tmp_path, 'add', 'ghost', '--role', 'cashier')
    assert code == 2 and 'войти нечем' in capsys.readouterr().err


def test_cli_sets_password_directions_and_disables(tmp_path):
    cli(tmp_path, 'add', 'karina', '--role', 'manager', '--password', '--directions', 'Зал')
    store = store_of(tmp_path)
    assert password_matches('new-secret-1', store.password_hash('karina'))
    assert store.get('karina').directions == ('Зал',)
    cli(tmp_path, 'set', 'karina', '--all-directions', '--phone', '+998901234567')
    assert store.get('karina').directions == () and store.get('karina').phone == '+998901234567'
    cli(tmp_path, 'disable', 'karina')
    assert not store.get('karina').active
    # Выключение не стирает пароль: включили — входит как раньше.
    cli(tmp_path, 'enable', 'karina')
    assert password_matches('new-secret-1', store.password_hash('karina'))


def test_cli_rejects_a_short_password(tmp_path, capsys):
    code, _ = cli(tmp_path, 'add', 'anvar', '--role', 'cashier', '--password', password='short')
    assert code == 2 and 'Пароль' in capsys.readouterr().err


def test_cli_imports_environment_users_with_hashed_passwords(tmp_path):
    code, out = cli(tmp_path, 'import-env', dashboard_panel_users=USERS,
                    phone_users={'+998901234567': 'karina'}, manager_directions={'oshxona': ('Кухня',)})
    assert code == 0 and 'Перенесено: 6' in out
    store = store_of(tmp_path)
    assert password_matches('secret', store.password_hash('cashier'))
    assert store.get('karina').phone == '+998901234567'
    assert store.get('oshxona').directions == ('Кухня',)
    # Второй перенос ничего не перетирает.
    code, out = cli(tmp_path, 'import-env', dashboard_panel_users=USERS)
    assert 'Перенесено: 0' in out


# ── Postgres ────────────────────────────────────────────────────────────────

@contextmanager
def postgres_schema():
    import psycopg
    from psycopg import sql
    schema = 'users_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query))
    query['options'] = '-csearch_path=' + schema
    try:
        yield urlunsplit(parts._replace(query=urlencode(query)))
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@pytest.mark.skipif(not POSTGRES_URL, reason='RETRO_TEST_POSTGRES_URL не задан')
def test_postgres_store_and_sms_admin(tmp_path):
    from retro.db import Database
    sms = FakeSms()
    with postgres_schema() as url:
        store = PanelUserStore(Database(url))
        store.save('boss', role='admin', password_hash=None, phone=ADMIN_PHONE, directions=())
        store.save('boss', role='admin', password_hash=hash_password('boss-pass-1'), phone=ADMIN_PHONE,
                   directions=())
        assert store.summary() == (True, True)
        assert store.by_phone(ADMIN_PHONE).login == 'boss'
        with open_client(create_app(settings(tmp_path, database_url=url), sms_client=sms), Clock()) as client:
            ask(client, ADMIN_PHONE)
            assert confirm(client, sms.code(), ADMIN_PHONE).json()['role'] == 'admin'
            client.cookies.clear()
            assert sign_in(client, 'boss', 'boss-pass-1').json()['role'] == 'admin'


def test_cli_sets_the_display_name(tmp_path):
    code, out = cli(tmp_path, 'add', 'boss', '--role', 'admin', '--name', 'Бехруз', '--phone', '90 646 25 40')
    assert code == 0 and 'Бехруз' in out
    assert store_of(tmp_path).get('boss').name == 'Бехруз'


def test_name_column_is_added_to_an_existing_table(tmp_path):
    import sqlite3
    # Таблица в том виде, как её создавал T-443 (и SQL руками на бою).
    with sqlite3.connect(tmp_path / 'panel-users.sqlite3') as connection:
        connection.execute('''CREATE TABLE panel_users (login TEXT PRIMARY KEY, role TEXT NOT NULL,
            password_hash TEXT, phone TEXT UNIQUE, directions TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        connection.execute("INSERT INTO panel_users VALUES ('admin_phone','admin',NULL,'+998906462540','',1,'t','t')")
    account = store_of(tmp_path).get('admin_phone')
    assert account.name is None and account.label == 'admin_phone' and account.phone == '+998906462540'
