"""Сборка данных кабинета учредителя из iiko, бухгалтерии и закупа.

Каждый блок экрана грузится своим запросом: если iiko долго думает, деньги
бухгалтера всё равно видны, а блок iiko честно говорит, что данных нет, —
вместо нулей, которые выглядели бы фактом.
"""

import asyncio
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from fastapi import HTTPException

from retro.modules.accountant.handover_dates import receipt_day
from retro.logging_config import log_safe_failure
from retro.modules.cashier.expenses import cash_to_finance
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.cashier.till import handover_check, shokh_gives, shokh_total, till_totals
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
    result['history'] = dividend_history(state, monday)
    return result


DIVIDEND_HISTORY_WEEKS = 6


def dividend_history(state, monday: date, weeks: int = DIVIDEND_HISTORY_WEEKS) -> list[dict]:
    """Прошлые недели для 7a: «собрано X из Y» и сколько выдано собственнику
    из сейфа в понедельник выдачи («Выдать собственнику из сейфа» в 2a —
    резерв уменьшается, остаток бухгалтера не меняется). Недели без цели и
    без отложенного не показываем: истории там нет."""
    first = monday - timedelta(days=7 * weeks)
    flows = overview.daily_flows(state.accountant_finance.cash_flows_between(
        first, monday - timedelta(days=1)))
    # Выдачи собственнику — одним чтением резерва: раньше каждая неделя
    # собирала полную сводку всех резервов, 18 запросов вместо одного.
    paid_by_day = defaultdict(Decimal)
    for row in state.accountant_finance.reserve_entries('dividends'):
        if row['kind'] == 'withdrawal':
            paid_by_day[row['day']] += Decimal(row['amount'])
    result = []
    for index in range(1, weeks + 1):
        start = monday - timedelta(days=7 * index)
        end = start + timedelta(days=6)
        _, _, label = overview.week_of(start)
        target = state.dividend_targets.get(label)
        collected = sum((flows.get((start + timedelta(days=offset)).isoformat(), {}).get(
            'dividends', Decimal(0)) for offset in range(7)), Decimal(0))
        payout_day = end + timedelta(days=1)
        paid_out = paid_by_day.get(payout_day.isoformat(), Decimal(0))
        if target is None and not collected and not paid_out:
            continue
        amount = Decimal(target['amount']) if target else None
        result.append(dict(week=label, start=start.isoformat(), end=end.isoformat(),
                           payout_day=payout_day.isoformat(),
                           target=money(amount) if amount is not None else None,
                           collected=money(collected), paid_out=money(paid_out),
                           done=amount is not None and collected >= amount))
    return result


async def cashier_day(request, day: date):
    """Касса дня глазами кассира: выручка по заведениям, «Демо», расчёт передачи."""
    snapshot, error = await iiko_or_error(request, 'load', day, operation='cashier_day')
    if snapshot is None:
        return None, error
    state = request.app.state
    # Выдачи Шоху из кассы вычитаются вместе с расходами кассира: без них
    # «Проверка передачи» показывала бы недостачу там, где её нет.
    totals = await asyncio.to_thread(till_totals, state, day)
    demo = next((payment.amount for payment in snapshot.payments if payment.name == 'Демо'), Decimal(0))
    breakdown = snapshot.revenue_breakdown
    return dict(
        retro=money(breakdown.retro) if breakdown else None,
        school=money(breakdown.school) if breakdown else None,
        banquet=money(breakdown.bekhruz_banquet) if breakdown else None,
        register_revenue=money(snapshot.revenue), retro_checks=snapshot.receipt_count, demo=money(demo),
        expected_handover=money(cash_to_finance(snapshot, totals.cash_out, totals.receipts)),
        fetched_at=snapshot.fetched_at.isoformat()), None


def shift_accrued(request, day: date) -> Decimal:
    """Начислено сменным за смену `day` — та же сумма, что «Смена {день}» в 2a:
    у начисленных — начисление, у остальных — ставка по отметке дня."""
    from retro.modules.accountant.routes import attendance_payroll

    state = request.app.state
    roster = state.accountant_roster.list(day)
    _, rows = attendance_payroll(request, day, roster,
                                 state.accountant_finance.exceptions_for_day(day), frozen_pay=True)
    return sum((row.payable for row in rows if row.payable is not None), Decimal(0))


async def day_outlook(request, day: date, accounting: dict, expected):
    """«Уйдёт сегодня» и «У бухгалтера к вечеру ≈» (Функционал §3.8).

    Зарплаты = начислено сменным за вчера + оклады, выданные сегодня.
    Закуп ≈ и прочее ≈ — среднее за 7 прошлых дней (закуп бухгалтера и прочие
    расходы без дивидендов). К вечеру ≈ = на утро + к передаче − зарплаты −
    закуп − прочее. Оценки округляем до 10 000."""
    state = request.app.state
    recent, shift = await asyncio.gather(
        asyncio.to_thread(state.accountant_finance.cash_flows_between,
                          day - timedelta(days=7), day - timedelta(days=1)),
        asyncio.to_thread(shift_accrued, request, day - timedelta(days=1)))
    recent = overview.daily_flows(recent)
    average = lambda key: overview.round_to(sum(
        (values.get(key, Decimal(0)) for values in recent.values()), Decimal(0)) / 7, overview.OUTLOOK_STEP)
    monthly = sum((Decimal(row['amount']) for row in accounting['monthly_payments']['today']), Decimal(0))
    salary, procurement, other = shift + monthly, average('procurement'), average('other')
    opening = accounting['ledger']['cash_flow'].get('opening_balance')
    evening = None
    if opening is not None:
        evening = overview.round_to(Decimal(opening) + Decimal(expected or 0) - salary - procurement - other,
                                    overview.OUTLOOK_STEP)
    return dict(salary_due=money(salary), salary_shift=money(shift), salary_monthly=money(monthly),
                procurement=money(procurement), other=money(other),
                evening=money(evening) if evening is not None else None,
                handover_expected=expected is not None, estimate=True)


def handover_expected(cashier, handover_state):
    """Расчёт кассира для сверки и отчёта. Полученное бухгалтером (подтверждённая
    передача или его ручная запись) сверено сервером с текущим расчётом кассы
    (ledger.handover_state) — берём ровно тот расчёт, иначе — расчёт по iiko.
    Возвращает (сумма или None, подтверждено ли)."""
    confirmed = bool(handover_state and handover_state.get('confirmed_at'))
    if handover_state and handover_state.get('checked') and handover_state.get('calculation') is not None:
        return money(handover_state['calculation']), confirmed
    return (cashier['expected_handover'] if cashier else None), confirmed


async def month_cashier(request, first: date, last: date):
    """Демо и расчёт кассира по дням месяца для Excel: по одному отчёту iiko на
    день, не больше шести разом. День без ответа iiko — пустые ячейки."""
    gate = asyncio.Semaphore(6)
    state = request.app.state

    async def one(day):
        async with gate:
            cashier, error = await cashier_day(request, day)
        handover_state = await asyncio.to_thread(
            handover_check, state, day, Decimal(cashier['expected_handover']) if cashier else None)
        expected, _ = handover_expected(cashier, handover_state)
        if cashier is None:
            return day, None, error
        return day, dict(demo=cashier['demo'], expected=expected), None

    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    results = await asyncio.gather(*(one(day) for day in days))
    errors = [error for _, _, error in results if error]
    return {day.isoformat(): value for day, value, _ in results if value}, (errors[0] if errors else None)


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
    recorded = await asyncio.to_thread(state.accountant_finance.handover_for_day, receipt_day(day))
    handover_state = await asyncio.to_thread(
        handover_check, state, day, Decimal(cashier['expected_handover']) if cashier else None) or {}
    expected, confirmed = handover_expected(cashier, handover_state)
    checked = bool(handover_state.get('checked')) and handover_state.get('shortfall') is not None
    if checked:
        # Полученное бухгалтером (подтверждение или ручная запись, 2a) сверено
        # сервером с текущим расчётом кассы — та же недостача, что ошибка
        # «От кассира получено меньше расчёта» в 2a и в Excel дня.
        shortfall = Decimal(handover_state['shortfall'] or 0)
        check = dict(status='mismatch' if shortfall > 1 else 'ok',
                     difference=money(Decimal(recorded) - Decimal(expected)))
    elif (recorded is not None and handover_state.get('source') == 'accountant'
          and not handover_state.get('cashier_active')):
        # Приход записал бухгалтер, а кассир в панели в этот день не работал:
        # расчёт iiko не знает реальных расходов кассы — сверки нет, как в 2a.
        check = dict(status='unchecked', difference=None)
    else:
        check = overview.handover_check(recorded, expected)
    if day == today_tashkent() and check['status'] == 'missing':
        # Смена ещё идёт: не переданная касса сегодня — не недостача.
        check = dict(status='pending', difference=None)
    ledger = accounting['ledger']
    day_orders = (orders or {}).get(day.isoformat(), {})
    outlook = None
    if day == today_tashkent():
        outlook = await day_outlook(request, day, accounting, expected)
    return dict(
        date=day.isoformat(), weekday=day.weekday(), today=day == today_tashkent(),
        cashier=cashier, cashier_error=cashier_error,
        orders={key: value['orders'] for key, value in day_orders.items()} if orders is not None else None,
        handover=dict(receipt_date=receipt_day(day).isoformat(), recorded=money(recorded) if recorded is not None else None,
                      expected=expected, confirmed=confirmed, checked=checked,
                      confirmed_at=handover_state.get('confirmed_at') if confirmed else None,
                      shortfall=handover_state.get('shortfall') if checked else None,
                      expected_changed=bool(handover_state.get('expected_changed')), **check),
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
    transfers = state.accountant_finance.supplier_transfers(first, day)
    purchases = state.shokh.purchases_between(first, day)
    # «На руках у Шоха» считает store.pocket_position — та же формула, что на
    # экране закупа; своей копии здесь быть не должно.
    position = pocket_position(state.shokh, state.accountant_finance, day)
    pocket = None if position['pocket'] is None else money(position['pocket'])
    # Выдачи Шоху из кассы: в движениях бухгалтера их нет (передача уже меньше
    # на эту сумму), поэтому в «Закуп · наличные Шоху» они идут отдельно — один раз.
    from_till = shokh_total(shokh_gives(state.accountant_finance, first, day))
    return dict(month=first.isoformat()[:7], through=day.isoformat(),
                expenses=overview.expense_categories(flows, transfers, shokh_from_till=from_till),
                shokh=overview.shokh_month(purchases, flows, pocket=pocket, from_till=from_till,
                                           transfers=transfers))


def selected_day(value: date | None) -> date:
    day = value or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    return day
