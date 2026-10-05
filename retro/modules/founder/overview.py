"""Чистые расчёты кабинета учредителя: неделя, дивиденды, расходы, прогноз.

Сюда не ходят ни iiko, ни база — только уже прочитанные факты. Поэтому всё,
что показывает экран учредителя, проверяется тестом без сети.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from retro.accounting_period import accounting_range_start
from retro.modules.accountant.expense_catalog import GROUPS, ITEMS
from retro.modules.accountant.reserves import is_monthly_salary


# Счёт Шефа выше этой суммы за один счёт учредитель хочет видеть сразу.
CHEF_BILL_LIMIT = Decimal(400000)
# «Отстаём от плана»: собрано меньше 85 % того, что к этому дню положено.
DIVIDEND_BEHIND_SHARE = Decimal('0.85')
# Совет «отложить сегодня» округляем вверх до этого шага, чтобы не называть
# сумму вида 1 285 714,29. Не путать с шагом кнопок ± в редакторе цели
# (DIVIDEND_EDIT_STEP в founder-cabinet-logic.js) — тот в десять раз крупнее.
DIVIDEND_SUGGEST_STEP = Decimal(50000)
# «Касса свободно даёт ≈» округляем до этого шага (Функционал §3.7).
FREE_CASH_STEP = Decimal(500000)
# Оценки «Уйдёт сегодня» и «У бухгалтера к вечеру ≈» — до 10 000 (§3.8).
OUTLOOK_STEP = Decimal(10000)
# Прогноз по дню недели — среднее за последние восемь таких же дней.
FORECAST_WEEKS = 8
WEEKDAYS = ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')

GROUP_LABELS = {code: label for code, label, _ in GROUPS}


def money(value):
    return str(Decimal(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def week_of(day: date):
    """Неделя с понедельника по воскресенье и её ISO-метка «2026-W39»."""
    monday = day - timedelta(days=day.weekday())
    year, number, _ = day.isocalendar()
    return monday, monday + timedelta(days=6), f'{year}-W{number:02d}'


def parse_week(value: str):
    """«2026-W39» → понедельник этой недели; иначе ValueError."""
    if (not isinstance(value, str) or len(value) != 8 or value[4:6] != '-W'
            or not value[:4].isdigit() or not value[6:].isdigit()):
        raise ValueError('Неделя указывается как ГГГГ-Wнн.')
    try:
        return date.fromisocalendar(int(value[:4]), int(value[6:]), 1)
    except ValueError:
        raise ValueError('Такой недели нет в календаре.') from None


def flow_kind(row) -> str:
    """Куда относится движение денег бухгалтера.

    Дивиденды — это перевод в сейф (резерв `dividends`) и выплата
    собственнику прямо из кассы: и то и другое уменьшает деньги на расходы
    ради владельца. Остальные переводы группы «Дивиденды и переводы»
    (Oxbridge, блогеры) — обычные расходы."""
    kind, code = row['type'], row.get('item_code')
    if kind in ('handover', 'cashier_transfer'):
        return 'handover'
    if kind == 'other_receipt':
        return 'receipt'
    if kind == 'reserve_transfer' or code == 'distribution_dividends':
        return 'dividends'
    if kind == 'salary_payment':
        return 'salary'
    if kind == 'procurement_advance':
        return 'procurement'
    group = ITEMS.get(code, (None,))[0]
    if group == 'salary':
        return 'salary'
    if group == 'procurement':
        return 'procurement'
    return 'other'


def daily_flows(rows):
    """{день: {handover, receipt, salary, procurement, other, dividends}}."""
    result = defaultdict(lambda: defaultdict(Decimal))
    for row in rows:
        result[row['day']][flow_kind(row)] += Decimal(row['amount'])
    return result


SUPPLIER_TRANSFERS_LABEL = 'Закуп · перечисления'


def expense_categories(rows, transfers=(), *, shokh_from_till=Decimal(0)):
    """Куда ушли деньги за период: группы справочника, зарплаты раздельно.

    Приход (передача кассира, прочие поступления) сюда не входит.
    `transfers` — перечисления поставщикам со счёта: это расход на закуп, но не
    наличные, поэтому в движения кассы (`rows`) они не попадают и приходят
    отдельно — своей категорией. `shokh_from_till` — наличные Шоху прямо из
    кассы кассира: среди движений бухгалтера их тоже нет, а закуп это тот же."""
    totals = defaultdict(Decimal)
    transferred = sum((Decimal(row['amount']) for row in transfers), Decimal(0))
    if transferred:
        totals[SUPPLIER_TRANSFERS_LABEL] += transferred
    if shokh_from_till:
        totals['Закуп · наличные Шоху'] += Decimal(shokh_from_till)
    for row in rows:
        kind = flow_kind(row)
        if kind in ('handover', 'receipt'):
            continue
        amount = Decimal(row['amount'])
        if kind == 'dividends':
            totals['Дивиденды'] += amount
        elif row['type'] == 'salary_payment':
            totals['Зарплаты · сменные'] += amount
        elif kind == 'salary':
            label = 'Зарплаты · оклады' if is_monthly_salary(row.get('item_code')) \
                else 'Зарплаты · сменные'
            totals[label] += amount
        elif is_shokh_advance(row):
            totals['Закуп · наличные Шоху'] += amount
        elif kind == 'procurement':
            totals['Закуп · напрямую'] += amount
        else:
            group = ITEMS.get(row.get('item_code'), (None,))[0]
            totals[GROUP_LABELS.get(group, 'Прочее')] += amount
    total = sum(totals.values(), Decimal(0))
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return dict(total=money(total), categories=[
        dict(label=label, amount=money(amount),
             share_percent=money(amount * 100 / total) if total else None)
        for label, amount in ordered])


def dividend_week(day: date, target, flows, *, free_cash=None):
    """Недельная цель учредителя против того, что бухгалтер реально отложил.

    `flows` — результат `daily_flows` за неделю, `target` — Decimal или None.
    План к дню растёт ровно по дням недели: к среде — три седьмых цели."""
    monday, sunday, label = week_of(day)
    monday = accounting_range_start(monday, day)
    week_days = (sunday - monday).days + 1
    days = [monday + timedelta(days=offset) for offset in range(week_days)]
    by_day = [dict(date=d.isoformat(), weekday=WEEKDAYS[d.weekday()],
                   amount=money(flows.get(d.isoformat(), {}).get('dividends', Decimal(0))),
                   future=d > day)
              for d in days]
    collected = sum((Decimal(item['amount']) for item in by_day if not item['future']), Decimal(0))
    today_amount = Decimal(next(item['amount'] for item in by_day if item['date'] == day.isoformat()))
    before = collected - today_amount
    elapsed = (day - monday).days + 1
    result = dict(week=label, start=monday.isoformat(), end=sunday.isoformat(),
                  payout_day=(sunday + timedelta(days=1)).isoformat(),
                  target=money(target) if target is not None else None,
                  collected=money(collected), collected_today=money(today_amount),
                  days=by_day, free_cash_week=money(free_cash) if free_cash is not None else None)
    if target is None or target <= 0:
        return {**result, 'pace': None, 'due': None, 'left': None, 'behind': False, 'done': False,
                'suggest_today': None, 'days_left': week_days + 1 - elapsed}
    # `pace` — план к сегодняшнему дню, включая сегодня (метка на полосе).
    # По Функционалу §3.7 «Отстаём» — отложено меньше 85 % этого плана: утром,
    # пока сегодняшнюю долю не отложили, статус подсказывает её отложить.
    # `due` — план к концу вчера, остаётся в ответе для подписей.
    pace = target * elapsed / week_days
    due = target * (elapsed - 1) / week_days
    left = max(Decimal(0), target - collected)
    days_left = week_days + 1 - elapsed
    share = max(Decimal(0), target - before) / days_left
    share = (share / DIVIDEND_SUGGEST_STEP).to_integral_value(
        rounding=ROUND_CEILING) * DIVIDEND_SUGGEST_STEP
    # Уже отложенное сегодня вычитаем, и больше остатка до цели не советуем.
    suggest = min(left, max(Decimal(0), share - today_amount))
    return {**result, 'pace': money(pace), 'due': money(due), 'left': money(left),
            'behind': collected < pace * DIVIDEND_BEHIND_SHARE, 'done': collected >= target,
            'suggest_today': money(suggest), 'days_left': days_left}


def round_to(value, step):
    """До ближайшего кратного шага (половина — вверх)."""
    return (Decimal(value) / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step


def free_cash_per_week(flows, days):
    """«Касса свободно даёт ≈» (Функционал §3.7): среднее за день (от кассира −
    зарплаты − закуп бухгалтера − прочие расходы без дивидендов) × 7, до 500 000.

    Прочие поступления в формулу не входят — только деньги от кассира.
    Считаются только дни, где передача кассира записана: без неё день
    выглядел бы убыточным и тянул среднее вниз."""
    known = []
    for day in days:
        values = flows.get(day.isoformat())
        if not values or 'handover' not in values:
            continue
        known.append(values['handover']
                     - values.get('salary', Decimal(0)) - values.get('procurement', Decimal(0))
                     - values.get('other', Decimal(0)))
    if not known:
        return None
    return max(Decimal(0), round_to(sum(known, Decimal(0)) / len(known) * 7, FREE_CASH_STEP))


def handover_check(recorded, expected):
    """Сошлась ли передача кассира с тем, что должно было прийти.

    `expected` — расчёт по iiko и расходам кассира, `recorded` — сумма,
    которую бухгалтер записал как полученную. Разница до одного сума —
    округление, а не недостача."""
    if expected is None:
        return dict(status='unknown', difference=None)
    if recorded is None:
        return dict(status='missing', difference=None)
    difference = Decimal(recorded) - Decimal(expected)
    return dict(status='ok' if abs(difference) <= 1 else 'mismatch', difference=money(difference))


def register_days(rows):
    """Строки iiko «день × касса» → {день: {retro|school: {revenue, orders}}}."""
    result = defaultdict(dict)
    for row in rows:
        result[row['day']][row['direction']] = dict(revenue=Decimal(row['revenue']),
                                                    orders=int(row['orders']))
    return result


def weekday_forecast(history, today: date):
    """Средняя выручка и чеки по дням недели за последние восемь недель.

    Сегодняшний день в историю не входит: смена ещё идёт."""
    buckets = defaultdict(list)
    for day_text, values in history.items():
        day = date.fromisoformat(day_text)
        if day >= today or (today - day).days > FORECAST_WEEKS * 7:
            continue
        buckets[day.weekday()].append(values)
    result = []
    for weekday in range(7):
        samples = buckets.get(weekday, [])

        def average(direction, field):
            values = [item[direction][field] for item in samples if direction in item]
            return sum(values, Decimal(0)) / len(values) if values else None

        retro, school = average('retro', 'revenue'), average('school', 'revenue')
        retro_orders, school_orders = average('retro', 'orders'), average('school', 'orders')
        result.append(dict(
            weekday=weekday, label=WEEKDAYS[weekday], samples=len(samples),
            retro=money(retro) if retro is not None else None,
            school=money(school) if school is not None else None,
            revenue=money((retro or 0) + (school or 0)) if samples else None,
            orders=int(((retro_orders or Decimal(0)) + (school_orders or Decimal(0))).quantize(
                Decimal(1), rounding=ROUND_HALF_UP)) if samples else None))
    return result


def month_forecast(history, forecast, today: date):
    """Прогноз месяца (Функционал §3.8): факт с 1-го числа по сегодня плюс
    прогноз оставшегося. Сегодняшний день ещё идёт: в факт входит то, что
    уже пробито, а в прогноз — недобранное до среднего этого дня недели
    (если день уже перерос среднее, прогноз на него 0)."""
    first = accounting_range_start(today.replace(day=1), today)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    fact = Decimal(0)
    today_fact = Decimal(0)
    cursor = first
    while cursor <= today:
        values = history.get(cursor.isoformat(), {})
        revenue = sum((item['revenue'] for item in values.values()), Decimal(0))
        fact += revenue
        if cursor == today:
            today_fact = revenue
        cursor += timedelta(days=1)
    expected = Decimal(0)
    cursor = today
    while cursor <= last:
        average = forecast[cursor.weekday()]['revenue']
        average = Decimal(average) if average is not None else Decimal(0)
        expected += max(Decimal(0), average - today_fact) if cursor == today else average
        cursor += timedelta(days=1)
    return dict(month=first.isoformat()[:7], fact=money(fact), forecast=money(expected),
                total=money(fact + expected), fact_through=today.isoformat())


def chef_bills(rows, *, week_start: date, week_end: date, month_start: date):
    """Счета Шефа из iiko: по счёту, с отметкой тех, что выше порога."""
    bills = [dict(row, over=Decimal(row['amount']) > CHEF_BILL_LIMIT) for row in rows]
    bills.sort(key=lambda row: (row['day'], Decimal(row['amount'])), reverse=True)
    week = [row for row in bills if week_start.isoformat() <= row['day'] <= week_end.isoformat()]
    month = [row for row in bills if row['day'] >= month_start.isoformat()]
    total = lambda items: money(sum((Decimal(row['amount']) for row in items), Decimal(0)))
    return dict(limit=money(CHEF_BILL_LIMIT), bills=bills, week=week,
                week_total=total(week), month_total=total(month),
                week_over=sum(row['over'] for row in week), month_over=sum(row['over'] for row in month))


def is_shokh_advance(row):
    """Выдача подотчёта Шоху — так же, как её видит резерв `shoh`."""
    return ((row['type'] == 'other_expense' and row.get('item_code') == 'proc_shoh') or
            (row['type'] == 'procurement_advance'
             and str(row.get('description') or '').startswith('Шох:')))


def shokh_month(purchases, flows, *, pocket, from_till=Decimal(0), transfers=(), expenses=None):
    """Закуп за месяц: сколько выдали Шоху, сколько он записал, что проверить.

    «Напрямую» — закуп, который бухгалтер оплатил сам наличными, мимо подотчёта
    Шоха (мясо, уголь, хлеб и прочие статьи группы «Закуп»). «Перечислениями»
    (`transfers`) — оплата поставщикам со счёта, безнал (Функционал 7b).
    `from_till` — выдачи Шоху прямо из кассы кассира: в выдано они входят, в
    движениях бухгалтера их нет. `expenses` — расходы Шоха по счёт-фактуре,
    которые с ТЗ 02.10 вносит бухгалтер (базар, сумма): когда они переданы,
    потрачено считается по ним, а покупки с телефона не учитываются."""
    given = sum((Decimal(row['amount']) for row in flows if is_shokh_advance(row)),
                Decimal(from_till))
    direct = sum((Decimal(row['amount']) for row in flows
                  if flow_kind(row) == 'procurement' and not is_shokh_advance(row)), Decimal(0))
    by_item = defaultdict(Decimal)
    if expenses is not None:
        purchases = []
        spent = sum((Decimal(row['amount']) for row in expenses), Decimal(0))
        for row in expenses:
            by_item[row.get('place') or 'Без базара'] += Decimal(row['amount'])
    else:
        spent = sum((Decimal(row['total']) for row in purchases), Decimal(0))
        for row in purchases:
            by_item[row['item']] += Decimal(row['total'])
    # `total` покупки — наличные в целых сумах (shokh.store.cash_amount).
    flagged = [dict(id=row['id'], day=row['day'], item=row['item'], total=row['total'],
                    reason=('нет в iiko' if row.get('off_catalog') else
                            'нет фото' if not row['has_photo'] else 'цена выше обычной'))
               for row in purchases
               if row['accepted_at'] is None and (row.get('off_catalog') or not row['has_photo']
                                                  or row['price_above_usual'])]
    top = sorted(by_item.items(), key=lambda item: (-item[1], item[0]))[:5]
    transferred = sum((Decimal(row['amount']) for row in transfers), Decimal(0))
    return dict(given=money(given), given_from_till=money(from_till), spent=money(spent),
                direct=money(direct), transfers=money(transferred), pocket=pocket, purchases=len(purchases), flagged=flagged,
                source='invoices' if expenses is not None else 'phone',
                top_items=[dict(item=name, amount=money(amount)) for name, amount in top])
