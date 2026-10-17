"""Read-only Hikvision ISAPI client for people and entrance events."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Generic, TypeVar
from zoneinfo import ZoneInfo

import httpx

from retro.config import HikvisionConfig


TZ = ZoneInfo('Asia/Tashkent')
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PAGES = 200
PEOPLE_PAGE_SIZE = 60
EVENT_PAGE_SIZE = 50


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
        if not isinstance(item, dict) or _integer(item.get('major')) != 5 \
                or _integer(item.get('minor')) != 75:
            continue
        serial_no = str(item.get('serialNo', '')).strip()
        employee_no = str(item.get('employeeNoString', item.get('employeeNo', ''))).strip()
        occurred_at = _event_time(item.get('time'))
        if not serial_no or not employee_no or occurred_at is None:
            continue
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

    def _authorization(self, target: str) -> str:
        if self._challenge is None:
            raise HikvisionError('unauthorized')
        self._nonce_count += 1
        return build_digest_header(
            self._challenge, username=self.config.username, password=self.config.password,
            method='POST', request_target=target, nonce_count=self._nonce_count,
            cnonce=self._cnonce())

    async def _attempt(self, target: str, content: bytes) -> httpx.Response:
        headers = {
            'Authorization': self._authorization(target),
            'Content-Type': 'application/json',
            'Content-Length': str(len(content)),
        }
        try:
            return await self._http.post(self._url(target), content=content, headers=headers)
        except httpx.TimeoutException:
            raise HikvisionError('timeout') from None
        except httpx.HTTPError:
            raise HikvisionError('network') from None

    async def _post_json(self, target: str, body: dict) -> str:
        await self._ensure_challenge()
        content = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        response = await self._attempt(target, content)
        if response.status_code == 401:
            self._challenge = DigestChallenge.parse(response.headers.get('WWW-Authenticate'))
            self._nonce_count = 0
            response = await self._attempt(target, content)
        if response.status_code == 401:
            raise HikvisionError('unauthorized')
        if response.status_code != 200:
            raise HikvisionError('device_error')
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise HikvisionError('invalid_response')
        try:
            return response.content.decode('utf-8')
        except UnicodeDecodeError:
            raise HikvisionError('invalid_response') from None

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
