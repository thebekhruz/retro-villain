"""Read-only view of faces already enrolled on Hikvision.

Only linked employee numbers are used. Local uploads always take priority;
device metadata never confirms or overwrites a pending local replacement.

T-473: снимки с терминала держим в памяти (по номеру и адресу лица) и докачиваем
фоном — список менеджера открывается сразу с фото, а не ждёт устройство на каждую
картинку (раньше: по два запроса за раз, таймаут 8 с, без кеша — часть фото
не успевала и оставались инициалы).
"""

import asyncio
import hashlib
import logging
from datetime import datetime
from time import monotonic

from retro.integrations.hikvision import HikvisionError
from retro.modules.cashier.service import TZ

PHOTO_SYNC_FAILED = 'Не удалось проверить фото в Hikvision. Обновите страницу после восстановления связи.'
PHOTO_SYNC_MISSING = 'Hikvision не подключён — фото на устройстве пока не проверены.'


class DevicePhotos:
    def __init__(self, *, clock=monotonic):
        self._clock = clock
        self._expires = 0
        self._people = {}
        self._checked_at = None
        self._error = None
        self._lock = asyncio.Lock()
        self.downloads = asyncio.Semaphore(2)
        # employee_no -> (адрес лица, байты, mime): живёт до перезапуска; после
        # него фоновая докачка (warm) заполняет его заново.
        self._faces: dict[str, tuple[str, bytes, str]] = {}
        self._warming: asyncio.Task | None = None

    async def enrich(self, rows, client):
        wanted = [row for row in rows if row['hikvision_id'] and not row['manual_attendance']
                  and not row['photo_updated_at']]
        if not wanted:
            return rows, None
        if client is None:
            self._error = PHOTO_SYNC_MISSING
        elif self._clock() >= self._expires:
            async with self._lock:
                if self._clock() >= self._expires:
                    try:
                        people = await client.fetch_people()
                        self._people = {person.employee_no: person for person in people}
                        self._checked_at = datetime.now(TZ).isoformat(timespec='microseconds')
                        self._error = None
                    except HikvisionError:
                        self._error = PHOTO_SYNC_FAILED
                    # Не повторять полный опрос для каждой картинки/карточки.
                    self._expires = self._clock() + (10 if self._error else 60)
        result = []
        for row in rows:
            row = dict(row)
            if row['hikvision_id'] and not row['manual_attendance'] and not row['photo_updated_at']:
                person = self._people.get(row['hikvision_id'])
                if person and person.face_count and person.face_count > 0:
                    row['device_photo'] = {'checked_at': self._checked_at, 'url': person.face_url,
                                           'version': self.version(person.face_url, self._checked_at)}
                elif self._error or (person and person.face_count is None):
                    row['photo_unknown'] = True
            result.append(row)
        return result, self._error

    @staticmethod
    def version(face_url: str | None, checked_at: str | None) -> str:
        """Версия снимка для ?v=: меняется вместе с лицом на устройстве, а не с
        каждой проверкой списка — браузер держит картинку в кеше."""
        if face_url:
            return 'hik-' + hashlib.sha1(face_url.encode()).hexdigest()[:16]
        return 'hik-' + (checked_at or '')

    def cached(self, employee_no: str, url: str | None) -> tuple[bytes, str] | None:
        hit = self._faces.get(employee_no)
        if hit is None or (url and hit[0] != url):
            return None
        return hit[1], hit[2]

    async def read(self, client, employee_no: str, url: str | None) -> tuple[bytes, str]:
        """Снимок с устройства: из памяти, иначе скачать и запомнить."""
        hit = self.cached(employee_no, url)
        if hit is not None:
            return hit
        async with self.downloads:
            hit = self.cached(employee_no, url)
            if hit is not None:
                return hit
            found = url
            if not found:
                face = await client.find_face(employee_no)
                found = face.get('faceURL') if face else None
            if not found:
                raise HikvisionError('invalid_response')
            content, mime = await client.read_face_photo(found)
        self._faces[employee_no] = (url or found, content, mime)
        return content, mime

    def warm(self, rows, client) -> asyncio.Task | None:
        """Фоном скачать все лица, которых ещё нет в памяти. Одна докачка за раз."""
        if client is None or (self._warming is not None and not self._warming.done()):
            return None
        self._warming = asyncio.create_task(self._warm(list(rows), client))
        return self._warming

    async def _warm(self, rows, client):
        try:
            rows, error = await self.enrich(rows, client)
            if error:
                return
            for row in rows:
                remote = row.get('device_photo')
                if remote and self.cached(row['hikvision_id'], remote['url']) is None:
                    try:
                        await self.read(client, row['hikvision_id'], remote['url'])
                    except HikvisionError:
                        continue
        except Exception:
            logging.getLogger(__name__).exception('device photo warmup failed')
