"""API кабинета менеджера (ТЗ 09.10, М-01…М-03; T-450).

Менеджер видит сменных сотрудников своих направлений, фотографирует человека,
и система отправляет в Hikvision его самого (если его там ещё нет) и его
лицо. С T-450 менеджер и заводит новых людей своих разделов: имя, должность,
сменный или временный; с T-452 — и удаляет их. Ставку менеджер может только
предложить (T-453): она ждёт бухгалтера, который подтверждает её или ставит
свою. Похожие имена в реестре показываем, как бухгалтеру: решает человек,
карточки сами не объединяем. Самих ставок, выплат и денег в ответах нет —
только то, что менеджер предложил сам: разделы бухгалтера ему закрыты.
"""

import asyncio
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from retro.modules.accountant import work_period
from retro.modules.accountant.names import person_name, similar_names
from retro.modules.accountant.roster import GROUPS, group_for
from retro.integrations.hikvision import HikvisionError
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
UNKNOWN_ROLE = 'Такой должности нет в реестре. Выберите из списка.'
FOREIGN_ROLE = 'Эта должность — не из ваших разделов.'
SIMILAR = 'Похожие уже есть в реестре. Это тот же человек?'


class PhotoInput(BaseModel):
    # data:image/jpeg;base64,… — экран заранее ужимает снимок.
    image: str


class EmployeeInput(BaseModel):
    """Новый сотрудник из кабинета (T-450). Ставку менеджер только предлагает
    (T-453): до подтверждения бухгалтером её нет."""
    name: str
    role: str
    proposed_rate: str | None = None
    # Временный (T-434): период «с — по», обе границы необязательны.
    employment_type: str = 'shift'
    work_from: date | None = None
    work_to: date | None = None
    # «Это другой человек»: менеджер видел похожих в реестре и подтвердил.
    confirm_new: bool = False


def current_account(request: Request) -> ManagerAccount:
    """Учётная запись кабинета: логин, роль и направления.

    Направления менеджера — из его учётной записи (T-442); без них и
    у администратора — весь ресторан, включая группы вне направлений. Вход по
    телефону (T-433) даёт тот же логин."""
    role = getattr(request.state, 'dashboard_role', None) or 'all'
    login = getattr(request.state, 'dashboard_user', None) or role
    own = request.app.state.accounts.directions(login) if role == 'manager' else None
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
    if row.get('device_photo') and not row['photo_updated_at']:
        return dict(state='sent', message=None)
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
        remote = row.get('device_photo')
        if remote:
            return dict(url=f'/api/manager/employees/{row["id"]}/photo?v=hik-{quote(remote["checked_at"], safe="")}',
                        updated_at=remote['checked_at'], source='hikvision')
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
                photo_unknown=bool(row.get('photo_unknown')),
                rate=rate_json(row),
                can_photo=allowed, can_retry=allowed and bool(row['photo_updated_at']) and due)


def rate_json(row: dict) -> dict:
    """Ставка глазами менеджера (T-453): set — бухгалтер назначил (сумму не
    показываем), proposed — ждёт бухгалтера (видна своя предложенная сумма),
    none — ставки нет."""
    if row.get('has_rate'):
        return dict(state='set', proposed=None)
    if row.get('proposed_rate'):
        return dict(state='proposed', proposed=row['proposed_rate'])
    return dict(state='none', proposed=None)


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


def capitalized(text: str) -> str:
    text = ' '.join(text.split())
    return text[:1].upper() + text[1:]


def role_options(rows: list[dict], account: ManagerAccount) -> list[dict]:
    """Должности для формы «Новый сотрудник»: базовые из реестра и те, что уже
    есть у людей, — только группы разделов менеджера. Повар — общий «Повар»,
    если на кухне ещё нет ни одного повара с цехом."""
    seen: dict[str, dict] = {}
    names = [*GROUPS, 'кондитер', *(row['role'] for row in rows)]
    if not any(name.casefold().startswith('повар') for name in names):
        names.append('повар')
    for name in names:
        try:
            group = group_for(name)
        except ValueError:
            continue
        key = ' '.join(name.casefold().split())
        if account.sees(group) and key not in seen:
            seen[key] = dict(role=capitalized(name), group=group)
    order = {group: index for index, group in enumerate(
        group for name in account.directions for group in DIRECTIONS[name])}
    return sorted(seen.values(), key=lambda item: (order.get(item['group'], len(order)),
                                                   item['role'].casefold()))


def similar_people(request: Request, name: str, account: ManagerAccount) -> list[dict]:
    """Похожие имена в реестре — как у бухгалтера (М-04). Своих (разделы
    менеджера) можно открыть — «Это он»; чужих и окладников только показываем."""
    try:
        clean = person_name(name)
    except ValueError:
        return []
    roster = request.app.state.accountant_roster
    found = [dict(id=person.id, name=person.name, role=person.role, group=person.group_name,
                  mine=account.sees(person.group_name))
             for person in roster.list() if similar_names(clean, person.name)]
    found += [dict(id=None, name=person.name, role=person.role, group='На окладе', mine=False)
              for person in roster.list_monthly() if similar_names(clean, person.name)]
    return found


@router.get('/home')
async def home(request: Request):
    """Сменные сотрудники направлений менеджера по алфавиту. Окладники — не
    здесь: у них нет смен и турникета в этом кабинете. Временный, чей период
    закончился, — тоже не здесь."""
    account = current_account(request)
    cards = await asyncio.to_thread(request.app.state.accountant_roster.manager_cards)
    rows = [row for row in cards
            if account.sees(row['group']) and not ended(row)]
    rows, photo_message = await request.app.state.manager_device_photos.enrich(
        rows, request.app.state.hikvision_writer)
    rows.sort(key=lambda row: (row['name'].casefold().replace('ё', 'е'), row['id']))
    stored = await asyncio.to_thread(request.app.state.accounts.get, account.login)
    return dict(login=account.login, name=stored.label if stored else account.login,
                role=account.role, directions=list(account.directions),
                hikvision=dict(configured=request.app.state.hikvision_writer is not None),
                photo_message=photo_message,
                roles=role_options(cards, account),
                employees=[card_json(row, account) for row in rows])


@router.post('/employees', status_code=201)
async def create_employee(request: Request, body: EmployeeInput):
    """Новый сотрудник раздела менеджера (T-450). Группа — по должности, как у
    бухгалтера; чужой раздел отклоняется до записи. Ставки нет: её назначает
    бухгалтер. Имя и должность — кириллицей (М-04)."""
    account = current_account(request)
    try:
        group = group_for(body.role)
    except ValueError:
        raise HTTPException(422, UNKNOWN_ROLE) from None
    if not account.sees(group):
        raise HTTPException(403, FOREIGN_ROLE)
    if not body.confirm_new:
        matches = await asyncio.to_thread(similar_people, request, body.name, account)
        if matches:
            return JSONResponse(dict(detail=SIMILAR, matches=matches), 409)
    try:
        employee = await asyncio.to_thread(
            request.app.state.accountant_roster.add, name=body.name, role=body.role, rate=None,
            group_name=group, by=account.login, cyrillic=True, employment_type=body.employment_type,
            work_from=body.work_from, work_to=body.work_to, proposed_rate=body.proposed_rate or None)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    row = await asyncio.to_thread(_card_or_404, request, employee.id)
    return dict(employee=card_json(row, account))


@router.get('/employees/{employee_id}')
async def employee(request: Request, employee_id: int):
    row = await asyncio.to_thread(_card_or_404, request, employee_id)
    rows, _ = await request.app.state.manager_device_photos.enrich([row], request.app.state.hikvision_writer)
    return dict(employee=card_json(rows[0], current_account(request)))


@router.delete('/employees/{employee_id}', status_code=204)
async def delete_employee(request: Request, employee_id: int):
    """Удалить сотрудника своего раздела (T-452) — то же удаление, что у
    бухгалтера в «Сотрудниках»: с сегодняшнего дня человека нет ни в кабинете,
    ни у бухгалтера, прошлые смены и выплаты остаются. Кто удалил — в истории
    карточки. На терминале Hikvision человек остаётся, как и при удалении
    бухгалтером."""
    account = current_account(request)
    await asyncio.to_thread(_photo_card, request, employee_id, account)
    try:
        await asyncio.to_thread(request.app.state.accountant_roster.delete, employee_id, by=account.login)
    except ValueError:
        raise HTTPException(404, NOT_FOUND) from None
    return Response(status_code=204)


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
async def photo(request: Request, employee_id: int):
    found = await asyncio.to_thread(request.app.state.accountant_roster.photo, employee_id)
    if found is None:
        row = await asyncio.to_thread(_card_or_404, request, employee_id)
        client = request.app.state.hikvision_writer
        rows, _ = await request.app.state.manager_device_photos.enrich([row], client)
        remote = rows[0].get('device_photo')
        if rows[0].get('photo_unknown') or (remote and client is None):
            raise HTTPException(503, 'Не удалось загрузить фото из Hikvision. Повторите позже.')
        if remote is None:
            raise HTTPException(404, 'У сотрудника нет фото.')
        try:
            async with request.app.state.manager_device_photos.downloads:
                url = remote['url']
                if not url:
                    face = await client.find_face(row['hikvision_id'])
                    url = face.get('faceURL') if face else None
                if not url:
                    raise HikvisionError('invalid_response')
                content, mime = await client.read_face_photo(url)
        except HikvisionError:
            raise HTTPException(503, 'Не удалось загрузить фото из Hikvision. Повторите позже.') from None
        # Новое локальное фото, загруженное за время чтения устройства, приоритетнее.
        found = await asyncio.to_thread(request.app.state.accountant_roster.photo, employee_id)
        if found is None:
            return Response(content, media_type=mime, headers={'Cache-Control': 'no-store'})
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
