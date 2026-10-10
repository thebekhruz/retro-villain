"""Вход менеджера по номеру телефона и SMS-коду (ТЗ 09.10, М-05, T-433).

Ввёл номер → получил код → подтвердил → кабинет открыт в сохранённой сессии.
Номер находит существующую учётную запись и никогда не создаёт новую; код
одноразовый, живёт 5 минут, хранится отпечатком; попытки и SMS ограничены.
Настоящий Eskiz не вызывается: SMS ловит заглушка, клиент Eskiz проверяется
на httpx.MockTransport. Поток — на SQLite и на Postgres
(RETRO_TEST_POSTGRES_URL), как в CI.
"""

import asyncio
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from test_accountant_design_parity import POSTGRES_URL
from retro.config import SmsConfig, Settings, parse_sms_config
from retro.integrations.sms import ConsoleSms, EskizSms, SmsError, sms_sender
from retro.phone_login import ADDRESS_REQUESTS_PER_HOUR, MAX_ATTEMPTS, PHONE_SENDS_PER_HOUR
from retro.phone_numbers import display_phone, normalize_phone, parse_phone_users

USERS = {'cashier': ('secret', 'cashier'), 'accountant': ('secret', 'accountant'),
         'director': ('secret', 'director'), 'founder': ('secret', 'founder'),
         'karina': ('secret', 'manager'), 'oshxona': ('secret', 'manager')}
KARINA = '+998901234567'
OSHXONA = '+998935550011'
PHONES = {KARINA: 'karina', OSHXONA: 'oshxona'}
START = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)


class FakeSms:
    """SMS-шлюз в памяти: что и кому «отправлено», сбой по заказу."""

    def __init__(self):
        self.sent = []
        self.fail = None

    async def send(self, phone, text):
        if self.fail:
            raise SmsError(self.fail)
        self.sent.append((phone, text))

    def code(self, index=-1):
        return re.search(r'\b(\d{6})\b', self.sent[index][1]).group(1)

    async def close(self):
        pass


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


def settings(tmp_path, **extra):
    values = dict(data_dir=tmp_path, dashboard_panel_users=USERS, phone_users=PHONES,
                  sms=SmsConfig(provider='console'))
    values.update(extra)
    return Settings(**values)


def sqlite_app(tmp_path, sender, **extra):
    return create_app(settings(tmp_path, **extra), expense_db_path=tmp_path / 'cashier.sqlite3',
                      accountant_db_path=tmp_path / 'accountant.sqlite3',
                      director_db_path=tmp_path / 'director.sqlite3',
                      founder_db_path=tmp_path / 'founder.sqlite3', sms_client=sender)


@contextmanager
def postgres_app(tmp_path, sms):
    import psycopg
    from psycopg import sql
    schema = 'phone_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query))
    query['options'] = '-csearch_path=' + schema
    url = urlunsplit(parts._replace(query=urlencode(query)))
    try:
        yield create_app(settings(tmp_path, database_url=url), sms_client=sms)
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


def open_client(app, clock):
    app.state.phone_login.clock = clock
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000),
                      follow_redirects=False)


@pytest.fixture
def sms():
    return FakeSms()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(params=['sqlite', 'postgres'])
def phone(request, tmp_path, sms, clock):
    if request.param == 'postgres':
        if not POSTGRES_URL:
            pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
        with postgres_app(tmp_path, sms) as app, open_client(app, clock) as client:
            yield client
    else:
        with open_client(sqlite_app(tmp_path, sms), clock) as client:
            yield client


def ask(client, number=KARINA):
    return client.post('/api/session/phone/code', json={'phone': number})


def confirm(client, code, number=KARINA):
    return client.post('/api/session/phone/verify', json={'phone': number, 'code': code})


def wrong(code):
    return f'{(int(code) + 1) % 1_000_000:06d}'


# ── Номер ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw', ['+998901234567', '+998 90 123 45 67', '998901234567', '901234567',
                                 '90 123-45-67', '(90) 123 45 67', '+998 (90) 123-45-67', '00998901234567'])
def test_any_way_of_typing_the_number_gives_one_phone(raw):
    assert normalize_phone(raw) == '+998901234567'


@pytest.mark.parametrize('raw', ['', '+998 90 123 45', '+7 999 123 45 67', '89011234567', '9989012345678',
                                 '+998 90 123 45 678', 'abc', None])
def test_incomplete_or_foreign_number_is_refused(raw):
    assert normalize_phone(raw) is None


def test_number_is_shown_in_groups():
    assert display_phone('+998901234567') == '+998 90 123 45 67'


def test_phone_users_bind_numbers_to_existing_manager_logins():
    users = parse_phone_users('+998 90 123 45 67=karina; 935550011=oshxona;998935550012=karina', USERS)
    assert users == {'+998901234567': 'karina', '+998935550011': 'oshxona', '+998935550012': 'karina'}


@pytest.mark.parametrize('value, problem', [
    ('+998901234567=nobody', 'нет среди менеджеров'),        # номер не заводит учётную запись
    ('+998901234567=accountant', 'нет среди менеджеров'),    # бухгалтер входит по паролю
    ('+998901234567=karina;998901234567=oshxona', 'дважды'),  # один номер — одна запись
    ('+99890123=karina', 'не номер'),
    ('+998901234567', 'пары'),
])
def test_phone_users_are_validated_on_start(value, problem):
    with pytest.raises(ValueError, match=problem):
        parse_phone_users(value, USERS)


# ── Настройки SMS ───────────────────────────────────────────────────────────

def test_sms_is_off_without_a_provider():
    assert parse_sms_config({}) is None


def test_eskiz_needs_its_credentials():
    with pytest.raises(ValueError, match='ESKIZ_EMAIL'):
        parse_sms_config({'SMS_PROVIDER': 'eskiz'})
    config = parse_sms_config({'SMS_PROVIDER': 'eskiz', 'ESKIZ_EMAIL': 'retro@example.com',
                               'ESKIZ_PASSWORD': 'eskiz-pass'})
    assert (config.provider, config.sender) == ('eskiz', '4546')
    assert '{code}' in config.template
    # Пароль Eskiz не печатается вместе с настройками.
    assert 'eskiz-pass' not in repr(config)


def test_console_sms_is_refused_on_hosting():
    # Консоль пишет код в лог: на Railway это раздача входа всем, кто читает лог.
    with pytest.raises(ValueError, match='только для стенда'):
        parse_sms_config({'SMS_PROVIDER': 'console', 'RAILWAY_ENVIRONMENT': 'production'})
    assert parse_sms_config({'SMS_PROVIDER': 'console'}).provider == 'console'


@pytest.mark.parametrize('template', ['Код входа', 'Код {code} и ещё {code}', 'Код {code} {name}'])
def test_sms_template_must_hold_the_code_once(template):
    with pytest.raises(ValueError, match='SMS_TEMPLATE'):
        parse_sms_config({'SMS_PROVIDER': 'console', 'SMS_TEMPLATE': template})


def test_settings_read_phone_login_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv('DASHBOARD_PANEL_USERS', ';'.join(
        f'{login}:{password}:{role}' for login, (password, role) in USERS.items()))
    monkeypatch.setenv('DASHBOARD_PHONE_USERS', '+998 90 123 45 67=karina')
    monkeypatch.setenv('SMS_PROVIDER', 'console')
    monkeypatch.setenv('RETRO_DATA_DIR', str(tmp_path))
    for name in list(__import__('os').environ):
        if name.startswith('RAILWAY_'):
            monkeypatch.delenv(name)
    loaded = Settings.from_env()
    assert loaded.phone_users == {'+998901234567': 'karina'}
    assert loaded.phone_login_configured


# ── Поток входа ─────────────────────────────────────────────────────────────

def test_number_then_code_opens_the_manager_cabinet(phone, sms):
    sent = ask(phone, '90 123 45 67')
    assert sent.status_code == 200
    assert sent.json() == {'phone': '+998 90 123 45 67', 'sent': True, 'resend_in': 60, 'expires_in': 300}
    assert [number for number, _ in sms.sent] == [KARINA]
    assert 'Retro Milliy' in sms.sent[0][1]

    login = confirm(phone, sms.code())
    assert login.status_code == 200
    assert login.json() == {'role': 'manager', 'path': '/manager'}
    cookie = login.headers['set-cookie']
    assert 'retro_session=' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    # Сохранённая сессия на телефоне — 30 дней, а не 12 часов входа по паролю.
    assert 'Max-Age=2592000' in cookie

    assert phone.get('/manager').status_code == 200
    home = phone.get('/api/manager/home').json()
    assert home['login'] == 'karina'
    # Учётная запись менеджера — та же, что по паролю: финансы закрыты.
    assert phone.get('/accountant').status_code == 403
    assert phone.get('/api/accountant/day').status_code == 403
    assert [row['id'] for row in phone.get('/api/config').json()['modules']] == ['manager']


def test_number_finds_the_existing_account_and_never_creates_one(phone, sms):
    users_before = dict(phone.app.state.settings.dashboard_panel_users)
    ask(phone, OSHXONA)
    assert confirm(phone, sms.code(), OSHXONA).status_code == 200
    assert phone.get('/api/manager/home').json()['login'] == 'oshxona'
    assert phone.app.state.settings.dashboard_panel_users == users_before


def test_code_is_single_use(phone, sms):
    ask(phone)
    code = sms.code()
    assert confirm(phone, code).status_code == 200
    phone.cookies.clear()
    again = confirm(phone, code)
    assert again.status_code == 400
    assert again.json()['code'] == 'no_code'
    assert phone.get('/manager').status_code == 303


def test_wrong_code_tells_how_many_attempts_are_left(phone, sms):
    ask(phone)
    code = sms.code()
    answers = [confirm(phone, wrong(code)) for _ in range(MAX_ATTEMPTS)]
    assert [answer.status_code for answer in answers] == [401] * MAX_ATTEMPTS
    assert answers[0].json() == {'detail': 'Неверный код. Осталось попыток: 4.', 'code': 'wrong',
                                 'attempts_left': 4}
    assert answers[-1].json()['detail'] == 'Неверный код. Попытки закончились — запросите новый код.'
    assert answers[-1].json()['attempts_left'] == 0
    # После пятой ошибки и верный код не открывает: только новый код.
    late = confirm(phone, code)
    assert late.status_code == 429
    assert late.json()['code'] == 'attempts'
    assert 'retro_session' not in phone.cookies


def test_code_expires_after_five_minutes(phone, sms, clock):
    ask(phone)
    clock.advance(minutes=5)
    expired = confirm(phone, sms.code())
    assert expired.status_code == 410
    assert expired.json() == {'detail': 'Код истёк — запросите новый.', 'code': 'expired'}
    assert 'retro_session' not in phone.cookies


def test_code_still_works_just_before_expiry(phone, sms, clock):
    ask(phone)
    clock.advance(minutes=4, seconds=59)
    assert confirm(phone, sms.code()).status_code == 200


def test_resend_waits_a_minute_and_replaces_the_old_code(phone, sms, clock):
    ask(phone)
    first = sms.code()
    clock.advance(seconds=20)
    early = ask(phone)
    # Код ещё действует: экран снова показывает ввод кода, новой SMS нет.
    assert early.status_code == 200
    assert early.json() == {'phone': '+998 90 123 45 67', 'sent': False, 'resend_in': 40, 'expires_in': 280}
    assert len(sms.sent) == 1

    clock.advance(seconds=40)
    again = ask(phone)
    assert again.json()['sent'] is True
    assert len(sms.sent) == 2
    second = sms.code()
    # Действует последний код; прежний больше не открывает.
    if second != first:
        assert confirm(phone, first).status_code == 401
    assert confirm(phone, second).status_code == 200


def test_resend_after_spent_attempts_waits_for_the_minute(phone, sms, clock):
    ask(phone)
    for _ in range(MAX_ATTEMPTS):
        confirm(phone, wrong(sms.code()))
    clock.advance(seconds=30)
    early = ask(phone)
    assert early.status_code == 429
    assert early.json() == {'detail': 'Новый код можно запросить через 30 с.', 'code': 'cooldown',
                            'retry_after': 30}
    assert early.headers['retry-after'] == '30'
    clock.advance(seconds=30)
    assert ask(phone).json()['sent'] is True
    assert confirm(phone, sms.code()).status_code == 200


def test_number_gets_at_most_five_sms_an_hour(phone, sms, clock):
    for _ in range(PHONE_SENDS_PER_HOUR):
        assert ask(phone).json()['sent'] is True
        clock.advance(seconds=61)
    blocked = ask(phone)
    assert blocked.status_code == 429
    assert blocked.json()['code'] == 'phone_limit'
    assert blocked.json()['detail'] == 'Слишком много SMS на этот номер. Попробуйте через 55 мин.'
    assert len(sms.sent) == PHONE_SENDS_PER_HOUR
    # Час с первой SMS прошёл — можно снова.
    clock.advance(minutes=55)
    assert ask(phone).json()['sent'] is True


def test_unknown_number_gets_no_sms_and_a_clear_answer(phone, sms):
    answer = ask(phone, '+998 97 000 00 00')
    assert answer.status_code == 404
    assert answer.json() == {'detail': 'Этот номер не подключён к панели. Обратитесь к администратору.',
                             'code': 'unknown'}
    assert sms.sent == []
    assert confirm(phone, '123456', '+998970000000').status_code == 400


def test_one_device_cannot_walk_through_numbers(phone, sms):
    for index in range(ADDRESS_REQUESTS_PER_HOUR):
        assert ask(phone, f'+998 97 000 {index:02d} 00').status_code == 404
    blocked = ask(phone)
    assert blocked.status_code == 429
    assert blocked.json()['code'] == 'address_limit'
    # Лимит на адрес бережёт и баланс: знакомому номеру тоже ничего не ушло.
    assert sms.sent == []


def test_bad_number_and_bad_code_are_refused_before_any_work(phone, sms):
    assert ask(phone, '+998 90 12').json()['code'] == 'bad_phone'
    ask(phone)
    for code in ('12345', '1234567', 'abcdef', '١٢٣٤٥٦'):
        assert confirm(phone, code).json()['code'] == 'bad_code'
    # Неправильный формат попытку не тратит.
    assert confirm(phone, sms.code()).status_code == 200


def test_undelivered_sms_gives_no_session_and_can_be_retried(phone, sms):
    sms.fail = 'rejected'
    failed = ask(phone)
    assert failed.status_code == 502
    assert failed.json() == {'detail': 'SMS не отправлено, попробуйте позже.', 'code': 'not_sent'}
    assert confirm(phone, '000000').json()['code'] == 'no_code'
    assert 'retro_session' not in phone.cookies
    # Неудачная отправка минуту не занимает: можно сразу ещё раз.
    sms.fail = None
    assert ask(phone).json()['sent'] is True
    assert confirm(phone, sms.code()).status_code == 200


def test_the_code_is_kept_only_as_a_fingerprint(tmp_path, sms, clock):
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        ask(client)
    code = sms.code()
    with sqlite3.connect(tmp_path / 'phone-login.sqlite3') as connection:
        rows = connection.execute('SELECT * FROM phone_login_codes').fetchall()
    assert rows
    assert code not in json.dumps(rows, default=str)


def test_unknown_numbers_are_not_stored(tmp_path, sms, clock):
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        ask(client, '+998 97 111 22 33')
    with sqlite3.connect(tmp_path / 'phone-login.sqlite3') as connection:
        rows = connection.execute('SELECT phone FROM phone_login_codes').fetchall()
    assert rows == [('',)]


# ── Сохранённая сессия ──────────────────────────────────────────────────────

def signed_in(tmp_path, sms, clock):
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        ask(client)
        confirm(client, sms.code())
        return client.cookies['retro_session']


def test_saved_session_opens_the_cabinet_without_a_code_after_restart(tmp_path, sms, clock):
    cookie = signed_in(tmp_path, sms, clock)
    with open_client(sqlite_app(tmp_path, FakeSms()), clock) as restarted:
        restarted.cookies.set('retro_session', cookie)
        assert restarted.get('/manager').status_code == 200
        assert restarted.get('/login').headers['location'] == '/manager'
    assert len(sms.sent) == 1


def test_logout_ends_the_phone_session(tmp_path, sms, clock):
    cookie = signed_in(tmp_path, sms, clock)
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        client.cookies.set('retro_session', cookie)
        assert client.post('/api/session/logout').status_code == 204
        client.cookies.set('retro_session', cookie)
        page = client.get('/manager')
    assert page.status_code == 303
    assert page.headers['location'] == '/login'


def test_session_ends_after_thirty_days(tmp_path, sms, clock):
    cookie = signed_in(tmp_path, sms, clock)
    path = tmp_path / 'sessions.json'
    saved = json.loads(path.read_text(encoding='utf-8'))
    for row in saved.values():
        assert row['phone'] == KARINA
        row['started'] = (datetime.now(timezone.utc) - timedelta(days=30, seconds=1)).isoformat()
    path.write_text(json.dumps(saved), encoding='utf-8')
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        client.cookies.set('retro_session', cookie)
        assert client.get('/manager').headers['location'] == '/login'


def test_removing_the_number_closes_its_session(tmp_path, sms, clock):
    # Менеджер ушёл: номер сняли из настроек — сессия на его телефоне не живёт
    # ещё месяц, а закрывается со следующим запуском.
    cookie = signed_in(tmp_path, sms, clock)
    app = create_app(settings(tmp_path, phone_users={OSHXONA: 'oshxona'}), sms_client=sms)
    with open_client(app, clock) as client:
        client.cookies.set('retro_session', cookie)
        assert client.get('/manager').status_code == 303
    assert KARINA not in (tmp_path / 'sessions.json').read_text(encoding='utf-8')


def test_password_sessions_are_not_touched_by_phone_checks(tmp_path, sms, clock):
    with open_client(sqlite_app(tmp_path, sms, phone_users={}), clock) as client:
        client.post('/api/session', json={'username': 'karina', 'password': 'secret'})
        assert client.get('/manager').status_code == 200


# ── Экран входа и выключенный вход по номеру ────────────────────────────────

def test_login_screen_offers_the_phone_only_when_it_is_configured(tmp_path, sms, clock):
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        assert 'data-phone-login="on"' in client.get('/login').text
    # Номера есть, SMS-шлюза нет — входа по номеру тоже нет.
    with open_client(sqlite_app(tmp_path, None, sms=None), clock) as client:
        assert 'data-phone-login="off"' in client.get('/login').text
    plain = create_app(settings(tmp_path, phone_users={}, sms=None))
    with open_client(plain, clock) as client:
        assert 'data-phone-login="off"' in client.get('/login').text
        off = ask(client)
        assert off.status_code == 503
        assert off.json()['code'] == 'off'
        # Вход по логину и паролю — как был.
        assert client.post('/api/session', json={'username': 'accountant', 'password': 'secret'}
                           ).json() == {'role': 'accountant', 'path': '/accountant'}


def test_phone_login_pages_are_reachable_before_sign_in(tmp_path, sms, clock):
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        assert client.get('/static/login-logic.js').status_code == 200
        assert ask(client).status_code == 200


def test_foreign_site_cannot_ask_for_codes(tmp_path, sms, clock):
    with open_client(sqlite_app(tmp_path, sms), clock) as client:
        answer = client.post('/api/session/phone/code', json={'phone': KARINA},
                             headers={'Origin': 'https://evil.example'})
    assert answer.status_code == 403
    assert sms.sent == []


# ── SMS-шлюзы ───────────────────────────────────────────────────────────────

ESKIZ = SmsConfig(provider='eskiz', email='retro@example.com', password='eskiz-pass', sender='4546')


class EskizStub:
    """notify.eskiz.uz в памяти: вход, отправка, истёкший токен и сбои."""

    def __init__(self):
        self.calls = []
        self.tokens = iter(['tok-1', 'tok-2', 'tok-3'])
        self.valid = set()
        self.send_status = 200
        self.expire_next = False

    def __call__(self, request: httpx.Request):
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        self.calls.append((request.url.path, form, request.headers.get('authorization')))
        if request.url.path == '/api/auth/login':
            if form != {'email': 'retro@example.com', 'password': 'eskiz-pass'}:
                return httpx.Response(401, json={'message': 'Unauthorized'})
            token = next(self.tokens)
            self.valid.add(token)
            return httpx.Response(200, json={'message': 'token_generated', 'data': {'token': token}})
        if request.url.path == '/api/message/sms/send':
            token = (request.headers.get('authorization') or '').removeprefix('Bearer ')
            if self.expire_next:
                self.expire_next = False
                self.valid.discard(token)
            if token not in self.valid:
                return httpx.Response(401, json={'message': 'Expired'})
            if self.send_status != 200:
                return httpx.Response(self.send_status, json={'message': 'Шаблон не прошёл модерацию'})
            return httpx.Response(200, json={'id': str(uuid4()), 'message': 'Waiting for SMS provider',
                                             'status': 'waiting'})
        return httpx.Response(404)


def run(coroutine):
    return asyncio.run(coroutine)


def test_eskiz_logs_in_once_and_sends_the_text():
    stub = EskizStub()
    client = EskizSms(ESKIZ, transport=httpx.MockTransport(stub))

    async def scenario():
        await client.send('+998901234567', 'Код 123456')
        await client.send('+998901234567', 'Код 654321')
        await client.close()

    run(scenario())
    paths = [path for path, _, _ in stub.calls]
    assert paths == ['/api/auth/login', '/api/message/sms/send', '/api/message/sms/send']
    _, form, auth = stub.calls[1]
    assert form == {'mobile_phone': '998901234567', 'message': 'Код 123456', 'from': '4546'}
    assert auth == 'Bearer tok-1'


def test_eskiz_renews_an_expired_token_once():
    stub = EskizStub()
    client = EskizSms(ESKIZ, transport=httpx.MockTransport(stub))

    async def scenario():
        await client.send('+998901234567', 'Код 111111')
        stub.expire_next = True
        await client.send('+998901234567', 'Код 222222')
        await client.close()

    run(scenario())
    assert [path for path, _, _ in stub.calls] == [
        '/api/auth/login', '/api/message/sms/send', '/api/message/sms/send',
        '/api/auth/login', '/api/message/sms/send']
    assert stub.calls[-1][2] == 'Bearer tok-2'


@pytest.mark.parametrize('status, code', [(400, 'rejected'), (402, 'rejected'), (500, 'unavailable'),
                                          (403, 'unauthorized')])
def test_eskiz_refusals_become_categories(status, code):
    stub = EskizStub()
    stub.send_status = status
    client = EskizSms(ESKIZ, transport=httpx.MockTransport(stub))

    async def scenario():
        try:
            await client.send('+998901234567', 'Код 123456')
        finally:
            await client.close()

    with pytest.raises(SmsError) as caught:
        run(scenario())
    assert caught.value.code == code


def test_eskiz_with_wrong_credentials_is_unauthorized():
    stub = EskizStub()
    config = SmsConfig(provider='eskiz', email='retro@example.com', password='wrong')
    client = EskizSms(config, transport=httpx.MockTransport(stub))

    async def scenario():
        try:
            await client.send('+998901234567', 'Код 123456')
        finally:
            await client.close()

    with pytest.raises(SmsError) as caught:
        run(scenario())
    assert caught.value.code == 'unauthorized'
    assert [path for path, _, _ in stub.calls] == ['/api/auth/login']


@pytest.mark.parametrize('failure, code', [(httpx.ConnectTimeout('slow'), 'timeout'),
                                           (httpx.ConnectError('down'), 'network')])
def test_eskiz_network_failures_are_categorized(failure, code):
    def broken(request):
        raise failure
    client = EskizSms(ESKIZ, transport=httpx.MockTransport(broken))

    async def scenario():
        try:
            await client.send('+998901234567', 'Код 123456')
        finally:
            await client.close()

    with pytest.raises(SmsError) as caught:
        run(scenario())
    assert caught.value.code == code


def test_sms_sender_follows_the_settings():
    assert sms_sender(None) is None
    assert isinstance(sms_sender(SmsConfig(provider='console')), ConsoleSms)
    assert isinstance(sms_sender(ESKIZ), EskizSms)


def test_codes_never_reach_the_log_through_eskiz(tmp_path, clock, caplog):
    # Боевой путь: Eskiz отказал — в логе категория, но не текст и не код.
    stub = EskizStub()
    stub.send_status = 400
    eskiz = EskizSms(ESKIZ, transport=httpx.MockTransport(stub))
    app = sqlite_app(tmp_path, eskiz, sms=ESKIZ)
    caplog.set_level(logging.DEBUG)
    with open_client(app, clock) as client:
        assert ask(client).status_code == 502
    sent = [form for path, form, _ in stub.calls if path == '/api/message/sms/send']
    code = re.search(r'\b(\d{6})\b', sent[0]['message']).group(1)
    assert code not in caplog.text
    assert 'eskiz-pass' not in caplog.text
    assert 'send_code:rejected' in caplog.text


def test_console_sms_writes_the_code_only_to_the_server_log(caplog):
    caplog.set_level(logging.WARNING, logger='retro.sms')
    run(ConsoleSms().send('+998901234567', 'Код 123456'))
    assert '123456' in caplog.text and 'СТЕНД' in caplog.text


def test_attempts_and_single_use_hold_even_for_parallel_requests(tmp_path):
    # Два одновременных ввода: попытка занимается до сравнения, а сессию
    # выдаёт только тот, кто первым погасил код.
    from retro.phone_login import PhoneLoginStore
    store = PhoneLoginStore(tmp_path / 'phone-login.sqlite3')
    result = store.reserve(phone=KARINA, address='127.0.0.1', now=START, salt='00', code_hash='x')
    assert result[0] == 'new'
    store.mark_sent(result[1], KARINA)
    assert [store.take_attempt(result[1]) for _ in range(MAX_ATTEMPTS + 1)] == [1, 2, 3, 4, 5, None]
    again = store.reserve(phone=KARINA, address='127.0.0.1', now=START + timedelta(minutes=1),
                          salt='00', code_hash='y')
    store.mark_sent(again[1], KARINA)
    assert store.use(again[1], START) is True
    assert store.use(again[1], START) is False
