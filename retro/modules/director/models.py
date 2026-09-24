from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from retro.modules.cashier.service import (
    BANQUET_SECTION, PAYMENT_ALIASES, RETRO_REGISTER, SCHOOL_REGISTER, DataError,
)
from retro.modules.founder.models import classify_direction


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
    non_cash_payment_type: str = ''


BREAKDOWN_KEYS = ('sales', 'chef', 'tasting', 'other_zero')


def sale_kind(row):
    if row.revenue != 0:
        return 'sales'
    purpose = ' '.join((row.non_cash_payment_type or '').casefold().replace('ё', 'е').split())
    return {'счет шефа': 'chef', 'дегустация': 'tasting'}.get(purpose, 'other_zero')


@dataclass(frozen=True)
class ItemMetric:
    quantity: Decimal
    revenue: Decimal
    cost: Decimal
    breakdown: dict = field(default_factory=dict)

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
    excluded_revenue: dict[str, Decimal] = field(default_factory=dict)
    scope_excluded_revenue: Decimal = Decimal(0)
    yandex_menu_revenue: Decimal = Decimal(0)
    excluded_groups: tuple[str, ...] = ()

    def json(self):
        def money(value):
            return str(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))

        def metric(value):
            revenue = value.revenue.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            cost = value.cost.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            result = dict(quantity=str(value.quantity), revenue=str(revenue), cost=str(cost),
                        gross_profit=str(revenue - cost), margin_percent=str(value.margin_percent)
                        if value.margin_percent is not None else None)
            if value.breakdown:
                result['breakdown'] = {key: metric(part) for key, part in value.breakdown.items()}
            return result
        return dict(period_start=self.period_start.isoformat(), period_end=self.period_end.isoformat(),
                    cash_total=money(self.cash_total), yandex_revenue=money(self.yandex_revenue),
                    calculation_version='2026-09-24', source_cache_max_age_seconds=60, report_cache_max_age_seconds=300,
                    menu_revenue=money(sum((v.revenue for v in self.item_metrics['all'].values()), Decimal(0))),
                    excluded_revenue={key: money(value) for key, value in self.excluded_revenue.items()},
                    excluded_groups=list(self.excluded_groups),
                    scope_excluded_revenue=money(self.scope_excluded_revenue),
                    yandex_menu_revenue=money(self.yandex_menu_revenue),
                    item_metrics={group: {name: metric(value) for name, value in values.items()}
                                  for group, values in self.item_metrics.items()},
                    waiter_metrics={name: metric(value) for name, value in self.waiter_metrics.items()})


def completed_period(today: date):
    end = today - timedelta(days=1)
    return end - timedelta(days=9), end


def direction(row):
    group = classify_direction(row.register, row.section, row.item)
    return 'oxbridge' if group == 'school' else group


def payment_total(rows, payment_name):
    total = Decimal(0)
    for row in rows:
        group = classify_direction(row.register, row.section, row.item)
        if group is None:
            continue
        normalized = PAYMENT_ALIASES.get(row.payment, row.payment)
        if normalized == payment_name:
            total += row.amount
    return total


def build_snapshot(rows, categories, period_start, period_end, *, excluded_groups=frozenset(),
                   yandex_revenue=None):
    if period_end - period_start != timedelta(days=9):
        raise DataError('Период отчёта директора должен составлять десять дней.')
    expected_days = {period_start + timedelta(days=index) for index in range(10)}
    values = list(rows)
    days = {row.day for row in values}
    if not days <= expected_days:
        raise DataError('iiko вернул продажи вне периода отчёта директора.')
    def new_bucket():
        return {kind: [Decimal(0), Decimal(0), Decimal(0)] for kind in BREAKDOWN_KEYS}

    def add(bucket, kind, row):
        values = bucket[kind]
        values[0] += row.quantity
        values[1] += row.revenue
        values[2] += row.cost

    def finish(bucket):
        parts = {kind: ItemMetric(*values) for kind, values in bucket.items()}
        totals = [sum((values[index] for values in bucket.values()), Decimal(0))
                  for index in range(3)]
        return ItemMetric(*totals, breakdown=parts)

    metrics = {name: defaultdict(new_bucket)
               for name in ('all', 'retro', 'oxbridge', 'banquet', 'yandex')}
    cash_total = Decimal(0)
    yandex_total = Decimal(0)
    waiters = defaultdict(new_bucket)
    excluded_revenue = defaultdict(Decimal)
    scope_excluded = Decimal(0)
    for row in values:
        group = direction(row)
        if group is None:
            scope_excluded += row.revenue
            continue
        cash_total += row.revenue
        if row.category in excluded_groups:
            excluded_revenue[row.category] += row.revenue
            continue
        if categories and row.category not in categories:
            raise DataError(f'Для категории iiko «{row.category}» не настроен тип отчёта.')
        if not isinstance(row.item, str) or not row.item.strip():
            raise DataError('iiko не указал название блюда.')
        if not row.waiter.strip():
            raise DataError('iiko не указал официанта для позиции.')
        names = ['all', group]
        payment = PAYMENT_ALIASES.get(row.payment_type, row.payment_type)
        if payment == 'Яндекс Еда':
            names.append('yandex')
            yandex_total += row.revenue
        kind = sale_kind(row)
        add(waiters[row.waiter], kind, row)
        for name in names:
            add(metrics[name][row.item], kind, row)
    return DirectorSnapshot(period_start, period_end, cash_total,
                            yandex_revenue if yandex_revenue is not None else yandex_total,
                            {group: {item: finish(amounts) for item, amounts in items.items()}
                             for group, items in metrics.items()},
                            {name: finish(amounts) for name, amounts in waiters.items()},
                            dict(excluded_revenue), scope_excluded, yandex_total,
                            tuple(sorted(excluded_groups)))
