import hashlib
import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from retro.config import HikvisionConfig
from retro.integrations.hikvision import (
    DigestChallenge,
    HikvisionClient,
    HikvisionError,
    build_digest_header,
    parse_events_page,
    parse_people_page,
)


CONFIG = HikvisionConfig(
    base_url='https://203.0.113.10:8443', username='reader', password='secret',
    source='main-entry', poll_seconds=30, timeout_seconds=8, verify_tls=True)


def test_digest_challenge_and_header_include_query_in_ha2():
    challenge = DigestChallenge.parse(
        'Digest realm="terminal", nonce="abc", qop="auth,auth-int", '
        'opaque="opaque-value", algorithm=MD5')

    header = build_digest_header(
        challenge, username='reader', password='secret', method='POST',
        request_target='/ISAPI/AccessControl/AcsEvent?format=json',
        nonce_count=1, cnonce='0011223344556677')

    ha1 = hashlib.md5(b'reader:terminal:secret').hexdigest()
    ha2 = hashlib.md5(b'POST:/ISAPI/AccessControl/AcsEvent?format=json').hexdigest()
    expected = hashlib.md5(
        f'{ha1}:abc:00000001:0011223344556677:auth:{ha2}'.encode()).hexdigest()
    assert 'qop=auth' in header
    assert 'nc=00000001' in header
    assert 'opaque="opaque-value"' in header
    assert f'response="{expected}"' in header


@pytest.mark.parametrize('value', [None, '', 'Basic realm="x"', 'Digest realm="x"'])
def test_invalid_digest_challenge_is_rejected(value):
    with pytest.raises(HikvisionError) as caught:
        DigestChallenge.parse(value)
    assert caught.value.code == 'unauthorized'
    if value:
        assert value not in str(caught.value)


def test_transport_probes_then_posts_replayable_json_with_digest():
    requests = []

    def terminal(request: httpx.Request):
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="abc", qop="auth"'})
        assert request.headers['authorization'].startswith('Digest ')
        assert request.headers['content-length'] == str(len(request.content))
        return httpx.Response(200, json={
            'UserInfoSearch': {'responseStatusStrg': 'OK', 'UserInfo': []}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(terminal)) as http:
            client = HikvisionClient(CONFIG, http=http, cnonce=lambda: '0011223344556677')
            await client.fetch_people()

    asyncio.run(exercise())

    assert [(request.method, request.url.path) for request in requests] == [
        ('GET', '/ISAPI/System/deviceInfo'),
        ('POST', '/ISAPI/AccessControl/UserInfo/Search'),
    ]
    assert json.loads(requests[1].content)['UserInfoSearchCond']['maxResults'] == 60


def test_transport_refreshes_stale_nonce_once():
    nonces = []

    def terminal(request: httpx.Request):
        if request.method == 'GET':
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="old", qop="auth"'})
        auth = request.headers['authorization']
        nonces.append('new' if 'nonce="new"' in auth else 'old')
        if len(nonces) == 1:
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="new", qop="auth"'})
        return httpx.Response(200, json={
            'UserInfoSearch': {'responseStatusStrg': 'OK', 'UserInfo': []}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(terminal)) as http:
            await HikvisionClient(CONFIG, http=http).fetch_people()

    asyncio.run(exercise())

    assert nonces == ['old', 'new']


def test_people_page_is_typed_and_uses_device_pagination_status():
    page = parse_people_page(json.dumps({'UserInfoSearch': {
        'responseStatusStrg': 'MORE', 'numOfMatches': 3,
        'UserInfo': [
            {'employeeNo': '10', 'name': '  Азиза  '},
            {'employeeNo': 11},
            {'name': 'Нет номера'},
        ]}}))

    assert [(person.employee_no, person.name) for person in page.items] == [
        ('10', 'Азиза'), ('11', None)]
    assert page.has_more is True
    assert page.matches == 3


def test_event_page_filters_to_successful_passes_and_parses_tashkent_time():
    page = parse_events_page(json.dumps({'AcsEvent': {
        'responseStatusStrg': 'OK', 'numOfMatches': 4, 'InfoList': [
            {'major': 5, 'minor': 75, 'serialNo': '7', 'employeeNoString': '10',
             'time': '2026-09-21T09:05:00+05:00', 'pictureURL': 'https://ignored/photo.jpg'},
            {'major': 5, 'minor': 76, 'serialNo': '8', 'employeeNoString': '10',
             'time': '2026-09-21T09:06:00+05:00'},
            {'major': 5, 'minor': 75, 'serialNo': '', 'employeeNoString': '10',
             'time': '2026-09-21T09:07:00+05:00'},
            {'major': 5, 'minor': 75, 'serialNo': '9', 'employeeNoString': '',
             'time': '2026-09-21T09:08:00+05:00'},
        ]}}))

    assert len(page.items) == 1
    assert page.items[0].serial_no == '7'
    assert page.items[0].employee_no == '10'
    assert page.items[0].occurred_at == datetime(2026, 9, 21, 9, 5,
                                                  tzinfo=ZoneInfo('Asia/Tashkent'))
    assert not hasattr(page.items[0], 'picture_url')


@pytest.mark.parametrize('raw', ['', 'nope', '{}', '{"AcsEvent": []}'])
def test_malformed_structural_page_fails_closed(raw):
    with pytest.raises(HikvisionError) as caught:
        parse_events_page(raw)
    assert caught.value.code == 'invalid_response'
    if raw:
        assert raw not in str(caught.value)


def test_event_fetch_paginates_by_num_matches():
    positions = []

    def terminal(request: httpx.Request):
        if request.method == 'GET':
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="abc", qop="auth"'})
        body = json.loads(request.content)
        position = body['AcsEventCond']['searchResultPosition']
        positions.append(position)
        more = position == 0
        return httpx.Response(200, json={'AcsEvent': {
            'responseStatusStrg': 'MORE' if more else 'OK',
            'numOfMatches': 2 if more else 1,
            'InfoList': [{
                'major': 5, 'minor': 75, 'serialNo': str(position + 1),
                'employeeNoString': '10', 'time': '2026-09-21T09:05:00+05:00'}],
        }})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(terminal)) as http:
            return await HikvisionClient(CONFIG, http=http).fetch_events(
                datetime(2026, 9, 21, tzinfo=ZoneInfo('Asia/Tashkent')),
                datetime(2026, 9, 21, 12, tzinfo=ZoneInfo('Asia/Tashkent')))

    events = asyncio.run(exercise())

    assert positions == [0, 2]
    assert [event.serial_no for event in events] == ['1', '3']
