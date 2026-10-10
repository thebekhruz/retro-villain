"""API кабинета менеджера (ТЗ 09.10, М-01…М-04).

Менеджер работает только здесь: ищет человека в общей базе, заводит
сменного или временного сотрудника своего направления и видит, добавлен ли
тот в Hikvision. Ставок, выплат и денег в ответах нет: это работа
бухгалтера, и его разделы менеджеру закрыты.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from retro.modules.accountant.names import collapse_spaces, matches_query, person_name, role_name, similar_names
from retro.modules.accountant.roster import group_for

from .directions import (DIRECTION_ROLES, DIRECTIONS, EMPLOYMENT_TYPES, ManagerAccount,
                         direction_of_group, group_in_direction)
from .push import push_to_hikvision

router = APIRouter(prefix='/api/manager', tags=['manager'])

FULL_ACCESS_ROLES = {'admin', 'all'}
SEARCH_LIMIT = 20

# Почему карточка не в Hikvision — словами для менеджера. Карточка при этом
# всегда сохранена: ошибка устройства не теряет ввод.
HIKVISION_MESSAGES = {
    'not_configured': 'Hikvision не подключён — карточка сохранена и ждёт отправки.',
    'network': 'Hikvision недоступен — карточка сохранена и ждёт отправки.',
    'timeout': 'Hikvision не ответил вовремя — карточка сохранена. Отправьте ещё раз.',
    'unauthorized': 'Hikvision не принял учётную запись панели — карточка сохранена. Нужна проверка доступа к устройству.',
    'device_error': 'Hikvision отказал в добавлении — карточка сохранена. Отправьте ещё раз.',
    'invalid_response': 'Hikvision ответил непонятно — карточка сохранена. Отправьте ещё раз.',
    'not_confirmed': 'Hikvision не подтвердил добавление — карточка сохранена. Отправьте ещё раз.',
    'number_linked': 'Номер Hikvision уже привязан к другому сотруднику — сообщите бухгалтеру.',
    'internal': 'Не удалось отправить в Hikvision — карточка сохранена. Отправьте ещё раз.',
}


class ManagerEmployeeInput(BaseModel):
    name: str
    role: str
    direction: str
    employment_type: str
    request_key: str
    # «Это другой человек»: менеджер видел похожих и подтвердил, что новый.
    confirm_new: bool = False


def current_account(request: Request) -> ManagerAccount:
    """Учётная запись кабинета: логин, роль и направления.

    Направления менеджера — из DASHBOARD_MANAGER_DIRECTIONS; без строки там и
    у администратора — все. Вход по телефону (T-433) даст тот же логин."""
    role = getattr(request.state, 'dashboard_role', None) or 'all'
    login = getattr(request.state, 'dashboard_user', None) or role
    directions = tuple(DIRECTIONS)
    if role == 'manager':
        directions = request.app.state.settings.manager_directions.get(login) or directions
    return ManagerAccount(login=login, role=role, directions=directions)


def hikvision_json(row: dict) -> dict:
    number = row['hikvision_id'] or row['hikvision_employee_no']
    if row['hikvision_id']:
        state = 'sent'
    elif row['manual_attendance']:
        state = 'manual'
    elif row['hikvision_state'] in ('pending', 'error'):
        state = row['hikvision_state']
    else:
        state = 'none'
    code = row['hikvision_error'] if state in ('pending', 'error') else None
    if state == 'pending' and code is None:
        message = 'Ждёт отправки в Hikvision.'
    else:
        message = HIKVISION_MESSAGES.get(code) if code else None
    return dict(state=state, employee_no=number, error=code, message=message,
                synced_at=row['hikvision_synced_at'])


def card_json(row: dict, account: ManagerAccount) -> dict:
    hikvision = hikvision_json(row)
    mine = bool(row['created_by']) and (row['created_by'] == account.login
                                        or account.role in FULL_ACCESS_ROLES)
    return dict(id=row['id'], kind='shift', name=row['name'], role=row['role'], group=row['group'],
                direction=row['direction'] or direction_of_group(row['group']),
                employment_type=row['employment_type'],
                employment_label=EMPLOYMENT_TYPES.get(row['employment_type'], 'Сменный'),
                created_by=row['created_by'], created_at=row['created_at'],
                hikvision=hikvision, mine=mine,
                can_retry=mine and hikvision['state'] in ('pending', 'error'))


def monthly_json(row) -> dict:
    # Окладник — в общей базе, но не в кабинете менеджера: только узнать.
    return dict(id=row.id, kind='monthly', name=row.name, role=row.role, group='На окладе',
                direction=None, employment_type=None, employment_label='На окладе',
                created_by=None, created_at=None, hikvision=None, mine=False, can_retry=False)


def roster_cards(request: Request) -> list[dict]:
    return request.app.state.accountant_roster.manager_cards()


def similar_people(request: Request, name: str, account: ManagerAccount) -> list[dict]:
    roster = request.app.state.accountant_roster
    found = [card_json(row, account) for row in roster.manager_cards() if similar_names(name, row['name'])]
    found += [monthly_json(row) for row in roster.list_monthly() if similar_names(name, row.name)]
    return found


def replay(roster, employee_id: int, account: ManagerAccount) -> JSONResponse:
    return JSONResponse(dict(employee=card_json(roster.manager_card(employee_id), account), created=False))


def _card_or_404(request: Request, employee_id: int) -> dict:
    row = request.app.state.accountant_roster.manager_card(employee_id)
    if row is None:
        raise HTTPException(404, 'Сотрудник не найден.')
    return row


@router.get('/home')
def home(request: Request):
    account = current_account(request)
    rows = roster_cards(request)
    mine = [card for card in (card_json(row, account) for row in rows) if card['mine']]
    mine.sort(key=lambda card: card['created_at'] or '', reverse=True)
    directions = []
    for name in account.directions:
        groups = DIRECTIONS[name]
        known = {row['role'] for row in rows if row['group'] in groups and row['role']}
        roles = sorted(set(DIRECTION_ROLES.get(name, ())) | {role.strip().capitalize() for role in known},
                       key=str.casefold)
        directions.append(dict(name=name, groups=list(groups), roles=roles))
    return dict(login=account.login, role=account.role, directions=directions,
                employment_types=[dict(id=key, name=label) for key, label in EMPLOYMENT_TYPES.items()],
                hikvision=dict(configured=request.app.state.hikvision_writer is not None),
                mine=mine, base_count=len(rows))


@router.get('/search')
def search(request: Request, q: str = ''):
    account = current_account(request)
    query = collapse_spaces(q)[:80]
    if len(query) < 2:
        return dict(query=query, results=[], more=False)
    roster = request.app.state.accountant_roster
    found = [card_json(row, account) for row in roster.manager_cards()
             if matches_query(query, row['name'], row['role'])
             or (query.isdigit() and query in (row['hikvision_id'], row['hikvision_employee_no']))]
    found += [monthly_json(row) for row in roster.list_monthly() if matches_query(query, row.name, row.role)]
    found.sort(key=lambda card: card['name'].casefold())
    return dict(query=query, results=found[:SEARCH_LIMIT], more=len(found) > SEARCH_LIMIT)


@router.get('/employees/{employee_id}')
def employee(request: Request, employee_id: int):
    return dict(employee=card_json(_card_or_404(request, employee_id), current_account(request)))


@router.post('/employees', status_code=201)
def create_employee(request: Request, body: ManagerEmployeeInput):
    account = current_account(request)
    if body.direction not in DIRECTIONS:
        raise HTTPException(422, 'Выберите направление.')
    if not account.owns(body.direction):
        raise HTTPException(403, 'Это направление ведёт другой менеджер.')
    if body.employment_type not in EMPLOYMENT_TYPES:
        raise HTTPException(422, 'Выберите тип: сменный или временный.')
    try:
        name, role = person_name(body.name), role_name(body.role)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    roster = request.app.state.accountant_roster
    # Повтор того же «Сохранить» (двойной тап, обрыв связи) — та же карточка.
    existing = roster.by_request_key(body.request_key.strip())
    if existing is not None:
        return replay(roster, existing, account)
    try:
        role_group = group_for(role)
    except ValueError:
        role_group = None
    group = group_in_direction(body.direction, role_group)
    if group is None:
        raise HTTPException(422, f'Должность «{role}» — из направления «{direction_of_group(role_group)}», '
                                 f'а не «{body.direction}».')
    if not body.confirm_new:
        matches = similar_people(request, name, account)
        # Первое нажатие могло успеть сохранить карточку, пока шло второе:
        # тогда «похожий» — она сама.
        existing = roster.by_request_key(body.request_key.strip()) if matches else None
        if existing is not None:
            return replay(roster, existing, account)
        if matches:
            return JSONResponse(dict(detail='Похожие уже есть в базе. Это он или другой человек?',
                                     matches=matches), 409)
    try:
        employee_id, created = roster.manager_create(
            name=name, role=role, group_name=group, direction=body.direction,
            employment_type=body.employment_type, created_by=account.login,
            request_key=body.request_key)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    card = card_json(roster.manager_card(employee_id), account)
    return JSONResponse(dict(employee=card, created=created), 201 if created else 200)


@router.post('/employees/{employee_id}/hikvision')
async def send_to_hikvision(request: Request, employee_id: int):
    account = current_account(request)
    row = await asyncio.to_thread(_card_or_404, request, employee_id)
    if not row['hikvision_employee_no'] and not row['hikvision_id']:
        raise HTTPException(422, 'Эту карточку ведёт бухгалтер: в Hikvision её отправляют из «Сотрудников».')
    if not card_json(row, account)['mine']:
        raise HTTPException(403, 'Отправить может менеджер, который завёл карточку.')
    await push_to_hikvision(request.app.state, employee_id, by=account.login,
                            request_id=getattr(request.state, 'request_id', '-'))
    row = await asyncio.to_thread(_card_or_404, request, employee_id)
    return dict(employee=card_json(row, account))
