from collections import defaultdict
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DirectorSnapshot:
    period_start: date
    period_end: date
    cash_total: Decimal
    yandex_revenue: Decimal
    item_metrics: dict[str, dict[str, ItemMetric]]
    waiter_metrics: dict[str, ItemMetric]

    def json(self):
        def metric(value):
            return dict(quantity=str(value.quantity), revenue=str(value.revenue), cost=str(value.cost),
                        gross_profit=str(value.gross_profit), margin_percent=str(value.margin_percent)
                        if value.margin_percent is not None else None)
        return dict(period_start=self.period_start.isoformat(), period_end=self.period_end.isoformat(),
                    cash_total=str(self.cash_total), yandex_revenue=str(self.yandex_revenue),
                    item_metrics={group: {name: metric(value) for name, value in values.items()}
                                  for group, values in self.item_metrics.items()},
                    waiter_metrics={name: metric(value) for name, value in self.waiter_metrics.items()})


def completed_period(today: date):
    end = today - timedelta(days=1)
    return end - timedelta(days=9), end


def direction(row):
    if row.register == SCHOOL_REGISTER:
        return 'oxbridge'
    if row.register == RETRO_REGISTER and row.section == BANQUET_SECTION:
        return None
    if row.register == RETRO_REGISTER and 'бехруз' not in row.section.casefold():
        return 'retro'
    raise DataError('В iiko появилась неизвестная касса или отделение.')


def build_snapshot(rows, categories, period_start, period_end, *, excluded_groups=frozenset()):
    expected_days = {period_start + timedelta(days=index) for index in range(10)}
    values = list(rows)
    days = {row.day for row in values}
    if days != expected_days:
        raise DataError('iiko не вернул все десять дней для отчёта директора.')
    metrics = {name: defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
               for name in ('all', 'retro', 'oxbridge', 'yandex')}
    cash_total = Decimal(0)
    yandex_total = Decimal(0)
    waiters = defaultdict(lambda: [Decimal(0), Decimal(0), Decimal(0)])
    for row in values:
        if row.category in excluded_groups:
            continue
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
        waiter = waiters[row.waiter]
        waiter[0] += row.quantity
        waiter[1] += row.revenue
        waiter[2] += row.cost
        for name in names:
            bucket = metrics[name][row.item]
            bucket[0] += row.quantity
            bucket[1] += row.revenue
            bucket[2] += row.cost
    return DirectorSnapshot(period_start, period_end, cash_total, yandex_total,
                            {group: {item: ItemMetric(*amounts) for item, amounts in items.items()}
                             for group, items in metrics.items()},
                            {name: ItemMetric(*amounts) for name, amounts in waiters.items()})
