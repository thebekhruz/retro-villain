import asyncio
import base64
import binascii
import re
import secrets
from ipaddress import ip_address
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from retro.config import ROOT, Settings
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

STATIC = Path(__file__).parent / 'static'
LOOPBACK = ('127.0.0.1', '::1')
LOCAL_HOSTNAMES = ('127.0.0.1', 'localhost', '::1')
# Страницы финансового отдела и его API: и чтение, и записи о зарплатах.
FINANCE_PATHS = re.compile(r'/accountant(/|$)|/api/accountant(/|$)')


def create_app(settings=None, *, expense_db_path=None, accountant_db_path=None, rate_transport=None,
               director_db_path=None, gemini_transport=None, booking_transport=None):
    settings = settings or Settings.from_env()
    app = FastAPI(title='Retro Milliy', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.iiko = IikoClient(settings)
    app.state.bookings = BookingAnalyticsClient(settings, transport=booking_transport)
    app.state.iiko_lock = asyncio.Lock()
    app.state.director_lock = asyncio.Lock()
    app.state.cache = SnapshotCache()
    database_path = expense_db_path or ROOT / 'build' / 'cashier.sqlite3'
    app.state.expenses = ExpenseStore(database_path)
    app.state.usd_rates = UsdRates(database_path, transport=rate_transport)
    accountant_path = accountant_db_path or ROOT / 'build' / 'accountant-demo.sqlite3'
    app.state.accountant_roster = RosterStore(accountant_path)
    app.state.accountant_finance = FinanceStore(accountant_path)
    director_path = director_db_path or ROOT / 'build' / 'director.sqlite3'
    app.state.director_store = DirectorReportStore(director_path)
    app.state.gemini = GeminiClient(settings, transport=gemini_transport)
    app.state.director_service = DirectorService(app.state.iiko, app.state.gemini, app.state.director_store)

    def from_this_machine(request):
        """Личные данные и деньги финансового отдела открываем только с самой машины.

        Заголовок Host подставляет сам клиент: с `Host: localhost` модуль
        открывался из сети ресторана по общему паролю дашборда. Решает адрес
        соединения, проверка Host остаётся дополнительным ограничителем.
        """
        address = request.client.host if request.client else None
        return address in LOOPBACK and request.url.hostname in LOCAL_HOSTNAMES

    @app.middleware('http')
    async def security(request: Request, call_next):
        if FINANCE_PATHS.match(request.url.path) and not from_this_machine(request):
            return JSONResponse({'detail': 'Модуль финансов доступен только локально до настройки защиты.'}, 403)
        if settings.dashboard_allowed_network and request.client:
            try:
                address = ip_address(request.client.host)
            except ValueError:
                return JSONResponse({'detail': 'Адрес клиента не распознан.'}, 403)
            if not address.is_loopback and address not in settings.dashboard_allowed_network:
                return JSONResponse({'detail': 'Доступ разрешён только из локальной сети ресторана.'}, 403)
        authorized = False
        if settings.dashboard_password:
            header = request.headers.get('Authorization', '')
            if header.startswith('Basic '):
                try:
                    user, password = base64.b64decode(header[6:], validate=True).decode().split(':', 1)
                    authorized = secrets.compare_digest(user.encode(), settings.dashboard_user.encode()) & secrets.compare_digest(password.encode(), settings.dashboard_password.encode())
                except (ValueError, UnicodeDecodeError, binascii.Error):
                    pass
            if not authorized:
                return JSONResponse({'detail': 'Для просмотра отчётов требуется вход.'}, 401,
                                    headers={'WWW-Authenticate': 'Basic realm="Retro Milliy", charset="UTF-8"', 'Cache-Control': 'no-store'})
        elif request.client is None or request.client.host not in ('127.0.0.1', '::1'):
            return JSONResponse({'detail': 'Внешний доступ закрыт. Настройте защиту дашборда.'}, 403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
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
    def config():
        return dict(today=today_tashkent().isoformat(), timezone='Asia/Tashkent',
                    configured=settings.configured, restaurant='Retro Milliy',
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
