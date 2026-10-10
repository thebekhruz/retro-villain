"""Hikvision ISAPI client for people and entrance events.

Reads people and passes. Writes come only from the manager cabinet: adding
one person (create_person) and that person's face (upload_face). Both are
explicit, never run from the poller, and success means the device confirmed
the record.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Generic, TypeVar
from zoneinfo import ZoneInfo

import httpx

from retro.config import HikvisionConfig


TZ = ZoneInfo('Asia/Tashkent')
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PAGES = 200
# DS-K1T341CM V3.3.40 объявляет maxResults <= 30; больший запрос отклоняется.
PEOPLE_PAGE_SIZE = 30
EVENT_PAGE_SIZE = 50
# Срок действия карточки на устройстве для новых людей. Конец — как у
# заводских карточек Hikvision: «бессрочно» в пределах формата устройства.
VALID_UNTIL = '2037-12-31T23:59:59'
# Лицо человека: библиотека лиц терминала доступа — «blackFD» с номером 1,
# FPID — тот же номер, что у человека (employeeNo).
FACE_RECORD = '/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json'
FACE_SETUP = '/ISAPI/Intelligent/FDLib/FDSetUp?format=json'
FACE_SEARCH = '/ISAPI/Intelligent/FDLib/FDSearch?format=json'
FACE_LIBRARY = {'faceLibType': 'blackFD', 'FDID': '1'}


class HikvisionError(Exception):
    """A safe, categorized failure; message never includes an upstream payload."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class DigestChallenge:
    realm: str
    nonce: str
    qop: str | None = None
    opaque: str | None = None
    algorithm: str = 'MD5'

    @classmethod
    def parse(cls, header: str | None) -> DigestChallenge:
        if not header or not header.lstrip().casefold().startswith('digest '):
            raise HikvisionError('unauthorized')
        values: dict[str, str] = {}
        pattern = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))')
        for match in pattern.finditer(header.lstrip()[7:]):
            value = match.group(2) if match.group(2) is not None else match.group(3)
            values[match.group(1).casefold()] = value.replace(r'\"', '"').replace(r'\\', '\\')
        realm, nonce = values.get('realm'), values.get('nonce')
        if not realm or not nonce or values.get('algorithm', 'MD5').casefold() != 'md5':
            raise HikvisionError('unauthorized')
        qops = [item.strip().casefold() for item in values.get('qop', '').split(',') if item.strip()]
        if qops and 'auth' not in qops:
            raise HikvisionError('unauthorized')
        return cls(realm=realm, nonce=nonce, qop='auth' if qops else None,
                   opaque=values.get('opaque'), algorithm='MD5')


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _quoted(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"')


def build_digest_header(challenge: DigestChallenge, *, username: str, password: str,
                        method: str, request_target: str, nonce_count: int,
                        cnonce: str) -> str:
    ha1 = _md5(f'{username}:{challenge.realm}:{password}')
    ha2 = _md5(f'{method}:{request_target}')
    nc = f'{nonce_count:08x}'
    if challenge.qop:
        response = _md5(f'{ha1}:{challenge.nonce}:{nc}:{cnonce}:{challenge.qop}:{ha2}')
    else:
        response = _md5(f'{ha1}:{challenge.nonce}:{ha2}')
    parts = [
        f'username="{_quoted(username)}"',
        f'realm="{_quoted(challenge.realm)}"',
        f'nonce="{_quoted(challenge.nonce)}"',
        f'uri="{_quoted(request_target)}"',
        f'response="{response}"',
        f'algorithm={challenge.algorithm}',
    ]
    if challenge.opaque is not None:
        parts.append(f'opaque="{_quoted(challenge.opaque)}"')
    if challenge.qop:
        parts.extend((f'qop={challenge.qop}', f'nc={nc}', f'cnonce="{_quoted(cnonce)}"'))
    return 'Digest ' + ', '.join(parts)


@dataclass(frozen=True)
class HikvisionPerson:
    employee_no: str
    name: str | None


@dataclass(frozen=True)
class HikvisionEvent:
    source: str
    serial_no: str
    employee_no: str
    occurred_at: datetime


T = TypeVar('T')


@dataclass(frozen=True)
class IsapiPage(Generic[T]):
    items: tuple[T, ...]
    has_more: bool
    matches: int


def _decoded_container(raw: str, key: str) -> dict:
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise HikvisionError('invalid_response') from None
    if not isinstance(decoded, dict) or not isinstance(decoded.get(key), dict):
        raise HikvisionError('invalid_response')
    return decoded[key]


def _integer(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_people_page(raw: str) -> IsapiPage[HikvisionPerson]:
    search = _decoded_container(raw, 'UserInfoSearch')
    raw_items = search.get('UserInfo', [])
    if not isinstance(raw_items, list):
        raise HikvisionError('invalid_response')
    people = []
    for item in raw_items:
        if not isinstance(item, dict) or item.get('employeeNo') is None:
            continue
        employee_no = str(item['employeeNo']).strip()
        if not employee_no:
            continue
        raw_name = item.get('name')
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else None
        people.append(HikvisionPerson(employee_no, name))
    matches = _integer(search.get('numOfMatches'), len(raw_items))
    return IsapiPage(tuple(people), str(search.get('responseStatusStrg', '')).upper() == 'MORE',
                     matches)


def _status_payload(raw: bytes) -> dict | None:
    """Ответ устройства на запись: {"statusCode": 1, "statusString": "OK", …}."""
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeDecodeError):
        return None
    if isinstance(decoded, dict) and isinstance(decoded.get('ResponseStatus'), dict):
        decoded = decoded['ResponseStatus']
    return decoded if isinstance(decoded, dict) else None


def _status_text(status: dict) -> str:
    text = ' '.join(str(status.get(key, '')) for key in ('subStatusCode', 'errorMsg', 'statusString'))
    return text.replace(' ', '').casefold()


def _already_exists(status: dict) -> bool:
    """«employeeNoAlreadyExist» / «employeeNo already exist» — номер занят."""
    return 'alreadyexist' in _status_text(status)


def _accepted(response: httpx.Response, status: dict | None) -> bool:
    return (response.status_code == 200 and status is not None
            and _integer(status.get('statusCode'), None) == 1)


def _face_exists(status: dict) -> bool:
    """«deviceUserAlreadyExistFace» / «faceExist» — у человека лицо уже есть."""
    text = _status_text(status)
    return 'faceexist' in text or ('face' in text and 'alreadyexist' in text)


def _face_failure(response: httpx.Response, status: dict | None) -> HikvisionError:
    if response.status_code == 200 and status is None:
        return HikvisionError('invalid_response')
    # «Invalid Content» на фото — устройство не нашло или не приняло лицо.
    # Пропавший человек (…NotExist) — не про снимок, это обычный отказ.
    if (status is not None and _integer(status.get('statusCode'), None) == 6
            and 'notexist' not in _status_text(status)):
        return HikvisionError('face_rejected')
    return HikvisionError('device_error')


def face_multipart(employee_no: str, image: bytes, mime: str = 'image/jpeg',
                   boundary: str | None = None) -> tuple[bytes, str]:
    """Тело FaceDataRecord / FDSetUp: JSON-часть «FaceDataRecord» и снимок «img».

    Тело собирается целиком заранее: digest-повтор после 401 шлёт те же байты."""
    record = json.dumps({**FACE_LIBRARY, 'FPID': employee_no}, separators=(',', ':')).encode()
    while boundary is None or boundary.encode() in image:
        boundary = 'retro' + secrets.token_hex(12)
    extension = 'png' if mime == 'image/png' else 'jpg'
    mark = b'--' + boundary.encode()
    content = b''.join((
        mark, b'\r\nContent-Disposition: form-data; name="FaceDataRecord"\r\n',
        b'Content-Type: application/json\r\n\r\n', record, b'\r\n',
        mark, b'\r\nContent-Disposition: form-data; name="img"; filename="face.',
        extension.encode(), b'"\r\nContent-Type: ', mime.encode(), b'\r\n\r\n', image, b'\r\n',
        mark, b'--\r\n'))
    return content, f'multipart/form-data; boundary={boundary}'


def _event_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TZ)
    return parsed.astimezone(TZ)


def parse_events_page(raw: str, source: str = 'main-entry') -> IsapiPage[HikvisionEvent]:
    search = _decoded_container(raw, 'AcsEvent')
    raw_items = search.get('InfoList', [])
    if not isinstance(raw_items, list):
        raise HikvisionError('invalid_response')
    events = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise HikvisionError('invalid_response')
        major, minor = _integer(item.get('major'), None), _integer(item.get('minor'), None)
        if major is None or minor is None:
            raise HikvisionError('invalid_response')
        if major != 5 or minor != 75:
            continue
        serial_no = str(item.get('serialNo', '')).strip()
        employee_no = str(item.get('employeeNoString', item.get('employeeNo', ''))).strip()
        occurred_at = _event_time(item.get('time'))
        if (not serial_no or serial_no == 'None' or not employee_no
                or employee_no == 'None' or occurred_at is None):
            raise HikvisionError('invalid_response')
        events.append(HikvisionEvent(source, serial_no, employee_no, occurred_at))
    matches = _integer(search.get('numOfMatches'), len(raw_items))
    return IsapiPage(tuple(events), str(search.get('responseStatusStrg', '')).upper() == 'MORE',
                     matches)


class HikvisionClient:
    def __init__(self, config: HikvisionConfig, *, http: httpx.AsyncClient | None = None,
                 cnonce: Callable[[], str] | None = None):
        self.config = config
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=config.timeout_seconds, verify=config.verify_tls, follow_redirects=False)
        self._cnonce = cnonce or (lambda: secrets.token_hex(8))
        self._challenge: DigestChallenge | None = None
        self._nonce_count = 0

    def _url(self, path: str) -> str:
        return self.config.base_url + path

    async def _ensure_challenge(self):
        if self._challenge is not None:
            return
        try:
            response = await self._http.get(self._url('/ISAPI/System/deviceInfo'))
        except httpx.TimeoutException:
            raise HikvisionError('timeout') from None
        except httpx.HTTPError:
            raise HikvisionError('network') from None
        self._challenge = DigestChallenge.parse(response.headers.get('WWW-Authenticate'))
        self._nonce_count = 0

    def _authorization(self, method: str, target: str) -> str:
        if self._challenge is None:
            raise HikvisionError('unauthorized')
        self._nonce_count += 1
        return build_digest_header(
            self._challenge, username=self.config.username, password=self.config.password,
            method=method, request_target=target, nonce_count=self._nonce_count,
            cnonce=self._cnonce())

    async def _attempt(self, method: str, target: str, content: bytes,
                       content_type: str) -> httpx.Response:
        headers = {
            'Authorization': self._authorization(method, target),
            'Content-Type': content_type,
            'Content-Length': str(len(content)),
        }
        try:
            return await self._http.request(method, self._url(target), content=content, headers=headers)
        except httpx.TimeoutException:
            raise HikvisionError('timeout') from None
        except httpx.HTTPError:
            raise HikvisionError('network') from None

    async def _send(self, method: str, target: str, content: bytes,
                    content_type: str) -> httpx.Response:
        """Запрос с digest: JSON и multipart идут одним путём. Устаревший
        nonce (401) обновляется один раз, и тело уходит повторно как есть."""
        await self._ensure_challenge()
        response = await self._attempt(method, target, content, content_type)
        if response.status_code == 401:
            self._challenge = DigestChallenge.parse(response.headers.get('WWW-Authenticate'))
            self._nonce_count = 0
            response = await self._attempt(method, target, content, content_type)
        if response.status_code == 401:
            raise HikvisionError('unauthorized')
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise HikvisionError('invalid_response')
        return response

    async def _exchange(self, target: str, body: dict) -> httpx.Response:
        content = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        return await self._send('POST', target, content, 'application/json')

    async def _post_json(self, target: str, body: dict) -> str:
        response = await self._exchange(target, body)
        if response.status_code != 200:
            raise HikvisionError('device_error')
        try:
            return response.content.decode('utf-8')
        except UnicodeDecodeError:
            raise HikvisionError('invalid_response') from None

    async def find_person(self, employee_no: str) -> HikvisionPerson | None:
        """Один человек по номеру на устройстве; None — такого номера нет."""
        raw = await self._post_json('/ISAPI/AccessControl/UserInfo/Search?format=json', {
            'UserInfoSearchCond': {
                'searchID': 'retro-one', 'searchResultPosition': 0, 'maxResults': 1,
                'EmployeeNoList': [{'employeeNo': employee_no}],
            }})
        page = parse_people_page(raw)
        return next((person for person in page.items if person.employee_no == employee_no), None)

    async def create_person(self, employee_no: str, name: str, *, valid_from: date) -> str:
        """Добавить человека на устройство. 'created' — устройство подтвердило
        запись; 'exists' — номер уже есть (повтор после обрыва связи). Что
        именно лежит под номером, вызывающий проверяет поиском."""
        response = await self._exchange('/ISAPI/AccessControl/UserInfo/Record?format=json', {
            'UserInfo': {
                'employeeNo': employee_no, 'name': name, 'userType': 'normal',
                'Valid': {'enable': True, 'beginTime': f'{valid_from.isoformat()}T00:00:00',
                          'endTime': VALID_UNTIL, 'timeType': 'local'},
                'doorRight': '1', 'RightPlan': [{'doorNo': 1, 'planTemplateNo': '1'}],
            }})
        status = _status_payload(response.content)
        if response.status_code == 200 and status is not None and \
                _integer(status.get('statusCode'), None) == 1:
            return 'created'
        if status is not None and _already_exists(status):
            return 'exists'
        if response.status_code == 200 and status is None:
            raise HikvisionError('invalid_response')
        raise HikvisionError('device_error')

    async def has_face(self, employee_no: str) -> bool:
        """Проверка лица по FPID. Фото по faceURL не скачиваем; возвращённый
        устройством modelData не сохраняем и не передаём в интерфейс."""
        response = await self._exchange(FACE_SEARCH, {
            **FACE_LIBRARY, 'FPID': employee_no, 'searchResultPosition': 0, 'maxResults': 1,
        })
        payload = _status_payload(response.content)
        if not _accepted(response, payload):
            raise HikvisionError('invalid_response' if response.status_code == 200
                                and payload is None else 'device_error')
        matches = payload.get('MatchList', [])
        count = _integer(payload.get('numOfMatches'), None)
        if not isinstance(matches, list) or count is None or count != len(matches):
            raise HikvisionError('invalid_response')
        if any(not isinstance(item, dict) or 'FPID' not in item for item in matches):
            raise HikvisionError('invalid_response')
        return any(str(item['FPID']) == employee_no for item in matches)

    async def upload_face(self, employee_no: str, image: bytes, *, mime: str = 'image/jpeg') -> str:
        """Лицо человека под номером employee_no. 'created' — устройство
        приняло новое лицо; 'replaced' — лицо у номера уже было, и его
        заменили (FDSetUp). Человек на устройстве должен уже быть. Успех требует
        положительного ответа на запись и последующего поиска именно этого FPID.
        После обрыва записи наличие старого лица не считается успехом."""
        replacing = await self.has_face(employee_no)
        content, content_type = face_multipart(employee_no, image, mime)
        response = await self._send('PUT' if replacing else 'POST',
                                    FACE_SETUP if replacing else FACE_RECORD, content, content_type)
        status = _status_payload(response.content)
        # Лицо могло появиться между поиском и записью (другая панель, повтор).
        if not replacing and status is not None and _face_exists(status):
            replacing = True
            response = await self._send('PUT', FACE_SETUP, content, content_type)
            status = _status_payload(response.content)
        if not _accepted(response, status):
            raise _face_failure(response, status)
        if not await self.has_face(employee_no):
            raise HikvisionError('not_confirmed')
        return 'replaced' if replacing else 'created'

    async def fetch_people(self) -> tuple[HikvisionPerson, ...]:
        people: list[HikvisionPerson] = []
        position = 0
        for _ in range(MAX_PAGES):
            raw = await self._post_json('/ISAPI/AccessControl/UserInfo/Search?format=json', {
                'UserInfoSearchCond': {
                    'searchID': 'retro', 'searchResultPosition': position,
                    'maxResults': PEOPLE_PAGE_SIZE,
                }})
            page = parse_people_page(raw)
            people.extend(page.items)
            if not page.has_more:
                return tuple(people)
            if page.matches <= 0:
                raise HikvisionError('invalid_response')
            position += page.matches
        raise HikvisionError('invalid_response')

    async def fetch_events(self, from_at: datetime,
                           through_at: datetime) -> tuple[HikvisionEvent, ...]:
        events: list[HikvisionEvent] = []
        position = 0
        for _ in range(MAX_PAGES):
            raw = await self._post_json('/ISAPI/AccessControl/AcsEvent?format=json', {
                'AcsEventCond': {
                    'searchID': 'retro', 'searchResultPosition': position,
                    'maxResults': EVENT_PAGE_SIZE, 'major': 0, 'minor': 0,
                    'startTime': from_at.astimezone(TZ).isoformat(timespec='seconds'),
                    'endTime': through_at.astimezone(TZ).isoformat(timespec='seconds'),
                }})
            page = parse_events_page(raw, self.config.source)
            events.extend(page.items)
            if not page.has_more:
                return tuple(events)
            if page.matches <= 0:
                raise HikvisionError('invalid_response')
            position += page.matches
        raise HikvisionError('invalid_response')

    async def close(self):
        if self._owns_http:
            await self._http.aclose()
