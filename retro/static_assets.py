"""Страницы панели со ссылками на статику по хэшу содержимого (T-400).

Раньше каждый экран с `Cache-Control: no-cache` заново спрашивал сервер о 15–20
файлах. Сервер в Сингапуре, до него ~150 мс в одну сторону, и каждый переход
между разделами стоил пачки таких поездок. Теперь HTML отдаёт ссылки вида
`/static/app.js?v=<хэш>`. Такой адрес меняется вместе с файлом, поэтому браузер
держит его в кэше без проверок. После выкатки HTML (он не кэшируется) сам
ведёт на новые адреса.
"""
import hashlib
import re
from pathlib import Path

from fastapi.responses import HTMLResponse

ASSET = re.compile(r'\b(src|href)="/static/([A-Za-z0-9._/-]+)"')
IMMUTABLE = 'private, max-age=31536000, immutable'
# Ответ API по адресу с версией (?v=), например фото сотрудника: адрес
# меняется вместе с содержимым. Ставит его сам роут; без ?v= — no-store.
VERSIONED_PRIVATE = 'private, max-age=31536000'


class Pages:
    def __init__(self, root: Path):
        self.root = root
        self._versions = {}

    def version(self, name: str) -> str | None:
        if '..' in name.split('/'):
            return None
        path = self.root / name
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (name, stat.st_mtime_ns, stat.st_size)
        found = self._versions.get(key)
        if found is None:
            found = self._versions[key] = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        return found

    def html(self, name: str) -> str:
        def link(match):
            version = self.version(match[2])
            return match[0] if version is None else f'{match[1]}="/static/{match[2]}?v={version}"'
        return ASSET.sub(link, (self.root / name).read_text(encoding='utf-8'))

    def response(self, name: str) -> HTMLResponse:
        return HTMLResponse(self.html(name))
