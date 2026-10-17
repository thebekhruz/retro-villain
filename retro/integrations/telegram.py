"""Telegram Bot API: картинка в группу (T-478, снимок кассы в 23:30) и
текст учредителю (ТЗ 16.10, п. 4.2: Счёт Шефа за неделю).

Токен бота стоит в пути запроса (`/bot<token>/sendPhoto`), поэтому ни URL,
ни текст исключения httpx в лог и наружу не уходят — только категория сбоя,
как у SMS и Hikvision. Ответ Telegram читаем лишь ради `ok` и `message_id`.
"""

import httpx

TELEGRAM_URL = 'https://api.telegram.org'
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


class TelegramError(Exception):
    """Категория сбоя: network, timeout, unauthorized, rejected, unavailable, invalid_response."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class TelegramBot:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 30):
        self._token = token
        self._http = httpx.AsyncClient(base_url=TELEGRAM_URL, timeout=timeout,
                                       follow_redirects=False, transport=transport)

    async def send_photo(self, chat_id: str, photo: bytes, caption: str, *,
                         filename: str = 'report.png') -> int:
        """Отправляет PNG и возвращает message_id."""
        try:
            response = await self._http.post(
                f'/bot{self._token}/sendPhoto',
                data={'chat_id': chat_id, 'caption': caption[:CAPTION_LIMIT]},
                files={'photo': (filename, photo, 'image/png')})
        except httpx.TimeoutException:
            raise TelegramError('timeout') from None
        except httpx.HTTPError:
            raise TelegramError('network') from None
        if response.status_code in (401, 404):
            raise TelegramError('unauthorized')
        if response.status_code in (400, 403):
            # Чат не найден, бота нет в группе или его оттуда удалили.
            raise TelegramError('rejected')
        if response.status_code != 200:
            raise TelegramError('unavailable')
        try:
            payload = response.json()
            message_id = payload['result']['message_id']
        except (ValueError, KeyError, TypeError):
            raise TelegramError('invalid_response') from None
        if payload.get('ok') is not True or isinstance(message_id, bool) or not isinstance(message_id, int):
            raise TelegramError('invalid_response')
        return message_id

    async def send_message(self, chat_id: str, text: str) -> int:
        """Отправляет текст без разметки и возвращает message_id.

        Разметку не включаем: имена официантов и столы приходят из iiko, и
        любой символ разметки в них ломал бы сообщение. Превью ссылки
        выключено — в чате нужна сама ссылка, а не карточка страницы входа."""
        return await self._send('sendMessage', json={
            'chat_id': chat_id, 'text': text[:MESSAGE_LIMIT],
            'link_preview_options': {'is_disabled': True}})

    async def _send(self, method: str, **request) -> int:
        try:
            response = await self._http.post(f'/bot{self._token}/{method}', **request)
        except httpx.TimeoutException:
            raise TelegramError('timeout') from None
        except httpx.HTTPError:
            raise TelegramError('network') from None
        if response.status_code in (401, 404):
            raise TelegramError('unauthorized')
        if response.status_code in (400, 403):
            raise TelegramError('rejected')
        if response.status_code != 200:
            raise TelegramError('unavailable')
        try:
            payload = response.json()
            message_id = payload['result']['message_id']
        except (ValueError, KeyError, TypeError):
            raise TelegramError('invalid_response') from None
        if payload.get('ok') is not True or isinstance(message_id, bool) or not isinstance(message_id, int):
            raise TelegramError('invalid_response')
        return message_id

    async def close(self):
        await self._http.aclose()
