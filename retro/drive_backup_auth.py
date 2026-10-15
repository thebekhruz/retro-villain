"""Один раз получить GOOGLE_DRIVE_REFRESH_TOKEN для резерва отчётов (T-476).

    GOOGLE_DRIVE_CLIENT_ID=… GOOGLE_DRIVE_CLIENT_SECRET=… python -m retro.drive_backup_auth

Клиент OAuth — типа «Desktop app» в Google Cloud Console. Скрипт открывает
вход в браузере, ловит ответ на 127.0.0.1 и печатает refresh-токен: его
кладут в переменные Railway. Право — только `drive.file`: приложение видит
лишь созданные им папки и файлы.
"""
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from retro.integrations.google_drive import SCOPE, TOKEN_URL

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'


def main() -> int:
    client_id = os.getenv('GOOGLE_DRIVE_CLIENT_ID', '').strip()
    client_secret = os.getenv('GOOGLE_DRIVE_CLIENT_SECRET', '').strip()
    if not client_id or not client_secret:
        print('Задайте GOOGLE_DRIVE_CLIENT_ID и GOOGLE_DRIVE_CLIENT_SECRET.', file=sys.stderr)
        return 2
    state = secrets.token_urlsafe(16)
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            received.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write('Готово, окно можно закрыть.'.encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Handler)
    redirect = f'http://127.0.0.1:{server.server_port}'
    url = AUTH_URL + '?' + urlencode(dict(
        client_id=client_id, redirect_uri=redirect, response_type='code', scope=SCOPE,
        access_type='offline', prompt='consent', state=state))
    print('Откройте ссылку и войдите в Google-аккаунт, на диске которого будет резерв:\n' + url)
    webbrowser.open(url)
    while 'code' not in received and 'error' not in received:
        server.handle_request()
    if received.get('state') != state or 'code' not in received:
        print('Вход не завершён: ' + received.get('error', 'неверный state'), file=sys.stderr)
        return 1
    response = httpx.post(TOKEN_URL, data=dict(
        code=received['code'], client_id=client_id, client_secret=client_secret,
        redirect_uri=redirect, grant_type='authorization_code'), timeout=30)
    token = response.json().get('refresh_token') if response.status_code == 200 else None
    if not token:
        print(f'Google не выдал refresh-токен ({response.status_code}).', file=sys.stderr)
        return 1
    print('\nGOOGLE_DRIVE_REFRESH_TOKEN=' + token)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
