import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from retro.integrations.claude import ClaudeClient
from retro.modules.cashier.service import DataError


def settings():
    return SimpleNamespace(claude_api_key='key', claude_model='claude-test',
                           claude_configured=True)


def response(text='{"summary":"Проверить блюдо","problems":[]}'):
    return httpx.Response(200, json={
        'stop_reason': 'end_turn',
        'content': [{'type': 'text', 'text': text}],
    })


def test_claude_client_uses_messages_api_header_and_json_schema():
    seen = {}

    def handler(request):
        seen['url'] = request.url
        seen['api_key'] = request.headers.get('x-api-key')
        seen['version'] = request.headers.get('anthropic-version')
        seen['body'] = json.loads(request.content)
        return response()

    snapshot = SimpleNamespace(json=lambda: {'period_start': '2026-09-08'})
    result = asyncio.run(ClaudeClient(
        settings(), transport=httpx.MockTransport(handler)).analyze(snapshot))

    assert result == {'summary': 'Проверить блюдо', 'problems': []}
    assert seen['url'] == httpx.URL('https://api.anthropic.com/v1/messages')
    assert seen['url'].query == b''
    assert seen['api_key'] == 'key'
    assert seen['version'] == '2023-06-01'
    assert seen['body']['model'] == 'claude-test'
    assert seen['body']['max_tokens'] == 1200
    assert seen['body']['output_config']['format']['type'] == 'json_schema'
    assert seen['body']['output_config']['format']['schema']['additionalProperties'] is False
    schema_text = json.dumps(seen['body']['output_config']['format']['schema'])
    assert 'maxItems' not in schema_text
    assert 'maxLength' not in schema_text
    direction = seen['body']['output_config']['format']['schema']['properties']['problems']['items']['properties']['direction']
    assert 'banquet' in direction['enum']
    assert '2026-09-08' in seen['body']['messages'][0]['content']
    assert 'не более 6' in seen['body']['messages'][0]['content']
    assert 'key' not in seen['body']['messages'][0]['content']


def test_claude_receives_bounded_server_summary_instead_of_full_menu():
    seen = {}

    def handler(request):
        seen['body'] = json.loads(request.content)
        return response()

    def metric(revenue='100', cost='50', profit='50', margin='50'):
        return {'quantity': '1', 'revenue': revenue, 'cost': cost,
                'gross_profit': profit, 'margin_percent': margin}

    items = {f'Нейтральное {index}': metric() for index in range(500)}
    items['Критическая позиция'] = metric('1000', '9000', '-8000', '-800')
    snapshot = SimpleNamespace(json=lambda: {
        'period_start': '2026-09-08', 'period_end': '2026-09-17',
        'cash_total': '51000', 'yandex_revenue': '0',
        'item_metrics': {'all': items, 'retro': items, 'oxbridge': {},
                         'banquet': {}, 'yandex': {}},
        'waiter_metrics': {},
    })

    asyncio.run(ClaudeClient(
        settings(), transport=httpx.MockTransport(handler)).analyze(snapshot))

    prompt = seen['body']['messages'][0]['content']
    assert 'Критическая позиция' in prompt
    assert 'Нейтральное 499' not in prompt
    assert len(prompt) < 20_000


@pytest.mark.parametrize('payload', [
    {'stop_reason': 'max_tokens', 'content': [{'type': 'text', 'text': '{}'}]},
    {'stop_reason': 'refusal', 'content': [{'type': 'text', 'text': 'Нет'}]},
    {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'not-json'}]},
    {'stop_reason': 'end_turn', 'content': []},
])
def test_claude_client_rejects_incomplete_or_invalid_response(payload):
    def handler(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(DataError, match='Claude'):
        asyncio.run(ClaudeClient(
            settings(), transport=httpx.MockTransport(handler)).analyze(
                SimpleNamespace(json=lambda: {})))


def test_claude_client_rejects_duplicate_problems():
    analysis = {
        'summary': 'Есть повторы.',
        'problems': [
            {'subject': 'Плов', 'direction': 'retro', 'reason': 'Причина 1',
             'priority': 'high', 'action': 'review'},
            {'subject': ' плов ', 'direction': 'retro', 'reason': 'Причина 2',
             'priority': 'medium', 'action': 'promote'},
        ],
    }

    def handler(request):
        return response(json.dumps(analysis, ensure_ascii=False))

    with pytest.raises(DataError, match='Claude'):
        asyncio.run(ClaudeClient(
            settings(), transport=httpx.MockTransport(handler)).analyze(
                SimpleNamespace(json=lambda: {})))


def test_claude_client_rejects_more_than_six_problems():
    problem = {'subject': 'Позиция', 'direction': 'retro', 'reason': 'Причина',
               'priority': 'high', 'action': 'review'}
    analysis = {
        'summary': 'Слишком много пунктов.',
        'problems': [{**problem, 'subject': f'Позиция {index}'} for index in range(7)],
    }

    def handler(request):
        return response(json.dumps(analysis, ensure_ascii=False))

    with pytest.raises(DataError, match='Claude'):
        asyncio.run(ClaudeClient(
            settings(), transport=httpx.MockTransport(handler)).analyze(
                SimpleNamespace(json=lambda: {})))


def test_claude_client_keeps_non_success_response_private():
    def handler(request):
        return httpx.Response(401, text='upstream secret payload')

    with pytest.raises(DataError, match='Claude не смог') as error:
        asyncio.run(ClaudeClient(
            settings(), transport=httpx.MockTransport(handler)).analyze(
                SimpleNamespace(json=lambda: {})))

    assert 'secret' not in str(error.value)
