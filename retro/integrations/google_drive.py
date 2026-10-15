"""Google Drive: папки и загрузка XLSX с превращением в Google Таблицу (T-476).

Доступ — OAuth-токен владельца диска с правом `drive.file`: приложение видит
только то, что создало само, и ничего больше на чужом диске. Сервисный
аккаунт не подходит: своего места на Drive у него нет.
"""
import json
import secrets
from time import monotonic

import httpx

TOKEN_URL = 'https://oauth2.googleapis.com/token'
FILES_URL = 'https://www.googleapis.com/drive/v3/files'
UPLOAD_URL = 'https://www.googleapis.com/upload/drive/v3/files'
SCOPE = 'https://www.googleapis.com/auth/drive.file'
FOLDER = 'application/vnd.google-apps.folder'
SHEET = 'application/vnd.google-apps.spreadsheet'
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class DriveError(Exception):
    """Ответ Google без токенов и тела запроса — только что и с каким кодом."""


class GoogleDrive:
    def __init__(self, config, *, transport=None, clock=monotonic):
        self.config, self.clock = config, clock
        self.client = httpx.AsyncClient(transport=transport, timeout=60)
        self._token, self._expires = None, 0.0

    async def close(self):
        await self.client.aclose()

    async def token(self) -> str:
        if self._token and self.clock() < self._expires:
            return self._token
        response = await self.client.post(TOKEN_URL, data=dict(
            client_id=self.config.client_id, client_secret=self.config.client_secret,
            refresh_token=self.config.refresh_token, grant_type='refresh_token'))
        if response.status_code != 200:
            # invalid_grant — токен отозван или истёк: нужен новый, повторы не помогут.
            reason = _error_code(response)
            raise DriveError(f'Google не выдал доступ ({response.status_code} {reason}).')
        body = response.json()
        self._token = body['access_token']
        self._expires = self.clock() + max(60, int(body.get('expires_in', 3600)) - 120)
        return self._token

    async def _call(self, method, url, operation, **kwargs):
        headers = {**kwargs.pop('headers', {}), 'Authorization': f'Bearer {await self.token()}'}
        response = await self.client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 401:
            self._token = None
        if response.status_code >= 300:
            raise DriveError(f'Google Drive: {operation} не удалось ({response.status_code} '
                             f'{_error_code(response)}).')
        return response.json()

    async def create_folder(self, name: str, parent: str | None = None) -> str:
        metadata = dict(name=name, mimeType=FOLDER, **({'parents': [parent]} if parent else {}))
        body = await self._call('POST', FILES_URL, 'создать папку', params={'fields': 'id'}, json=metadata)
        return body['id']

    async def upload_sheet(self, name: str, parent: str, content: bytes) -> str:
        """XLSX → Google Таблица: Drive конвертирует файл при загрузке."""
        boundary = 'retro-' + secrets.token_hex(12)
        metadata = json.dumps(dict(name=name, mimeType=SHEET, parents=[parent]), ensure_ascii=False)
        body = b''.join((
            f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode(),
            metadata.encode(), b'\r\n',
            f'--{boundary}\r\nContent-Type: {XLSX}\r\n\r\n'.encode(), content, b'\r\n',
            f'--{boundary}--\r\n'.encode()))
        result = await self._call(
            'POST', UPLOAD_URL, 'загрузить файл', params={'uploadType': 'multipart', 'fields': 'id'},
            headers={'Content-Type': f'multipart/related; boundary={boundary}'}, content=body)
        return result['id']


def _error_code(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ''
    error = body.get('error')
    if isinstance(error, dict):
        return str(error.get('status') or error.get('message') or '')[:80]
    return str(error or '')[:80]
