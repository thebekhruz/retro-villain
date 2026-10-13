"""API модуля «Пользователи» (T-445). Открыт только администратору.

Записи из базы администратор заводит, меняет, выключает и удаляет. Записи,
которые ещё живут только в переменных окружения, видны с пометкой и
переносятся в базу кнопкой — пароль переезжает отпечатком.

Себя выключить, удалить или лишить роли администратора нельзя: иначе один
неверный клик закрывает панель тому, кто только и может её открыть обратно.
"""

import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from retro.modules.manager.directions import DIRECTIONS
from retro.panel_users import ROLES, PanelUserError, change_account, import_env_account
from retro.phone_numbers import display_phone

router = APIRouter(prefix='/api/users', tags=['users'])

ROLE_NAMES = {'admin': 'Администратор', 'cashier': 'Кассир', 'accountant': 'Бухгалтер',
              'director': 'Директор', 'founder': 'Учредитель', 'manager': 'Менеджер',
              'shokh': 'Закуп · Шох'}
SELF = 'Свою учётную запись нельзя выключить, удалить или лишить роли администратора.'
ENV_ONLY = 'Эта запись пока в переменных окружения — сначала перенесите её в базу.'


class NewUser(BaseModel):
    login: str
    role: str
    name: str | None = None
    phone: str | None = None
    password: str | None = None
    directions: list[str] = []


class UserChange(BaseModel):
    role: str | None = None
    # Пустая строка — убрать имя (экран покажет логин); не передано — не менять.
    name: str | None = None
    # Пустая строка — снять номер; поле не передано — не менять.
    phone: str | None = None
    password: str | None = None
    remove_password: bool = False
    directions: list[str] | None = None
    active: bool | None = None


def row(account, me: str | None) -> dict:
    return dict(login=account.login, name=account.name, label=account.label, role=account.role, role_name=ROLE_NAMES.get(account.role, account.role),
                phone=display_phone(account.phone) if account.phone else None,
                password=account.has_password, directions=list(account.directions),
                active=account.active, source=account.source, me=account.login == me,
                updated_at=account.updated_at, updated_by=account.updated_by)


def state(request: Request):
    accounts = request.app.state.accounts
    return accounts, accounts.store, getattr(request.state, 'dashboard_user', None)


def listing(request: Request) -> dict:
    accounts, store, me = state(request)
    accounts.forget()
    users = [row(account, me) for account in store.all()]
    users += [row(accounts.env_account(login), me) for login in accounts.env_logins()]
    return dict(users=users, me=me,
                roles=[dict(id=role, name=ROLE_NAMES[role]) for role in ROLES],
                directions=list(DIRECTIONS),
                sms=bool(request.app.state.phone_login.sender))


async def apply(request: Request, work) -> dict:
    accounts = request.app.state.accounts
    try:
        await asyncio.to_thread(work)
    except PanelUserError as error:
        raise HTTPException(422, str(error)) from None
    finally:
        # Сессии видят изменение сразу, а не через срок кэша.
        accounts.forget()
    return await asyncio.to_thread(listing, request)


def guard_env(request: Request, login: str):
    accounts, store, _ = state(request)
    if store.get(login) is None and login in accounts.settings.dashboard_panel_users:
        raise HTTPException(409, ENV_ONLY)


@router.get('')
async def users(request: Request):
    return await asyncio.to_thread(listing, request)


@router.post('')
async def add_user(request: Request, body: NewUser):
    accounts, store, me = state(request)
    if body.login.strip() in accounts.settings.dashboard_panel_users:
        raise HTTPException(409, f'«{body.login.strip()}» уже есть в переменных окружения — перенесите её в базу.')
    return await apply(request, lambda: change_account(
        store, body.login, create=True, role=body.role, name=body.name, phone=(body.phone or '').strip() or None,
        password=body.password or None, directions=body.directions, by=me))


@router.patch('/{login}')
async def change_user(request: Request, login: str, body: UserChange):
    _, store, me = state(request)
    guard_env(request, login)
    if login == me and (body.active is False or (body.role is not None and body.role != 'admin')):
        raise HTTPException(409, SELF)
    changes = {}
    if body.role is not None:
        changes['role'] = body.role
    if body.name is not None:
        changes['name'] = body.name
    if body.phone is not None:
        changes['phone'] = body.phone.strip() or None
    if body.remove_password:
        changes['password'] = None
    elif body.password:
        changes['password'] = body.password
    if body.directions is not None:
        changes['directions'] = body.directions
    if body.active is not None:
        changes['active'] = body.active
    return await apply(request, lambda: change_account(store, login, create=False, by=me, **changes))


@router.delete('/{login}')
async def delete_user(request: Request, login: str):
    _, store, me = state(request)
    guard_env(request, login)
    if login == me:
        raise HTTPException(409, SELF)

    def work():
        if not store.delete(login):
            raise PanelUserError(f'«{login}» нет среди учётных записей.')
    return await apply(request, work)


@router.post('/{login}/import')
async def import_user(request: Request, login: str):
    accounts, store, me = state(request)
    return await apply(request, lambda: import_env_account(store, accounts.settings, login, by=me))
