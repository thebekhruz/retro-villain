"""API кабинета менеджера (ТЗ 09.10, М-01…М-03).

Карточки сотрудников заводит только бухгалтер. Менеджер видит сменных
сотрудников своих направлений, фотографирует человека, и система отправляет
в Hikvision его самого (если его там ещё нет) и его лицо. Ставок, выплат и
денег в ответах нет: это работа бухгалтера, и его разделы менеджеру закрыты.
"""

import asyncio
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from retro.modules.accountant import work_period
from retro.modules.cashier.service import today_tashkent
from retro.static_assets import VERSIONED_PRIVATE

from .directions import DIRECTIONS, ManagerAccount
from .photos import PhotoError, decode_photo
from .push import push_to_hikvision

router = APIRouter(prefix='/api/manager', tags=['manager'])

FULL_ACCESS_ROLES = {'admin', 'all'}

# Почему человек не в Hikvision — словами для менеджера. Фото при этом
# всегда сохранено: ошибка устройства не теряет снимок.
HIKVISION_MESSAGES = {
    'not_configured': 'Hikvision не подключён — карточка сохранена и ждёт отправки.',
    'network': 'Hikvision недоступен — карточка сохранена и ждёт отправки.',
    'timeout': 'Hikvision не ответил вовремя — карточка сохранена. Отправьте ещё раз.',
    'unauthorized': 'Hikvision не принял учётную запись панели — карточка сохранена. Нужна проверка доступа к устройству.',
    'device_error': 'Hikvision отказал в добавлении — карточка сохранена. Отправьте ещё раз.',
    'invalid_response': 'Hikvision ответил непонятно — карточка сохранена. Отправьте ещё раз.',
    'not_confirmed': 'Hikvision не подтвердил добавление — карточка сохранена. Отправьте ещё раз.',
    'number_linked': 'Номер Hikvision уже привязан к другому сотруднику — сообщите бухгалтеру.',
    'namesakes': 'В Hikvision уже есть люди с таким именем — номер привяжет бухгалтер в «Сотрудниках».',
    'internal': 'Не удалось отправить в Hikvision — карточка сохранена. Отправьте ещё раз.',
}
PENDING_MESSAGE = 'Ждёт отправки в Hikvision.'
MANUAL_MESSAGE = 'Отмечает бухгалтер вручную — на устройство не отправляем.'

# Лицо уходит на устройство отдельно от человека и после него. Его сбой не
# отменяет добавленного человека: тот «Добавлен», а фото ждёт повтора той
# же кнопкой. Причины без своего текста (отказ устройства, непонятный ответ,
# внутренняя ошибка) — общий FACE_FAILED.
FACE_MESSAGES = {
    'not_configured': 'Hikvision не подключён — фото сохранено и ждёт отправки.',
    'network': 'Hikvision недоступен — фото сохранено в карточке, отправка не подтверждена. Отправьте ещё раз.',
    'timeout': 'Hikvision не ответил вовремя — фото сохранено в карточке, отправка не подтверждена. Отправьте ещё раз.',
    'not_confirmed': 'Hikvision не подтвердил фото сотрудника — снимок сохранён в карточке. Отправьте ещё раз.',
    'unauthorized': 'Hikvision не принял учётную запись панели — фото не ушло на устройство. '
                    'Нужна проверка доступа к устройству.',
    'face_rejected': 'Устройство не приняло фото: лицо должно быть крупно и хорошо видно. '
                     'Загрузите другое фото и отправьте ещё раз.',
}
FACE_FAILED = 'Отправка фото на устройство не подтверждена — снимок сохранён в карточке. Отправьте ещё раз.'
FACE_PENDING = 'Фото ждёт отправки в Hikvision.'

NOT_FOUND = 'Сотрудника нет в реестре — возможно, бухгалтер его удалил.'
FOREIGN = 'Этот сотрудник — из другого направления.'
NO_PHOTO = 'Сначала сфотографируйте сотрудника.'


class PhotoInput(BaseModel):
    # data:image/jpeg;base64,… — экран заранее ужимает снимок.
    image: str


def current_account(request: Request) -> ManagerAccount:
    """Учётная запись кабинета: логин, роль и направления.

    Направления менеджера — из DASHBOARD_MANAGER_DIRECTIONS; без строки там и
    у администратора — весь ресторан, включая группы вне направлений. Вход по
    телефону (T-433) даёт тот же логин."""
    role = getattr(request.state, 'dashboard_role', None) or 'all'
    login = getattr(request.state, 'dashboard_user', None) or role
    own = request.app.state.settings.manager_directions.get(login) if role == 'manager' else None
    return ManagerAccount(login=login, role=role, directions=own or tuple(DIRECTIONS), everyone=not own)


def can_photo(row: dict, account: ManagerAccount) -> bool:
    """Фото и отправка: менеджер направления сотрудника или администратор."""
    return account.role in FULL_ACCESS_ROLES or account.sees(row['group'])


def hikvision_json(row: dict) -> dict:
    number = row['hikvision_id'] or row['hikvision_employee_no']
    code = None
    # Ручная отметка сильнее привязки: турникет такого человека не видит
    # (охрана, уборка), его день отмечает бухгалтер.
    if row['manual_attendance']:
        state, message = 'manual', MANUAL_MESSAGE
    elif row['hikvision_id']:
        state, message = 'sent', None
    elif row['hikvision_state'] in ('pending', 'error'):
        state, code = row['hikvision_state'], row['hikvision_error']
        message = HIKVISION_MESSAGES.get(code, PENDING_MESSAGE) if code else PENDING_MESSAGE
    elif row['photo_updated_at']:
        # Фото есть, а на устройство ещё не отправляли — ждёт отправки.
        state, message = 'pending', PENDING_MESSAGE
    else:
        state, message = 'none', None
    return dict(state=state, employee_no=number, message=message, face=face_json(row))


def face_json(row: dict) -> dict:
    """Лицо на устройстве: none — фото нет (или человек на ручной отметке:
    на устройство он не уходит); pending — лицо ещё не отправлено или фото
    новое; sent; error — с причиной."""
    if not row['photo_updated_at'] or row['manual_attendance']:
        return dict(state='none', message=None)
    state = row['face_state'] if row['face_state'] in ('sent', 'error') else 'pending'
    code = row['face_error']
    if state == 'sent':
        message = None
    elif state == 'error':
        message = FACE_MESSAGES.get(code, FACE_FAILED)
    else:
        message = FACE_MESSAGES.get(code, FACE_PENDING) if code else FACE_PENDING
    return dict(state=state, message=message)


def photo_json(row: dict) -> dict | None:
    # Адрес меняется с каждым снимком (?v=), поэтому браузер держит его в кеше.
    version = row['photo_updated_at']
    if not version:
        return None
    return dict(url=f'/api/manager/employees/{row["id"]}/photo?v={quote(version, safe="")}',
                updated_at=version)


def card_json(row: dict, account: ManagerAccount) -> dict:
    hikvision = hikvision_json(row)
    allowed = can_photo(row, account)
    # «Отправить ещё раз» — пока человек или его лицо не дошли до устройства.
    due = hikvision['state'] in ('pending', 'error') or hikvision['face']['state'] in ('pending', 'error')
    return dict(id=row['id'], name=row['name'], role=row['role'], group=row['group'],
                employment_type=row['employment_type'],
                # Период временного (T-434): пометка «временный · 08.10–10.10».
                work_period=work_period.label(row.get('work_from'), row.get('work_to')),
                photo=photo_json(row), hikvision=hikvision,
                can_photo=allowed, can_retry=allowed and bool(row['photo_updated_at']) and due)


def _card_or_404(request: Request, employee_id: int) -> dict:
    row = request.app.state.accountant_roster.manager_card(employee_id)
    if row is None:
        raise HTTPException(404, NOT_FOUND)
    return row


def _photo_card(request: Request, employee_id: int, account: ManagerAccount) -> dict:
    row = _card_or_404(request, employee_id)
    if not can_photo(row, account):
        raise HTTPException(403, FOREIGN)
    return row


def ended(row: dict) -> bool:
    """Временный, чей период уже закончился (T-434): в списке менеджера его нет."""
    return bool(row.get('work_to')) and row['work_to'] < today_tashkent()


@router.get('/home')
def home(request: Request):
    """Сменные сотрудники направлений менеджера по алфавиту. Окладники — не
    здесь: у них нет смен и турникета в этом кабинете. Временный, чей период
    закончился, — тоже не здесь."""
    account = current_account(request)
    rows = [row for row in request.app.state.accountant_roster.manager_cards()
            if account.sees(row['group']) and not ended(row)]
    rows.sort(key=lambda row: (row['name'].casefold().replace('ё', 'е'), row['id']))
    return dict(login=account.login, role=account.role, directions=list(account.directions),
                hikvision=dict(configured=request.app.state.hikvision_writer is not None),
                employees=[card_json(row, account) for row in rows])


@router.get('/employees/{employee_id}')
def employee(request: Request, employee_id: int):
    return dict(employee=card_json(_card_or_404(request, employee_id), current_account(request)))


@router.put('/employees/{employee_id}/photo')
async def upload_photo(request: Request, employee_id: int, body: PhotoInput, send: bool = False):
    """Сохранить фото; send=true также отправляет его на терминал в этом запросе.
    Сбой устройства оставляет снимок в карточке и возможность повторить отправку.
    Без send сохраняется прежний API для локального сохранения."""
    account = current_account(request)
    await asyncio.to_thread(_photo_card, request, employee_id, account)
    try:
        mime, content = await asyncio.to_thread(decode_photo, body.image)
        await asyncio.to_thread(request.app.state.accountant_roster.set_photo,
                                employee_id, mime, content, by=account.login)
    except PhotoError as error:
        raise HTTPException(422, str(error)) from None
    except ValueError:
        raise HTTPException(404, NOT_FOUND) from None
    if send:
        await push_to_hikvision(request.app.state, employee_id, by=account.login,
                                request_id=getattr(request.state, 'request_id', '-'))
    row = await asyncio.to_thread(_card_or_404, request, employee_id)
    return dict(employee=card_json(row, account))


@router.get('/employees/{employee_id}/photo')
def photo(request: Request, employee_id: int):
    found = request.app.state.accountant_roster.photo(employee_id)
    if found is None:
        raise HTTPException(404, 'У сотрудника нет фото.')
    content, mime, _ = found
    # Адрес с ?v= меняется вместе со снимком: по нему кешируем надолго, но
    # только в браузере сотрудника (private). Без ?v= панель ставит no-store.
    return Response(content, media_type=mime, headers={'Cache-Control': VERSIONED_PRIVATE})


@router.post('/employees/{employee_id}/hikvision')
async def send_to_hikvision(request: Request, employee_id: int):
    """Человек (если его ещё нет на устройстве) и его лицо. На ручной
    отметке — ничего не отправляем: фото остаётся в карточке."""
    account = current_account(request)
    row = await asyncio.to_thread(_photo_card, request, employee_id, account)
    if not row['photo_updated_at']:
        raise HTTPException(422, NO_PHOTO)
    if not row['manual_attendance']:
        await push_to_hikvision(request.app.state, employee_id, by=account.login,
                                request_id=getattr(request.state, 'request_id', '-'))
        row = await asyncio.to_thread(_card_or_404, request, employee_id)
    return dict(employee=card_json(row, account))
