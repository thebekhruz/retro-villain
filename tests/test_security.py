from ipaddress import ip_network

from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings


PANEL_USERS = {
    'cashier': ('test-password', 'cashier'),
    'accountant': ('test-password', 'accountant'),
    'director': ('test-password', 'director'),
    'founder': ('test-password', 'founder'),
}


def test_panel_user_can_open_only_its_own_panel(tmp_path):
    app = create_app(Settings(dashboard_panel_users=PANEL_USERS, data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1', follow_redirects=False) as client:
        missing = client.get('/')
        login_page = client.get('/login')
        wrong_password = client.post(
            '/api/session', json={'username': 'cashier', 'password': '0000'})
        login = client.post(
            '/api/session', json={'username': 'cashier', 'password': 'test-password'})
        own = client.get('/')
        config = client.get('/api/config')
        forbidden = client.get('/accountant')
        forbidden_api = client.get('/api/accountant/day')

    assert missing.status_code == 303
    assert missing.headers['location'] == '/login'
    assert login_page.status_code == 200
    assert wrong_password.status_code == 401
    assert login.status_code == 200
    assert login.json() == {'role': 'cashier', 'path': '/'}
    assert 'retro_session=' in login.headers['set-cookie']
    assert 'HttpOnly' in login.headers['set-cookie']
    assert 'SameSite=strict' in login.headers['set-cookie']
    assert own.status_code == 200
    assert config.status_code == 200
    assert config.json()['role'] == 'cashier'
    assert forbidden.status_code == 403
    assert forbidden_api.status_code == 403


def test_logout_invalidates_dashboard_session(tmp_path):
    app = create_app(Settings(dashboard_panel_users=PANEL_USERS, data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1', follow_redirects=False) as client:
        client.post('/api/session', json={
            'username': 'director', 'password': 'test-password'})
        logout = client.post('/api/session/logout')
        page = client.get('/director')

    assert logout.status_code == 204
    assert 'retro_session=' in logout.headers['set-cookie']
    assert 'Max-Age=0' in logout.headers['set-cookie']
    assert page.status_code == 303
    assert page.headers['location'] == '/login'


def test_logout_page_clears_session_and_redirects_to_login(tmp_path):
    app = create_app(Settings(dashboard_panel_users=PANEL_USERS, data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1', follow_redirects=False) as client:
        client.post('/api/session', json={
            'username': 'director', 'password': 'test-password'})
        logout = client.get('/logout')
        page = client.get('/director')

    assert logout.status_code == 303
    assert logout.headers['location'] == '/login'
    assert 'retro_session=' in logout.headers['set-cookie']
    assert 'Max-Age=0' in logout.headers['set-cookie']
    assert page.status_code == 303
    assert page.headers['location'] == '/login'


def test_each_panel_user_can_open_the_assigned_page(tmp_path):
    app = create_app(Settings(dashboard_panel_users=PANEL_USERS, data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1') as client:
        responses = {
            role: client.get(path, auth=(role, 'test-password')).status_code
            for role, path in {
                'cashier': '/',
                'accountant': '/accountant',
                'director': '/director',
                'founder': '/founder',
            }.items()
        }

    assert responses == {'cashier': 200, 'accountant': 200, 'director': 200, 'founder': 200}


def test_each_dashboard_page_exposes_logout_control(tmp_path):
    app = create_app(Settings(dashboard_user='viewer', dashboard_password='secret', data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1') as client:
        pages = [client.get(path, auth=('viewer', 'secret')) for path in (
            '/', '/accountant', '/accountant/employees', '/director', '/founder')]

    for page in pages:
        assert page.status_code == 200
        assert 'id="logout"' in page.text
        assert '>Выйти</button>' in page.text
        assert 'src="/static/logout.js"' in page.text


def test_legacy_dashboard_user_keeps_access_to_all_panels(tmp_path):
    settings = Settings(
        dashboard_user='viewer', dashboard_password='secret', data_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1') as client:
        statuses = [client.get(path, auth=('viewer', 'secret')).status_code
                    for path in ('/', '/accountant', '/director', '/founder')]

    assert statuses == [200, 200, 200, 200]


def test_external_peer_cannot_forge_local_host(tmp_path):
    settings = Settings(dashboard_user='viewer', dashboard_password='secret', data_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app, client=('203.0.113.5', 50000), base_url='http://localhost') as client:
        response = client.get('/api/accountant/day', headers={'host': 'localhost'},
                              auth=('viewer', 'secret'))
    assert response.status_code == 403


def test_cross_origin_report_generation_is_rejected(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000), base_url='http://127.0.0.1') as client:
        response = client.post('/api/director/reports',
                               headers={'origin': 'https://attacker.example'})
    assert response.status_code == 403


def test_untrusted_peer_cannot_supply_forwarded_client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, client=('203.0.113.5', 50000)) as client:
        response = client.get('/api/config', headers={'x-forwarded-for': '127.0.0.1'})
    assert response.status_code == 403


def test_trusted_proxy_uses_one_forwarded_address(tmp_path):
    settings = Settings(
        dashboard_user='viewer', dashboard_password='secret',
        dashboard_allowed_network=ip_network('10.10.8.0/24'),
        trusted_proxy_network=ip_network('192.0.2.0/24'), data_dir=tmp_path)
    app = create_app(settings)
    with TestClient(app, client=('192.0.2.10', 50000)) as client:
        accepted = client.get('/api/config', headers={'x-forwarded-for': '10.10.8.15'},
                              auth=('viewer', 'secret'))
        rejected = client.get('/api/config', headers={'x-forwarded-for': '10.10.8.15, 127.0.0.1'},
                              auth=('viewer', 'secret'))
    assert accepted.status_code == 200
    assert rejected.status_code == 403


def test_director_attendance_does_not_expose_payroll_amounts(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    app.state.accountant_roster.add(
        name='Тест', role='официант', rate='100000', group_name='Обслуживание зала')
    with TestClient(app, client=('127.0.0.1', 50000), base_url='http://127.0.0.1') as client:
        employee = client.get('/api/director/attendance').json()['employees'][0]
    assert employee['name'] == 'Тест'
    assert 'rate' not in employee
    assert 'payable' not in employee
