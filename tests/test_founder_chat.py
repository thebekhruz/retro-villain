import asyncio
import json
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.integrations.hikvision import HikvisionEvent, HikvisionPerson
from retro.integrations.claude import ClaudeClient
from retro.modules.cashier.service import DataError, TZ, demo_snapshot
from retro.modules.founder.chat import FounderChatStore
from retro.modules.founder.tools import FounderChatTools


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


def test_claude_chat_executes_read_only_tool_and_returns_result_to_model():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json={
                'stop_reason': 'tool_use',
                'content': [{
                    'type': 'tool_use', 'id': 'tool-1',
                    'name': 'get_revenue_analytics',
                    'input': {'start': '2026-09-01', 'end': '2026-09-07',
                              'granularity': 'week', 'directions': ['retro']},
                }],
            })
        return claude_response('Выручка Retro за 1–7 сентября — 100 сум.')

    calls = []

    async def tool_handler(name, arguments):
        calls.append((name, arguments))
        return {'period': {'start': arguments['start'], 'end': arguments['end']},
                'totals': {'retro': '100'}, 'currency': 'UZS'}

    settings = SimpleNamespace(
        claude_api_key='secret-key', claude_model='claude-test', claude_configured=True)
    result = asyncio.run(ClaudeClient(
        settings, transport=httpx.MockTransport(handler)).chat(
            [{'role': 'user', 'content': 'Какая была выручка Retro за прошлую неделю?'}],
            tools=FounderChatTools.definitions,
            tool_handler=tool_handler,
            current_date='2026-09-22'))

    assert result == 'Выручка Retro за 1–7 сентября — 100 сум.'
    assert {tool['name'] for tool in requests[0]['tools']} == {
        'get_revenue_analytics', 'get_bookings', 'get_employee_attendance',
        'get_iiko_sales_details', 'get_cashier_day', 'get_accounting_day',
        'get_saved_director_reports'}
    assert calls[0][0] == 'get_revenue_analytics'
    tool_result = requests[1]['messages'][-1]['content'][0]
    assert tool_result['tool_use_id'] == 'tool-1'
    assert json.loads(tool_result['content'])['totals']['retro'] == '100'
    assert 'Текущая дата ресторана' in requests[0]['system']


def test_founder_chat_booking_tool_returns_existing_founder_metrics():
    coverage = {'history_started_at': '2026-09-01T08:00:00Z',
                'historical_data_complete': True}

    class BookingsStub:
        async def load(self, start, end):
            assert (start, end) == (date(2026, 9, 1), date(2026, 9, 7))
            return {
                'submitted': {
                    'coverage': coverage, 'excluded_missing_date': 0,
                    'totals': {'bookings': 2, 'guests': 5,
                               'unknown_guest_bookings': 0},
                    'by_date': [{'value': '2026-09-01', 'bookings': 2,
                                 'guests': 5, 'unknown_guest_bookings': 0}],
                    'by_source': [{'value': 'direct', 'label': 'Прямые',
                                   'bookings': 2, 'guests': 5,
                                   'unknown_guest_bookings': 0}],
                },
                'cancelled': {
                    'coverage': coverage, 'excluded_missing_date': 0,
                    'totals': {'bookings': 1, 'guests': 0,
                               'unknown_guest_bookings': 0},
                    'by_date': [{'value': '2026-09-02', 'bookings': 1,
                                 'guests': 0, 'unknown_guest_bookings': 0}],
                    'by_source': [],
                },
            }

    app = SimpleNamespace(state=SimpleNamespace(bookings=BookingsStub()))
    result = asyncio.run(FounderChatTools(app).execute('get_bookings', {
        'start': '2026-09-01', 'end': '2026-09-07', 'granularity': 'week'}))

    assert result['totals'] == {
        'bookings': 2, 'guests': 5, 'unknown_guest_bookings': 0, 'cancelled': 1}
    assert result['sources'][0]['name'] == 'Прямые'


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


def test_founder_chat_can_read_revenue_and_full_employee_financial_fields(tmp_path):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(200, json={
                'stop_reason': 'tool_use',
                'content': [
                    {'type': 'tool_use', 'id': 'revenue-1',
                     'name': 'get_revenue_analytics',
                     'input': {'start': '2026-09-16', 'end': '2026-09-16',
                               'granularity': 'day',
                               'directions': ['retro', 'school', 'banquet']}},
                    {'type': 'tool_use', 'id': 'attendance-1',
                     'name': 'get_employee_attendance',
                     'input': {'date': '2026-09-16', 'status': 'late'}},
                ],
            })
        return claude_response('За 16 сентября выручка 300 сум; опоздала Азиза Каримова.')

    class IikoStub:
        async def load_founder_analytics(self, start, end, granularity, directions):
            assert (start, end) == (date(2026, 9, 16), date(2026, 9, 16))
            return {'period': {'start': str(start), 'end': str(end)},
                    'totals': {'retro': '100', 'school': '100', 'banquet': '100',
                               'selected': '300'},
                    'currency': 'UZS', 'reconciled': True, 'warnings': []}

    settings = Settings(
        claude_api_key='key', claude_model='claude-test', data_dir=tmp_path)
    app = create_app(settings, claude_transport=httpx.MockTransport(handler))
    app.state.iiko = IikoStub()
    employee = app.state.accountant_roster.add(
        name='Азиза Каримова', role='официант', rate='250000',
        group_name='Обслуживание зала')
    app.state.accountant_roster.link_hikvision_people((
        HikvisionPerson('hik-aziza', 'Азиза Каримова'),))
    app.state.attendance_store.ingest(HikvisionEvent(
        'retro-main-entry', 'event-1', 'hik-aziza',
        datetime(2026, 9, 16, 10, 15, tzinfo=TZ)), employee.id)

    with TestClient(app, client=('127.0.0.1', 50000),
                    base_url='http://127.0.0.1') as client:
        response = client.post('/api/founder/chat', json={
            'message': 'Какая была выручка и кто опоздал 16 сентября?'})

    assert response.status_code == 200
    results = requests[1]['messages'][-1]['content']
    revenue = json.loads(next(item for item in results
                              if item['tool_use_id'] == 'revenue-1')['content'])
    attendance = json.loads(next(item for item in results
                                 if item['tool_use_id'] == 'attendance-1')['content'])
    assert revenue['totals']['selected'] == '300'
    assert attendance['counts']['late'] == 1
    assert attendance['employees'] == [{
        'employee_id': employee.id,
        'name': 'Азиза Каримова', 'role': 'официант',
        'group': 'Обслуживание зала', 'status': 'late',
        'first_entry': '2026-09-16T10:15:00+05:00',
        'rate': '250000', 'payable': '250000', 'exception': False,
        'demo': False, 'hikvision_registered': True,
    }]
    assert attendance['employees'][0]['rate'] == '250000'
    assert attendance['employees'][0]['payable'] == '250000'


def test_founder_tools_expose_full_accounting_day(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    employee = app.state.accountant_roster.add(
        name='Лина', role='официант', rate='275000', group_name='Обслуживание зала')

    result = asyncio.run(FounderChatTools(app).execute(
        'get_accounting_day', {'date': '2026-09-16'}))

    row = next(item for item in result['employees'] if item['employee_id'] == employee.id)
    assert row['rate'] == '275000'
    assert 'ledger' in result
    assert 'reserves' in result
    assert 'monthly_employees' in result


def test_founder_tools_expose_iiko_and_local_cashier_data(tmp_path):
    class IikoStub:
        async def load(self, day):
            return replace(demo_snapshot(day), demo=False)

    class RateStub:
        async def get(self, day):
            return SimpleNamespace(json=lambda: {
                'date': day.isoformat(), 'official_rate': '12500',
                'restaurant_rate': '12300'})

        def balance(self, day):
            return {'date': day.isoformat(), 'amount': '100'}

    app = create_app(Settings(data_dir=tmp_path))
    app.state.iiko = IikoStub()
    app.state.usd_rates = RateStub()
    app.state.expenses.add(date(2026, 9, 16), 'Такси', '20000')
    app.state.expenses.add_receipt(date(2026, 9, 16), 'Возврат', '50000')

    result = asyncio.run(FounderChatTools(app).execute(
        'get_cashier_day', {'date': '2026-09-16'}))

    assert result['revenue'] == '18450000'
    assert result['expenses'][0]['description'] == 'Такси'
    assert result['expense_total'] == '20000'
    assert result['receipt_total'] == '50000'
    assert result['usd_rate']['restaurant_rate'] == '12300'
    assert result['usd_balance']['amount'] == '100'


def test_founder_tools_can_query_custom_iiko_dimensions():
    seen = {}

    class IikoStub:
        async def load_sales_details(self, start, end, dimensions, *, limit):
            seen.update(start=start, end=end, dimensions=dimensions, limit=limit)
            return {'rows': [{'dimensions': {'DishName': 'Стейк'}, 'revenue': '500000'}]}

    app = SimpleNamespace(state=SimpleNamespace(
        iiko=IikoStub(), iiko_lock=asyncio.Lock()))
    result = asyncio.run(FounderChatTools(app).execute('get_iiko_sales_details', {
        'start': '2026-09-01', 'end': '2026-09-07',
        'dimensions': ['DishName'], 'limit': 50,
    }))

    assert result['rows'][0]['dimensions']['DishName'] == 'Стейк'
    assert seen == {
        'start': date(2026, 9, 1), 'end': date(2026, 9, 7),
        'dimensions': ('DishName',), 'limit': 50,
    }


@pytest.mark.parametrize('dimensions,limit', [
    (['DishName', 'DishName'], 10),
    (['UnknownField'], 10),
    (['DishName'], 0),
    (['DishName'], True),
])
def test_founder_iiko_detail_tool_rejects_unbounded_or_unknown_queries(dimensions, limit):
    app = SimpleNamespace(state=SimpleNamespace(
        iiko=SimpleNamespace(), iiko_lock=asyncio.Lock()))
    with pytest.raises(DataError):
        asyncio.run(FounderChatTools(app).execute('get_iiko_sales_details', {
            'start': '2026-09-01', 'end': '2026-09-07',
            'dimensions': dimensions, 'limit': limit,
        }))


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
    assert 'src="/static/founder-markdown.js"' in page.text
    assert 'src="/static/founder-chat.js"' in page.text
