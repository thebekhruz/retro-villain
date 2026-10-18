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
        'text': 'Новое меню', 'recipient_ids': ['A' * 32], 'source': 'dashboard'}


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


def test_broadcast_client_keeps_one_pool_across_polls():
    """Статус рассылки опрашивают до её конца — пул должен жить между опросами."""
    polls = []

    def handler(request):
        polls.append(request.url.path)
        return httpx.Response(200, json=JOB)

    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(handler))
    asyncio.run(client.status(JOB['id']))
    first = client._http
    asyncio.run(client.status(JOB['id']))

    assert first is not None and client._http is first
    assert len(polls) == 2
    asyncio.run(client.close())
    assert client._http is None


# ── Журнал рассылок (ТЗ 16.10, п. 5.3; контракт бота T-503) ─────────────────

JOURNAL_JOB = dict(
    JOB, id='job_restart_1234567890', status='interrupted', audience=5, sent=2, blocked=1, failed=1,
    pending=1, source='dashboard', manual=True, author='Бехруз', text='Вечером живая музыка',
    recipients_known=True, resumable=True, resume_count=0, resumed_at=None, resumed_by=None,
    throttled_until=None)
DELIVERY = {'id': 'A' * 32, 'name': 'Азиза', 'phone': '••• 12 34', 'status': 'sent',
            'error': None, 'at': '2026-10-17T09:00:02.000Z'}


def journal_handler(requests, responses):
    def handler(request):
        requests.append(request)
        return responses[(request.method, request.url.path)]()
    return handler


def test_broadcast_journal_reads_history_deliveries_and_resumes_with_author():
    requests = []
    responses = {
        ('GET', '/admin/broadcast/jobs'): lambda: httpx.Response(200, json={
            # Второе задание — старого формата: полей журнала у него нет.
            'jobs': [JOURNAL_JOB, JOB], 'total': 7, 'next_cursor': 'eyJhIjoiYiJ9'}),
        ('GET', '/admin/broadcast/jobs/job_restart_1234567890/deliveries'): lambda: httpx.Response(200, json={
            'job_id': 'job_restart_1234567890', 'recipients_known': True, 'total': 2,
            # Полный номер от бота до экрана не доходит.
            'recipients': [DELIVERY, dict(DELIVERY, id='B' * 32, name='', phone='+998 90 123 45 67',
                                          status='failed', error='delivery_unknown')],
            'next_offset': None}),
        ('POST', '/admin/broadcast/jobs/job_restart_1234567890/resume'): lambda: httpx.Response(
            202, json=dict(JOURNAL_JOB, status='queued', resumable=False, resume_count=1, resumed_by='Карина')),
        ('POST', '/admin/broadcast/jobs'): lambda: httpx.Response(202, json=JOB),
    }
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(journal_handler(requests, responses)))

    page = asyncio.run(client.list('manual', cursor='eyJjIjoiZCJ9'))
    assert page['supported'] is True and page['total'] == 7 and page['next_cursor'] == 'eyJhIjoiYiJ9'
    journal, legacy = page['jobs']
    assert journal['source'] == 'dashboard' and journal['manual'] is True and journal['author'] == 'Бехруз'
    assert journal['pending'] == 1 and journal['resumable'] is True
    assert legacy['source'] is None and legacy['manual'] is None and legacy['text'] is None
    assert legacy['pending'] == 0 and legacy['recipients_known'] is False and legacy['resumable'] is False
    assert dict(requests[0].url.params) == {
        'limit': '20', 'source': 'dashboard,tg_admin', 'cursor': 'eyJjIjoiZCJ9'}

    asyncio.run(client.list('auto'))
    assert dict(requests[1].url.params) == {'limit': '20', 'source': 'auto'}
    asyncio.run(client.list())
    assert dict(requests[2].url.params) == {'limit': '20'}

    deliveries = asyncio.run(client.deliveries('job_restart_1234567890', ['pending', 'sending'], 100))
    assert [row['phone'] for row in deliveries['recipients']] == ['••• 12 34', '••• 45 67']
    assert deliveries['recipients'][1]['name'] == '' and deliveries['recipients'][1]['error'] == 'delivery_unknown'
    assert dict(requests[3].url.params) == {'limit': '100', 'offset': '100', 'status': 'pending,sending'}

    resumed = asyncio.run(client.resume('job_restart_1234567890', '  Карина  '))
    assert resumed['started'] is True and resumed['job']['resumed_by'] == 'Карина'
    assert json.loads(requests[4].content) == {'author': 'Карина'}

    asyncio.run(client.start(JOB['id'], 'Новое меню', ['A' * 32], author='Карина ' * 20))
    body = json.loads(requests[5].content)
    assert body['source'] == 'dashboard' and len(body['author']) <= 80 and body['author'].startswith('Карина')
    assert all(request.headers['Authorization'] == 'Bearer write-secret' for request in requests)


@pytest.mark.parametrize('status', [404, 405])
def test_broadcast_journal_is_unsupported_on_previous_bot(status):
    """Бот до T-503: на GET /admin/broadcast/jobs — 404 или 405 (там был только запуск)."""
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json={'error': 'method_not_allowed'})))
    assert asyncio.run(client.list()) == {'supported': False, 'jobs': [], 'total': 0, 'next_cursor': None}


@pytest.mark.parametrize('patch', [
    {'source': 'sms'},
    {'manual': 'yes'},
    {'author': 'А' * 81},
    {'pending': 3},
    {'resumable': 'true'},
    {'resume_count': -1},
    {'throttled_until': 1760000000},
    {'text': 'x' * 4097},
])
def test_broadcast_journal_rejects_malformed_job_fields(patch):
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'jobs': [dict(JOURNAL_JOB, **patch)], 'total': 1,
                                                  'next_cursor': None})))
    with pytest.raises(DataError, match='некорректный ответ'):
        asyncio.run(client.list())


def test_broadcast_journal_trusts_source_over_manual_and_resumable_needs_pending():
    from retro.integrations.broadcasts import validate_job
    auto = validate_job(dict(JOURNAL_JOB, source='auto', manual=True))
    assert auto['manual'] is False
    # Досылать некому — кнопки не будет, что бы ни сказал бот.
    finished = validate_job(dict(JOURNAL_JOB, sent=3, pending=0, resumable=True))
    assert finished['resumable'] is False


def test_broadcast_resume_conflicts_and_missing_jobs_are_explained():
    from retro.integrations.broadcasts import BroadcastNotFound
    answers = iter([
        httpx.Response(409, json={'error': 'not_resumable'}),
        httpx.Response(409, json={'error': 'broadcast_in_progress'}),
        httpx.Response(404, json={'error': 'not_found'}),
    ])
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(lambda request: next(answers)))
    with pytest.raises(BroadcastConflict, match='дослать нельзя'):
        asyncio.run(client.resume(JOB['id']))
    with pytest.raises(BroadcastConflict, match='уже выполняется'):
        asyncio.run(client.resume(JOB['id']))
    with pytest.raises(BroadcastNotFound, match='не найдена'):
        asyncio.run(client.resume(JOB['id']))


def test_broadcast_deliveries_validate_request_and_answer():
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={
            'job_id': 'другое-задание', 'recipients_known': True, 'total': 0,
            'recipients': [], 'next_offset': None})))
    with pytest.raises(DataError, match='некорректный ответ'):
        asyncio.run(client.deliveries(JOB['id']))
    for statuses, offset in ((['read'], 0), (['sent', 'sent'], 0), (None, -1)):
        with pytest.raises(DataError, match='Некорректный запрос'):
            asyncio.run(client.deliveries(JOB['id'], statuses, offset))
    with pytest.raises(DataError, match='идентификатор'):
        asyncio.run(client.deliveries('../audience'))


def test_broadcast_audience_phone_is_masked_even_if_bot_sends_it_whole():
    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'subscribers': 1, 'profiles': 1, 'recipients': [
            {'id': 'A' * 32, 'name': 'Азиза', 'phone': '+998901234567'}]})))
    assert asyncio.run(client.audience())['recipients'][0]['phone'] == '••• 45 67'


def test_broadcast_reads_retry_once_on_a_connection_the_bot_already_closed():
    """Node закрывает простаивающее соединение через 5 с, пул держит минуту:
    первое чтение после паузы повторяем один раз. Досылку не повторяем."""
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if len(calls) in (1, 3, 4, 5):
            raise httpx.ReadError('connection closed', request=request)
        return httpx.Response(200, json={'jobs': [JOB], 'total': 1, 'next_cursor': None})

    client = BookingBroadcastClient(settings(), transport=httpx.MockTransport(handler))
    assert asyncio.run(client.list())['jobs'][0]['id'] == JOB['id']
    with pytest.raises(DataError, match='связаться'):
        asyncio.run(client.list())
    with pytest.raises(DataError, match='связаться'):
        asyncio.run(client.resume(JOB['id']))
    assert calls == [('GET', '/admin/broadcast/jobs')] * 4 + [('POST', '/admin/broadcast/jobs/' + JOB['id'] + '/resume')]
