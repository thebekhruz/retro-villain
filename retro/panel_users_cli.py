"""Управление учётными записями панели из командной строки (T-442).

    python -m retro.panel_users list
    python -m retro.panel_users add boss --role admin --name "Бехруз" --phone "+998 90 646 25 40"
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
    ROLES, PanelUserError, PanelUserStore, change_account, check_login, import_env_account,
)

BY = 'команда'  # кем записано изменение в базе


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
    parts = [f'{account.login:<16}', f'{account.name or "—":<20}', f'{account.role:<10}', account.phone or '—',
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
        command.add_argument('--name', help='имя для интерфейса, например «Карина»')
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
        if args.command == 'delete':
            if current is None:
                raise PanelUserError(f'«{login}» в базе нет.')
            store.delete(login)
            print(f'«{login}» удалена.', file=out)
            if login in settings.dashboard_panel_users:
                print('Внимание: логин остался в DASHBOARD_PANEL_USERS и снова действует оттуда.', file=out)
            return 0
        if args.command in ('disable', 'enable'):
            account = change_account(store, login, create=False, active=args.command == 'enable', by=BY)
            print(describe(account), file=out)
            return 0
        changes = {}
        if args.role:
            changes['role'] = args.role
        if args.name is not None:
            changes['name'] = args.name
        if args.phone:
            changes['phone'] = args.phone
        if getattr(args, 'no_phone', False):
            changes['phone'] = None
        if args.directions:
            changes['directions'] = args.directions
        if getattr(args, 'all_directions', False):
            changes['directions'] = ()
        if args.password:
            changes['password'] = password_source()
        if getattr(args, 'no_password', False):
            changes['password'] = None
        account = change_account(store, login, create=args.command == 'add', by=BY, **changes)
        print(describe(account), file=out)
        if login in settings.dashboard_panel_users:
            print('Запись в базе теперь сильнее строки из DASHBOARD_PANEL_USERS.', file=out)
        return 0
    except PanelUserError as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        return 2


def import_env(settings: Settings, store: PanelUserStore, out) -> int:
    """Перенос из переменных. Записи, которые уже есть в базе, не трогаем: база сильнее."""
    moved = []
    for login in settings.dashboard_panel_users:
        if store.get(login) is not None:
            print(f'«{login}» уже в базе — пропуск.', file=out)
            continue
        import_env_account(store, settings, login, by=BY)
        moved.append(login)
    print(f'Перенесено: {len(moved)}' + (f' ({", ".join(moved)})' if moved else '') + '.', file=out)
    if moved:
        print('Теперь можно снять DASHBOARD_PANEL_USERS, DASHBOARD_PHONE_USERS и '
              'DASHBOARD_MANAGER_DIRECTIONS.', file=out)
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))
