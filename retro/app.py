import asyncio
import base64
import binascii
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
from retro.integrations.gemini import GeminiClient
from retro.logging_config import configure_logging
from retro.security import client_address, is_finance_path, is_local_host, validate_mutation_origin

STATIC = Path(__file__).parent / 'static'


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


def dashboard_identity(request: Request, settings: Settings) -> str | None:
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
            return role
    if settings.dashboard_password:
        valid = (secrets.compare_digest(username.encode(), settings.dashboard_user.encode())
                 & secrets.compare_digest(password.encode(), settings.dashboard_password.encode()))
        if valid:
            return 'all'
    return None


def create_app(settings=None, *, expense_db_path=None, accountant_db_path=None, rate_transport=None,
               director_db_path=None, gemini_transport=None, booking_transport=None):
    configure_logging()
    settings = settings or Settings.from_env()
    app = FastAPI(title='Retro Milliy', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
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
    director_path = director_db_path or settings.data_dir / 'director.sqlite3'
    app.state.director_store = DirectorReportStore(director_path)
    app.state.gemini = GeminiClient(settings, transport=gemini_transport)
    app.state.director_service = DirectorService(
        app.state.iiko, app.state.gemini, app.state.director_store, settings.report_retention)

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
        role = dashboard_identity(request, settings) if auth_configured else 'all'
        if auth_configured:
            if role is None:
                return JSONResponse({'detail': 'Для просмотра отчётов требуется вход.'}, 401,
                                    headers={'WWW-Authenticate': 'Basic realm="Retro Milliy", charset="UTF-8"', 'Cache-Control': 'no-store'})
        elif not address.is_loopback:
            return JSONResponse({'detail': 'Внешний доступ закрыт. Настройте защиту дашборда.'}, 403)
        required_panel = panel_for_path(request.url.path)
        if required_panel and role not in ('all', required_panel):
            return JSONResponse({'detail': 'Эта панель недоступна для вашей учётной записи.'}, 403)
        request.state.dashboard_role = role
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

    @app.get('/api/config')
    def config(request: Request):
        return dict(today=today_tashkent().isoformat(), timezone='Asia/Tashkent',
                    configured=settings.configured, restaurant='Retro Milliy',
                    role=getattr(request.state, 'dashboard_role', 'all'),
                    modules=[dict(id='cashier', name='Кассир', available=True),
                             dict(id='accountant', name='Бухгалтер', available=True),
                             dict(id='director', name='Директор', available=True),
                             dict(id='founder', name='Учредитель', available=True)], planned_modules=0)

    app.include_router(cashier_router)
    app.include_router(accountant_router)
    app.include_router(director_router)
    app.include_router(founder_router)
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()
