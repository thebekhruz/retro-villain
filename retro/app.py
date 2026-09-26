import asyncio
import base64
import binascii
import posixpath
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from retro.report_cache import ReportCache, load_iiko
from retro.financial_requests import FinancialRequests
from retro.config import Settings
from retro.db import Database
from retro.integrations.iiko import IikoClient
from retro.integrations.bookings import BookingAnalyticsClient
from retro.integrations.broadcasts import BookingBroadcastClient
from retro.integrations.cbu import UsdRates
from retro.modules.cashier.expenses import ExpenseStore
from retro.modules.cashier.routes import router as cashier_router
from retro.modules.accountant.routes import router as accountant_router
from retro.modules.shokh.routes import router as shokh_router
from retro.modules.shokh.store import ShokhStore
from retro.modules.accountant.roster import RosterStore
from retro.modules.accountant.ledger import FinanceStore
from retro.modules.cashier.service import SnapshotCache, today_tashkent
from retro.modules.director.store import DirectorReportStore
from retro.modules.director.service import DirectorService
from retro.modules.director.routes import router as director_router
from retro.modules.founder.routes import router as founder_router
from retro.modules.founder.chat import FounderChatStore
from retro.modules.founder.dividends import DividendTargetStore
from retro.integrations.claude import ClaudeClient
from retro.integrations.hikvision import HikvisionClient
from retro.integrations.hikvision_poller import HikvisionPoller
from retro.modules.accountant.hikvision import AttendanceService, AttendanceStore
from retro.logging_config import configure_logging
from retro.security import effective_scheme, client_address, validate_mutation_origin
from retro.sessions import SessionIdentity, SessionStore

STATIC = Path(__file__).parent / 'static'
SESSION_COOKIE = 'retro_session'
PUBLIC_PATHS = {'/login', '/api/session', '/static/login.css', '/static/login.js',
                '/static/i18n.js', '/static/i18n-uz.js', '/static/favicon.svg',
                '/static/favicon-32.png', '/static/apple-touch-icon.png'}
ROLE_PATHS = {'cashier': '/', 'accountant': '/accountant',
              'director': '/director', 'founder': '/founder',
              'shokh': '/shokh', 'admin': '/', 'all': '/'}
FULL_ACCESS_ROLES = {'admin', 'all'}


class LoginInput(BaseModel):
    username: str
    password: str


# Статика модулей: файл → панели, которым он нужен. Раньше /static/ отдавался
# любой вошедшей роли целиком, и кассир открывал /static/accountant.html или
# /static/director-app.js — чужие окна и их логику. Файлы, которых здесь нет,
# общие (каркас, меню, словари, выход, иконки): их получает любой вошедший.
# Новый файл модуля вписывать сюда — tests/test_module_access.py сверяет
# таблицу с тем, что реально подключают страницы.
STATIC_PANELS: dict[str, frozenset[str]] = {
    name: frozenset(panels) for name, panels in {
        # Кассир
        'index.html': {'cashier'}, 'app.js': {'cashier'}, 'cashier-logic.js': {'cashier'},
        'cashier.css': {'cashier'},
        # Бухгалтер: финансы дня, сотрудники, ведомость
        'accountant.html': {'accountant'}, 'employees.html': {'accountant'},
        'payroll.html': {'accountant'}, 'accountant.js': {'accountant'},
        'employees.js': {'accountant'}, 'payroll.js': {'accountant'},
        'payroll-logic.js': {'accountant'}, 'accountant.css': {'accountant'},
        'employees.css': {'accountant'}, 'payroll.css': {'accountant'},
        # Расчёты бухгалтерии читают экраны директора и учредителя
        'accountant-logic.js': {'accountant', 'director', 'founder'},
        # Директор
        'director.html': {'director'}, 'director-app.html': {'director'},
        'director.js': {'director'}, 'director-app.js': {'director'},
        'director.css': {'director'}, 'ai-chat.css': {'director'}, 'period.js': {'director'},
        # Общее у директора и учредителя: телефонный каркас, чат, разметка ответов
        'director-app.css': {'director', 'founder'}, 'director-logic.js': {'director', 'founder'},
        'ai-chat.js': {'director', 'founder'}, 'founder-chat.js': {'director', 'founder'},
        'founder-markdown.js': {'director', 'founder'},
        # Учредитель
        'founder.html': {'founder'}, 'founder-cabinet.html': {'founder'},
        'founder.js': {'founder'}, 'founder-cabinet.js': {'founder'},
        'founder-logic.js': {'founder'}, 'founder-cabinet-logic.js': {'founder'},
        'founder-broadcast.js': {'founder'}, 'founder-broadcast-logic.js': {'founder'},
        'founder.css': {'founder'}, 'founder-cabinet.css': {'founder'},
        # Закуп · Шох
        'shokh.html': {'shokh'}, 'shokh.js': {'shokh'}, 'shokh-logic.js': {'shokh'},
        'shokh.css': {'shokh'},
    }.items()
}


def static_panels(path: str) -> frozenset[str] | None:
    """Панели, которым открыт файл /static/…; None — файл общий.

    Имя приводим к виду, в котором его найдёт диск: «a/../accountant.html»
    и «Accountant.html» (на регистронезависимой ФС) — тот же файл."""
    if not path.startswith('/static/'):
        return None
    name = posixpath.normpath(path[len('/static/'):]).lstrip('/').lower()
    return STATIC_PANELS.get(name)


def panel_for_path(path: str) -> str | None:
    if path == '/' or path.startswith('/api/cashier/'):
        return 'cashier'
    if path == '/accountant' or path.startswith('/accountant/') or path.startswith('/api/accountant/'):
        return 'accountant'
    if path == '/director' or path.startswith('/director/') or path.startswith('/api/director/'):
        return 'director'
    if path == '/founder' or path.startswith('/founder/') or path.startswith('/api/founder/'):
        return 'founder'
    if path == '/shokh' or path.startswith('/shokh/') or path.startswith('/api/shokh/'):
        return 'shokh'
    return None


def dashboard_identity(
        request: Request, settings: Settings, sessions: SessionStore,
) -> SessionIdentity | None:
    session_identity = sessions.identity(request.cookies.get(SESSION_COOKIE))
    if session_identity:
        return session_identity
    header = request.headers.get('Authorization', '')
    if not header.startswith('Basic '):
        return None
    try:
        username, password = base64.b64decode(
            header[6:], validate=True).decode().split(':', 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    panel_user = settings.dashboard_panel_users.get(username)
    if panel_user:
        expected_password, role = panel_user
        if secrets.compare_digest(password.encode(), expected_password.encode()):
            return SessionIdentity(username, role)
    if settings.dashboard_password:
        valid = (secrets.compare_digest(username.encode(), settings.dashboard_user.encode())
                 & secrets.compare_digest(password.encode(), settings.dashboard_password.encode()))
        if valid:
            return SessionIdentity(username, 'all')
    return None


def create_app(settings=None, *, expense_db_path=None, accountant_db_path=None, rate_transport=None,
               director_db_path=None, founder_db_path=None, claude_transport=None, booking_transport=None,
               broadcast_transport=None, hikvision_client=None, hikvision_poller=None):
    configure_logging()
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application):
        poller = application.state.hikvision_poller
        if poller is not None:
            poller.start()
        try:
            yield
        finally:
            if poller is not None:
                await poller.stop()
            await application.state.reports.close()
            close = getattr(application.state.iiko, 'close', None)
            if close is not None:
                await close()

    app = FastAPI(title='Retro Milliy', docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.settings = settings
    # Список сессий лежит рядом с базами модулей, на том же томе:
    # иначе каждый деплой выбрасывает смену на экран входа.
    app.state.sessions = SessionStore(settings.data_dir / 'sessions.json')
    app.state.iiko = IikoClient(settings)
    app.state.bookings = BookingAnalyticsClient(settings, transport=booking_transport)
    app.state.broadcasts = BookingBroadcastClient(settings, transport=broadcast_transport)
    app.state.reports = ReportCache()
    app.state.director_lock = asyncio.Lock()
    app.state.cache = SnapshotCache()
    # В Postgres все таблицы живут в одной базе; в SQLite — каждый модуль в
    # своём файле, как было. Явно переданные пути (тесты, скрипты) сильнее.
    shared = Database(settings.database_url) if settings.database_url else None
    database_path = expense_db_path or shared or settings.data_dir / 'cashier.sqlite3'
    app.state.expenses = ExpenseStore(database_path)
    app.state.usd_rates = UsdRates(database_path, transport=rate_transport)
    accountant_path = accountant_db_path or shared or settings.data_dir / 'accountant.sqlite3'
    app.state.accountant_roster = RosterStore(accountant_path)
    app.state.accountant_finance = FinanceStore(accountant_path)
    app.state.attendance_store = AttendanceStore(accountant_path)
    # Закуп живёт в той же базе, что подотчёт бухгалтера: они про одни деньги.
    app.state.shokh = ShokhStore(accountant_path)
    # Недельную цель дивидендов ставит учредитель, а видит бухгалтер: храним
    # рядом с резервом `dividends`, в который эти деньги и откладываются.
    app.state.dividend_targets = DividendTargetStore(accountant_path)
    app.state.attendance = AttendanceService(
        app.state.attendance_store,
        source=settings.hikvision.source if settings.hikvision else 'retro-main-entry',
        enabled=settings.hikvision_configured,
        poll_seconds=settings.hikvision.poll_seconds if settings.hikvision else 30)
    if hikvision_poller is not None:
        app.state.hikvision_poller = hikvision_poller
    elif settings.hikvision is not None:
        client = hikvision_client or HikvisionClient(settings.hikvision)
        app.state.hikvision_poller = HikvisionPoller(
            settings.hikvision, client, app.state.accountant_roster,
            app.state.attendance_store)
    else:
        app.state.hikvision_poller = None
    director_path = director_db_path or shared or settings.data_dir / 'director.sqlite3'
    app.state.director_store = DirectorReportStore(director_path)
    founder_path = founder_db_path or shared or settings.data_dir / 'founder.sqlite3'
    app.state.founder_chat_store = FounderChatStore(founder_path)
    app.state.claude = ClaudeClient(settings, transport=claude_transport)
    app.state.director_service = DirectorService(
        app.state.iiko, app.state.claude, app.state.director_store, settings.report_retention,
        loader=lambda today: load_iiko(app.state, 'load_director_report', today, timeout=150))

    app.state.financial_requests = FinancialRequests(
        shared or Path(accountant_db_path or settings.data_dir / 'accountant.sqlite3')
        .with_name('financial-requests.sqlite3'))

    @app.middleware('http')
    async def security_middleware(request: Request, call_next):
        request.state.request_id = secrets.token_hex(8)
        try:
            address = client_address(request, settings)
        except ValueError as error:
            return JSONResponse({'detail': str(error)}, 403)
        if settings.dashboard_allowed_network:
            if not address.is_loopback and address not in settings.dashboard_allowed_network:
                return JSONResponse({'detail': 'Доступ разрешён только из локальной сети ресторана.'}, 403)
        auth_configured = bool(settings.dashboard_password or settings.dashboard_panel_users)
        identity = (dashboard_identity(request, settings, app.state.sessions)
                    if auth_configured else SessionIdentity('local', 'all'))
        role = identity.role if identity else None
        public = request.url.path in PUBLIC_PATHS
        # Закрыт внешний доступ только тогда, когда защита не настроена вовсе.
        # Раньше эта проверка стояла в ветке elif и срабатывала на страницу
        # входа: пароли заданы, но форму логина снаружи никто не получал.
        if not auth_configured and not address.is_loopback:
            return JSONResponse({'detail': 'Внешний доступ закрыт. Настройте защиту дашборда.'}, 403)
        if auth_configured and not public and role is None:
            if not request.url.path.startswith(('/api/', '/static/')):
                return RedirectResponse('/login', status_code=303)
            return JSONResponse({'detail': 'Для просмотра отчётов требуется вход.'}, 401,
                                headers={'WWW-Authenticate': 'Basic realm="Retro Milliy", charset="UTF-8"', 'Cache-Control': 'no-store'})
        required_panel = panel_for_path(request.url.path)
        allowed_panels = static_panels(request.url.path)
        if not public and role not in FULL_ACCESS_ROLES and (
                (required_panel and role != required_panel)
                or (allowed_panels is not None and role not in allowed_panels)):
            return JSONResponse({'detail': 'Эта панель недоступна для вашей учётной записи.'}, 403)
        request.state.dashboard_role = role
        request.state.dashboard_user = identity.username if identity else None
        try:
            validate_mutation_origin(request)
        except ValueError as error:
            return JSONResponse({'detail': str(error)}, 403)
        response = await app.state.financial_requests.dispatch(request, call_next)
        response.headers['Cache-Control'] = (
            'private, no-cache' if request.url.path.startswith('/static/') else 'no-store')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Request-ID'] = request.state.request_id
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html')

    @app.get('/login')
    def login_page(request: Request):
        role = getattr(request.state, 'dashboard_role', None)
        if role:
            return RedirectResponse(ROLE_PATHS[role], status_code=303)
        return FileResponse(STATIC / 'login.html')

    @app.post('/api/session')
    def login(request: Request, body: LoginInput):
        role = None
        panel_user = settings.dashboard_panel_users.get(body.username)
        if panel_user:
            expected_password, candidate_role = panel_user
            if secrets.compare_digest(body.password.encode(), expected_password.encode()):
                role = candidate_role
        if role is None and settings.dashboard_password:
            valid = (secrets.compare_digest(body.username.encode(), settings.dashboard_user.encode())
                     & secrets.compare_digest(body.password.encode(), settings.dashboard_password.encode()))
            if valid:
                role = 'all'
        if role is None:
            return JSONResponse({'detail': 'Неверный логин или пароль.'}, 401)
        token = app.state.sessions.create(body.username, role)
        response = JSONResponse({'role': role, 'path': ROLE_PATHS[role]})
        response.set_cookie(
            SESSION_COOKIE, token, max_age=12 * 60 * 60, httponly=True,
            samesite='strict', secure=effective_scheme(request) == 'https', path='/')
        return response

    def finish_logout(request: Request, response: Response):
        app.state.sessions.delete(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path='/', samesite='strict')
        return response

    @app.post('/api/session/logout', status_code=204)
    def logout(request: Request):
        return finish_logout(request, Response(status_code=204))

    @app.get('/logout')
    def logout_page(request: Request):
        return finish_logout(request, RedirectResponse('/login', status_code=303))

    @app.get('/accountant')
    def accountant():
        return FileResponse(STATIC / 'accountant.html')

    @app.get('/accountant/employees')
    def accountant_employees():
        return FileResponse(STATIC / 'employees.html')

    @app.get('/accountant/payroll')
    def accountant_payroll():
        return FileResponse(STATIC / 'payroll.html')

    @app.get('/shokh')
    def shokh_page():
        return FileResponse(STATIC / 'shokh.html')

    @app.get('/director')
    def director():
        # Телефон директора (6a). Прежний десктопный отчёт с полной таблицей
        # блюд и PDF-архивом остаётся рядом, на /director/report.
        return FileResponse(STATIC / 'director-app.html')

    @app.get('/director/report')
    def director_report():
        return FileResponse(STATIC / 'director.html')

    @app.get('/founder')
    def founder():
        # Кабинет учредителя (7a на телефоне, 7b на компьютере). Аналитика
        # iiko, брони и рассылки живут на /founder/analytics.
        return FileResponse(STATIC / 'founder-cabinet.html')

    @app.get('/founder/analytics')
    def founder_analytics():
        return FileResponse(STATIC / 'founder.html')

    MODULE_NAMES = (('cashier', 'Кассир', '/'), ('accountant', 'Бухгалтер', '/accountant'),
                    ('director', 'Директор', '/director'), ('founder', 'Учредитель', '/founder'),
                    ('shokh', 'Закуп · Шох', '/shokh'))

    @app.get('/api/config')
    def config(request: Request):
        # Меню рисуется на клиенте, а право входа знает только сервер. Чужие
        # модули не отдаём вовсе: роль не должна даже знать, какие окна есть
        # у других (раньше они приходили погашенными, с замком).
        role = getattr(request.state, 'dashboard_role', 'all')
        modules = [dict(id=panel, name=name, path=path, available=True)
                   for panel, name, path in MODULE_NAMES
                   if role in FULL_ACCESS_ROLES or role == panel]
        return dict(today=today_tashkent().isoformat(), timezone='Asia/Tashkent',
                    configured=settings.configured, restaurant='Retro Milliy',
                    role=role, modules=modules, planned_modules=0)

    app.include_router(cashier_router)
    app.include_router(accountant_router)
    app.include_router(shokh_router)
    app.include_router(director_router)
    app.include_router(founder_router)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()
