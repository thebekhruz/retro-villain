import asyncio
import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.integrations.bookings import BookingAnalyticsClient
from retro.integrations.cbu import UsdRates
from retro.modules.cashier.expenses import ExpenseStore
from retro.modules.cashier.routes import router as cashier_router
from retro.modules.accountant.routes import router as accountant_router
from retro.modules.accountant.roster import RosterStore
from retro.modules.accountant.ledger import FinanceStore
from retro.modules.cashier.service import SnapshotCache, today_tashkent
from retro.modules.director.store import DirectorReportStore
from retro.modules.director.service import DirectorService
from retro.modules.director.routes import router as director_router
from retro.modules.founder.routes import router as founder_router
from retro.modules.founder.chat import FounderChatStore
from retro.integrations.claude import ClaudeClient
from retro.integrations.hikvision import HikvisionClient
from retro.integrations.hikvision_poller import HikvisionPoller
from retro.modules.accountant.hikvision import AttendanceService, AttendanceStore
from retro.logging_config import configure_logging
from retro.security import effective_scheme, client_address, is_finance_path, is_local_host, validate_mutation_origin
from retro.sessions import SessionIdentity, SessionStore

STATIC = Path(__file__).parent / 'static'
SESSION_COOKIE = 'retro_session'
PUBLIC_PATHS = {'/login', '/api/session', '/static/login.css', '/static/login.js'}
ROLE_PATHS = {'cashier': '/', 'accountant': '/accountant',
              'director': '/director', 'founder': '/founder',
              'admin': '/', 'all': '/'}
FULL_ACCESS_ROLES = {'admin', 'all'}


class LoginInput(BaseModel):
    username: str
    password: str


def panel_for_path(path: str) -> str | None:
    if path == '/' or path.startswith('/api/cashier/'):
        return 'cashier'
    if path == '/accountant' or path.startswith('/accountant/') or path.startswith('/api/accountant/'):
        return 'accountant'
    if path == '/director' or path.startswith('/director/') or path.startswith('/api/director/'):
        return 'director'
    if path == '/founder' or path.startswith('/founder/') or path.startswith('/api/founder/'):
        return 'founder'
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
               hikvision_client=None, hikvision_poller=None):
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

    app = FastAPI(title='Retro Milliy', docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)
    app.state.settings = settings
    app.state.sessions = SessionStore()
    app.state.iiko = IikoClient(settings)
    app.state.bookings = BookingAnalyticsClient(settings, transport=booking_transport)
    app.state.iiko_lock = asyncio.Lock()
    app.state.director_lock = asyncio.Lock()
    app.state.cache = SnapshotCache()
    database_path = expense_db_path or settings.data_dir / 'cashier.sqlite3'
    app.state.expenses = ExpenseStore(database_path)
    app.state.usd_rates = UsdRates(database_path, transport=rate_transport)
    accountant_path = accountant_db_path or settings.data_dir / 'accountant.sqlite3'
    app.state.accountant_roster = RosterStore(accountant_path)
    app.state.accountant_finance = FinanceStore(accountant_path)
    app.state.attendance_store = AttendanceStore(accountant_path)
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
    director_path = director_db_path or settings.data_dir / 'director.sqlite3'
    app.state.director_store = DirectorReportStore(director_path)
    founder_path = founder_db_path or settings.data_dir / 'founder.sqlite3'
    app.state.founder_chat_store = FounderChatStore(founder_path)
    app.state.claude = ClaudeClient(settings, transport=claude_transport)
    app.state.director_service = DirectorService(
        app.state.iiko, app.state.claude, app.state.director_store, settings.report_retention)

    @app.middleware('http')
    async def security_middleware(request: Request, call_next):
        request.state.request_id = secrets.token_hex(8)
        try:
            address = client_address(request, settings)
        except ValueError as error:
            return JSONResponse({'detail': str(error)}, 403)
        if is_finance_path(request.url.path) and (
                not address.is_loopback or not is_local_host(request.url.hostname)):
            return JSONResponse({'detail': 'Модуль финансов доступен только локально до настройки защиты.'}, 403)
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
        if (required_panel and not public and role not in FULL_ACCESS_ROLES
                and role != required_panel):
            return JSONResponse({'detail': 'Эта панель недоступна для вашей учётной записи.'}, 403)
        request.state.dashboard_role = role
        request.state.dashboard_user = identity.username if identity else None
        try:
            validate_mutation_origin(request)
        except ValueError as error:
            return JSONResponse({'detail': str(error)}, 403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
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

    @app.get('/director')
    def director():
        return FileResponse(STATIC / 'director.html')

    @app.get('/founder')
    def founder():
        return FileResponse(STATIC / 'founder.html')

    MODULE_NAMES = (('cashier', 'Кассир', '/'), ('accountant', 'Бухгалтер', '/accountant'),
                    ('director', 'Директор', '/director'), ('founder', 'Учредитель', '/founder'))

    @app.get('/api/config')
    def config(request: Request):
        # Меню рисуется на клиенте, а право входа знает только сервер.
        # Отдаём его вместе с причиной: закрытый модуль должен выглядеть
        # закрытым, а не открывать страницу с отказом.
        role = getattr(request.state, 'dashboard_role', 'all')
        try:
            local = client_address(request, settings).is_loopback and is_local_host(request.url.hostname)
        except ValueError:
            local = False
        modules = []
        for panel, name, path in MODULE_NAMES:
            reason = ''
            if role not in FULL_ACCESS_ROLES and role != panel:
                reason = 'Доступно другой учётной записи'
            elif panel == 'accountant' and not local:
                reason = 'Открывается только на машине сервера'
            modules.append(dict(id=panel, name=name, path=path,
                                available=not reason, reason=reason))
        return dict(today=today_tashkent().isoformat(), timezone='Asia/Tashkent',
                    configured=settings.configured, restaurant='Retro Milliy',
                    role=role, modules=modules, planned_modules=0)

    app.include_router(cashier_router)
    app.include_router(accountant_router)
    app.include_router(director_router)
    app.include_router(founder_router)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()
