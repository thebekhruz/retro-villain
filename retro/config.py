import os
import re
from ipaddress import IPv4Network, IPv6Network, ip_network
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from urllib.parse import urlsplit
from decimal import Decimal

from dotenv import load_dotenv

from retro.runtime import resolve_data_dir
from retro.modules.manager.directions import parse_manager_directions
from retro.phone_numbers import parse_phone_users

ROOT = Path(__file__).resolve().parent.parent
IIKO_ORIGIN = 'https://retro3158.iikoweb.ru'
SMS_PROVIDERS = ('eskiz', 'console')
# Текст SMS с кодом входа. У Eskiz каждый текст проходит модерацию шаблона:
# отправится только тот, что одобрен в кабинете Eskiz, слово в слово.
SMS_TEMPLATE = 'Код подтверждения для входа в платформу Retro: {code}'
# История продаж (T-499): с какого дня держим чеки iiko в своей базе и что
# считаем обедом. Окно — минуты от полуночи по Ташкенту, правая граница не входит.
SALES_HISTORY_START = date(2026, 9, 1)
LUNCH_HOURS = (12 * 60, 15 * 60)
# Сколько дней хранить почасовые контрольные снимки продаж (T-507).
SALES_SNAPSHOTS_KEPT_DAYS = 400
# Счёт Шефа за неделю (ТЗ 16.10, п. 4.2), сум по меню: с этой суммы учредитель
# получает сообщение, повтор — когда сумма выросла ещё на шаг. Переменные
# CHEF_WEEK_LIMIT и CHEF_WEEK_REPEAT_STEP меняют их без правки кода.
CHEF_WEEK_LIMIT = Decimal(30_000_000)
CHEF_WEEK_REPEAT_STEP = Decimal(10_000_000)
TELEGRAM_TOKEN = r'[0-9]{5,16}:[A-Za-z0-9_-]{30,64}'
TELEGRAM_CHAT = r'-?[0-9]{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31}'
DEFAULT_PRICE_TIERS = (30000, 45000)


@dataclass(frozen=True)
class SmsConfig:
    """SMS для входа по номеру (ТЗ 09.10, М-05): Eskiz на бою, консоль на стенде."""
    provider: str
    email: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    sender: str = '4546'
    template: str = SMS_TEMPLATE
    timeout_seconds: int = 10


@dataclass(frozen=True)
class DriveBackupConfig:
    """Резерв отчётов в Google Drive (T-476): OAuth-клиент и токен владельца диска."""
    client_id: str
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    folder_name: str = 'Retro Milliy · резерв отчётов'
    start: date | None = None


@dataclass(frozen=True)
class HikvisionConfig:
    base_url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    source: str = 'retro-main-entry'
    poll_seconds: int = 30
    timeout_seconds: int = 8
    verify_tls: bool = True


@dataclass(frozen=True)
class TelegramReportConfig:
    """Снимок кассы в Telegram-группу (T-478): бот, чат и время отправки."""
    token: str = field(repr=False)
    chat_id: str
    send_at: time = time(23, 30)
    # Выключено — бот шлёт только по кнопке проверки, без расписания.
    auto: bool = True


@dataclass(frozen=True)
class ChefAlertConfig:
    """Счёт Шефа за неделю (ТЗ 16.10, п. 4.2): порог, шаг повтора и чат учредителя.

    Чат учредителя свой: группа кассы (TELEGRAM_REPORT_CHAT_ID) для его данных
    не подходит. Без бота или чата сообщение не уходит — порог виден в
    кабинете, а событие пишется в журнал `founder_chef_alerts`."""
    week_limit: Decimal = CHEF_WEEK_LIMIT
    # 0 — без повторов: одно сообщение за неделю.
    repeat_step: Decimal = CHEF_WEEK_REPEAT_STEP
    token: str = field(default='', repr=False)
    chat_id: str = ''
    # Корень панели для ссылки в сообщении; пусто — сообщение без ссылки.
    public_url: str = ''

    @property
    def recipient(self) -> bool:
        return bool(self.token and self.chat_id)


@dataclass(frozen=True)
class PushConfig:
    """Уведомления в браузер (Web Push). Ключ VAPID необязателен: без него сервер
    создаёт свой и хранит в базе. `subject` — контакт для служб доставки; Apple
    принимает только mailto: или https:."""
    private_key: str = field(default='', repr=False)
    subject: str = 'mailto:push@retro-milliy.uz'


@dataclass(frozen=True)
class Settings:
    base_url: str = IIKO_ORIGIN
    login: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    store_id: int | None = None
    dashboard_user: str = field(default='', repr=False)
    dashboard_password: str = field(default='', repr=False)
    dashboard_panel_users: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    # Направления менеджеров (ТЗ 09.10, М-01): логин → «Кухня», «Зал», «Уборка».
    # Менеджер без строки здесь видит все направления.
    manager_directions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Вход по номеру телефона (ТЗ 09.10, М-05): «+998901234567» → логин из
    # DASHBOARD_PANEL_USERS. Номер находит уже заведённую учётную запись,
    # новых по номеру не бывает.
    phone_users: dict[str, str] = field(default_factory=dict, repr=False)
    sms: SmsConfig | None = field(default=None, repr=False)
    dashboard_allowed_network: IPv4Network | IPv6Network | None = None
    trusted_proxy_network: IPv4Network | IPv6Network | None = None
    manual_handover_only: bool = False
    # Временный режим проверки: выдачу можно записать без данных кассира за день,
    # и отрицательный остаток не отменяет операцию. Ставит фиктивный приход 0,
    # поэтому на боевом контуре держать включённым нельзя — только на время
    # прогона. Выключается снятием ACCOUNTANT_CHECK_MODE.
    check_mode: bool = False
    # Модуль «Закуп · Шох» (телефон Шоха). С ТЗ 02.10 (п. 5) расходы Шоха
    # вносит бухгалтер по счёт-фактуре на «Балансе Шохруха», а модуля закупа
    # нет вовсе: ни в меню, ни на входе, ни в API. Вернуть — SHOKH_MODULE=1.
    shokh_module: bool = False
    # KPI официантов (T-465…T-469): вкладка у менеджера, карточка и настройка
    # периода у директора, /api/*/kpi. Пока обкатывается только на стенде
    # (prerelease): в этой ветке включён по умолчанию, в `main` — выключен
    # (T-522). Переопределить — KPI_MODULE=1 / KPI_MODULE=0.
    kpi_module: bool = True
    claude_api_key: str = field(default='', repr=False)
    claude_model: str = ''
    director_categories: dict[str, str] = field(default_factory=dict)
    director_excluded_groups: frozenset[str] = field(default_factory=frozenset)
    # Пороги ценовых диапазонов меню директора, сум (ТЗ 16.10, 3.2): два порога —
    # «до 30 тыс», «30–45 тыс», «от 45 тыс». Пороги не окончательные, поэтому в
    # переменной окружения, а не в коде.
    director_price_tiers: tuple[int, ...] = DEFAULT_PRICE_TIERS
    booking_api_url: str = ''
    booking_api_token: str = field(default='', repr=False)
    booking_broadcast_token: str = field(default='', repr=False)
    website_booking_api_url: str = ''
    website_booking_api_token: str = field(default='', repr=False)
    hikvision: HikvisionConfig | None = field(default=None, repr=False)
    drive_backup: DriveBackupConfig | None = field(default=None, repr=False)
    telegram_report: TelegramReportConfig | None = field(default=None, repr=False)
    chef_alert: ChefAlertConfig = field(default_factory=ChefAlertConfig, repr=False)
    push: PushConfig = field(default_factory=PushConfig, repr=False)
    data_dir: Path = ROOT / 'build'
    # Строка подключения Postgres. Пустая — работаем на файловом SQLite
    # (локальная разработка и тесты). На Railway диска нет, поэтому в
    # продакшене она обязательна: иначе базы исчезнут на выкате.
    database_url: str = ''
    report_retention: int = 24
    sales_history_start: date = SALES_HISTORY_START
    lunch_hours: tuple[int, int] = LUNCH_HOURS
    sales_snapshots_kept_days: int = SALES_SNAPSHOTS_KEPT_DAYS

    @property
    def configured(self):
        return bool(self.login and self.password and self.store_id is not None)

    @property
    def claude_configured(self):
        return bool(self.claude_api_key and self.claude_model)

    @property
    def booking_configured(self):
        return bool(self.booking_api_url and self.booking_api_token)

    @property
    def booking_broadcast_configured(self):
        return bool(self.booking_api_url and self.booking_broadcast_token)

    @property
    def hikvision_configured(self):
        return self.hikvision is not None

    @property
    def phone_login_configured(self):
        return bool(self.phone_users and self.sms is not None)

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / 'build' / '.env', override=False)
        database_url = os.getenv('DATABASE_URL', '').strip()
        require_durable_storage(database_url)
        base = os.getenv('IIKO_SERVER_URL', IIKO_ORIGIN).rstrip('/')
        if base != IIKO_ORIGIN:
            raise ValueError('IIKO_SERVER_URL должен указывать на сервер Retro Milliy.')
        store = os.getenv('IIKO_STORE_ID', '').strip()
        if store and (not store.isdigit() or int(store) <= 0):
            raise ValueError('IIKO_STORE_ID должен быть положительным числом.')
        user, password = os.getenv('DASHBOARD_USER', ''), os.getenv('DASHBOARD_PASSWORD', '')
        if bool(user) != bool(password):
            raise ValueError('Для защиты укажите и DASHBOARD_USER, и DASHBOARD_PASSWORD.')
        panel_users = parse_dashboard_panel_users(os.getenv('DASHBOARD_PANEL_USERS', ''))
        manager_directions = parse_manager_directions(
            os.getenv('DASHBOARD_MANAGER_DIRECTIONS', ''), panel_users)
        phone_users = parse_phone_users(os.getenv('DASHBOARD_PHONE_USERS', ''), panel_users)
        sms = parse_sms_config(os.environ)
        network_value = os.getenv('DASHBOARD_ALLOWED_NETWORK', '').strip()
        allowed_network = ip_network(network_value, strict=False) if network_value else None
        proxy_value = os.getenv('TRUSTED_PROXY_NETWORK', '').strip()
        trusted_proxy_network = ip_network(proxy_value, strict=False) if proxy_value else None
        manual = os.getenv('ACCOUNTANT_MANUAL_HANDOVER', '').strip().casefold() in {'1', 'true', 'yes', 'да'}
        check_mode_on = os.getenv('ACCOUNTANT_CHECK_MODE', '').strip().casefold() \
            in {'1', 'true', 'yes', 'да'}
        shokh_module_on = os.getenv('SHOKH_MODULE', '').strip().casefold() in {'1', 'true', 'yes', 'да'}
        kpi_value = os.getenv('KPI_MODULE', '').strip().casefold()
        kpi_module_on = cls.kpi_module if not kpi_value else kpi_value in {'1', 'true', 'yes', 'да'}
        categories = parse_director_categories(os.getenv('IIKO_DIRECTOR_CATEGORIES', ''))
        excluded_groups = parse_director_excluded_groups(os.getenv('IIKO_DIRECTOR_EXCLUDED_GROUPS', ''))
        price_tiers = parse_director_price_tiers(os.getenv('DIRECTOR_PRICE_TIERS', ''))
        booking_url = os.getenv('BOOKING_ANALYTICS_URL', '').strip().rstrip('/')
        booking_token = os.getenv('ANALYTICS_API_TOKEN', '').strip()
        broadcast_token = os.getenv('BROADCAST_API_TOKEN', '').strip()
        if bool(booking_url) != bool(booking_token):
            raise ValueError('BOOKING_ANALYTICS_URL и ANALYTICS_API_TOKEN задаются вместе.')
        if booking_url:
            parsed = urlsplit(booking_url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or \
                    parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError('BOOKING_ANALYTICS_URL должен быть корневым HTTPS URL без credentials/query.')
        if broadcast_token and not booking_url:
            raise ValueError('Для BROADCAST_API_TOKEN требуется BOOKING_ANALYTICS_URL.')
        if broadcast_token and broadcast_token == booking_token:
            raise ValueError('BROADCAST_API_TOKEN должен отличаться от ANALYTICS_API_TOKEN.')
        website_booking_url = os.getenv('WEBSITE_BOOKING_ANALYTICS_URL', '').strip().rstrip('/')
        website_booking_token = os.getenv('WEBSITE_ANALYTICS_API_TOKEN', '').strip()
        if bool(website_booking_url) != bool(website_booking_token):
            raise ValueError('WEBSITE_BOOKING_ANALYTICS_URL и WEBSITE_ANALYTICS_API_TOKEN задаются вместе.')
        if website_booking_url:
            parsed = urlsplit(website_booking_url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or \
                    parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError('WEBSITE_BOOKING_ANALYTICS_URL должен быть корневым HTTPS URL без credentials/query.')
            if website_booking_url == booking_url:
                raise ValueError('WEBSITE_BOOKING_ANALYTICS_URL должен указывать на другой источник, чтобы не удваивать брони.')
            if website_booking_token in (booking_token, broadcast_token):
                raise ValueError('WEBSITE_ANALYTICS_API_TOKEN должен быть отдельным токеном источника сайта.')
        retention_value = os.getenv('DIRECTOR_REPORT_RETENTION', '24').strip()
        if not retention_value.isdigit() or not 1 <= int(retention_value) <= 1000:
            raise ValueError('DIRECTOR_REPORT_RETENTION должен быть числом от 1 до 1000.')
        hikvision = parse_hikvision_config(os.environ)
        drive_backup = parse_drive_backup_config(os.environ)
        telegram_report = parse_telegram_report_config(os.environ)
        sales_history_start = parse_sales_history_start(os.getenv('SALES_HISTORY_START', ''))
        lunch_hours = parse_lunch_hours(os.getenv('LUNCH_HOURS', ''))
        snapshots_kept_days = parse_sales_snapshots_kept_days(os.getenv('SALES_SNAPSHOTS_KEPT_DAYS', ''))
        chef_alert = parse_chef_alert_config(os.environ)
        push = parse_push_config(os.environ, public_url=chef_alert.public_url)
        return cls(
            base_url=base,
            login=os.getenv('IIKO_LOGIN', ''),
            password=os.getenv('IIKO_PASSWORD', ''),
            store_id=int(store) if store else None,
            dashboard_user=user,
            dashboard_password=password,
            dashboard_panel_users=panel_users,
            manager_directions=manager_directions,
            phone_users=phone_users,
            sms=sms,
            dashboard_allowed_network=allowed_network,
            trusted_proxy_network=trusted_proxy_network,
            manual_handover_only=manual,
            check_mode=check_mode_on,
            shokh_module=shokh_module_on,
            kpi_module=kpi_module_on,
            claude_api_key=os.getenv('CLAUDE_API_KEY', ''),
            claude_model=os.getenv('CLAUDE_MODEL', 'claude-sonnet-4-6'),
            director_categories=categories,
            director_excluded_groups=excluded_groups,
            director_price_tiers=price_tiers,
            booking_api_url=booking_url,
            booking_api_token=booking_token,
            booking_broadcast_token=broadcast_token,
            website_booking_api_url=website_booking_url,
            website_booking_api_token=website_booking_token,
            hikvision=hikvision,
            drive_backup=drive_backup,
            telegram_report=telegram_report,
            chef_alert=chef_alert,
            push=push,
            data_dir=resolve_data_dir(os.getenv('RETRO_DATA_DIR', '').strip()),
            database_url=database_url,
            report_retention=int(retention_value),
            sales_history_start=sales_history_start,
            lunch_hours=lunch_hours,
            sales_snapshots_kept_days=snapshots_kept_days,
        )


def parse_drive_backup_config(environ) -> DriveBackupConfig | None:
    names = ('GOOGLE_DRIVE_CLIENT_ID', 'GOOGLE_DRIVE_CLIENT_SECRET', 'GOOGLE_DRIVE_REFRESH_TOKEN')
    values = [environ.get(name, '').strip() for name in names]
    if not any(values):
        return None
    if not all(values):
        raise ValueError('Для резерва в Google Drive задайте ' + ', '.join(names) + ' вместе.')
    folder = environ.get('GOOGLE_DRIVE_FOLDER_NAME', '').strip() or DriveBackupConfig.folder_name
    start_value = environ.get('GOOGLE_BACKUP_START', '').strip()
    try:
        start = date.fromisoformat(start_value) if start_value else None
    except ValueError:
        raise ValueError('GOOGLE_BACKUP_START — дата вида 2026-10-14.') from None
    return DriveBackupConfig(*values, folder_name=folder, start=start)


def parse_clock(value: str, name: str) -> int:
    """«12», «12:30», «24:00» → минуты от полуночи."""
    match = re.fullmatch(r'([01]?[0-9]|2[0-4])(?::([0-5][0-9]))?', value.strip())
    if not match:
        raise ValueError(f'{name}: время задаётся как ЧЧ или ЧЧ:ММ.')
    minutes = int(match[1]) * 60 + int(match[2] or 0)
    if minutes > 24 * 60:
        raise ValueError(f'{name}: время не позже 24:00.')
    return minutes


def parse_lunch_hours(value: str) -> tuple[int, int]:
    """LUNCH_HOURS=12:00-15:00 — окно обеда по времени закрытия чека."""
    if not value.strip():
        return LUNCH_HOURS
    parts = value.split('-')
    if len(parts) != 2:
        raise ValueError('LUNCH_HOURS задаётся как ЧЧ:ММ-ЧЧ:ММ, например 12:00-15:00.')
    start, end = (parse_clock(part, 'LUNCH_HOURS') for part in parts)
    if start >= end:
        raise ValueError('LUNCH_HOURS: начало обеда должно быть раньше конца.')
    return start, end


def parse_sales_snapshots_kept_days(value: str) -> int:
    """SALES_SNAPSHOTS_KEPT_DAYS=400 — сколько дней хранить почасовые снимки продаж."""
    value = value.strip()
    if not value:
        return SALES_SNAPSHOTS_KEPT_DAYS
    if not value.isdigit() or not 1 <= int(value) <= 3660:
        raise ValueError('SALES_SNAPSHOTS_KEPT_DAYS должен быть числом дней от 1 до 3660.')
    return int(value)


def parse_sales_history_start(value: str) -> date:
    """SALES_HISTORY_START=2026-09-01 — первый день истории продаж в базе."""
    if not value.strip():
        return SALES_HISTORY_START
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError('SALES_HISTORY_START задаётся датой ГГГГ-ММ-ДД.') from None


def parse_telegram_report_config(environ) -> TelegramReportConfig | None:
    token = environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = environ.get('TELEGRAM_REPORT_CHAT_ID', '').strip()
    if not token and not chat_id:
        return None
    if token and not chat_id and environ.get('TELEGRAM_FOUNDER_CHAT_ID', '').strip():
        # Бот только для учредителя (ТЗ 16.10, п. 4.2): снимка кассы в группу нет.
        return None
    if not token or not chat_id:
        raise ValueError('TELEGRAM_BOT_TOKEN и TELEGRAM_REPORT_CHAT_ID задаются вместе.')
    if not re.fullmatch(TELEGRAM_TOKEN, token):
        raise ValueError('TELEGRAM_BOT_TOKEN должен быть токеном бота из @BotFather.')
    if not re.fullmatch(TELEGRAM_CHAT, chat_id):
        raise ValueError('TELEGRAM_REPORT_CHAT_ID — числовой id группы (например -1001234567890) или @канал.')
    raw_time = environ.get('TELEGRAM_REPORT_TIME', '23:30').strip()
    match = re.fullmatch(r'([01][0-9]|2[0-3]):([0-5][0-9])', raw_time)
    if not match:
        raise ValueError('TELEGRAM_REPORT_TIME задаётся как ЧЧ:ММ по Ташкенту, например 23:30.')
    auto = environ.get('TELEGRAM_REPORT_AUTO', 'true').strip().casefold()
    if auto not in {'true', 'false', '1', '0', 'yes', 'no'}:
        raise ValueError('TELEGRAM_REPORT_AUTO должен быть true или false.')
    return TelegramReportConfig(token=token, chat_id=chat_id,
                                send_at=time(int(match[1]), int(match[2])),
                                auto=auto in {'true', '1', 'yes'})


def parse_chef_alert_config(environ) -> ChefAlertConfig:
    def amount(name, default, *, allow_zero):
        raw = environ.get(name, '').strip().replace(' ', '').replace('_', '')
        if not raw:
            return default
        if not raw.isdigit() or len(raw) > 13 or (int(raw) == 0 and not allow_zero):
            raise ValueError(f'{name} — сумма в сумах цифрами, например {int(default)}.')
        return Decimal(int(raw))

    limit = amount('CHEF_WEEK_LIMIT', CHEF_WEEK_LIMIT, allow_zero=False)
    step = amount('CHEF_WEEK_REPEAT_STEP', CHEF_WEEK_REPEAT_STEP, allow_zero=True)
    chat_id = environ.get('TELEGRAM_FOUNDER_CHAT_ID', '').strip()
    token = environ.get('TELEGRAM_BOT_TOKEN', '').strip() if chat_id else ''
    if chat_id:
        if not token:
            raise ValueError('TELEGRAM_FOUNDER_CHAT_ID работает только вместе с TELEGRAM_BOT_TOKEN.')
        if not re.fullmatch(TELEGRAM_TOKEN, token):
            raise ValueError('TELEGRAM_BOT_TOKEN должен быть токеном бота из @BotFather.')
        if not re.fullmatch(TELEGRAM_CHAT, chat_id):
            raise ValueError('TELEGRAM_FOUNDER_CHAT_ID — числовой id чата (например 123456789 или '
                             '-1001234567890) или @канал.')
    # Railway сам задаёт RAILWAY_PUBLIC_DOMAIN; свой домен — DASHBOARD_PUBLIC_URL.
    public = environ.get('DASHBOARD_PUBLIC_URL', '').strip().rstrip('/')
    if not public and environ.get('RAILWAY_PUBLIC_DOMAIN', '').strip():
        public = 'https://' + environ['RAILWAY_PUBLIC_DOMAIN'].strip().strip('/')
    if public:
        parsed = urlsplit(public)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('DASHBOARD_PUBLIC_URL — адрес панели без пути, например https://dashboard.example.uz.')
    return ChefAlertConfig(week_limit=limit, repeat_step=step, token=token, chat_id=chat_id,
                           public_url=public)


def parse_push_config(environ, *, public_url: str = '') -> PushConfig:
    private_key = environ.get('VAPID_PRIVATE_KEY', '').strip()
    if private_key:
        from retro.web_push import VapidKeys
        VapidKeys.from_private(private_key)  # ошибка формата — сразу при старте
    subject = environ.get('VAPID_SUBJECT', '').strip()
    if subject:
        if not re.fullmatch(r'mailto:[^@\s]+@[^@\s]+\.[^@\s]+|https://[^\s/]+(/\S*)?', subject):
            raise ValueError('VAPID_SUBJECT — mailto:адрес или https://адрес (так требует Apple).')
    elif public_url.startswith('https://'):
        subject = public_url
    return PushConfig(private_key=private_key, **({'subject': subject} if subject else {}))


def parse_hikvision_config(environ) -> HikvisionConfig | None:
    url = environ.get('HIKVISION_URL', '').strip().rstrip('/')
    username = environ.get('HIKVISION_USER', '').strip()
    password = environ.get('HIKVISION_PASSWORD', '')
    present = (bool(url), bool(username), bool(password))
    if not any(present):
        return None
    if not all(present):
        raise ValueError('HIKVISION_URL, HIKVISION_USER и HIKVISION_PASSWORD задаются вместе.')
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('HIKVISION_URL должен быть корневым HTTP(S) URL без credentials/query.') from None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or port == 0):
        raise ValueError('HIKVISION_URL должен быть корневым HTTP(S) URL без credentials/query.')
    source = environ.get('HIKVISION_SOURCE', 'retro-main-entry').strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', source):
        raise ValueError('HIKVISION_SOURCE должен быть безопасным именем длиной до 64 символов.')
    poll = _bounded_int(environ.get('HIKVISION_POLL_SECONDS', '30'),
                        'HIKVISION_POLL_SECONDS', 10, 3600)
    timeout = _bounded_int(environ.get('HIKVISION_TIMEOUT_SECONDS', '8'),
                           'HIKVISION_TIMEOUT_SECONDS', 1, 60)
    verify_raw = environ.get('HIKVISION_VERIFY_TLS', 'true').strip().casefold()
    if verify_raw not in {'true', 'false', '1', '0', 'yes', 'no'}:
        raise ValueError('HIKVISION_VERIFY_TLS должен быть true или false.')
    return HikvisionConfig(
        base_url=url, username=username, password=password, source=source,
        poll_seconds=poll, timeout_seconds=timeout,
        verify_tls=verify_raw in {'true', '1', 'yes'})


def parse_sms_config(environ) -> SmsConfig | None:
    """SMS_PROVIDER: пусто — входа по номеру нет; eskiz — боевой; console — стенд.

    Консоль пишет код в лог сервера, поэтому на хостинге она запрещена: там лог
    читают не только те, кому положен вход."""
    provider = environ.get('SMS_PROVIDER', '').strip().casefold()
    if not provider:
        return None
    if provider not in SMS_PROVIDERS:
        raise ValueError('SMS_PROVIDER должен быть eskiz или console.')
    template = environ.get('SMS_TEMPLATE', '').strip() or SMS_TEMPLATE
    if (template.count('{code}') != 1 or template.replace('{code}', '').count('{')
            or template.replace('{code}', '').count('}') or len(template) > 300):
        raise ValueError('SMS_TEMPLATE должен содержать {code} ровно один раз и быть не длиннее 300 знаков.')
    timeout = _bounded_int(environ.get('SMS_TIMEOUT_SECONDS', '10'), 'SMS_TIMEOUT_SECONDS', 1, 60)
    if provider == 'console':
        if hosting_without_disk(environ):
            raise ValueError('SMS_PROVIDER=console — только для стенда: на хостинге коды попали бы в лог.')
        return SmsConfig(provider='console', template=template, timeout_seconds=timeout)
    email = environ.get('ESKIZ_EMAIL', '').strip()
    password = environ.get('ESKIZ_PASSWORD', '')
    if not email or not password:
        raise ValueError('Для SMS_PROVIDER=eskiz задайте ESKIZ_EMAIL и ESKIZ_PASSWORD.')
    sender = environ.get('ESKIZ_FROM', '').strip() or '4546'
    if not re.fullmatch(r'[A-Za-z0-9_ -]{1,11}', sender):
        raise ValueError('ESKIZ_FROM — имя отправителя Eskiz: до 11 латинских букв и цифр.')
    return SmsConfig(provider='eskiz', email=email, password=password, sender=sender,
                     template=template, timeout_seconds=timeout)


def _bounded_int(value: str, name: str, minimum: int, maximum: int) -> int:
    raw = value.strip()
    if not raw.isdigit() or not minimum <= int(raw) <= maximum:
        raise ValueError(f'{name} должен быть числом от {minimum} до {maximum}.')
    return int(raw)


def parse_director_categories(value: str) -> dict[str, str]:
    allowed = {'menu', 'dessert', 'drink'}
    if not value.strip():
        return {}
    result = {}
    for pair in value.split(';'):
        if pair.count('=') != 1:
            raise ValueError('IIKO_DIRECTOR_CATEGORIES должен содержать пары «категория=тип».')
        name, kind = (part.strip() for part in pair.split('='))
        if not name or kind not in allowed or name in result:
            raise ValueError('IIKO_DIRECTOR_CATEGORIES содержит некорректную категорию.')
        result[name] = kind
    return result



def hosting_without_disk(environ=None) -> bool:
    """Похоже ли, что мы на хостинге, где файловая система не переживает выкат.

    Railway проставляет свои переменные в каждом окружении. Локальная машина их
    не имеет, поэтому разработка и тесты продолжают работать на файлах.
    """
    values = os.environ if environ is None else environ
    return any(key.startswith('RAILWAY_') for key in values)


def require_durable_storage(database_url: str, environ=None) -> None:
    """Не дать подняться на хостинге без места, где данные переживут выкат.

    Молчаливый откат на файлы внутри контейнера уже приводил к потере реестра
    и начислений: выкат выглядел успешным, а данные исчезали. Упавший выкат
    видно сразу и он исправим, потерянные деньги — нет. Поэтому здесь ошибка,
    а не предупреждение.
    """
    values = os.environ if environ is None else environ
    if database_url or not hosting_without_disk(values):
        return
    if values.get('RETRO_DATA_DIR', '').strip():
        # Каталог задали явно — значит подключили диск и отвечают за это сами.
        return
    raise ValueError(
        'Негде хранить данные: не задан ни DATABASE_URL, ни RETRO_DATA_DIR. '
        'На хостинге файлы внутри контейнера исчезают при каждом выкате вместе '
        'с реестром сотрудников, начислениями и закупом. Подключите Postgres '
        '(тогда DATABASE_URL появится сам) либо диск и укажите RETRO_DATA_DIR.')


def parse_dashboard_panel_users(value: str) -> dict[str, tuple[str, str]]:
    if not value.strip():
        return {}
    required_roles = {'cashier', 'accountant', 'director', 'founder'}
    # `shokh` и `manager` — необязательные роли: закуп и кабинет менеджера
    # подключают отдельно, и уже настроенные развёртывания не должны падать
    # из-за их отсутствия. Менеджеров может быть несколько.
    allowed_roles = required_roles | {'admin', 'shokh', 'manager'}
    result = {}
    roles = set()
    for raw_entry in value.split(';'):
        parts = [part.strip() for part in raw_entry.split(':')]
        if len(parts) != 3:
            raise ValueError('DASHBOARD_PANEL_USERS должен содержать пары user:password:role.')
        username, password, role = parts
        if (not username or not password or role not in allowed_roles or username in result
                or len(username) > 64 or len(password) > 256):
            raise ValueError('DASHBOARD_PANEL_USERS содержит некорректную учётную запись.')
        result[username] = (password, role)
        roles.add(role)
    if not required_roles.issubset(roles):
        raise ValueError('DASHBOARD_PANEL_USERS должен задавать по одному пользователю для каждой панели.')
    return result


def parse_director_price_tiers(value: str) -> tuple[int, ...]:
    """«30000,45000» → (30000, 45000). Пусто — пороги по умолчанию.

    Пороги — целые суммы в сум по возрастанию, от одного до пяти: больше
    шести чипов в ряд на телефоне не помещаются. Пробелы внутри числа
    («30 000») допускаются — так сумму обычно и пишут."""
    raw = value.strip()
    if not raw:
        return DEFAULT_PRICE_TIERS
    message = 'DIRECTOR_PRICE_TIERS — пороги цен в сум через запятую по возрастанию, например 30000,45000.'
    parts = [re.sub(r'[\s\u00a0\u202f]', '', part) for part in raw.split(',')]
    if not 1 <= len(parts) <= 5 or not all(part.isdigit() for part in parts):
        raise ValueError(message)
    tiers = tuple(int(part) for part in parts)
    if tiers[0] <= 0 or any(low >= high for low, high in zip(tiers, tiers[1:])):
        raise ValueError(message)
    return tiers


def parse_director_excluded_groups(value: str) -> frozenset[str]:
    groups = [item.strip() for item in value.split(';') if item.strip()]
    if len(groups) != len(set(groups)):
        raise ValueError('IIKO_DIRECTOR_EXCLUDED_GROUPS содержит повторяющуюся группу.')
    return frozenset(groups)
