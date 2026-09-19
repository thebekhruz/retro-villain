import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from retro.integrations.gemini import GeminiClient
from retro.modules.cashier.service import DataError


def test_gemini_client_sends_prompt_and_validates_json():
    seen = {}

    def handler(request):
        seen['url'] = str(request.url)
        seen['api_key'] = request.headers.get('x-goog-api-key')
        seen['body'] = json.loads(request.content)
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [
            {'text': '{"summary":"Проверить блюдо","problems":[]}'},
        ]}}]})

    settings = SimpleNamespace(gemini_api_key='key', gemini_model='gemini-test',
                               gemini_configured=True)
    snapshot = SimpleNamespace(json=lambda: {'period_start': '2026-09-08'})
    result = asyncio.run(GeminiClient(settings, transport=httpx.MockTransport(handler)).analyze(snapshot))

    assert result == {'summary': 'Проверить блюдо', 'problems': []}
    assert str(httpx.URL(seen['url']).path).endswith('/v1beta/models/gemini-test:generateContent')
    assert httpx.URL(seen['url']).query == b''
    assert seen['api_key'] == 'key'
    assert seen['body']['generationConfig']['responseMimeType'] == 'application/json'


def test_gemini_client_rejects_invalid_response():
    def handler(request):
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': 'not-json'}]}}]})

    settings = SimpleNamespace(gemini_api_key='key', gemini_model='gemini-test',
                               gemini_configured=True)
    snapshot = SimpleNamespace(json=lambda: {})
    with pytest.raises(DataError, match='Gemini'):
        asyncio.run(GeminiClient(settings, transport=httpx.MockTransport(handler)).analyze(snapshot))
