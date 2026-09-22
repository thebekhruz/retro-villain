import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from time import monotonic
from typing import Awaitable, Callable, TypeVar
from uuid import uuid4
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Tashkent')
PAYMENT_SOURCES = ('Демо', 'UzCard', 'Наличные (Инкасса QR)', 'Xumo',
                   'Я Rahmat', 'Яндекс Еда', 'Click/Payme Безналичный перевод',
                   'Единый QR', 'Uzum')
RETRO_REGISTER = 'Kassa-FiscalBox1'
SCHOOL_REGISTER = 'GL-Kassa-Oksbrich'
BANQUET_SECTION = 'Бехруз (Свадьба)'
# iiko's existing spelling differs from the reference supplied by the user.
PAYMENT_ALIASES = {'Яндех Еда': 'Яндекс Еда'}


class DataError(Exception):
    """Safe user-facing validation error, never an upstream response body."""


class ReportReplaced(Exception):
    """The in-flight daily report was intentionally superseded by a newer request."""


T = TypeVar('T')


class LatestReportRunner:
    """Run one daily report at a time, cancelling it when a newer request arrives."""

    def __init__(self):
        self._replace_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._replace_lock:
            previous = self._task
            if previous is not None and not previous.done():
                previous.cancel()
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            task = asyncio.create_task(operation())
            self._task = task
        try:
            # The runner owns the operation lifecycle. A disconnected HTTP client
            # must not make the task untrackable before the next request replaces it.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise ReportReplaced from None
            raise
        finally:
            if task.done():
                async with self._replace_lock:
                    if self._task is task:
                        self._task = None


def today_tashkent(now=None):
    return (now or datetime.now(TZ)).astimezone(TZ).date()


@dataclass(frozen=True)
class Payment:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class RevenueBreakdown:
    retro: Decimal
    school: Decimal
    bekhruz_banquet: Decimal

    @property
    def total(self):
        return self.retro + self.school + self.bekhruz_banquet

    def json(self):
        return {name: str(getattr(self, name)) for name in
                ('retro', 'school', 'bekhruz_banquet', 'total')}


@dataclass(frozen=True)
class Snapshot:
    id: str
    day: date
    revenue: Decimal
    receipt_count: int
    payments: tuple[Payment, ...]
    fetched_at: datetime
    demo: bool = False
    revenue_breakdown: RevenueBreakdown | None = None
    cash_prepayment: Decimal = Decimal(0)
    new_prepayment: Decimal = Decimal(0)

    @property
    def average_receipt(self):
        if self.receipt_count == 0:
            return None
        return (self.revenue / self.receipt_count).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)

    def json(self):
        result = dict(snapshot_id=self.id, date=self.day.isoformat(), revenue=str(self.revenue),
                    receipt_count=self.receipt_count,
                    average_receipt=str(self.average_receipt) if self.average_receipt is not None else None,
                    payments=[dict(name=p.name, amount=str(p.amount)) for p in self.payments],
                    fetched_at=self.fetched_at.isoformat(), demo=self.demo, currency='UZS',
                    payment_total=str(sum((p.amount for p in self.payments), Decimal(0))),
                    cash_prepayment=str(self.cash_prepayment),
                    new_prepayment=str(self.new_prepayment))
        if self.revenue_breakdown is not None:
            result['revenue_breakdown'] = self.revenue_breakdown.json()
        return result


def cell(row, index):
    try:
        return row[f'field{index}']['value']
    except (KeyError, TypeError):
        raise DataError('iiko вернул неполный отчёт. Повторите обновление.') from None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise DataError('iiko вернул некорректную сумму или количество.')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise DataError('iiko вернул некорректное число.') from None
    if not result.is_finite():
        raise DataError('iiko вернул некорректное число.')
    return result


def build_revenue_breakdown(rows):
    if not isinstance(rows, list):
        raise DataError('iiko вернул некорректное разделение выручки.')
    amounts = dict(retro=Decimal(0), school=Decimal(0), bekhruz_banquet=Decimal(0))
    for register_row in rows:
        if not isinstance(register_row, dict):
            raise DataError('iiko вернул некорректную строку разделения выручки.')
        register = cell(register_row, 0)
        if register not in (RETRO_REGISTER, SCHOOL_REGISTER):
            raise DataError('В iiko появилась неизвестная касса. Разделение выручки требует проверки.')
        children = register_row.get('children')
        if not isinstance(children, list) or not children:
            raise DataError('iiko не вернул отделения для кассы.')
        subtotal = Decimal(0)
        for section_row in children:
            section = cell(section_row, 1)
            if not isinstance(section, str) or not section.strip():
                raise DataError('iiko не указал отделение для продажи.')
            amount = number(cell(section_row, 2))
            subtotal += amount
            if register == SCHOOL_REGISTER:
                amounts['school'] += amount
            elif section == BANQUET_SECTION:
                amounts['bekhruz_banquet'] += amount
            elif isinstance(section, str) and 'бехруз' in section.casefold():
                raise DataError('В iiko найдено новое отделение Бехруз. Проверьте распределение выручки.')
            else:
                amounts['retro'] += amount
        if abs(number(cell(register_row, 2)) - subtotal) > Decimal(1):
            raise DataError('Суммы по отделениям iiko не совпали с итогом кассы.')
    return RevenueBreakdown(**amounts)


def build_snapshot(day, total_rows, payment_rows, *, demo=False, revenue_breakdown=None):
    if not isinstance(total_rows, list) or not isinstance(payment_rows, list):
        raise DataError('Некорректная структура отчёта iiko.')
    if len(total_rows) > 1:
        raise DataError('Вместо итога за один день iiko вернул несколько строк.')
    revenue, count = Decimal(0), Decimal(0)
    if total_rows:
        total = total_rows[0]
        if cell(total, 0) != day.isoformat():
            raise DataError('Дата ответа iiko не совпадает с выбранным днём.')
        count, revenue = number(cell(total, 1)), number(cell(total, 2))
        if count < 0 or count != count.to_integral_value():
            raise DataError('iiko вернул некорректное количество чеков.')
    payments = []
    names = set()
    for row in payment_rows:
        name = cell(row, 0)
        if not isinstance(name, str) or not name.strip() or name in names:
            raise DataError('iiko вернул пустое или повторяющееся название оплаты.')
        names.add(name)
        payments.append(Payment(name, number(cell(row, 1))))
    if bool(total_rows) != bool(payment_rows):
        raise DataError('iiko вернул только часть отчёта. Повторите обновление.')
    if abs(sum((p.amount for p in payments), Decimal(0)) - revenue) > Decimal(1):
        raise DataError('Продажи и суммы по оплатам не совпали. Обновите отчёт ещё раз.')
    if count == 0 and revenue != 0:
        raise DataError('Продажи есть, но iiko не вернул количество чеков.')
    amounts = {name: Decimal(0) for name in PAYMENT_SOURCES}
    for payment in payments:
        name = PAYMENT_ALIASES.get(payment.name, payment.name)
        if name not in amounts:
            if payment.name == '(без оплаты)' and payment.amount == 0:
                continue
            raise DataError('В iiko появился новый тип оплаты. '
                            'Нужно проверить справочник; сумма не будет скрыта или перераспределена.')
        amounts[name] += payment.amount
    normalized = tuple(Payment(name, amounts[name]) for name in PAYMENT_SOURCES)
    return Snapshot(uuid4().hex, day, revenue, int(count), normalized, datetime.now(TZ),
                    demo, revenue_breakdown)


class SnapshotCache:
    """Bounded, in-memory only. An export never silently reloads different totals."""
    def __init__(self, ttl=900, limit=64):
        self.ttl, self.limit = ttl, limit
        self.entries = OrderedDict()

    def put(self, snapshot):
        self.entries[snapshot.id] = (monotonic(), snapshot)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)

    def get(self, snapshot_id, day):
        entry = self.entries.get(snapshot_id)
        if entry is None:
            raise DataError('Обновите данные перед скачиванием отчёта.')
        created, snapshot = entry
        if monotonic() - created > self.ttl or snapshot.day != day:
            raise DataError('Снимок устарел или относится к другой дате. Обновите данные.')
        return snapshot

    def latest_for_day(self, day):
        for created, snapshot in reversed(self.entries.values()):
            if snapshot.day == day and not snapshot.demo and monotonic() - created <= self.ttl:
                return snapshot
        return None


def demo_snapshot(day):
    def row(*values):
        return {f'field{i}': {'value': value} for i, value in enumerate(values)}
    return build_snapshot(day, [row(day.isoformat(), 126, 18450000)],
                          [row('Наличные (Инкасса QR)', 7200000), row('UzCard', 5400000),
                           row('Xumo', 3200000), row('Click/Payme Безналичный перевод', 1650000),
                           row('Яндекс Еда', 1000000)], demo=True)
