from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from retro.modules.cashier.service import (
    BANQUET_SECTION, PAYMENT_ALIASES, RETRO_REGISTER, SCHOOL_REGISTER, DataError,
)


@dataclass(frozen=True)
class SalesRow:
    day: date
    register: str
    section: str
    payment_type: str
    item: str
    category: str
    quantity: Decimal
    revenue: Decimal
    cost: Decimal
    waiter: str
    order_id: str


@dataclass(frozen=True)
class ItemMetric:
    quantity: Decimal
    revenue: Decimal
    cost: Decimal

    @property
    def gross_profit(self):
        return self.revenue - self.cost

    @property
    def margin_percent(self):
        if not self.revenue:
            return None
        return (self.gross_profit / self.revenue * 100).quantize(Decimal('.01'), ROUND_HALF_UP)


# Период берём только из закрытых дней: сегодняшняя смена ещё идёт, и её
# цифры меняются под руками. Верхняя граница защищает iiko от запроса,
# который он всё равно не успеет собрать.
DEFAULT_PERIOD_DAYS = 10
MAX_PERIOD_DAYS = 62


@dataclass(frozen=True)
class DayTotal:
    """Одна строка таблицы «по дням»: выручка дня в разрезах и по оплатам."""

    day: date
    revenue: Decimal
    cost: Decimal
    directions: dict[str, Decimal]
    payments: dict[str, Decimal]

    @property
    def gross_profit(self):
        return self.revenue - self.cost

    def json(self):
        return dict(day=self.day.isoformat(), revenue=str(self.revenue), cost=str(self.cost),
                    gross_profit=str(self.gross_profit),
                    directions={name: str(value) for name, value in self.directions.items()},
                    payments={name: str(value) for name, value in self.payments.items()})


@dataclass(frozen=True)
class DirectorSnapshot:
    period_start: date
    period_end: date
    cash_total: Decimal
    yandex_revenue: Decimal
    item_metrics: dict[str, dict[str, ItemMetric]]
    waiter_metrics: dict[str, ItemMetric]
    payment_totals: dict[str, Decimal] = field(default_factory=dict)
    daily_totals: tuple = ()
    missing_days: tuple = ()

    def json(self):
        def metric(value):
            return dict(quantity=str(value.quantity), revenue=str(value.revenue), cost=str(value.cost),
                        gross_profit=str(value.gross_profit), margin_percent=str(value.margin_percent)
                        if value.margin_percent is not None else None)
        return dict(period_start=self.period_start.isoformat(), period_end=self.period_end.isoformat(),
                    period_days=(self.period_end - self.period_start).days + 1,
                    cash_total=str(self.cash_total), yandex_revenue=str(self.yandex_revenue),
                    item_metrics={group: {name: metric(value) for name, value in values.items()}
                                  for group, values in self.item_metrics.items()},
                    waiter_metrics={name: metric(value) for name, value in self.waiter_metrics.items()},
                    payment_totals={name: str(value) for name, value in self.payment_totals.items()},
                    daily_totals=[value.json() for value in self.daily_totals],
                    missing_days=[value.isoformat() for value in self.missing_days])


def completed_period(today: date, days: int = DEFAULT_PERIOD_DAYS):
    if days < 1:
        raise DataError('Период не может быть короче одного дня.')
    end = today - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def resolve_period(today: date, start: date | None = None, end: date | None = None,
                   days: int | None = None):
    """Проверенный диапазон закрытых дней: либо явные даты, либо последние N."""
    if start is None and end is None:
        return completed_period(today, days or DEFAULT_PERIOD_DAYS)
    if start is None or end is None:
        raise DataError('Укажите и начало, и конец периода.')
    if start > end:
        raise DataError('Начало периода позже его конца.')
    if end >= today:
        raise DataError('Сегодняшний день ещё не закрыт: выберите период по вчерашний день.')
    if (end - start).days + 1 > MAX_PERIOD_DAYS:
        raise DataError(f'Период длиннее {MAX_PERIOD_DAYS} дней iiko не отдаёт. Выберите короче.')
    return start, end


def direction(row):
    if isinstance(row.item, str) and 'бехруз' in row.item.casefold():
        return 'banquet'
    if row.register == SCHOOL_REGISTER:
        return 'oxbridge'
    if row.register == RETRO_REGISTER and row.section == BANQUET_SECTION:
        return None
    if row.register == RETRO_REGISTER and 'бехруз' not in row.section.casefold():
        return 'retro'
    raise DataError('В iiko появилась неизвестная касса или отделение.')


def build_snapshot(rows, categories, period_start, period_end, *, excluded_groups=frozenset()):
    span = (period_end - period_start).days + 1
    expected_days = {period_start + timedelta(days=index) for index in range(span)}
    values = list(rows)
    days = {row.day for row in values}
    if days - expected_days:
        raise DataError('iiko вернул продажи за дни вне запрошенного периода.')
    if not days:
        raise DataError('iiko не вернул ни одной продажи за выбранный период.')
    metrics = {name: defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
               for name in ('all', 'retro', 'oxbridge', 'banquet', 'yandex')}
    cash_total = Decimal(0)
    yandex_total = Decimal(0)
    payment_totals = defaultdict(Decimal)
    # Дни держим в словаре, а не в списке: iiko отдаёт строки вперемешку,
    # а таблица «по дням» читается только в календарном порядке.
    daily = {}
    waiters = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
    for row in values:
        if row.category in excluded_groups:
            continue
        if categories and row.category not in categories:
            raise DataError(f'Для категории iiko «{row.category}» не настроен тип отчёта.')
        if not isinstance(row.item, str) or not row.item.strip():
            raise DataError('iiko не указал название блюда.')
        if not row.waiter.strip():
            raise DataError('iiko не указал официанта для позиции.')
        if min(row.quantity, row.revenue, row.cost) < 0:
            raise DataError('iiko вернул отрицательное значение позиции.')
        group = direction(row)
        if group is None:
            continue
        names = ['all', group]
        payment = PAYMENT_ALIASES.get(row.payment_type, row.payment_type)
        if payment == 'Яндекс Еда':
            names.append('yandex')
            yandex_total += row.revenue
        cash_total += row.revenue
        payment_totals[payment] += row.revenue
        day_row = daily.setdefault(row.day, dict(revenue=Decimal(0), cost=Decimal(0),
                                                 directions=defaultdict(Decimal),
                                                 payments=defaultdict(Decimal)))
        day_row['revenue'] += row.revenue
        day_row['cost'] += row.cost
        day_row['payments'][payment] += row.revenue
        for name in names[1:]:
            day_row['directions'][name] += row.revenue
        waiter = waiters[row.waiter]
        waiter[0] += row.quantity
        waiter[1] += row.revenue
        waiter[2] += row.cost
        for name in names:
            bucket = metrics[name][row.item]
            bucket[0] += row.quantity
            bucket[1] += row.revenue
            bucket[2] += row.cost
    daily_totals = tuple(DayTotal(day, daily[day]['revenue'], daily[day]['cost'],
                                  dict(daily[day]['directions']), dict(daily[day]['payments']))
                         for day in sorted(daily))
    missing_days = tuple(sorted(expected_days - days))
    return DirectorSnapshot(period_start, period_end, cash_total, yandex_total,
                            {group: {item: ItemMetric(*amounts) for item, amounts in items.items()}
                             for group, items in metrics.items()},
                            {name: ItemMetric(*amounts) for name, amounts in waiters.items()},
                            dict(payment_totals), daily_totals, missing_days)
