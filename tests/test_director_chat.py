import json

import httpx
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.modules.director.tools import DirectorChatTools


def test_director_chat_uses_least_privilege_tools_and_separate_history(tmp_path):
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        return httpx.Response(200, json={
            'stop_reason': 'end_turn',
            'content': [{'type': 'text', 'text': 'Проверьте маржу десертов.'}],
        })

    app = create_app(Settings(
        claude_api_key='key', claude_model='claude-test', data_dir=tmp_path),
        claude_transport=httpx.MockTransport(handler))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1') as client:
        response = client.post('/api/director/chat', json={'message': 'Что проверить?'})
        director_history = client.get('/api/director/chat').json()['messages']
        founder_history = client.get('/api/founder/chat').json()['messages']

    assert response.status_code == 200
    assert len(director_history) == 2
    assert founder_history == []
    assert 'ассистент директора' in seen['body']['system']
    assert {tool['name'] for tool in seen['body']['tools']} == {
        'get_director_dashboard', 'get_iiko_sales_details',
        'get_employee_attendance', 'get_saved_director_reports',
    }
    assert seen['body']['max_tokens'] == 5600


def test_director_chat_is_available_only_to_director_role(tmp_path):
    users = {
        'director': ('password', 'director'),
        'cashier': ('password', 'cashier'),
    }
    app = create_app(Settings(dashboard_panel_users=users, data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1') as client:
        client.post('/api/session', json={'username': 'director', 'password': 'password'})
        assert client.get('/api/director/chat').status_code == 200
        client.post('/api/session/logout')
        client.post('/api/session', json={'username': 'cashier', 'password': 'password'})
        assert client.get('/api/director/chat').status_code == 403


def test_director_attendance_tool_never_exposes_payroll_fields(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    app.state.accountant_roster.add(
        name='Тест', role='официант', rate='100000', group_name='Обслуживание зала')
    result = DirectorChatTools(app)._attendance({'date': '2026-09-22', 'status': 'all'})

    assert result['employees'][0]['name'] == 'Тест'
    assert 'rate' not in result['employees'][0]
    assert 'payable' not in result['employees'][0]


def test_director_page_exposes_scoped_accessible_chat(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        # Выдвижной чат остался у полного отчёта; у телефона AI — своя вкладка.
        page = client.get('/director/report')

    assert page.status_code == 200
    assert 'id="ai-chat-toggle"' in page.text
    assert 'data-endpoint="/api/director/chat"' in page.text
    assert 'ПОМОЩНИК ДИРЕКТОРА' in page.text
    assert 'src="/static/founder-markdown.js"' in page.text
    assert 'src="/static/founder-chat.js"' in page.text
    assert 'href="/static/ai-chat.css"' in page.text
