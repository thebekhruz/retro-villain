"""Сборка данных кабинета учредителя из iiko, бухгалтерии и закупа.

Каждый блок экрана грузится своим запросом: если iiko долго думает, деньги
бухгалтера всё равно видны, а блок iiko честно говорит, что данных нет, —
вместо нулей, которые выглядели бы фактом.
"""

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException

from retro.logging_config import log_safe_failure
from retro.modules.cashier.expenses import cash_to_finance
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.shokh.store import pocket_position
from retro.report_cache import load_iiko

from . import overview
from .overview import money


def month_bounds(day: date):
    first = day.replace(day=1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first, last


async def iiko_or_error(request, method, *args, operation, timeout=90, **kwargs):
    """Ответ iiko либо текст ошибки для экрана. Ошибку пишем в журнал без тела ответа."""
    try:
        return await load_iiko(request.app.state, method, *args, request=request,
                               timeout=timeout, **kwargs), None
    except TimeoutError as error:
        log_safe_failure('founder-cabinet', error, operation=operation,
                         request_id=request.state.request_id)
        return None, 'iiko отвечает слишком долго. Повторите позже.'
    except DataError as error:
        log_safe_failure('founder-cabinet', error, operation=operation,
                         request_id=request.state.request_id)
        return None, str(error)


def dividend_summary(state, day: date) -> dict:
    """Недельная цель, что уже отложено и сколько касса в среднем свободно даёт."""
    monday, _, week = overview.week_of(day)
    target = state.dividend_targets.get(week)
    first = min(monday, day - timedelta(days=7))
    flows = overview.daily_flows(state.accountant_finance.cash_flows_between(first, day))
    free = overview.free_cash_per_week(flows, [day - timedelta(days=offset) for offset in range(1, 8)])
    result = overview.dividend_week(day, Decimal(target['amount']) if target else None, flows,
                                    free_cash=free)
    result['target_source'] = target
    return result


async def cashier_day(request, day: date):
    """Касса дня глазами кассира: выручка по заведениям, «Демо», расчёт передачи."""
    snapshot, error = await iiko_or_error(request, 'load', day, operation='cashier_day')
    if snapshot is None:
        return None, error
    state = request.app.state
    expenses, receipts = await asyncio.gather(
        asyncio.to_thread(state.expenses.list, day), asyncio.to_thread(state.expenses.list_receipts, day))
    expense_total = sum((item.amount for item in expenses), Decimal(0))
    receipt_total = sum((item.amount for item in receipts), Decimal(0))
    demo = next((payment.amount for payment in snapshot.payments if payment.name == 'Демо'), Decimal(0))
    breakdown = snapshot.revenue_breakdown
    return dict(
        retro=money(breakdown.retro) if breakdown else None,
        school=money(breakdown.school) if breakdown else None,
        banquet=money(breakdown.bekhruz_banquet) if breakdown else None,
        register_revenue=money(snapshot.revenue), retro_checks=snapshot.receipt_count, demo=money(demo),
        expected_handover=money(cash_to_finance(snapshot, expense_total, receipt_total)),
        fetched_at=snapshot.fetched_at.isoformat()), None


async def founder_day(request, day: date, *, orders=None, flows=None):
    """Один день: касса iiko, деньги бухгалтера и сверка передачи между ними."""
    from retro.modules.accountant.routes import day_view

    state = request.app.state
    accounting, (cashier, cashier_error) = await asyncio.gather(
        day_view(request, day), cashier_day(request, day))
    if flows is None:
        flows = overview.daily_flows(
            await asyncio.to_thread(state.accountant_finance.cash_flows_between, day, day))
    values = flows.get(day.isoformat(), {})
    recorded = await asyncio.to_thread(state.accountant_finance.handover_for_day, day)
    expected = cashier['expected_handover'] if cashier else None
    check = overview.handover_check(recorded, expected)
    if day == today_tashkent() and check['status'] == 'missing':
        # Смена ещё идёт: не переданная касса сегодня — не недостача.
        check = dict(status='pending', difference=None)
    ledger = accounting['ledger']
    day_orders = (orders or {}).get(day.isoformat(), {})
    outlook = None
    if day == today_tashkent():
        # «Уйдёт сегодня»: зарплату видно по долгу начислений, а закуп и прочие
        # расходы ещё не внесены — берём их средним за прошлую неделю.
        recent = overview.daily_flows(await asyncio.to_thread(
            state.accountant_finance.cash_flows_between, day - timedelta(days=7), day - timedelta(days=1)))
        average = lambda key: sum((values.get(key, Decimal(0)) for values in recent.values()),
                                  Decimal(0)) / 7
        outlook = dict(salary_due=ledger.get('salary_debt'), procurement=money(average('procurement')),
                       other=money(average('other')), estimate=True)
    return dict(
        date=day.isoformat(), weekday=day.weekday(), today=day == today_tashkent(),
        cashier=cashier, cashier_error=cashier_error,
        orders={key: value['orders'] for key, value in day_orders.items()} if orders is not None else None,
        handover=dict(recorded=money(recorded) if recorded is not None else None,
                      expected=expected, **check),
        flows={key: money(values.get(key, Decimal(0)))
               for key in ('salary', 'procurement', 'other', 'dividends', 'receipt')},
        opening_balance=ledger['cash_flow'].get('opening_balance'),
        outlook=outlook,
        closing_balance=ledger['cash_balance'],
        accounting=accounting)


async def founder_week(request, day: date):
    monday, sunday, label = overview.week_of(day)
    today = today_tashkent()
    last = min(sunday, today)
    state = request.app.state
    rows, (orders_rows, orders_error) = await asyncio.gather(
        asyncio.to_thread(state.accountant_finance.cash_flows_between, monday, last),
        iiko_or_error(request, 'load_daily_orders', monday, last, operation='week_orders'))
    flows = overview.daily_flows(rows)
    orders = overview.register_days(orders_rows) if orders_rows is not None else None
    days = [monday + timedelta(days=offset) for offset in range(7)]
    built = await asyncio.gather(*(founder_day(request, d, orders=orders, flows=flows)
                                   for d in days if d <= last))
    by_date = {item['date']: item for item in built}
    return dict(week=label, start=monday.isoformat(), end=sunday.isoformat(), today=today.isoformat(),
                orders_error=orders_error,
                days=[by_date.get(d.isoformat(), dict(date=d.isoformat(), weekday=d.weekday(),
                                                      future=True, today=False))
                      for d in days])


async def founder_forecast(request, day: date):
    """Прогноз по дням недели по средним за восемь недель — это оценка, не факт."""
    start = day - timedelta(days=overview.FORECAST_WEEKS * 7)
    rows, error = await iiko_or_error(request, 'load_daily_orders', start, day,
                                      operation='forecast', timeout=120)
    if rows is None:
        return dict(estimate=True, error=error, weekdays=None, month=None, today=None)
    history = overview.register_days(rows)
    weekdays = overview.weekday_forecast(history, day)
    today_values = history.get(day.isoformat(), {})
    return dict(
        estimate=True, error=None, weeks=overview.FORECAST_WEEKS, weekdays=weekdays,
        month=overview.month_forecast(history, weekdays, day),
        today=dict(forecast=weekdays[day.weekday()],
                   orders={key: value['orders'] for key, value in today_values.items()}))


async def founder_chef(request, day: date):
    monday, sunday, _ = overview.week_of(day)
    first, _ = month_bounds(day)
    start = min(first, monday)
    rows, error = await iiko_or_error(request, 'load_chef_bills', start, day, operation='chef_bills')
    if rows is None:
        return dict(error=error)
    return dict(error=None, date=day.isoformat(), month=first.isoformat()[:7],
                **overview.chef_bills(rows, week_start=monday, week_end=sunday, month_start=first))


def founder_spending(state, day: date):
    first, _ = month_bounds(day)
    flows = state.accountant_finance.cash_flows_between(first, day)
    purchases = state.shokh.purchases_between(first, day)
    # «На руках у Шоха» считает store.pocket_position — та же формула, что на
    # экране закупа; своей копии здесь быть не должно.
    position = pocket_position(state.shokh, state.accountant_finance, day)
    pocket = None if position['pocket'] is None else money(position['pocket'])
    return dict(month=first.isoformat()[:7], through=day.isoformat(),
                expenses=overview.expense_categories(flows),
                shokh=overview.shokh_month(purchases, flows, pocket=pocket))


def selected_day(value: date | None) -> date:
    day = value or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    return day
