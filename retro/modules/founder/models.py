from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from retro.modules.cashier.service import (
    BANQUET_SECTION,
    PAYMENT_ALIASES,
    PAYMENT_SOURCES,
    RETRO_REGISTER,
    SCHOOL_REGISTER,
    TZ,
    DataError,
)


DIRECTIONS = ('retro', 'school', 'banquet')
GRANULARITIES = ('day', 'week', 'month')
RETRO_SECTIONS = frozenset({
    '2.1 Бар мили', 'Бар Рестаран', 'Вынос', 'Доставка', 'Летка',
    'Напитки Мили', 'Напитки Ресторан', 'Ресторан', 'Ресторан Алкоголь',
})
SCHOOL_SECTIONS = frozenset({'Зал'})


@dataclass(frozen=True)
class RevenueRow:
    day: date
    register: str
    section: str
    item: str
    amount: Decimal
    cost: Decimal | None = None


@dataclass(frozen=True)
class PaymentRow:
    day: date
    register: str
    section: str
    item: str
    payment: str
    amount: Decimal


def is_banquet_item(item):
    return isinstance(item, str) and 'бехруз' in item.casefold()


def classify_direction(register, section, item):
    if register not in (RETRO_REGISTER, SCHOOL_REGISTER):
        raise DataError('В iiko появилась неизвестная касса. Разделение выручки требует проверки.')
    if not isinstance(section, str) or not section.strip():
        raise DataError('iiko не указал отделение для продажи.')
    if not isinstance(item, str) or not item.strip():
        raise DataError('iiko не указал название блюда для разделения выручки.')
    if is_banquet_item(item):
        return 'banquet'
    if register == SCHOOL_REGISTER:
        if section in SCHOOL_SECTIONS:
            return 'school'
        raise DataError('В iiko появилось неизвестное отделение школы. '
                        'Разделение выручки требует проверки.')
    if section == BANQUET_SECTION:
        return None
    if 'бехруз' in section.casefold():
        raise DataError('В iiko найдено новое отделение Бехруз. Проверьте распределение выручки.')
    if section in RETRO_SECTIONS:
        return 'retro'
    raise DataError('В iiko появилось неизвестное отделение Retro. '
                    'Разделение выручки требует проверки.')


def _next_month(day):
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def _periods(start, end, granularity):
    cursor = start
    while cursor <= end:
        if granularity == 'day':
            natural_end = cursor
        elif granularity == 'week':
            natural_end = cursor + timedelta(days=6 - cursor.weekday())
        else:
            natural_end = _next_month(cursor) - timedelta(days=1)
        period_end = min(end, natural_end)
        yield cursor, period_end
        cursor = period_end + timedelta(days=1)


def _period_start(day, start, granularity):
    if granularity == 'day':
        natural = day
    elif granularity == 'week':
        natural = day - timedelta(days=day.weekday())
    else:
        natural = day.replace(day=1)
    return max(start, natural)


def _amount(value):
    return str(value)


def _validate_inputs(start, end, granularity, directions):
    if start > end:
        raise DataError('Дата начала периода должна быть не позже даты конца.')
    if granularity not in GRANULARITIES:
        raise DataError('Неизвестная детализация аналитики.')
    if not directions or len(set(directions)) != len(directions):
        raise DataError('Выберите хотя бы одно направление без повторов.')
    if any(direction not in DIRECTIONS for direction in directions):
        raise DataError('Неизвестное направление аналитики.')


def build_analytics(revenue_rows, payment_rows, start, end, granularity, directions, *, now=None):
    directions = tuple(directions)
    _validate_inputs(start, end, granularity, directions)
    now = now or datetime.now(TZ)
    today = now.date()
    periods = list(_periods(start, end, granularity))

    revenue_by_group = defaultdict(lambda: defaultdict(Decimal))
    payment_by_group = defaultdict(lambda: defaultdict(lambda: defaultdict(Decimal)))
    totals = {direction: Decimal(0) for direction in DIRECTIONS}
    costs = {direction: Decimal(0) for direction in DIRECTIONS}
    missing_cost = set()
    payment_totals = defaultdict(Decimal)
    seen_payments = set()
    unknown_payments = set()
    daily_revenue = defaultdict(Decimal)
    daily_payments = defaultdict(Decimal)

    for row in revenue_rows:
        if not start <= row.day <= end:
            raise DataError('iiko вернул выручку вне выбранного периода.')
        direction = classify_direction(row.register, row.section, row.item)
        if direction is None:
            continue
        group = _period_start(row.day, start, granularity)
        revenue_by_group[group][direction] += row.amount
        totals[direction] += row.amount
        if row.cost is None:
            missing_cost.add(direction)
        else:
            costs[direction] += row.cost
        daily_revenue[row.day, direction] += row.amount

    for row in payment_rows:
        if not start <= row.day <= end:
            raise DataError('iiko вернул оплату вне выбранного периода.')
        direction = classify_direction(row.register, row.section, row.item)
        if direction is None:
            continue
        payment_name = PAYMENT_ALIASES.get(row.payment, row.payment)
        if payment_name not in PAYMENT_SOURCES:
            if row.payment == '(без оплаты)' and row.amount == 0:
                continue
            unknown_payments.add(payment_name)
        group = _period_start(row.day, start, granularity)
        payment_by_group[group][direction][payment_name] += row.amount
        if direction in directions:
            daily_payments[row.day, direction] += row.amount
            payment_totals[payment_name] += row.amount
            seen_payments.add(payment_name)

    selected_total = sum((totals[direction] for direction in directions), Decimal(0))
    payment_total = sum(payment_totals.values(), Decimal(0))
    discrepancy = payment_total - selected_total
    daily_discrepancies = [dict(date=day.isoformat(), direction=direction,
                               amount=str(daily_payments[day, direction] - daily_revenue[day, direction]))
                          for day, direction in sorted(daily_revenue.keys() | daily_payments.keys())
                          if direction in directions and abs(
                              daily_payments[day, direction] - daily_revenue[day, direction]) > Decimal(1)]
    reconciled = abs(discrepancy) <= Decimal(1) and not daily_discrepancies
    warnings = [] if reconciled else [
        f'Оплаты расходятся с выручкой на {abs(discrepancy)} сум. '
        'Данные не считаются сверенными.'
    ]
    if daily_discrepancies:
        warnings.append(f'Расхождения по дням/направлениям: {len(daily_discrepancies)}. '
                        'Встречные расхождения не погашают друг друга при сверке.')
    if unknown_payments:
        warnings.append(
            'Новые типы оплаты iiko показаны отдельно: ' +
            ', '.join(sorted(unknown_payments)) + '.')

    revenue_series = []
    payment_series = []
    for period_start, period_end in periods:
        values = {direction: _amount(revenue_by_group[period_start][direction])
                  for direction in directions}
        incomplete = period_start <= today <= period_end
        base = dict(start=period_start.isoformat(), end=period_end.isoformat(), incomplete=incomplete)
        revenue_series.append({**base, 'values': values,
                               'total': _amount(sum((revenue_by_group[period_start][direction]
                                                    for direction in directions), Decimal(0)))})
        payment_series.append({**base, 'directions': {
            direction: {name: _amount(amount) for name, amount in
                        payment_by_group[period_start][direction].items()}
            for direction in directions
        }})

    summary = []
    for name in sorted(seen_payments, key=lambda item: (-payment_totals[item], item)):
        amount = payment_totals[name]
        share = None if payment_total == 0 else str(
            (amount * Decimal(100) / payment_total).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
        summary.append(dict(name=name, amount=_amount(amount), share_percent=share))

    return {
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'granularity': granularity,
        'directions': list(directions),
        'includes_current_day': start <= today <= end,
        'updated_at': now.isoformat(),
        'source_cache_max_age_seconds': 60,
        'currency': 'UZS',
        'totals': {**{direction: _amount(totals[direction]) for direction in DIRECTIONS},
                   'selected': _amount(selected_total)},
        'cost_totals': {
            **{direction: None if direction in missing_cost else _amount(costs[direction])
               for direction in DIRECTIONS},
            'selected': None if missing_cost.intersection(directions) else _amount(
                sum((costs[direction] for direction in directions), Decimal(0))),
        },
        'revenue_series': revenue_series,
        'payment_summary': summary,
        'payment_series': payment_series,
        'payment_total': _amount(payment_total),
        'discrepancy': _amount(discrepancy),
        'daily_discrepancies': daily_discrepancies,
        'reconciled': reconciled,
        'warnings': warnings,
    }
