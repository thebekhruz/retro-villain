"""Управление учётными записями панели из командной строки (T-442).

    python -m retro.panel_users list
    python -m retro.panel_users add boss --role admin --phone "+998 90 646 25 40"
    python -m retro.panel_users add anvar --role cashier --password
    python -m retro.panel_users set karina --directions Кухня,Уборка
    python -m retro.panel_users set karina --no-phone
    python -m retro.panel_users disable karina
    python -m retro.panel_users import-env

База — та же, что у панели: DATABASE_URL (на Railway — `railway ssh`, затем
команда) или локальный SQLite в RETRO_DATA_DIR. Пароль не передаётся
аргументом (он остался бы в истории оболочки): --password спрашивает его.
Изменения видны работающей панели в течение нескольких секунд, перезапуск
не нужен; выключенная запись закрывает и свои открытые сессии.
"""

import argparse
import getpass
import sys

from retro.config import Settings
from retro.db import Database
from retro.panel_users import (
    ROLES, PanelUserError, PanelUserStore, check_directions, check_login, check_password, check_phone,
    check_role, hash_password,
)


def open_store(settings: Settings) -> PanelUserStore:
    return PanelUserStore(Database(settings.database_url) if settings.database_url
                          else settings.data_dir / 'panel-users.sqlite3')


def ask_password(stdin=sys.stdin) -> str:
    if not stdin.isatty():
        return stdin.readline().rstrip('\n')
    first = getpass.getpass('Пароль: ')
    if first != getpass.getpass('Ещё раз: '):
        raise PanelUserError('Пароли не совпали.')
    return first


def describe(account) -> str:
    parts = [f'{account.login:<16}', f'{account.role:<10}', account.phone or '—',
             'пароль' if account.has_password else 'только SMS']
    if account.directions:
        parts.append(','.join(account.directions))
    if not account.active:
        parts.append('ВЫКЛЮЧЕНА')
    return '  '.join(parts)


def run(argv, *, settings: Settings | None = None, store: PanelUserStore | None = None,
        out=sys.stdout, password_source=ask_password) -> int:
    parser = argparse.ArgumentParser(prog='python -m retro.panel_users',
                                     description='Учётные записи панели Retro.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='все записи из базы')
    for name in ('add', 'set'):
        command = commands.add_parser(name, help='завести запись' if name == 'add' else 'изменить запись')
        command.add_argument('login')
        command.add_argument('--role', choices=ROLES, required=name == 'add')
        command.add_argument('--phone', help='номер для входа по SMS')
        command.add_argument('--password', action='store_true', help='задать пароль (спросит)')
        command.add_argument('--directions', help='направления менеджера через запятую')
        if name == 'set':
            command.add_argument('--no-phone', action='store_true', help='снять номер')
            command.add_argument('--no-password', action='store_true', help='только вход по SMS')
            command.add_argument('--all-directions', action='store_true', help='весь ресторан')
    for name, text in (('disable', 'выключить: вход закрыт, сессии закрываются'),
                       ('enable', 'включить обратно'), ('delete', 'удалить из базы')):
        commands.add_parser(name, help=text).add_argument('login')
    commands.add_parser('import-env', help='перенести DASHBOARD_PANEL_USERS и номера в базу')
    args = parser.parse_args(argv)

    settings = settings or Settings.from_env()
    store = store or open_store(settings)
    try:
        if args.command == 'list':
            for account in store.all():
                print(describe(account), file=out)
            env_only = sorted(set(settings.dashboard_panel_users) - {a.login for a in store.all()})
            if env_only:
                print('Только в переменных окружения: ' + ', '.join(env_only), file=out)
            return 0
        if args.command == 'import-env':
            return import_env(settings, store, out)
        login = check_login(args.login)
        current = store.get(login)
        if args.command in ('disable', 'enable', 'delete'):
            if current is None:
                raise PanelUserError(f'«{login}» в базе нет.')
            if args.command == 'delete':
                store.delete(login)
                print(f'«{login}» удалена.', file=out)
                if login in settings.dashboard_panel_users:
                    print('Внимание: логин остался в DASHBOARD_PANEL_USERS и снова действует оттуда.', file=out)
                return 0
            store.save(login, role=current.role, password_hash=store.password_hash(login), phone=current.phone,
                       directions=current.directions, active=args.command == 'enable')
            print(describe(store.get(login)), file=out)
            return 0
        if args.command == 'add' and current is not None:
            raise PanelUserError(f'«{login}» уже есть — меняйте командой set.')
        if args.command == 'set' and current is None:
            raise PanelUserError(f'«{login}» в базе нет — заведите командой add.')
        role = check_role(args.role) if args.role else current.role
        phone = current.phone if current else None
        if args.phone:
            phone = check_phone(args.phone)
        if getattr(args, 'no_phone', False):
            phone = None
        directions = current.directions if current else ()
        if args.directions:
            directions = check_directions(args.directions)
        if getattr(args, 'all_directions', False):
            directions = ()
        if role != 'manager':
            directions = ()
        password_hash = store.password_hash(login) if current else None
        if args.password:
            password_hash = hash_password(check_password(password_source()))
        if getattr(args, 'no_password', False):
            password_hash = None
        if password_hash is None and phone is None:
            raise PanelUserError('Нужен пароль (--password) или номер (--phone): иначе войти нечем.')
        store.save(login, role=role, password_hash=password_hash, phone=phone, directions=directions,
                   active=current.active if current else True)
        print(describe(store.get(login)), file=out)
        if login in settings.dashboard_panel_users:
            print('Запись в базе теперь сильнее строки из DASHBOARD_PANEL_USERS.', file=out)
        return 0
    except PanelUserError as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        return 2


def import_env(settings: Settings, store: PanelUserStore, out) -> int:
    """Перенос из переменных: пароли — отпечатками, номера и направления — как есть.
    Записи, которые уже есть в базе, не трогаем: база сильнее."""
    phones: dict[str, str] = {}
    for phone, login in settings.phone_users.items():
        phones.setdefault(login, phone)
    moved = []
    for login, (password, role) in settings.dashboard_panel_users.items():
        if store.get(login) is not None:
            print(f'«{login}» уже в базе — пропуск.', file=out)
            continue
        store.save(login, role=role, password_hash=hash_password(password), phone=phones.get(login),
                   directions=settings.manager_directions.get(login, ()))
        moved.append(login)
    extra = [phone for phone, login in settings.phone_users.items() if phones.get(login) != phone]
    print(f'Перенесено: {len(moved)}' + (f' ({", ".join(moved)})' if moved else '') + '.', file=out)
    if extra:
        print('В базе у записи один номер; не перенесены: ' + ', '.join(extra), file=out)
    if moved:
        print('Теперь можно снять DASHBOARD_PANEL_USERS, DASHBOARD_PHONE_USERS и '
              'DASHBOARD_MANAGER_DIRECTIONS.', file=out)
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))
