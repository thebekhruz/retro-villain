import hashlib
import json
import asyncio
import re
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
    assert json.loads(requests[1].content)['UserInfoSearchCond']['maxResults'] == 30


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
        'responseStatusStrg': 'OK', 'numOfMatches': 2, 'InfoList': [
            {'major': 5, 'minor': 75, 'serialNo': '7', 'employeeNoString': '10',
             'time': '2026-09-21T09:05:00+05:00', 'pictureURL': 'https://ignored/photo.jpg'},
            {'major': 5, 'minor': 76, 'serialNo': '8', 'employeeNoString': '10',
             'time': '2026-09-21T09:06:00+05:00'},
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


def _device(handler):
    """Устройство с digest-вызовом на deviceInfo и ответами handler на POST."""
    requests = []

    def terminal(request: httpx.Request):
        if request.method == 'GET':
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="abc", qop="auth"'})
        assert request.headers['authorization'].startswith('Digest ')
        requests.append((request.url.path, json.loads(request.content)))
        return handler(request)

    return terminal, requests


def _run(handler, work):
    terminal, requests = _device(handler)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(terminal)) as http:
            return await work(HikvisionClient(CONFIG, http=http, cnonce=lambda: '0011223344556677'))

    return asyncio.run(exercise()), requests


def test_create_person_posts_userinfo_record_with_validity_and_door():
    """Кабинет менеджера (T-432): единственная запись в устройство."""
    from datetime import date
    result, requests = _run(
        lambda request: httpx.Response(200, json={'statusCode': 1, 'statusString': 'OK', 'subStatusCode': 'ok'}),
        lambda client: client.create_person('134', 'Карамат', valid_from=date(2026, 10, 10)))
    assert result == 'created'
    path, body = requests[0]
    assert path == '/ISAPI/AccessControl/UserInfo/Record'
    assert body == {'UserInfo': {
        'employeeNo': '134', 'name': 'Карамат', 'userType': 'normal',
        'Valid': {'enable': True, 'beginTime': '2026-10-10T00:00:00', 'endTime': '2037-12-31T23:59:59',
                  'timeType': 'local'},
        'doorRight': '1', 'RightPlan': [{'doorNo': 1, 'planTemplateNo': '1'}]}}


@pytest.mark.parametrize('status, payload', [
    (400, {'statusCode': 6, 'statusString': 'Invalid Content', 'subStatusCode': 'employeeNoAlreadyExist'}),
    (200, {'statusCode': 6, 'statusString': 'Invalid Content', 'errorMsg': 'employeeNo already exist'}),
])
def test_existing_number_is_reported_not_raised(status, payload):
    from datetime import date
    result, _ = _run(lambda request: httpx.Response(status, json=payload),
                     lambda client: client.create_person('134', 'Карамат', valid_from=date(2026, 10, 10)))
    assert result == 'exists'


@pytest.mark.parametrize('status, payload, code', [
    (400, {'statusCode': 4, 'statusString': 'Invalid Operation', 'subStatusCode': 'notSupport'}, 'device_error'),
    (500, None, 'device_error'),
    (200, None, 'invalid_response'),
])
def test_refused_or_unreadable_record_is_an_error(status, payload, code):
    from datetime import date

    def handler(request):
        if payload is None:
            return httpx.Response(status, content=b'<html>oops</html>')
        return httpx.Response(status, json=payload)

    with pytest.raises(HikvisionError) as caught:
        _run(handler, lambda client: client.create_person('134', 'Карамат', valid_from=date(2026, 10, 10)))
    assert caught.value.code == code


def test_find_person_searches_one_employee_number():
    def handler(request):
        return httpx.Response(200, json={'UserInfoSearch': {
            'responseStatusStrg': 'OK', 'numOfMatches': 1,
            'UserInfo': [{'employeeNo': '134', 'name': 'Карамат'}]}})

    person, requests = _run(handler, lambda client: client.find_person('134'))
    assert person.name == 'Карамат'
    assert requests[0][1]['UserInfoSearchCond']['EmployeeNoList'] == [{'employeeNo': '134'}]

    missing, _ = _run(lambda request: httpx.Response(200, json={'UserInfoSearch': {
        'responseStatusStrg': 'NO MATCH', 'numOfMatches': 0}}), lambda client: client.find_person('999'))
    assert missing is None


# ── Лицо человека (фото из кабинета менеджера) ─────────────────────────────

JPEG = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00' + bytes(range(256)) + b'\r\n--\r\n\xff\xd9'


def face_parts(content: bytes, content_type: str) -> dict[str, tuple[str, str | None, bytes]]:
    """multipart/form-data → {имя части: (Content-Type, filename, байты)}."""
    boundary = content_type.split('boundary=', 1)[1].encode()
    chunks = content.split(b'--' + boundary)
    assert chunks[0] == b'' and chunks[-1] == b'--\r\n'
    parts = {}
    for chunk in chunks[1:-1]:
        assert chunk.startswith(b'\r\n') and chunk.endswith(b'\r\n')
        head, _, body = chunk[2:-2].partition(b'\r\n\r\n')
        headers = dict(line.split(': ', 1) for line in head.decode().split('\r\n'))
        disposition = headers['Content-Disposition']
        name = re.search(r'\bname="([^"]+)"', disposition).group(1)
        filename = re.search(r'filename="([^"]+)"', disposition)
        parts[name] = (headers['Content-Type'], filename.group(1) if filename else None, body)
    return parts


def digest_matches(request: httpx.Request) -> bool:
    """Подпись digest посчитана для этого метода и адреса (PUT ≠ POST)."""
    header = request.headers['authorization']
    values = {match.group(1): match.group(2) if match.group(2) is not None else match.group(3)
              for match in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header[7:])}
    target = request.url.raw_path.decode()
    ha1 = hashlib.md5(f'reader:{values["realm"]}:secret'.encode()).hexdigest()
    ha2 = hashlib.md5(f'{request.method}:{target}'.encode()).hexdigest()
    expected = hashlib.md5(f'{ha1}:{values["nonce"]}:{values["nc"]}:{values["cnonce"]}:'
                           f'{values["qop"]}:{ha2}'.encode()).hexdigest()
    return values['uri'] == target and values['response'] == expected


def _face_run(handler, work, *, existing=False, search_handler=None):
    requests = []
    stored = existing

    def terminal(request: httpx.Request):
        nonlocal stored
        if request.method == 'GET':
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="abc", qop="auth"'})
        assert digest_matches(request)
        requests.append(request)
        if request.url.path.endswith('/FDSearch'):
            query = json.loads(request.content)
            assert query == {'faceLibType': 'blackFD', 'FDID': '1', 'FPID': '134',
                             'searchResultPosition': 0, 'maxResults': 1}
            if search_handler:
                return search_handler(request)
            return httpx.Response(200, json={**OK, 'numOfMatches': int(stored),
                                             'MatchList': [{'FPID': '134'}] if stored else []})
        response = handler(request)
        try:
            if response.status_code == 200 and response.json().get('statusCode') == 1:
                stored = True
        except ValueError:
            pass
        return response

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(terminal)) as http:
            return await work(HikvisionClient(CONFIG, http=http, cnonce=lambda: '0011223344556677'))

    return asyncio.run(exercise()), requests


OK = {'statusCode': 1, 'statusString': 'OK', 'subStatusCode': 'ok'}


def test_upload_face_posts_face_data_record_multipart_with_fpid():
    result, requests = _face_run(lambda request: httpx.Response(200, json=OK),
                                 lambda client: client.upload_face('134', JPEG))
    assert result == 'created'
    before, request, after = requests
    assert before.url.path == after.url.path == '/ISAPI/Intelligent/FDLib/FDSearch'
    assert (request.method, request.url.path, request.url.query) == (
        'POST', '/ISAPI/Intelligent/FDLib/FaceDataRecord', b'format=json')
    assert request.headers['content-type'].startswith('multipart/form-data; boundary=')
    assert request.headers['content-length'] == str(len(request.content))
    parts = face_parts(request.content, request.headers['content-type'])
    kind, filename, record = parts['FaceDataRecord']
    assert (kind, filename) == ('application/json', None)
    assert json.loads(record) == {'faceLibType': 'blackFD', 'FDID': '1', 'FPID': '134'}
    assert parts['img'] == ('image/jpeg', 'face.jpg', JPEG)


def test_face_multipart_is_readable_by_a_standard_form_parser():
    from starlette.requests import Request
    from retro.integrations.hikvision import face_multipart
    content, content_type = face_multipart('134', JPEG)

    async def parse():
        async def receive():
            return {'type': 'http.request', 'body': content, 'more_body': False}
        form = await Request({'type': 'http', 'method': 'POST', 'headers': [
            (b'content-type', content_type.encode())]}, receive).form()
        image = form['img']
        return form['FaceDataRecord'], image.filename, image.content_type, await image.read()

    record, filename, kind, image = asyncio.run(parse())
    assert json.loads(record)['FPID'] == '134'
    assert (filename, kind, image) == ('face.jpg', 'image/jpeg', JPEG)


@pytest.mark.parametrize('payload', [
    {'statusCode': 6, 'statusString': 'Invalid Content', 'subStatusCode': 'deviceUserAlreadyExistFace'},
    {'statusCode': 6, 'statusString': 'Invalid Content', 'errorMsg': 'faceExist'},
])
def test_existing_face_is_replaced_through_fd_setup(payload):
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(400, json=payload)
        return httpx.Response(200, json=OK)

    result, requests = _face_run(handler, lambda client: client.upload_face('134', JPEG))
    assert result == 'replaced'
    assert [(request.method, request.url.path) for request in requests] == [
        ('POST', '/ISAPI/Intelligent/FDLib/FDSearch'),
        ('POST', '/ISAPI/Intelligent/FDLib/FaceDataRecord'),
        ('PUT', '/ISAPI/Intelligent/FDLib/FDSetUp'),
        ('POST', '/ISAPI/Intelligent/FDLib/FDSearch')]
    # Тот же снимок и тот же FPID, digest подписан методом PUT (digest_matches).
    assert face_parts(requests[2].content, requests[2].headers['content-type']) == \
        face_parts(requests[1].content, requests[1].headers['content-type'])


def test_face_digest_refresh_resends_the_same_multipart_body():
    bodies = []

    def terminal(request: httpx.Request):
        if request.method == 'GET':
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="old", qop="auth"'})
        if request.url.path.endswith('/FDSearch'):
            return httpx.Response(200, json={**OK, 'numOfMatches': int(bool(bodies)),
                                             'MatchList': [{'FPID': '134'}] if bodies else []})
        bodies.append((request.headers['authorization'], request.content))
        if len(bodies) == 1:
            return httpx.Response(401, headers={
                'WWW-Authenticate': 'Digest realm="terminal", nonce="new", qop="auth"'})
        return httpx.Response(200, json=OK)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(terminal)) as http:
            return await HikvisionClient(CONFIG, http=http).upload_face('134', JPEG)

    assert asyncio.run(exercise()) == 'created'
    assert 'nonce="old"' in bodies[0][0] and 'nonce="new"' in bodies[1][0]
    assert bodies[0][1] == bodies[1][1]


@pytest.mark.parametrize('status, payload, code', [
    (400, {'statusCode': 6, 'statusString': 'Invalid Content', 'subStatusCode': 'faceModelingFailed'},
     'face_rejected'),
    (400, {'statusCode': 6, 'statusString': 'Invalid Content', 'subStatusCode': 'employeeNoNotExist'},
     'device_error'),
    (400, {'statusCode': 4, 'statusString': 'Invalid Operation', 'subStatusCode': 'notSupport'}, 'device_error'),
    (500, None, 'device_error'),
    (200, None, 'invalid_response'),
])
def test_refused_or_unreadable_face_is_an_error(status, payload, code):
    def handler(request):
        if payload is None:
            return httpx.Response(status, content=b'<html>oops</html>')
        return httpx.Response(status, json=payload)

    with pytest.raises(HikvisionError) as caught:
        _face_run(handler, lambda client: client.upload_face('134', JPEG))
    assert caught.value.code == code


def test_refused_face_replacement_is_an_error():
    def handler(request):
        if request.method == 'POST':
            return httpx.Response(400, json={'statusCode': 6, 'subStatusCode': 'deviceUserAlreadyExistFace'})
        return httpx.Response(500, content=b'')

    with pytest.raises(HikvisionError) as caught:
        _face_run(handler, lambda client: client.upload_face('134', JPEG))
    assert caught.value.code == 'device_error'


def test_known_face_is_updated_directly_without_deleting_or_recreating_person():
    result, requests = _face_run(lambda request: httpx.Response(200, json=OK),
                                 lambda client: client.upload_face('134', JPEG), existing=True)
    assert result == 'replaced'
    assert [(request.method, request.url.path.rsplit('/', 1)[-1]) for request in requests] == [
        ('POST', 'FDSearch'), ('PUT', 'FDSetUp'), ('POST', 'FDSearch')]


@pytest.mark.parametrize('matches', [[], [{'FPID': '999'}]])
def test_acknowledgement_without_the_target_face_is_not_success(matches):
    with pytest.raises(HikvisionError, match='not_confirmed'):
        _face_run(lambda request: httpx.Response(200, json=OK),
                  lambda client: client.upload_face('134', JPEG),
                  search_handler=lambda request: httpx.Response(200, json={
                      **OK, 'numOfMatches': len(matches), 'MatchList': matches}))


@pytest.mark.parametrize('payload', [
    OK, {**OK, 'numOfMatches': 1}, {**OK, 'numOfMatches': 1, 'MatchList': [{}]},
    {**OK, 'numOfMatches': 1, 'MatchList': 'invalid'},
])
def test_invalid_face_search_never_confirms_a_photo(payload):
    with pytest.raises(HikvisionError, match='invalid_response'):
        _face_run(lambda request: pytest.fail('must not write after invalid search'),
                  lambda client: client.upload_face('134', JPEG),
                  search_handler=lambda request: httpx.Response(200, json=payload))
