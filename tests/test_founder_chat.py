import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.claude import ClaudeClient
from retro.modules.cashier.service import DataError
from retro.modules.founder.chat import FounderChatStore


def claude_response(text='Проверьте маржинальность по направлениям.'):
    return httpx.Response(200, json={
        'stop_reason': 'end_turn',
        'content': [{'type': 'text', 'text': text}],
    })


def test_chat_store_keeps_histories_separate_and_clear_is_scoped(tmp_path):
    store = FounderChatStore(tmp_path / 'founder.sqlite3')
    store.append_exchange('founder-one', 'Вопрос 1', 'Ответ 1', '2026-09-21T12:00:00Z')
    store.append_exchange('founder-two', 'Вопрос 2', 'Ответ 2', '2026-09-21T12:01:00Z')

    assert [item['content'] for item in store.list('founder-one')] == ['Вопрос 1', 'Ответ 1']
    assert [item['content'] for item in store.list('founder-two')] == ['Вопрос 2', 'Ответ 2']

    assert store.clear('founder-one') == 2
    assert store.list('founder-one') == []
    assert len(store.list('founder-two')) == 2


def test_claude_chat_uses_bounded_history_and_server_system_prompt():
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        return claude_response('Короткий ответ.')

    settings = SimpleNamespace(
        claude_api_key='secret-key', claude_model='claude-test', claude_configured=True)
    messages = [
        {'role': 'user', 'content': f'Вопрос {index} ' + 'x' * 900}
        if index % 2 == 0 else {'role': 'assistant', 'content': f'Ответ {index} ' + 'y' * 900}
        for index in range(30)
    ]
    messages.append({'role': 'user', 'content': 'Финальный вопрос'})

    result = asyncio.run(ClaudeClient(
        settings, transport=httpx.MockTransport(handler)).chat(messages))

    assert result == 'Короткий ответ.'
    assert seen['body']['messages'][-1]['content'] == 'Финальный вопрос'
    assert seen['body']['messages'][0]['role'] == 'user'
    assert len(seen['body']['messages']) <= 24
    assert sum(len(item['content']) for item in seen['body']['messages']) <= 20_000
    assert 'не выдумывай цифры' in seen['body']['system']
    assert 'secret-key' not in json.dumps(seen['body'], ensure_ascii=False)


@pytest.mark.parametrize('payload', [
    {'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': 'обрыв'}]},
    {'stop_reason': 'end_turn', 'content': []},
])
def test_claude_chat_rejects_incomplete_answers(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    settings = SimpleNamespace(
        claude_api_key='key', claude_model='claude-test', claude_configured=True)
    with pytest.raises(DataError, match='Claude'):
        asyncio.run(ClaudeClient(settings, transport=httpx.MockTransport(handler)).chat(
            [{'role': 'user', 'content': 'Вопрос'}]))


def test_founder_chat_api_persists_success_and_can_clear(tmp_path):
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        return claude_response()

    settings = Settings(
        claude_api_key='key', claude_model='claude-test', data_dir=tmp_path)
    app = create_app(settings, claude_transport=httpx.MockTransport(handler))
    with TestClient(app, client=('127.0.0.1', 50000), base_url='http://127.0.0.1') as client:
        empty = client.get('/api/founder/chat')
        answer = client.post('/api/founder/chat', json={'message': 'Что проверить сегодня?'})
        history = client.get('/api/founder/chat')
        cleared = client.delete('/api/founder/chat')
        after = client.get('/api/founder/chat')

    assert empty.json() == {'configured': True, 'messages': []}
    assert answer.status_code == 200
    assert answer.json()['message']['role'] == 'assistant'
    assert seen['body']['messages'][-1] == {
        'role': 'user', 'content': 'Что проверить сегодня?'}
    assert [item['role'] for item in history.json()['messages']] == ['user', 'assistant']
    assert cleared.status_code == 204
    assert after.json()['messages'] == []


def test_failed_founder_chat_request_is_not_persisted(tmp_path):
    def handler(request):
        return httpx.Response(503, text='private upstream response')

    settings = Settings(
        claude_api_key='key', claude_model='claude-test', data_dir=tmp_path)
    app = create_app(settings, claude_transport=httpx.MockTransport(handler))
    with TestClient(app, client=('127.0.0.1', 50000), base_url='http://127.0.0.1') as client:
        response = client.post('/api/founder/chat', json={'message': 'Вопрос'})
        history = client.get('/api/founder/chat').json()['messages']

    assert response.status_code == 503
    assert 'private' not in response.text
    assert history == []


def test_founder_accounts_have_separate_api_histories_and_cashier_is_forbidden(tmp_path):
    def handler(request):
        return claude_response('Ответ для текущего учредителя.')

    users = {
        'cashier': ('password', 'cashier'),
        'accountant': ('password', 'accountant'),
        'director': ('password', 'director'),
        'founder-one': ('password', 'founder'),
        'founder-two': ('password', 'founder'),
    }
    settings = Settings(
        claude_api_key='key', claude_model='claude-test',
        dashboard_panel_users=users, data_dir=tmp_path)
    app = create_app(settings, claude_transport=httpx.MockTransport(handler))
    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1') as client:
        client.post('/api/session', json={'username': 'founder-one', 'password': 'password'})
        assert client.post('/api/founder/chat', json={'message': 'Первый вопрос'}).status_code == 200
        client.post('/api/session/logout')

        client.post('/api/session', json={'username': 'founder-two', 'password': 'password'})
        assert client.get('/api/founder/chat').json()['messages'] == []
        client.post('/api/session/logout')

        client.post('/api/session', json={'username': 'founder-one', 'password': 'password'})
        assert len(client.get('/api/founder/chat').json()['messages']) == 2
        client.post('/api/session/logout')

        client.post('/api/session', json={'username': 'cashier', 'password': 'password'})
        assert client.get('/api/founder/chat').status_code == 403


def test_founder_page_exposes_accessible_chat_drawer(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        page = client.get('/founder')

    assert 'id="ai-chat-toggle"' in page.text
    assert 'aria-controls="ai-chat-drawer"' in page.text
    assert 'src="/static/founder-chat.js"' in page.text
