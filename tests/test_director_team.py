"""Телефон директора (T-377, 6a): команда — тот же реестр, что у бухгалтера."""

from datetime import date

from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.hikvision import HikvisionPerson
from retro.modules.accountant.roster import RosterStore
from retro.modules.cashier.service import today_tashkent

DAY = date(2026, 9, 16)


def client(tmp_path, **settings):
    app = create_app(Settings(**settings), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     director_db_path=tmp_path / 'director.sqlite3')
    return TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50000))


def test_director_edits_reach_the_accountant_registry_immediately(tmp_path):
    with client(tmp_path) as c:
        created = c.post('/api/director/team', json={
            'type': 'shift', 'name': 'Жасур Алиев', 'role': 'официант', 'amount': '180 000'})
        assert created.status_code == 201
        employee = created.json()['employee']
        assert employee['rate'] == '180000' and employee['group'] == 'Обслуживание зала'

        changed = c.patch(f"/api/director/team/shift/{employee['id']}", json={
            'name': 'Жасур Алиев', 'role': 'официант', 'amount': '195000'})
        assert changed.status_code == 200
        # Ставка действует с сегодняшнего дня; прошлые дни хранят прежнюю.
        today = today_tashkent().isoformat()
        staff = c.get('/api/accountant/staff', params={'date': today}).json()
        assert [row['rate'] for row in staff['employees']] == ['195000']
        # Правка ставки оставляет след с причиной, как правка бухгалтера.
        audit = c.app.state.accountant_roster._open().execute(
            'SELECT reason, old_rate, new_rate FROM accountant_roster_audit').fetchall()
        assert audit == [('Изменено директором', '180000', '195000')]

        assert c.delete(f"/api/director/team/shift/{employee['id']}").status_code == 204
        assert c.get('/api/accountant/staff', params={'date': today}).json()['employees'] == []
        assert c.delete(f"/api/director/team/shift/{employee['id']}").status_code == 404


def test_unknown_role_goes_to_unassigned_group_instead_of_failing(tmp_path):
    with client(tmp_path) as c:
        employee = c.post('/api/director/team', json={
            'type': 'shift', 'name': 'Новый', 'role': 'кальянщик', 'amount': '150000'}).json()['employee']
    assert employee['group'] == 'Не распределено'


def test_monthly_salary_edit_keeps_the_accountant_payments(tmp_path):
    with client(tmp_path) as c:
        roster = c.app.state.accountant_roster
        person = roster.add_monthly(name='Азиз', role='менеджер', salary='8000000',
                                    cash='3000000', remaining='5000000')
        response = c.patch(f'/api/director/team/monthly/{person.id}', json={
            'name': 'Азиз Каримов', 'role': 'менеджер', 'amount': '9000000'})
        assert response.status_code == 200
        saved = roster.list_monthly()[0]
        assert saved.salary == 9000000 and saved.cash == 3000000 and saved.remaining == 5000000
        assert c.patch('/api/director/team/monthly/999', json={
            'name': 'x', 'role': 'y', 'amount': '1'}).status_code == 404
        assert c.delete(f'/api/director/team/monthly/{person.id}').status_code == 204


def test_bad_amounts_are_refused(tmp_path):
    with client(tmp_path) as c:
        for amount in ('0', 'много', '-5'):
            response = c.post('/api/director/team', json={
                'type': 'shift', 'name': 'Тест', 'role': 'официант', 'amount': amount})
            assert response.status_code == 422, amount
        assert c.post('/api/director/team', json={
            'type': 'contract', 'name': 'Тест', 'role': 'официант', 'amount': '1'}).status_code == 422


def test_manual_attendance_hides_the_device_link_and_survives_sync(tmp_path):
    roster = RosterStore(tmp_path / 'accountant.sqlite3')
    guard = roster.add(name='Акмаль Рашидов', role='охрана', rate='150000', group_name='Охрана')
    waiter = roster.add(name='Нодира', role='официант', rate='180000', group_name='Обслуживание зала')
    roster.link_hikvision_people((HikvisionPerson('77', 'Нодира'),))
    roster.set_manual_attendance(guard.id, True)
    # Синхронизация по имени не должна привязать того, кого отмечают вручную.
    result = roster.link_hikvision_people((HikvisionPerson('55', 'Акмаль Рашидов'),))
    assert result['linked'] == 0
    people = {person.name: person for person in roster.list()}
    assert people['Акмаль Рашидов'].manual_attendance is True
    assert people['Акмаль Рашидов'].hikvision_id is None
    assert people['Нодира'].hikvision_id == '77'
    # Флаг на связанном сотруднике тоже гасит связь — на его день смотрит бухгалтер.
    roster.set_manual_attendance(waiter.id, True)
    assert {p.name: p.hikvision_id for p in roster.list(DAY)}['Нодира'] is None
    roster.set_manual_attendance(waiter.id, False)
    assert {p.name: p.hikvision_id for p in roster.list(DAY)}['Нодира'] == '77'


def test_director_marks_without_hikvision_and_accountant_sees_manual_status(tmp_path):
    with client(tmp_path) as c:
        employee = c.post('/api/director/team', json={
            'type': 'shift', 'name': 'Гульшан', 'role': 'техперсонал', 'amount': '130000',
            'manual_attendance': True}).json()['employee']
        assert employee['manual_attendance'] is True
        team = c.get('/api/director/team', params={'date': DAY.isoformat()}).json()
        assert team['counts']['no_hikvision'] == 1
        assert team['shift'][0]['status'] == 'unlinked'
        staff = c.get('/api/accountant/staff', params={'date': DAY.isoformat()}).json()
        assert staff['employees'][0]['status'] == 'unlinked'
        c.patch(f"/api/director/team/shift/{employee['id']}", json={
            'name': 'Гульшан', 'role': 'техперсонал', 'amount': '130000', 'manual_attendance': False})
        assert c.get('/api/director/team').json()['shift'][0]['manual_attendance'] is False


def test_team_and_accounting_are_director_only(tmp_path):
    users = {'director': ('password', 'director'), 'accountant': ('password', 'accountant')}
    with client(tmp_path, dashboard_panel_users=users) as c:
        c.post('/api/session', json={'username': 'accountant', 'password': 'password'})
        assert c.get('/api/director/team').status_code == 403
        assert c.post('/api/director/team', json={
            'type': 'shift', 'name': 'x', 'role': 'официант', 'amount': '1'}).status_code == 403
        c.post('/api/session/logout')
        c.post('/api/session', json={'username': 'director', 'password': 'password'})
        assert c.get('/api/director/team').status_code == 200
        assert c.get('/api/director/accounting/day', params={'date': DAY.isoformat()}).status_code == 200
        # Сам модуль бухгалтера директору по-прежнему закрыт.
        assert c.get('/api/accountant/day').status_code == 403


def test_director_accounting_day_is_the_accountant_view(tmp_path):
    with client(tmp_path) as c:
        director = c.get('/api/director/accounting/day', params={'date': DAY.isoformat()}).json()
        accountant = c.get('/api/accountant/day', params={'date': DAY.isoformat()}).json()
    assert director == accountant


def test_director_pages_split_phone_and_full_report(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000)) as c:
        phone = c.get('/director').text
        report = c.get('/director/report').text
    assert 'director-app.js' in phone and 'data-tab="ai"' in phone
    assert 'href="/director/report"' in phone
    assert 'id="period-host"' in report  # прежний отчёт с выбором периода на месте


def test_director_rate_edit_keeps_a_group_the_accountant_chose(tmp_path):
    with client(tmp_path) as c:
        person = c.app.state.accountant_roster.add(name='Шавкат', role='Шашлычник', rate='150000',
                                                   group_name='Кухня')
        c.patch(f'/api/director/team/shift/{person.id}', json={
            'name': 'Шавкат', 'role': 'Шашлычник', 'amount': '170000'})
        assert c.app.state.accountant_roster.list()[0].group_name == 'Кухня'


def test_monthly_edit_does_not_overwrite_a_payment_recorded_meanwhile(tmp_path):
    with client(tmp_path) as c:
        roster = c.app.state.accountant_roster
        person = roster.add_monthly(name='Азиз', role='менеджер', salary='8000000')
        roster.update_monthly(person.id, name='Азиз', role='менеджер', salary='8000000',
                              cash='2000000', remaining='6000000')
        c.patch(f'/api/director/team/monthly/{person.id}', json={
            'name': 'Азиз', 'role': 'менеджер', 'amount': '9000000'})
        saved = roster.list_monthly()[0]
        assert saved.salary == 9000000 and saved.cash == 2000000
