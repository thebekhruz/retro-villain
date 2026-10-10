"""Кабинет менеджера (ТЗ 09.10, М-01…М-04, T-432).

Карточки заводит бухгалтер. Менеджер видит сменных сотрудников своих
направлений, фотографирует человека, и система отправляет в Hikvision его
самого (если его там ещё нет) и его лицо. Повтор (двойной тап, обрыв связи,
сбой устройства) не создаёт второго человека на устройстве; сбой лица не
отменяет добавленного человека. Устройство — на уровне ISAPI (httpx
MockTransport) за настоящим HikvisionClient. На SQLite и на Postgres
(RETRO_TEST_POSTGRES_URL), как в CI.
"""

import asyncio
import base64
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from test_accountant_design_parity import POSTGRES_URL
from test_hikvision_isapi import CONFIG, JPEG, OK, face_parts
from retro.config import Settings, parse_dashboard_panel_users
from retro.integrations.hikvision import HikvisionClient, HikvisionEvent, HikvisionPerson
from retro.modules.accountant.hikvision import AttendanceStore
from retro.modules.accountant.names import person_name, role_name
from retro.modules.accountant.roster import DeviceNamesakes, RosterStore
from retro.modules.cashier.service import TZ
from retro.modules.manager.directions import parse_manager_directions
from retro.modules.manager.photos import NOT_IMAGE, TOO_LARGE, UNREADABLE
from retro.modules.manager.routes import (FACE_FAILED, FACE_PENDING, FOREIGN, MANUAL_MESSAGE, NO_PHOTO,
                                          NOT_FOUND, PENDING_MESSAGE)

USERS = {'cashier': ('secret', 'cashier'), 'accountant': ('secret', 'accountant'),
         'director': ('secret', 'director'), 'founder': ('secret', 'founder'),
         'boss': ('secret', 'admin'),
         'karina': ('secret', 'manager'), 'kitchen': ('secret', 'manager')}
# karina — без строки направлений, то есть все; kitchen — только кухня.
DIRECTIONS = {'kitchen': ('Кухня',)}
PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(64))
FACE_RECORD = ('POST', '/ISAPI/Intelligent/FDLib/FaceDataRecord')
FACE_SETUP = ('PUT', '/ISAPI/Intelligent/FDLib/FDSetUp')
PERSON_RECORD = ('POST', '/ISAPI/AccessControl/UserInfo/Record')


class Terminal:
    """Hikvision на уровне ISAPI: люди по номеру, лица по FPID, сбои по заказу.

    fail='down' — устройство недоступно на любой запрос; face_fail — сбой
    только у лица ('down' — обрыв, 'reject' — отказ устройства);
    lose_record — человек записан, а ответ на запись потерян."""

    def __init__(self):
        self.people: dict[str, str] = {}
        self.faces: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self.face_records: list[tuple[str, str, tuple]] = []
        self.fail = None
        self.face_fail = None
        self.lose_record = False

    def count(self, call) -> int:
        return self.requests.count(call)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == 'GET':
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="abc", qop="auth"'})
        assert request.headers['authorization'].startswith('Digest ')
        path = request.url.path
        self.requests.append((request.method, path))
        if self.fail == 'down':
            raise httpx.ConnectError('down', request=request)
        if path.startswith('/ISAPI/Intelligent/FDLib/'):
            return self.face(request, path)
        body = json.loads(request.content)
        if path == '/ISAPI/AccessControl/UserInfo/Search':
            cond = body['UserInfoSearchCond']
            wanted = [item['employeeNo'] for item in cond.get('EmployeeNoList', [])]
            people = sorted(self.people.items(), key=lambda item: int(item[0]))
            if wanted:
                people = [item for item in people if item[0] in wanted]
            start, size = cond['searchResultPosition'], cond['maxResults']
            page = people[start:start + size]
            status = 'NO MATCH' if not people else ('MORE' if start + size < len(people) else 'OK')
            return httpx.Response(200, json={'UserInfoSearch': {
                'responseStatusStrg': status, 'numOfMatches': len(page), 'totalMatches': len(people),
                'UserInfo': [{'employeeNo': number, 'name': name} for number, name in page]}})
        if path == '/ISAPI/AccessControl/UserInfo/Record':
            info = body['UserInfo']
            if info['employeeNo'] in self.people:
                return httpx.Response(400, json={'statusCode': 6, 'subStatusCode': 'employeeNoAlreadyExist'})
            self.people[info['employeeNo']] = info['name']
            if self.lose_record:
                self.lose_record = False
                raise httpx.ReadTimeout('lost', request=request)
            return httpx.Response(200, json=OK)
        return httpx.Response(404, json={'statusCode': 4, 'statusString': 'Invalid Operation'})

    def face(self, request: httpx.Request, path: str) -> httpx.Response:
        parts = face_parts(request.content, request.headers['content-type'])
        record = json.loads(parts['FaceDataRecord'][2])
        assert (record['faceLibType'], record['FDID']) == ('blackFD', '1')
        number = record['FPID']
        self.face_records.append((request.method, number, parts['img']))
        if self.face_fail == 'down':
            raise httpx.ConnectError('down', request=request)
        if self.face_fail == 'reject':
            return httpx.Response(500, content=b'')
        if number not in self.people:
            return httpx.Response(400, json={'statusCode': 6, 'subStatusCode': 'employeeNoNotExist'})
        if request.method == 'POST' and number in self.faces:
            return httpx.Response(400, json={'statusCode': 6, 'subStatusCode': 'deviceUserAlreadyExistFace'})
        self.faces[number] = parts['img'][2]
        return httpx.Response(200, json=OK)


def settings(tmp_path, **extra):
    return Settings(data_dir=tmp_path, dashboard_panel_users=USERS, manager_directions=DIRECTIONS, **extra)


def sqlite_app(tmp_path, writer):
    return create_app(settings(tmp_path), expense_db_path=tmp_path / 'cashier.sqlite3',
                      accountant_db_path=tmp_path / 'accountant.sqlite3',
                      director_db_path=tmp_path / 'director.sqlite3',
                      founder_db_path=tmp_path / 'founder.sqlite3', hikvision_writer=writer)


@contextmanager
def postgres_app(tmp_path, writer):
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
        yield create_app(settings(tmp_path, database_url=url), hikvision_writer=writer)
    finally:
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def terminal():
    return Terminal()


@pytest.fixture(params=['sqlite', 'postgres'])
def cabinet(request, tmp_path, terminal):
    http = httpx.AsyncClient(transport=httpx.MockTransport(terminal))
    writer = HikvisionClient(CONFIG, http=http)
    try:
        if request.param == 'postgres':
            if not POSTGRES_URL:
                pytest.skip('RETRO_TEST_POSTGRES_URL не задан')
            with postgres_app(tmp_path, writer) as app, TestClient(
                    app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000)) as client:
                yield client
        else:
            with TestClient(sqlite_app(tmp_path, writer), base_url='http://127.0.0.1',
                            client=('127.0.0.1', 50000)) as client:
                yield client
    finally:
        asyncio.run(http.aclose())


def roster(client) -> RosterStore:
    return client.app.state.accountant_roster


def staff(client, name='Карамат', role='хостес', group='Встреча гостей') -> int:
    """Карточка, как её заводит бухгалтер в «Сотрудниках»."""
    return roster(client).add(name=name, role=role, rate='150000', group_name=group).id


def data_url(content=JPEG, mime='image/jpeg') -> str:
    return f'data:{mime};base64,' + base64.b64encode(content).decode()


def upload(client, employee_id, image=None, user='karina'):
    return client.put(f'/api/manager/employees/{employee_id}/photo', json={'image': image or data_url()},
                      auth=(user, 'secret'))


def push(client, employee_id, user='karina'):
    return client.post(f'/api/manager/employees/{employee_id}/hikvision', auth=(user, 'secret'))


def card(client, employee_id, user='karina') -> dict:
    return client.get(f'/api/manager/employees/{employee_id}', auth=(user, 'secret')).json()['employee']


def home(client, user='karina') -> dict:
    return client.get('/api/manager/home', auth=(user, 'secret')).json()


# ── Кириллица у бухгалтера (М-04) ──────────────────────────────────────────

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


def test_accountant_input_is_cyrillic_but_old_names_stay(cabinet):
    store = roster(cabinet)
    old = store.add(name='Karimov Jahongir', role='менеджер', rate='360000', group_name='Управление')
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
    assert store.list()[0].name == 'Каримов Жахонгир'


# ── Роль, направления и список ─────────────────────────────────────────────

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


def test_home_lists_shift_staff_of_own_directions_alphabetically_without_money(cabinet):
    store = roster(cabinet)
    staff(cabinet, 'Юсупов Фаррух', 'Повар миллий', 'Кухня')
    staff(cabinet, 'Алиев Жасур', 'официант', 'Обслуживание зала')
    staff(cabinet, 'Ёқубов Самандар', 'ранер', 'Обслуживание зала')
    staff(cabinet, 'Абдуллаев Тимур', 'бармен', 'Бар')
    staff(cabinet, 'Юлдашев Дильшод', 'техперсонал', 'Уборка')
    staff(cabinet, 'Каримов Жахонгир', 'менеджер', 'Управление')
    gone = staff(cabinet, 'Мирзаев Шерзод', 'Повар тандыр', 'Кухня')
    store.delete(gone)
    store.add_monthly(name='Баходиров Ихтиёр', role='Менеджер', salary='5000000')

    kitchen = home(cabinet, 'kitchen')
    assert kitchen['directions'] == ['Кухня']
    assert [item['name'] for item in kitchen['employees']] == ['Юсупов Фаррух']

    every = home(cabinet)
    assert set(every) == {'login', 'role', 'directions', 'hikvision', 'employees'}
    assert (every['login'], every['role']) == ('karina', 'manager')
    assert every['directions'] == ['Кухня', 'Зал', 'Уборка']
    assert every['hikvision'] == {'configured': True}
    # По алфавиту, ё = е; без окладников и удалённых. Менеджер без строки
    # направлений ведёт весь ресторан — видит и «Управление».
    assert [item['name'] for item in every['employees']] == [
        'Абдуллаев Тимур', 'Алиев Жасур', 'Ёқубов Самандар', 'Каримов Жахонгир', 'Юлдашев Дильшод', 'Юсупов Фаррух']
    first = every['employees'][0]
    assert set(first) == {'id', 'name', 'role', 'group', 'employment_type', 'photo', 'hikvision',
                          'can_photo', 'can_retry'}
    assert (first['role'], first['group'], first['employment_type'], first['photo']) == (
        'бармен', 'Бар', 'shift', None)
    assert first['hikvision'] == {'state': 'none', 'employee_no': None, 'message': None,
                                  'face': {'state': 'none', 'message': None}}
    assert (first['can_photo'], first['can_retry']) == (True, False)
    assert [item['name'] for item in home(cabinet, 'boss')['employees']] == [
        item['name'] for item in every['employees']]


def test_photo_and_send_rights_follow_directions(cabinet):
    hall = staff(cabinet)
    office = staff(cabinet, 'Каримов Жахонгир', 'менеджер', 'Управление')
    # Менеджер кухни — не его направление.
    assert upload(cabinet, hall, user='kitchen').status_code == 403
    answer = push(cabinet, hall, user='kitchen')
    assert (answer.status_code, answer.json()['detail']) == (403, FOREIGN)
    other = card(cabinet, hall, 'kitchen')
    assert (other['can_photo'], other['can_retry']) == (False, False)
    # «Управление» ни в одном направлении: менеджеру с разделами нельзя, а
    # администратору и менеджеру на весь ресторан (без строки направлений) — можно.
    assert upload(cabinet, office, user='kitchen').status_code == 403
    assert upload(cabinet, office).status_code == 200
    assert upload(cabinet, office, user='boss').status_code == 200
    # Удалённого бухгалтером нет; без фото не отправляем.
    roster(cabinet).delete(office)
    for answer in (upload(cabinet, office, user='boss'), push(cabinet, office, user='boss'),
                   cabinet.get(f'/api/manager/employees/{office}', auth=('karina', 'secret'))):
        assert (answer.status_code, answer.json()['detail']) == (404, NOT_FOUND)
    answer = push(cabinet, hall)
    assert (answer.status_code, answer.json()['detail']) == (422, NO_PHOTO)


# ── Фото ───────────────────────────────────────────────────────────────────

def test_photo_upload_jpeg_and_png_and_serve_the_bytes(cabinet, terminal):
    employee_id = staff(cabinet)
    answer = upload(cabinet, employee_id)
    assert answer.status_code == 200, answer.text
    employee = answer.json()['employee']
    photo = employee['photo']
    assert photo['url'].startswith(f'/api/manager/employees/{employee_id}/photo?v=')
    assert photo['updated_at']
    # Фото есть, на устройство ещё не отправляли — ждёт отправки.
    assert employee['hikvision'] == {'state': 'pending', 'employee_no': None, 'message': PENDING_MESSAGE,
                                     'face': {'state': 'pending', 'message': FACE_PENDING}}
    assert (employee['can_photo'], employee['can_retry']) == (True, True)
    assert terminal.requests == []

    image = cabinet.get(photo['url'], auth=('kitchen', 'secret'))  # общая база: видно всем менеджерам
    assert image.status_code == 200
    assert image.content == JPEG
    assert image.headers['content-type'] == 'image/jpeg'
    assert image.headers['cache-control'] == 'private, max-age=31536000'
    # Без версии в адресе — не кешируем.
    plain = cabinet.get(f'/api/manager/employees/{employee_id}/photo', auth=('karina', 'secret'))
    assert plain.content == JPEG and plain.headers['cache-control'] == 'no-store'

    again = upload(cabinet, employee_id, data_url(PNG, 'image/png')).json()['employee']['photo']
    assert again['url'] != photo['url']
    image = cabinet.get(again['url'], auth=('karina', 'secret'))
    assert (image.content, image.headers['content-type']) == (PNG, 'image/png')
    history = roster(cabinet).history(employee_id)
    assert [row['reason'] for row in history[:2]] == ['Фото заменено', 'Добавлено фото']
    assert history[0]['changed_by'] == 'karina'


@pytest.mark.parametrize('image, message', [
    (data_url(b'just some text'), NOT_IMAGE),
    (data_url(JPEG, 'text/plain'), NOT_IMAGE),
    (data_url(b'GIF89a' + bytes(32), 'image/gif'), NOT_IMAGE),
    ('просто текст', UNREADABLE),
    ('data:image/jpeg;base64,@@@@', UNREADABLE),
    ('data:image/jpeg,' + base64.b64encode(JPEG).decode(), UNREADABLE),
    (data_url(b'\xff\xd8\xff' + bytes(2 * 1024 * 1024)), TOO_LARGE),
])
def test_photo_must_be_a_real_jpeg_or_png_up_to_2_mb(cabinet, image, message):
    employee_id = staff(cabinet)
    answer = upload(cabinet, employee_id, image)
    assert (answer.status_code, answer.json()['detail']) == (422, message)
    assert card(cabinet, employee_id)['photo'] is None
    assert cabinet.get(f'/api/manager/employees/{employee_id}/photo',
                       auth=('karina', 'secret')).json()['detail'] == 'У сотрудника нет фото.'


def test_photo_of_exactly_2_mb_is_accepted(cabinet):
    employee_id = staff(cabinet)
    content = b'\xff\xd8\xff' + bytes(2 * 1024 * 1024 - 3)
    assert upload(cabinet, employee_id, data_url(content)).status_code == 200
    assert roster(cabinet).photo(employee_id)[0] == content


# ── Отправка в Hikvision: человек и лицо ──────────────────────────────────

def test_photo_sends_the_person_and_then_the_face_with_fpid(cabinet, terminal):
    employee_id = staff(cabinet)
    upload(cabinet, employee_id)
    sent = push(cabinet, employee_id).json()['employee']
    number = sent['hikvision']['employee_no']
    assert sent['hikvision'] == {'state': 'sent', 'employee_no': number, 'message': None,
                                 'face': {'state': 'sent', 'message': None}}
    assert sent['can_retry'] is False
    assert terminal.people == {number: 'Карамат'}
    assert terminal.face_records == [('POST', number, ('image/jpeg', 'face.jpg', JPEG))]
    assert terminal.faces == {number: JPEG}
    # Сначала человек (поиск номера, запись, проверка), потом лицо.
    assert terminal.requests[-2:] == [('POST', '/ISAPI/AccessControl/UserInfo/Search'), FACE_RECORD]

    person, = roster(cabinet).list()
    assert person.hikvision_id == number
    reasons = [row['reason'] for row in roster(cabinet).history(employee_id)]
    assert reasons[:2] == ['Фото добавлено в Hikvision', 'Добавлен в Hikvision']

    # Повтор уже отправленного не трогает устройство.
    calls = len(terminal.requests)
    assert push(cabinet, employee_id).json()['employee']['hikvision']['state'] == 'sent'
    assert len(terminal.requests) == calls


def test_linked_employee_gets_only_the_face(cabinet, terminal):
    employee_id = staff(cabinet)
    roster(cabinet).set_hikvision_id(employee_id, '204')
    terminal.people['204'] = 'Карамат'
    before = upload(cabinet, employee_id).json()['employee']
    assert before['hikvision']['state'] == 'sent'
    assert before['hikvision']['face'] == {'state': 'pending', 'message': FACE_PENDING}
    assert before['can_retry'] is True
    sent = push(cabinet, employee_id).json()['employee']
    assert sent['hikvision']['face']['state'] == 'sent'
    assert terminal.requests == [FACE_RECORD]
    assert terminal.faces == {'204': JPEG}


def test_existing_face_on_the_device_is_replaced_through_fd_setup(cabinet, terminal):
    employee_id = staff(cabinet)
    roster(cabinet).set_hikvision_id(employee_id, '204')
    terminal.people['204'] = 'Карамат'
    terminal.faces['204'] = b'old face'
    upload(cabinet, employee_id)
    sent = push(cabinet, employee_id).json()['employee']
    assert sent['hikvision']['face']['state'] == 'sent'
    assert terminal.requests == [FACE_RECORD, FACE_SETUP]
    assert terminal.faces == {'204': JPEG}


def test_face_failure_keeps_the_person_and_retry_sends_only_the_face(cabinet, terminal):
    employee_id = staff(cabinet)
    upload(cabinet, employee_id)
    terminal.face_fail = 'reject'
    failed = push(cabinet, employee_id).json()['employee']
    assert failed['hikvision']['state'] == 'sent'
    assert failed['hikvision']['face'] == {'state': 'error', 'message': FACE_FAILED}
    assert failed['can_retry'] is True

    terminal.face_fail = 'down'
    failed = push(cabinet, employee_id).json()['employee']
    assert failed['hikvision']['face'] == {
        'state': 'error', 'message': 'Hikvision недоступен — фото не ушло на устройство. Отправьте ещё раз.'}

    terminal.face_fail = None
    sent = push(cabinet, employee_id).json()['employee']
    assert sent['hikvision']['face']['state'] == 'sent'
    assert sent['can_retry'] is False
    assert terminal.count(PERSON_RECORD) == 1
    assert terminal.count(FACE_RECORD) == 3
    assert len(terminal.people) == 1


def test_new_photo_puts_the_face_back_to_pending_and_resend_replaces_it(cabinet, terminal):
    employee_id = staff(cabinet)
    upload(cabinet, employee_id)
    number = push(cabinet, employee_id).json()['employee']['hikvision']['employee_no']
    fresh = upload(cabinet, employee_id, data_url(PNG, 'image/png')).json()['employee']
    assert fresh['hikvision']['state'] == 'sent'
    assert fresh['hikvision']['face'] == {'state': 'pending', 'message': FACE_PENDING}
    assert fresh['can_retry'] is True
    sent = push(cabinet, employee_id).json()['employee']
    assert sent['hikvision']['face']['state'] == 'sent'
    assert terminal.faces == {number: PNG}
    assert terminal.requests[-2:] == [FACE_RECORD, FACE_SETUP]
    assert terminal.face_records[-1][2] == ('image/png', 'face.png', PNG)
    assert terminal.count(PERSON_RECORD) == 1


def test_manual_attendance_keeps_the_photo_but_never_sends(cabinet, terminal):
    employee_id = staff(cabinet, 'Турсунов Камол', 'техперсонал', 'Уборка')
    roster(cabinet).set_manual_attendance(employee_id, True)
    employee = upload(cabinet, employee_id).json()['employee']
    assert employee['photo'] is not None
    assert employee['hikvision'] == {'state': 'manual', 'employee_no': None, 'message': MANUAL_MESSAGE,
                                     'face': {'state': 'none', 'message': None}}
    assert (employee['can_photo'], employee['can_retry']) == (True, False)
    answer = push(cabinet, employee_id)
    assert answer.status_code == 200
    assert answer.json()['employee']['hikvision']['state'] == 'manual'
    assert terminal.requests == []


def test_device_down_keeps_the_photo_and_retry_adds_exactly_one_person(cabinet, terminal):
    employee_id = staff(cabinet)
    upload(cabinet, employee_id)
    terminal.fail = 'down'
    failed = push(cabinet, employee_id).json()['employee']
    assert failed['hikvision']['state'] == 'error'
    assert failed['hikvision']['message'] == 'Hikvision недоступен — карточка сохранена и ждёт отправки.'
    assert failed['can_retry'] is True
    assert roster(cabinet).list()[0].hikvision_id is None
    assert terminal.people == {}

    terminal.fail = None
    sent = push(cabinet, employee_id).json()['employee']
    assert sent['hikvision']['state'] == 'sent'
    assert list(terminal.people.values()) == ['Карамат']
    assert terminal.faces == {sent['hikvision']['employee_no']: JPEG}


def test_lost_device_answer_is_found_by_number_not_created_twice(cabinet, terminal):
    employee_id = staff(cabinet)
    upload(cabinet, employee_id)
    terminal.lose_record = True
    failed = push(cabinet, employee_id).json()['employee']
    assert failed['hikvision']['state'] == 'error'
    assert failed['hikvision']['message'] == \
        'Hikvision не ответил вовремя — карточка сохранена. Отправьте ещё раз.'
    assert failed['hikvision']['face']['state'] == 'pending'
    # Устройство человека всё-таки записало; повтор находит его по номеру.
    sent = push(cabinet, employee_id).json()['employee']
    assert sent['hikvision']['state'] == 'sent'
    assert sent['hikvision']['employee_no'] == failed['hikvision']['employee_no']
    assert sent['hikvision']['face']['state'] == 'sent'
    assert terminal.count(PERSON_RECORD) == 1
    assert len(terminal.people) == 1


def test_numbers_start_above_everything_on_the_device(cabinet, terminal):
    terminal.people.update({'150': 'Старый Сотрудник', '57': 'Ещё Один'})
    employee_id = staff(cabinet)
    upload(cabinet, employee_id)
    assert push(cabinet, employee_id).json()['employee']['hikvision']['employee_no'] == '151'


def test_number_taken_by_a_stranger_on_the_device_gets_a_new_number(cabinet, terminal):
    employee_id = staff(cabinet)
    number = roster(cabinet).reserve_employee_no(employee_id, ())
    terminal.people.update({number: 'Старый Сотрудник', '57': 'Ещё Один'})
    upload(cabinet, employee_id)
    sent = push(cabinet, employee_id).json()['employee']
    assert sent['hikvision']['state'] == 'sent'
    assert sent['hikvision']['employee_no'] == '58'
    assert terminal.people[number] == 'Старый Сотрудник'
    assert terminal.people['58'] == 'Карамат'
    assert terminal.faces == {'58': JPEG}


def test_unlinked_namesake_on_the_device_is_taken_over_not_created_twice(cabinet, terminal):
    terminal.people.update({'77': 'Карамат', '78': 'Камола'})
    employee_id = staff(cabinet)
    upload(cabinet, employee_id)
    sent = push(cabinet, employee_id).json()['employee']
    assert (sent['hikvision']['state'], sent['hikvision']['employee_no']) == ('sent', '77')
    assert terminal.count(PERSON_RECORD) == 0
    assert terminal.faces == {'77': JPEG}


@pytest.mark.parametrize('device, extra_staff', [
    ({'77': 'Карамат', '78': 'Карамат'}, False),
    ({'77': 'Карамат'}, True),
])
def test_namesakes_are_left_to_the_accountant(cabinet, terminal, device, extra_staff):
    terminal.people.update(device)
    employee_id = staff(cabinet)
    if extra_staff:
        staff(cabinet, role='официант', group='Обслуживание зала')
    upload(cabinet, employee_id)
    failed = push(cabinet, employee_id).json()['employee']
    assert failed['hikvision']['state'] == 'error'
    assert failed['hikvision']['message'] == \
        'В Hikvision уже есть люди с таким именем — номер привяжет бухгалтер в «Сотрудниках».'
    assert terminal.count(PERSON_RECORD) == 0 and terminal.face_records == []
    assert roster(cabinet).manager_card(employee_id)['hikvision_employee_no'] is None


def test_without_hikvision_the_photo_waits_and_says_why(tmp_path):
    with TestClient(sqlite_app(tmp_path, None), base_url='http://127.0.0.1',
                    client=('127.0.0.1', 50000)) as client:
        employee_id = staff(client)
        upload(client, employee_id)
        waiting = push(client, employee_id).json()['employee']
        assert waiting['hikvision']['state'] == 'pending'
        assert waiting['hikvision']['message'] == 'Hikvision не подключён — карточка сохранена и ждёт отправки.'
        assert home(client)['hikvision'] == {'configured': False}
        # Человек уже привязан бухгалтером — ждёт только лицо.
        roster(client).set_hikvision_id(employee_id, '204')
        waiting = push(client, employee_id).json()['employee']
        assert waiting['hikvision']['state'] == 'sent'
        assert waiting['hikvision']['face'] == {
            'state': 'pending', 'message': 'Hikvision не подключён — фото сохранено и ждёт отправки.'}


# ── Посещаемость, опрос и номера (М-03) ────────────────────────────────────

def test_passes_before_confirmation_land_in_the_same_card(cabinet):
    employee_id = staff(cabinet)
    number = roster(cabinet).reserve_employee_no(employee_id, ())
    store = cabinet.app.state.attendance_store
    entered = datetime(2026, 10, 9, 9, 31, tzinfo=TZ)
    store.ingest(HikvisionEvent('retro-main-entry', 'serial-1', number, entered), None)
    upload(cabinet, employee_id)
    assert push(cabinet, employee_id).json()['employee']['hikvision']['employee_no'] == number
    first = store.first_entries(entered.date())
    assert first[employee_id].occurred_at == entered


def test_reserved_number_is_never_name_linked_or_imported_as_a_second_card(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster_store = RosterStore(path)
    AttendanceStore(path)
    employee_id = roster_store.add(name='Карамат', role='хостес', rate=None, group_name='Встреча гостей').id
    number = roster_store.reserve_employee_no(employee_id, ())
    # На устройстве есть тёзка под другим номером: по имени её не привязываем.
    report = roster_store.link_hikvision_people((HikvisionPerson('900', 'Карамат'),))
    assert report['linked'] == 0
    assert roster_store.list()[0].hikvision_id is None
    imported = roster_store.import_hikvision_people((HikvisionPerson(number, 'Карамат'),))
    assert imported['created'] == 0
    assert len(roster_store.list()) == 1
    # Опрос увидел нашего человека под нашим номером — отправка дошла.
    report = roster_store.link_hikvision_people((HikvisionPerson(number, 'Карамат'),))
    assert report['linked'] == 1
    assert roster_store.list()[0].hikvision_id == number
    assert roster_store.manager_card(employee_id)['hikvision_state'] == 'sent'


def test_numbers_follow_the_largest_known_one(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    roster_store, store = RosterStore(path), AttendanceStore(path)
    person = roster_store.add(name='Баходиров Ихтиер', role='менеджер', rate='360000', group_name='Управление')
    roster_store.set_hikvision_id(person.id, '120')
    store.ingest(HikvisionEvent('entry', 's-1', '133', datetime(2026, 10, 8, 9, 0, tzinfo=TZ)), None)
    store.ingest(HikvisionEvent('entry', 's-2', '4400123456789', datetime(2026, 10, 8, 9, 1, tzinfo=TZ)), None)
    ids = [roster_store.add(name=name, role='хостес', rate=None, group_name='Встреча гостей').id
           for name in ('Карамат', 'Камола')]
    assert [roster_store.reserve_employee_no(item, ()) for item in ids] == ['134', '135']
    # Выданный номер не меняется при повторе.
    assert roster_store.reserve_employee_no(ids[0], (HikvisionPerson('500', 'Кто-то'),)) == '134'


def test_concurrent_reservations_give_distinct_numbers_and_repeat_the_same(tmp_path):
    roster_store = RosterStore(tmp_path / 'accountant.sqlite3')
    first, second = (roster_store.add(name=name, role='хостес', rate=None, group_name='Встреча гостей').id
                     for name in ('Карамат', 'Камола'))

    async def together(*ids):
        return await asyncio.gather(*(asyncio.to_thread(roster_store.reserve_employee_no, item, ())
                                      for item in ids))

    numbers = asyncio.run(together(first, second, first, second))
    assert numbers[0] == numbers[2] and numbers[1] == numbers[3]
    assert sorted(set(numbers)) == ['1', '2']


def test_namesakes_on_the_device_raise_instead_of_guessing(tmp_path):
    roster_store = RosterStore(tmp_path / 'accountant.sqlite3')
    employee_id = roster_store.add(name='Карамат Юсупова', role='хостес', rate=None,
                                   group_name='Встреча гостей').id
    people = (HikvisionPerson('77', 'Юсупова Карамат'), HikvisionPerson('78', 'юсупова  карамат'))
    with pytest.raises(DeviceNamesakes):
        roster_store.reserve_employee_no(employee_id, people)
    # Один из тёзок уже привязан к другому сотруднику — второй наш.
    other = roster_store.add(name='Другая Сотрудница', role='хостес', rate=None, group_name='Встреча гостей')
    roster_store.set_hikvision_id(other.id, '78')
    assert roster_store.reserve_employee_no(employee_id, people) == '77'


def test_accountant_cannot_hand_a_reserved_number_to_someone_else(tmp_path):
    roster_store = RosterStore(tmp_path / 'accountant.sqlite3')
    employee_id = roster_store.add(name='Карамат', role='хостес', rate=None, group_name='Встреча гостей').id
    number = roster_store.reserve_employee_no(employee_id, ())
    other = roster_store.add(name='Камола', role='хостес', rate='150000', group_name='Встреча гостей')
    with pytest.raises(ValueError, match='Карамат'):
        roster_store.set_hikvision_id(other.id, number)


# ── Хранение фото ──────────────────────────────────────────────────────────

def test_photo_lives_with_the_card_and_face_mark_follows_the_photo_version(tmp_path):
    roster_store = RosterStore(tmp_path / 'accountant.sqlite3')
    employee_id = roster_store.add(name='Карамат', role='хостес', rate=None, group_name='Встреча гостей').id
    first = roster_store.set_photo(employee_id, 'image/jpeg', JPEG, by='karina')
    second = roster_store.set_photo(employee_id, 'image/png', PNG, by='karina')
    assert roster_store.photo(employee_id) == (PNG, 'image/png', second)
    # Отправка старого снимка закончилась после загрузки нового: отметку не ставим.
    assert roster_store.record_face(employee_id, 'sent', None, version=first) is False
    assert roster_store.manager_card(employee_id)['face_state'] == 'pending'
    assert roster_store.record_face(employee_id, 'sent', None, version=second) is True
    with pytest.raises(ValueError):
        roster_store.set_photo(employee_id, 'image/gif', b'GIF89a')
    roster_store.delete(employee_id)
    assert roster_store.photo(employee_id) is None


def test_old_roster_gains_the_new_columns_without_losing_rows(tmp_path):
    path = tmp_path / 'accountant.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('''CREATE TABLE accountant_employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_row INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL, role TEXT NOT NULL, group_name TEXT NOT NULL, rate TEXT,
            hikvision_id TEXT UNIQUE)''')
        connection.execute("INSERT INTO accountant_employees (source_row, name, role, group_name, rate, hikvision_id) "
                           "VALUES (1, 'Баходиров Ихтиер', 'менеджер', 'Управление', '360000', '7')")
    roster_store = RosterStore(path)
    RosterStore(path)  # повторный запуск миграции ничего не ломает
    person, = roster_store.list()
    assert (person.name, str(person.rate), person.hikvision_id, person.employment_type) == (
        'Баходиров Ихтиер', '360000', '7', 'shift')
    row = roster_store.manager_card(person.id)
    assert (row['face_state'], row['photo_updated_at']) == (None, None)
    assert roster_store.photo(person.id) is None
