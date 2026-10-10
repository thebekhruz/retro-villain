"""Кабинет менеджера (ТЗ 09.10, М-01…М-04, T-432).

Менеджер с телефона заводит сменного или временного сотрудника своего
направления, система сама добавляет его в Hikvision. Повтор (двойной тап,
обрыв связи, сбой устройства) не создаёт второго человека ни в реестре, ни на
устройстве. Имена — только кириллицей, тёзки не объединяются сами.
На SQLite и на Postgres (RETRO_TEST_POSTGRES_URL), как в CI.
"""

import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from test_accountant_design_parity import POSTGRES_URL
from retro.config import Settings, parse_dashboard_panel_users
from retro.integrations.hikvision import HikvisionError, HikvisionEvent, HikvisionPerson
from retro.modules.accountant.hikvision import AttendanceStore
from retro.modules.accountant.names import person_name, role_name, similar_names
from retro.modules.accountant.roster import RosterStore
from retro.modules.cashier.service import TZ
from retro.modules.manager.directions import parse_manager_directions

USERS = {'cashier': ('secret', 'cashier'), 'accountant': ('secret', 'accountant'),
         'director': ('secret', 'director'), 'founder': ('secret', 'founder'),
         'boss': ('secret', 'admin'),
         'karina': ('secret', 'manager'), 'kitchen': ('secret', 'manager')}
# karina — без строки направлений, то есть все; kitchen — только кухня.
DIRECTIONS = {'kitchen': ('Кухня',)}


class FakeDevice:
    """Hikvision в памяти: люди по номеру, счётчик созданий и сбои по заказу."""

    def __init__(self, people=None):
        self.people = dict(people or {})
        self.creates = 0
        self.fail = None          # код HikvisionError на любой запрос
        self.lose_response = False  # человек создан, но ответ потерян

    async def find_person(self, employee_no):
        if self.fail:
            raise HikvisionError(self.fail)
        name = self.people.get(employee_no)
        return HikvisionPerson(employee_no, name) if name is not None else None

    async def create_person(self, employee_no, name, *, valid_from):
        if self.fail:
            raise HikvisionError(self.fail)
        self.creates += 1
        if employee_no in self.people:
            return 'exists'
        self.people[employee_no] = name
        if self.lose_response:
            self.lose_response = False
            raise HikvisionError('timeout')
        return 'created'

    async def fetch_people(self):
        if self.fail:
            raise HikvisionError(self.fail)
        return tuple(HikvisionPerson(number, name) for number, name in self.people.items())

    async def close(self):
        pass


def settings(tmp_path, **extra):
    return Settings(data_dir=tmp_path, dashboard_panel_users=USERS, manager_directions=DIRECTIONS, **extra)


def sqlite_app(tmp_path, device):
    return create_app(settings(tmp_path), expense_db_path=tmp_path / 'cashier.sqlite3',
                      accountant_db_path=tmp_path / 'accountant.sqlite3',
                      director_db_path=tmp_path / 'director.sqlite3',
                      founder_db_path=tmp_path / 'founder.sqlite3', hikvision_writer=device)


@contextmanager
def postgres_app(tmp_path, device):
    import psycopg
    from psycopg import sql
    schema = 'manager_' + uuid4().hex
    admin = psycopg.connect(POSTGRES_URL, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    parts = urlsplit(POSTGRES_URL)
    query = dict(parse_qsl(parts.query))
    query['options'] = '-csearch_path=' + schema
    url = urlunsplit(parts._replace(query=urlencode(query)))
    try:
        yield create_app(settings(tmp_path, database_url=url), hikvision_writer=device)
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def device():
    return FakeDevice()


@pytest.fixture(params=['sqlite', 'postgres'])
def cabinet(request, tmp_path, device):
    if request.param == 'postgres':
        if not POSTGRES_URL:
            pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
        with postgres_app(tmp_path, device) as app, TestClient(
                app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
            yield client
    else:
        with TestClient(sqlite_app(tmp_path, device), base_url='http://127.0.0.1',
                        client=('127.0.0.1', 50000)) as client:
            yield client


def karamat(**extra):
    return {'name': 'Карамат', 'role': 'Хостес', 'direction': 'Зал', 'employment_type': 'temporary',
            'request_key': str(uuid4()), **extra}


def create(client, body, user='karina'):
    return client.post('/api/manager/employees', json=body, auth=(user, 'secret'))


def push(client, employee_id, user='karina'):
    return client.post(f'/api/manager/employees/{employee_id}/hikvision', auth=(user, 'secret'))


def roster_rows(client):
    return [row for row in client.app.state.accountant_roster.list()]


# ── Кириллица (М-04) ────────────────────────────────────────────────────────

@pytest.mark.parametrize('value, expected', [
    ('Карамат', 'Карамат'),
    ('  Абдулганиева   Сельвина ', 'Абдулганиева Сельвина'),
    ('Ўткир Қодиров', 'Ўткир Қодиров'),
    ('Ғайрат Ҳамидов', 'Ғайрат Ҳамидов'),
    ('Фёдор Анна-Мария', 'Фёдор Анна-Мария'),
])
def test_cyrillic_names_with_uzbek_letters_pass(value, expected):
    assert person_name(value) == expected


@pytest.mark.parametrize('value, message', [
    ('Karamat', 'Имя набрано латиницей — наберите кириллицей.'),
    ('Карамat', 'В имени «Карамat» латинская «a» вместо кириллической «а» — наберите кириллицей.'),
    ('Kарамат', 'В имени «Kарамат» латинская «K» вместо кириллической «К» — наберите кириллицей.'),
    ('Каrамат', 'В имени «Каrамат» латинская буква «r» — наберите кириллицей.'),
    ('Сотрудник 1', 'В имени «Сотрудник 1» недопустимый знак «1»: можно буквы кириллицы, пробел и дефис.'),
    ('Іван', 'В имени «Іван» буква «І» не из русской или узбекской кириллицы.'),
    ('', 'Укажите имя сотрудника.'),
])
def test_latin_and_mixed_names_are_rejected_with_the_letter(value, message):
    with pytest.raises(ValueError) as caught:
        person_name(value)
    assert str(caught.value) == message


def test_role_allows_digits_but_not_latin():
    assert role_name('Повар 2-го цеха (тандыр)') == 'Повар 2-го цеха (тандыр)'
    with pytest.raises(ValueError, match='латинская «a» вместо кириллической «а»'):
        role_name('Пов' + 'a' + 'р')


@pytest.mark.parametrize('first, second', [
    ('Каримов Жахонгир', 'Jahongir Karimov'),
    ('Баходиров Ихтиер', 'Bahodirov Ixtiyor'),
    ('Баходиров Ихтиёр', 'Баходиров Ихтиер'),
    ('Карамат', 'Kарамат Юсупова'),
    ('Абдулганиева Сельвина', 'abdulganieva selvina'),
    ('Қодиров Ўткир', 'Кодиров Уткир'),
])
def test_possible_duplicates_survive_alphabet_order_and_spelling(first, second):
    assert similar_names(first, second)


def test_different_people_are_not_similar():
    assert not similar_names('Каримов Жахонгир', 'Каримов Алишер')
    assert not similar_names('Карамат', 'Камола')


# ── Роль и направления ─────────────────────────────────────────────────────

def test_manager_is_an_optional_role_and_directions_are_validated():
    users = parse_dashboard_panel_users(
        'c:p:cashier;a:p:accountant;d:p:director;f:p:founder;m1:p:manager;m2:p:manager')
    assert users['m1'] == ('p', 'manager')
    assert parse_manager_directions('m1=Кухня,Уборка;m2=Зал', users) == {
        'm1': ('Кухня', 'Уборка'), 'm2': ('Зал',)}
    assert parse_manager_directions('', users) == {}
    for broken in ('m1=Бухгалтерия', 'a=Кухня', 'm1', 'm1=Кухня,Кухня', 'чужой=Зал'):
        with pytest.raises(ValueError):
            parse_manager_directions(broken, users)


def test_home_lists_own_directions_roles_and_no_money(cabinet):
    home = cabinet.get('/api/manager/home', auth=('kitchen', 'secret')).json()
    assert [item['name'] for item in home['directions']] == ['Кухня']
    assert 'Повар' in home['directions'][0]['roles']
    every = cabinet.get('/api/manager/home', auth=('karina', 'secret')).json()
    assert [item['name'] for item in every['directions']] == ['Кухня', 'Зал', 'Уборка']
    assert every['hikvision'] == {'configured': True}


def test_manager_cannot_create_in_a_foreign_direction(cabinet):
    answer = create(cabinet, karamat(), user='kitchen')
    assert answer.status_code == 403
    assert answer.json()['detail'] == 'Это направление ведёт другой менеджер.'
    answer = create(cabinet, karamat(direction='Кухня', role='Официант'), user='kitchen')
    assert answer.status_code == 422
    assert 'из направления «Зал»' in answer.json()['detail']
    assert roster_rows(cabinet) == []


# ── Сквозной сценарий: Карамат, временная хостес (М-02, приёмка п. 5, 8) ────

def test_karamat_is_registered_once_and_added_to_hikvision(cabinet, device):
    body = karamat()
    answer = create(cabinet, body)
    assert answer.status_code == 201, answer.text
    card = answer.json()['employee']
    assert (card['name'], card['role'], card['group'], card['direction']) == (
        'Карамат', 'Хостес', 'Встреча гостей', 'Зал')
    assert card['employment_label'] == 'Временный'
    assert card['hikvision']['state'] == 'pending'
    assert card['can_retry'] is True
    assert 'rate' not in card

    sent = push(cabinet, card['id']).json()['employee']
    assert sent['hikvision']['state'] == 'sent'
    number = sent['hikvision']['employee_no']
    assert device.people == {number: 'Карамат'}

    # Бухгалтер видит её в реестре без ставки, привязанной к устройству.
    person, = roster_rows(cabinet)
    assert (person.name, person.rate, person.group_name, person.hikvision_id) == (
        'Карамат', None, 'Встреча гостей', number)
    assert (person.employment_type, person.direction, person.created_by) == ('temporary', 'Зал', 'karina')
    staff = cabinet.get('/api/accountant/staff', params={'date': '2026-10-09'}, auth=('accountant', 'secret'))
    assert staff.status_code == 200
    assert [row['name'] for row in staff.json()['employees']] == ['Карамат']
    history = cabinet.app.state.accountant_roster.history(person.id)
    assert [row['action'] for row in history] == ['hikvision', 'create']
    assert history[1]['changed_by'] == 'karina'

    # Повторное «Сохранить» с тем же ключом (двойной тап, обрыв связи).
    again = create(cabinet, body)
    assert again.status_code == 200
    assert again.json()['created'] is False
    assert again.json()['employee']['id'] == card['id']
    # Повторная отправка уже отправленной — не трогает устройство.
    assert push(cabinet, card['id']).json()['employee']['hikvision']['state'] == 'sent'
    assert len(roster_rows(cabinet)) == 1
    assert device.creates == 1
    mine = cabinet.get('/api/manager/home', auth=('karina', 'secret')).json()['mine']
    assert [item['name'] for item in mine] == ['Карамат']


def test_device_down_keeps_the_card_and_retry_adds_exactly_one_person(cabinet, device):
    device.fail = 'network'
    card = create(cabinet, karamat()).json()['employee']
    failed = push(cabinet, card['id']).json()['employee']
    assert failed['hikvision']['state'] == 'error'
    assert failed['hikvision']['message'] == 'Hikvision недоступен — карточка сохранена и ждёт отправки.'
    assert failed['can_retry'] is True
    assert roster_rows(cabinet)[0].hikvision_id is None
    assert device.people == {}

    device.fail = None
    sent = push(cabinet, card['id']).json()['employee']
    assert sent['hikvision']['state'] == 'sent'
    assert list(device.people.values()) == ['Карамат']


def test_lost_device_answer_is_found_by_number_not_created_twice(cabinet, device):
    device.lose_response = True
    card = create(cabinet, karamat()).json()['employee']
    failed = push(cabinet, card['id']).json()['employee']
    assert failed['hikvision']['state'] == 'error'
    assert failed['hikvision']['error'] == 'timeout'
    # Устройство человека всё-таки записало; повтор находит его по номеру.
    sent = push(cabinet, card['id']).json()['employee']
    assert sent['hikvision']['state'] == 'sent'
    assert sent['hikvision']['employee_no'] == failed['hikvision']['employee_no']
    assert device.creates == 1
    assert len(device.people) == 1


def test_number_taken_by_a_stranger_on_the_device_gets_a_new_number(cabinet, device):
    card = create(cabinet, karamat()).json()['employee']
    number = card['hikvision']['employee_no']
    device.people[number] = 'Старый Сотрудник'
    device.people['57'] = 'Ещё Один'
    sent = push(cabinet, card['id']).json()['employee']
    assert sent['hikvision']['state'] == 'sent'
    assert sent['hikvision']['employee_no'] == '58'
    assert device.people[number] == 'Старый Сотрудник'
    assert device.people['58'] == 'Карамат'


def test_without_hikvision_the_card_waits_and_says_why(tmp_path):
    with TestClient(sqlite_app(tmp_path, None), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 50000)) as client:
        card = create(client, karamat()).json()['employee']
        waiting = push(client, card['id']).json()['employee']
        assert waiting['hikvision']['state'] == 'pending'
        assert waiting['hikvision']['message'] == 'Hikvision не подключён — карточка сохранена и ждёт отправки.'
        assert client.get('/api/manager/home', auth=('karina', 'secret')).json()['hikvision'] == {
            'configured': False}


def test_only_the_author_can_resend(cabinet):
    card = create(cabinet, karamat()).json()['employee']
    other = cabinet.get(f'/api/manager/employees/{card["id"]}', auth=('kitchen', 'secret')).json()
    assert other['employee']['mine'] is False and other['employee']['can_retry'] is False
    assert push(cabinet, card['id'], user='kitchen').status_code == 403
    assert push(cabinet, card['id'], user='boss').status_code == 200


# ── Кириллица и тёзки в API (М-04, приёмка п. 10) ──────────────────────────

def test_latin_name_is_refused_and_nothing_is_saved(cabinet, device):
    answer = create(cabinet, karamat(name='Karamat'))
    assert answer.status_code == 422
    assert answer.json()['detail'] == 'Имя набрано латиницей — наберите кириллицей.'
    answer = create(cabinet, karamat(name='Карамat'))
    assert answer.status_code == 422
    assert 'латинская «a» вместо кириллической «а»' in answer.json()['detail']
    answer = create(cabinet, karamat(role='Hostess'))
    assert answer.status_code == 422
    assert answer.json()['detail'] == 'Должность набрана латиницей — наберите кириллицей.'
    assert roster_rows(cabinet) == [] and device.people == {}


def test_namesake_is_shown_and_saved_only_after_explicit_confirmation(cabinet):
    roster = cabinet.app.state.accountant_roster
    old = roster.add(name='Karamat Yusupova', role='хостес', rate='150000', group_name='Встреча гостей')
    roster.add_monthly(name='Баходиров Ихтиёр', role='Менеджер', salary='5000000')
    body = karamat()
    answer = create(cabinet, body)
    assert answer.status_code == 409
    matches = answer.json()['matches']
    assert [(item['id'], item['name']) for item in matches] == [(old.id, 'Karamat Yusupova')]
    assert 'rate' not in matches[0]
    assert len(roster_rows(cabinet)) == 1

    answer = create(cabinet, {**body, 'confirm_new': True})
    assert answer.status_code == 201
    assert sorted(person.name for person in roster_rows(cabinet)) == ['Karamat Yusupova', 'Карамат']

    # Окладник тоже в общей базе: совпадение покажем, денег — нет.
    answer = create(cabinet, karamat(name='Баходиров Ихтиер', role='Официант', employment_type='shift'))
    assert answer.status_code == 409
    assert answer.json()['matches'][0]['kind'] == 'monthly'
    assert answer.json()['matches'][0]['group'] == 'На окладе'


def test_search_finds_latin_spelling_and_hikvision_number_without_money(cabinet):
    roster = cabinet.app.state.accountant_roster
    person = roster.add(name='Абдулганиева Сельвина', role='Хостес', rate='360000', group_name='Встреча гостей')
    roster.set_hikvision_id(person.id, '204')
    for query in ('селвина', 'Selvina', 'хостес', '204'):
        results = cabinet.get('/api/manager/search', params={'q': query}, auth=('karina', 'secret')).json()['results']
        assert [item['name'] for item in results] == ['Абдулганиева Сельвина'], query
        assert 'rate' not in results[0]
        assert results[0]['hikvision']['state'] == 'sent'
    assert cabinet.get('/api/manager/search', params={'q': 'Жасур'},
                       auth=('karina', 'secret')).json()['results'] == []
    assert cabinet.get('/api/manager/search', params={'q': 'с'},
                       auth=('karina', 'secret')).json()['results'] == []


def test_accountant_input_is_cyrillic_but_old_names_stay(cabinet):
    roster = cabinet.app.state.accountant_roster
    old = roster.add(name='Karimov Jahongir', role='менеджер', rate='360000', group_name='Управление')
    auth = ('accountant', 'secret')
    answer = cabinet.post('/api/accountant/employees', auth=auth, json={
        'name': 'Selvina', 'role': 'хостес', 'rate': '360000', 'group': 'Встреча гостей'})
    assert answer.status_code == 422
    assert answer.json()['detail'] == 'Имя набрано латиницей — наберите кириллицей.'
    # Ставку старой карточки с латинским именем можно менять: имя не трогали.
    answer = cabinet.patch(f'/api/accountant/employees/{old.id}', auth=auth, json={
        'name': 'Karimov Jahongir', 'role': 'менеджер', 'rate': '370000', 'reason': 'Повышение'})
    assert answer.status_code == 200, answer.text
    # Переименование — уже кириллицей.
    answer = cabinet.patch(f'/api/accountant/employees/{old.id}', auth=auth, json={
        'name': 'Каримов Жахонгир', 'role': 'менеджер', 'rate': '370000', 'reason': 'Кириллица'})
    assert answer.status_code == 200
    answer = cabinet.patch(f'/api/accountant/employees/{old.id}', auth=auth, json={
        'name': 'Каримов Жахонгиp', 'role': 'менеджер', 'rate': '370000', 'reason': 'Опечатка'})
    assert answer.status_code == 422
    assert roster.list()[0].name == 'Каримов Жахонгир'


# ── Посещаемость и опрос Hikvision (М-03) ──────────────────────────────────

def test_passes_before_confirmation_land_in_the_same_card(cabinet, device):
    card = create(cabinet, karamat()).json()['employee']
    number = card['hikvision']['employee_no']
    store = cabinet.app.state.attendance_store
    entered = datetime(2026, 10, 9, 9, 31, tzinfo=TZ)
    store.ingest(HikvisionEvent('retro-main-entry', 'serial-1', number, entered), None)
    push(cabinet, card['id'])
    first = store.first_entries(entered.date())
    assert first[card['id']].occurred_at == entered


def test_reserved_number_is_never_name_linked_or_imported_as_a_second_card(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster = RosterStore(path)
    AttendanceStore(path)
    employee_id, created = roster.manager_create(
        name='Карамат', role='Хостес', group_name='Встреча гостей', direction='Зал',
        employment_type='temporary', created_by='karina', request_key=str(uuid4()))
    assert created
    number = roster.manager_card(employee_id)['hikvision_employee_no']
    # На устройстве есть тёзка под другим номером: по имени её не привязываем.
    report = roster.link_hikvision_people((HikvisionPerson('900', 'Карамат'),))
    assert report['linked'] == 0
    assert roster.list()[0].hikvision_id is None
    imported = roster.import_hikvision_people((HikvisionPerson(number, 'Карамат'),))
    assert imported['created'] == 0
    assert len(roster.list()) == 1
    # Опрос увидел нашего человека под нашим номером — отправка дошла.
    report = roster.link_hikvision_people((HikvisionPerson(number, 'Карамат'),))
    assert report['linked'] == 1
    assert roster.list()[0].hikvision_id == number
    assert roster.manager_card(employee_id)['hikvision_state'] == 'sent'


def test_numbers_follow_the_largest_known_one(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster, store = RosterStore(path), AttendanceStore(path)
    person = roster.add(name='Баходиров Ихтиер', role='менеджер', rate='360000', group_name='Управление')
    roster.set_hikvision_id(person.id, '120')
    store.ingest(HikvisionEvent('entry', 's-1', '133', datetime(2026, 10, 8, 9, 0, tzinfo=TZ)), None)
    store.ingest(HikvisionEvent('entry', 's-2', '4400123456789', datetime(2026, 10, 8, 9, 1, tzinfo=TZ)), None)
    ids = [roster.manager_create(name=name, role='Хостес', group_name='Встреча гостей', direction='Зал',
                                 employment_type='shift', created_by='karina', request_key=str(uuid4()))[0]
           for name in ('Карамат', 'Камола')]
    assert [roster.manager_card(item)['hikvision_employee_no'] for item in ids] == ['134', '135']


def test_old_roster_gains_the_new_columns_without_losing_rows(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('''CREATE TABLE accountant_employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_row INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL, role TEXT NOT NULL, group_name TEXT NOT NULL, rate TEXT,
            hikvision_id TEXT UNIQUE)''')
        connection.execute("INSERT INTO accountant_employees (source_row, name, role, group_name, rate, hikvision_id) "
                           "VALUES (1, 'Баходиров Ихтиер', 'менеджер', 'Управление', '360000', '7')")
    roster = RosterStore(path)
    RosterStore(path)  # повторный запуск миграции ничего не ломает
    person, = roster.list()
    assert (person.name, str(person.rate), person.hikvision_id, person.employment_type) == (
        'Баходиров Ихтиер', '360000', '7', 'shift')
    assert roster.manager_card(person.id)['created_by'] is None


def test_concurrent_double_tap_creates_one_card(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster = RosterStore(path)
    key = str(uuid4())

    def save():
        return roster.manager_create(name='Карамат', role='Хостес', group_name='Встреча гостей',
                                     direction='Зал', employment_type='temporary',
                                     created_by='karina', request_key=key)

    async def both():
        return await asyncio.gather(asyncio.to_thread(save), asyncio.to_thread(save))

    first, second = asyncio.run(both())
    assert first[0] == second[0]
    assert sorted((first[1], second[1])) == [False, True]
    assert len(roster.list()) == 1


def test_accountant_cannot_hand_a_reserved_number_to_someone_else(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster = RosterStore(path)
    employee_id, _ = roster.manager_create(
        name='Карамат', role='Хостес', group_name='Встреча гостей', direction='Зал',
        employment_type='temporary', created_by='karina', request_key=str(uuid4()))
    number = roster.manager_card(employee_id)['hikvision_employee_no']
    other = roster.add(name='Камола', role='хостес', rate='150000', group_name='Встреча гостей')
    with pytest.raises(ValueError, match='Карамат'):
        roster.set_hikvision_id(other.id, number)
