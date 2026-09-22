import asyncio
import json

import httpx
import pytest

from retro.config import Settings
from retro.integrations.broadcasts import (
    BookingBroadcastClient,
    BroadcastConflict,
)
from retro.modules.cashier.service import DataError


JOB = {
    'id': '12345678-1234-4123-8123-123456789012',
    'status': 'completed',
    'audience': 3,
    'sent': 2,
    'blocked': 1,
    'failed': 0,
    'created_at': '2026-09-22T12:00:00.000Z',
    'started_at': '2026-09-22T12:00:00.100Z',
    'finished_at': '2026-09-22T12:00:01.000Z',
}
RECIPIENTS = [
    {'id': 'A' * 32, 'name': 'Азиза', 'phone': '••• 12 34'},
    {'id': 'B' * 32, 'name': 'Бехруз', 'phone': '••• 56 78'},
    {'id': 'C' * 32, 'name': 'Саида', 'phone': '••• 90 12'},
]


def settings():
    return Settings(
        booking_api_url='https://booking.example.test',
        booking_api_token='read-secret',
        booking_broadcast_token='write-secret',
    )


def test_broadcast_client_uses_separate_server_token_and_validates_responses():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/audience'):
            return httpx.Response(200, json={
                'subscribers': 3, 'profiles': 5, 'recipients': RECIPIENTS})
        return httpx.Response(202, json=JOB)

    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(handler))
    assert asyncio.run(client.audience())['recipients'][0]['name'] == 'Азиза'
    assert asyncio.run(client.start(JOB['id'], 'Новое меню', ['A' * 32]))['sent'] == 2
    assert asyncio.run(client.status(JOB['id']))['status'] == 'completed'
    assert [request.url.path for request in requests] == [
        '/admin/broadcast/audience', '/admin/broadcast/jobs',
        '/admin/broadcast/jobs/' + JOB['id'],
    ]
    assert all(request.headers['Authorization'] == 'Bearer write-secret' for request in requests)
    assert requests[1].headers['Idempotency-Key'] == JOB['id']
    assert json.loads(requests[1].content) == {
        'text': 'Новое меню', 'recipient_ids': ['A' * 32]}


@pytest.mark.parametrize('payload', [
    {'subscribers': -1, 'profiles': 2, 'recipients': []},
    {'subscribers': 3, 'profiles': 2, 'recipients': RECIPIENTS},
    {'subscribers': True, 'profiles': 2, 'recipients': []},
    {'subscribers': 1, 'profiles': 2, 'recipients': []},
    {'subscribers': 1, 'profiles': 2,
     'recipients': [{'id': 'telegram-id', 'name': 'A', 'phone': '+998'}]},
])
def test_broadcast_client_rejects_invalid_audience(payload):
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))
    with pytest.raises(DataError, match='некорректный ответ'):
        asyncio.run(client.audience())


def test_broadcast_client_preserves_upstream_conflict_without_exposing_details():
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(409, json={'error': 'broadcast_in_progress'})))
    with pytest.raises(BroadcastConflict, match='уже выполняется'):
        asyncio.run(client.start(JOB['id'], 'Текст', ['A' * 32]))


def test_broadcast_client_requires_configuration():
    client = BookingBroadcastClient(Settings())
    with pytest.raises(DataError, match='не настроена'):
        asyncio.run(client.audience())


def test_broadcast_client_rejects_empty_or_invalid_recipient_selection():
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(202, json=JOB)))
    for recipient_ids in ([], ['raw-telegram-id'], ['A' * 32, 'A' * 32]):
        with pytest.raises(DataError, match='получателя'):
            asyncio.run(client.start(JOB['id'], 'Текст', recipient_ids))
