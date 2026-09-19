from ipaddress import ip_network

from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings


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
