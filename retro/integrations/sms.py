"""SMS с кодом входа (ТЗ 09.10, М-05).

Два отправителя с одним методом send(phone, text):
* Eskiz (notify.eskiz.uz) — боевой. Токен берётся входом по email и паролю
  кабинета Eskiz и живёт в памяти; просроченный (401) получаем заново один раз.
* Консоль — для стенда: SMS никуда не уходит, код пишется в лог сервера.
  На хостинге консоль запрещена при разборе настроек (config.py).

Текст SMS не попадает в лог ни при успехе, ни при ошибке Eskiz: в нём
одноразовый код. Ошибки — коды категорий, как у Hikvision; ответ провайдера
наружу и в лог не отдаём.
"""

import asyncio
import logging

import httpx

from retro.config import SmsConfig

ESKIZ_URL = 'https://notify.eskiz.uz'
logger = logging.getLogger('retro.sms')


class SmsError(Exception):
    """Категория сбоя: not_configured, network, timeout, unauthorized, rejected, unavailable,
    invalid_response. Текст ответа провайдера сюда не кладём."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def render_message(template: str, code: str) -> str:
    return template.replace('{code}', code)


class ConsoleSms:
    """Стенд: «отправка» — строка в логе сервера. Только для проверки на петле."""

    async def send(self, phone: str, text: str) -> None:
        logger.warning('СТЕНД: SMS не отправлено, текст для %s: %s', phone, text)

    async def close(self):
        pass


class EskizSms:
    def __init__(self, config: SmsConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self._http = httpx.AsyncClient(base_url=ESKIZ_URL, timeout=config.timeout_seconds,
                                       follow_redirects=False, transport=transport,
                                       headers={'Accept': 'application/json'})
        self._token: str | None = None
        self._login_lock = asyncio.Lock()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise SmsError('timeout') from None
        except httpx.HTTPError:
            raise SmsError('network') from None

    async def _login(self, stale: str | None) -> str:
        async with self._login_lock:
            # Пока ждали замка, токен мог обновить параллельный запрос.
            if self._token is not None and self._token != stale:
                return self._token
            response = await self._request('POST', '/api/auth/login', data={
                'email': self.config.email, 'password': self.config.password})
            if response.status_code in (401, 403, 422):
                raise SmsError('unauthorized')
            if response.status_code >= 400:
                raise SmsError('unavailable')
            try:
                token = response.json()['data']['token']
            except (ValueError, KeyError, TypeError):
                raise SmsError('invalid_response') from None
            if not isinstance(token, str) or not token:
                raise SmsError('invalid_response')
            self._token = token
            return token

    async def send(self, phone: str, text: str) -> None:
        # Eskiz ждёт номер без плюса: 998901234567.
        form = {'mobile_phone': phone.lstrip('+'), 'message': text, 'from': self.config.sender}
        token = self._token or await self._login(None)
        response = await self._request('POST', '/api/message/sms/send', data=form,
                                       headers={'Authorization': 'Bearer ' + token})
        if response.status_code == 401:
            # Токен Eskiz живёт месяц; просроченный меняем один раз и повторяем.
            token = await self._login(token)
            response = await self._request('POST', '/api/message/sms/send', data=form,
                                           headers={'Authorization': 'Bearer ' + token})
        if response.status_code in (401, 403):
            raise SmsError('unauthorized')
        if response.status_code >= 500:
            raise SmsError('unavailable')
        if response.status_code >= 400:
            # Неодобренный шаблон, нет баланса, номер не принят — Eskiz отказал.
            raise SmsError('rejected')
        try:
            payload = response.json()
        except ValueError:
            raise SmsError('invalid_response') from None
        if not isinstance(payload, dict):
            raise SmsError('invalid_response')
        if str(payload.get('status', '')).casefold() in ('error', 'failed', 'rejected'):
            raise SmsError('rejected')

    async def close(self):
        await self._http.aclose()


def sms_sender(config: SmsConfig | None, *, transport: httpx.AsyncBaseTransport | None = None):
    """Отправитель по настройкам; None — вход по номеру не подключён."""
    if config is None:
        return None
    if config.provider == 'console':
        return ConsoleSms()
    return EskizSms(config, transport=transport)
