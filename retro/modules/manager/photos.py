"""Фото сотрудника из кабинета менеджера.

Экран сам ужимает снимок (~640 px, JPEG) и присылает data URL. Здесь —
проверка того, что пришло: только JPEG или PNG, причём не по подписи в
data URL, а по первым байтам самого снимка, и не больше 2 МБ после
декодирования. Карточку без фото сервер принимает: у старых карточек и у
карточек бухгалтера фото нет.
"""

import base64
import binascii

from retro.modules.accountant.roster import MAX_PHOTO_BYTES

DECLARED_TYPES = ('image/jpeg', 'image/jpg', 'image/pjpeg', 'image/png')
SIGNATURES = ((b'\xff\xd8\xff', 'image/jpeg'), (b'\x89PNG\r\n\x1a\n', 'image/png'))
# Base64 длиннее этого — заведомо больше 2 МБ, декодировать не нужно.
MAX_ENCODED = (MAX_PHOTO_BYTES + 2) // 3 * 4

NOT_IMAGE = 'Нужна фотография в формате JPEG или PNG.'
UNREADABLE = 'Не удалось прочитать фото — выберите снимок ещё раз.'
TOO_LARGE = 'Фото больше 2 МБ — выберите снимок поменьше.'


class PhotoError(ValueError):
    """Снимок не принят; текст — для менеджера."""


def image_type(content: bytes) -> str | None:
    return next((mime for mark, mime in SIGNATURES if content.startswith(mark)), None)


def decode_photo(value: str) -> tuple[str, bytes]:
    """data:image/jpeg;base64,… → (MIME по байтам, снимок)."""
    header, comma, payload = value.partition(',')
    if not comma or not header[:5].casefold() == 'data:':
        raise PhotoError(UNREADABLE)
    declared, *options = (part.strip().casefold() for part in header[5:].split(';'))
    if 'base64' not in options:
        raise PhotoError(UNREADABLE)
    if declared not in DECLARED_TYPES:
        raise PhotoError(NOT_IMAGE)
    # Переносы строк внутри base64 допустимы, лишним их не считаем.
    payload = ''.join(payload.split())
    if len(payload) > MAX_ENCODED:
        raise PhotoError(TOO_LARGE)
    try:
        content = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise PhotoError(UNREADABLE) from None
    if not content:
        raise PhotoError(UNREADABLE)
    if len(content) > MAX_PHOTO_BYTES:
        raise PhotoError(TOO_LARGE)
    mime = image_type(content)
    if mime is None:
        raise PhotoError(NOT_IMAGE)
    return mime, content
