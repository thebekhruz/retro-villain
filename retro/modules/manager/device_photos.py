"""Read-only view of faces already enrolled on Hikvision.

Only linked employee numbers are used. Local uploads always take priority;
device metadata never confirms or overwrites a pending local replacement.
"""

import asyncio
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
                    row['device_photo'] = {'checked_at': self._checked_at, 'url': person.face_url}
                elif self._error or (person and person.face_count is None):
                    row['photo_unknown'] = True
            result.append(row)
        return result, self._error
