"""Модуль «Аналитика» (T-524): выручка Retro за 7 дней против недели раньше и года назад.

Источник — история чеков iiko в базе панели (`sales_days`, T-486). Берётся
только направление **Retro** (касса ресторана без банкетного зала): итог по
направлениям лежит в `reconciliation.lines` каждого дня, как его посчитал
`prepare_day`; школа (`school`) и банкет (`banquet`) в цифры не входят.
Отдельных запросов в iiko модуль не делает — только читает уже загруженные дни;
сегодняшний день, если он в окне, освежает тем же `refresh_today`, что продажи.

Окно выбирает человек (T-525): любые `start`–`end`, по умолчанию семь
завершённых дней по вчера включительно (как «7 завершённых дней» в аналитике
учредителя): день в iiko закрывается ночью, и сравнивать идущий день с полными
нечестно. Сравнение идёт день к дню: «раньше» — столько же дней непосредственно
перед окном, «год назад» — в одном из двух режимов (T-648):

* `weekday` (по умолчанию) — те же дни 52 недели (364 дня) назад, чтобы дни
  недели совпадали: выручка ресторана зависит от дня недели сильнее, чем от
  числа, и для обычного периода это честное сравнение. Цена — дата уезжает
  на день за год (на два через 29 февраля): 31.12 сравнивается с 01.01;
* `date` — те же календарные даты годом раньше, для праздников и других дат,
  привязанных к календарю. Окно отсчитывается от начала периода и всегда той же
  длины; 29.02 в невисокосном году — 28.02.

Совпасть одновременно и дате, и дню недели нельзя — режим выбирает человек.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from retro.modules.cashier.service import TZ

WINDOW = 7
MAX_DAYS = 92
YEAR_SHIFT = 364
YEAR_MODES = ('weekday', 'date')
DIRECTION = 'retro'
DIRECTION_NAME = 'Retro · без школы и банкетного зала'
WEEKDAYS = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')


def day_word(count):
    rest, tail = count % 100, count % 10
    if 11 <= rest <= 19 or tail == 0 or tail >= 5:
        return 'дней'
    return 'день' if tail == 1 else 'дня'


def year_ago(start, end, mode='weekday'):
    """Окно «год назад» в режиме `mode`: (начало, конец, подпись).

    В режиме `date` сдвигается только начало, конец — та же длина от него: окно,
    пересекающее 29 февраля, иначе стало бы на день короче или длиннее периода."""
    if mode == 'weekday':
        return (start - timedelta(days=YEAR_SHIFT), end - timedelta(days=YEAR_SHIFT),
                'Год назад · те же дни недели')
    try:
        first = start.replace(year=start.year - 1)
    except ValueError:  # 29.02 — в прошлом году его нет
        first = start.replace(year=start.year - 1, day=28)
    return first, first + (end - start), 'Год назад · те же даты'


def windows(start, end, year='weekday'):
    """Три окна по выбранному периоду: он сам, столько же дней перед ним и год назад в режиме `year`."""
    length = (end - start).days + 1
    past_start, past_end, past_label = year_ago(start, end, year)
    return [dict(key='current', label='Выбранный период', start=start, end=end),
            dict(key='previous', label=f'{length} {day_word(length)} раньше',
                 start=start - timedelta(days=length), end=start - timedelta(days=1)),
            dict(key='year', label=past_label, start=past_start, end=past_end)]


def spans(start, end, year='weekday'):
    """Непрерывные отрезки дней, которые нужны для окон: период с окном «раньше»
    одним куском и окно год назад — отдельно (между ними ничего не грузим).
    Отрезки берутся из самих окон, чтобы загрузка не разошлась с показом."""
    _, previous, past = windows(start, end, year)
    return [(previous['start'], end), (past['start'], past['end'])]


def retro_revenue(state):
    """Выручка направления Retro за день из сохранённой сверки; None — в записи
    дня нет разбивки по направлениям (старый формат), день считаем незагруженным."""
    lines = (state.get('reconciliation') or {}).get('lines')
    if not isinstance(lines, dict):
        return None
    return Decimal(str((lines.get(DIRECTION) or {}).get('revenue', 0)))


def _day(day, state, today):
    revenue = None if state is None else retro_revenue(state)
    if revenue is None:
        return dict(day=day.isoformat(), weekday=WEEKDAYS[day.weekday()], revenue=None, missing=True, partial=False)
    return dict(day=day.isoformat(), weekday=WEEKDAYS[day.weekday()], revenue=str(revenue),
                missing=False, partial=day >= today or not state['closed'])


def _period(window, states, today):
    length = (window['end'] - window['start']).days + 1
    days = [_day(window['start'] + timedelta(days=offset), states.get(window['start'] + timedelta(days=offset)), today)
            for offset in range(length)]
    covered = [item for item in days if not item['missing']]
    total = sum((Decimal(item['revenue']) for item in covered), Decimal(0)) if covered else None
    return dict(key=window['key'], label=window['label'], start=window['start'].isoformat(),
                end=window['end'].isoformat(), total=None if total is None else str(total),
                days=days, days_with_data=len(covered),
                complete=len(covered) == length and not any(item['partial'] for item in days))


def compare(current, other):
    """Разница текущего окна с другим: в сумах и процентах; направление — для стрелки.

    Проценты — от другого окна («на сколько выросла по сравнению с»); при его
    нуле процент не определён. Без данных хотя бы с одной стороны сравнения нет."""
    if current is None or other is None:
        return dict(diff=None, percent=None, direction='unknown')
    diff = Decimal(current) - Decimal(other)
    percent = None
    if Decimal(other) > 0:
        percent = float((diff / Decimal(other) * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))
    return dict(diff=str(diff), percent=percent, direction='up' if diff > 0 else 'down' if diff < 0 else 'flat')


def build_report(start, end, states, *, today, history_start, configured=True, refresh_error=None, year='weekday'):
    """Ответ модуля из состояний дней (`SalesStore.states`) за все окна."""
    periods = [_period(window, states, today) for window in windows(start, end, year)]
    current = periods[0]
    comparison = [dict(against=other['key'], label=other['label'], start=other['start'], end=other['end'],
                       **compare(current['total'], other['total']))
                  for other in periods[1:]]
    all_days = [date.fromisoformat(item['day']) for period in periods for item in period['days']]
    missing = [day for day in all_days if day not in states or retro_revenue(states[day]) is None]
    warnings = []
    if not configured:
        warnings.append('iiko не подключён: показана только история продаж, уже сохранённая в базе панели.')
    # Пропуски — по окнам и с годом, если окно в другом году: «16.10» год назад и
    # «16.10» на прошлой неделе иначе не отличить. Дни после начала истории фон
    # ещё догрузит (по дню в минуту) — об этом говорим прямо, а не «нет данных».
    def label(day):
        return day.strftime('%d.%m.%Y') if day.year != today.year else day.strftime('%d.%m')
    loading = [day for day in missing if day >= history_start and day <= today]
    for period in periods:
        gone = [date.fromisoformat(item['day']) for item in period['days'] if item['missing']]
        if not gone:
            continue
        if len(gone) == len(period['days']):
            text = f'{period["label"]} ({label(gone[0])} – {label(gone[-1])}): данных нет'
        else:
            text = (f'{period["label"]}: нет данных за ' + ', '.join(label(day) for day in gone[:10])
                    + (f' и ещё {len(gone) - 10}' if len(gone) > 10 else ''))
        if all(day < history_start for day in gone):
            text += f' — история продаж в базе начинается с {history_start:%d.%m.%Y}.'
        elif configured and any(day in loading for day in gone):
            text += ' — эти дни ещё загружаются из iiko, по дню в минуту; в итоги они пока не вошли.'
        else:
            text += ' — эти дни в итоги не вошли.'
        warnings.append(text)
    stale = [day for day in all_days if day in states and day < today and not states[day]['closed']]
    if stale:
        warnings.append('Ещё не перечитаны после закрытия смены: '
                        + ', '.join(day.strftime('%d.%m') for day in stale) + '.')
    if today in states and end >= today:
        warnings.append('Сегодня учтены только закрытые чеки на '
                        f'{states[today]["fetched_at"].astimezone(TZ):%H:%M}; открытые столы iiko не отдаёт.')
    if refresh_error:
        warnings.append(refresh_error)
    updated = max((states[day]['fetched_at'] for day in all_days if day in states), default=None)
    return dict(
        source='iiko OLAP SALES · история чеков в базе панели', timezone=str(TZ),
        today=today.isoformat(), start=start.isoformat(), end=end.isoformat(), includes_today=end >= today,
        direction=DIRECTION, direction_name=DIRECTION_NAME,
        window_days=(end - start).days + 1, year=year, history_start=history_start.isoformat(), configured=configured,
        updated_at=updated.astimezone(TZ).isoformat(timespec='seconds') if updated else None,
        complete=all(period['complete'] for period in periods),
        periods=periods, comparison=comparison, warnings=warnings)


# ── Вся история (T-592) ────────────────────────────────────────────────────
# Выручка Retro с первого дня истории продаж по сегодня столбиками по дням,
# неделям (пн–вс) или месяцам. Тот же источник и то же направление, что выше;
# пропуск — не ноль, а неполный столбик, и это видно в ответе.

GROUPS = ('day', 'week', 'month')
MONTHS = ('янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек')


def bucket_bounds(day, group):
    """Календарные границы столбика, в который попадает день."""
    if group == 'week':
        first = day - timedelta(days=day.weekday())
        return first, first + timedelta(days=6)
    if group == 'month':
        first = day.replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        return first, following - timedelta(days=1)
    return day, day


def bucket_label(first, last, group, today):
    def short(day):
        return day.strftime('%d.%m') if day.year == today.year else day.strftime('%d.%m.%Y')
    if group == 'month':
        return f'{MONTHS[first.month - 1]} {first.year}'
    if group == 'week':
        return f'{short(first)} – {short(last)}'
    return f'{WEEKDAYS[first.weekday()]} {short(first)}'


def history_report(first, today, states, *, group, history_start, configured=True, refresh_error=None):
    """Столбики выручки Retro с `first` по `today` из состояний дней (`SalesStore.states`).

    Столбик на краю истории обрезан её началом или сегодняшним днём (`clipped`),
    столбик с пропуском или незакрытым днём — неполный (`complete=False`): сумма
    в нём по загруженным дням, среднее за день — по ним же, чтобы неполные
    недели и месяцы можно было сравнить с полными."""
    buckets, day = [], first
    while day <= today:
        start, end = bucket_bounds(day, group)
        last = min(end, today)
        loaded, partial = [], False
        cursor = day
        while cursor <= last:
            revenue = retro_revenue(states[cursor]) if cursor in states else None
            if revenue is not None:
                loaded.append(revenue)
                partial = partial or cursor >= today or not states[cursor]['closed']
            cursor += timedelta(days=1)
        length = (last - day).days + 1
        total = sum(loaded, Decimal(0)) if loaded else None
        clipped = day != start or last != end
        buckets.append(dict(
            start=day.isoformat(), end=last.isoformat(), label=bucket_label(start, end, group, today),
            total=None if total is None else str(total), days=length, days_with_data=len(loaded),
            average=None if total is None else str((total / len(loaded)).quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            partial=partial, clipped=clipped, complete=len(loaded) == length and not partial and not clipped))
        day = last + timedelta(days=1)
    days = (today - first).days + 1 if first <= today else 0
    loaded_days = sum(bucket['days_with_data'] for bucket in buckets)
    total = sum((Decimal(bucket['total']) for bucket in buckets if bucket['total'] is not None), Decimal(0))
    warnings = []
    if not configured:
        warnings.append('iiko не подключён: показана только история продаж, уже сохранённая в базе панели.')
    missing = days - loaded_days
    if missing:
        # Дни до начала истории фон не грузит; после — догрузит сам, по дню в минуту.
        before = sum(1 for offset in range(days) if first + timedelta(days=offset) < history_start
                     and retro_revenue(states.get(first + timedelta(days=offset)) or {}) is None)
        text = f'Нет данных за {missing} {day_word(missing)} из {days}'
        if configured and missing > before:
            text += ' — они ещё загружаются из iiko, по дню в минуту; в суммы пока не вошли.'
        else:
            text += ' — в суммы они не вошли.'
        warnings.append(text)
    if refresh_error:
        warnings.append(refresh_error)
    updated = max((state['fetched_at'] for state in states.values()), default=None)
    return dict(
        source='iiko OLAP SALES · история чеков в базе панели', timezone=str(TZ), today=today.isoformat(),
        direction=DIRECTION, direction_name=DIRECTION_NAME, group=group,
        start=first.isoformat(), end=today.isoformat(), history_start=history_start.isoformat(),
        configured=configured, days=days, days_with_data=loaded_days,
        total=str(total) if loaded_days else None,
        average=str((total / loaded_days).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if loaded_days else None,
        updated_at=updated.astimezone(TZ).isoformat(timespec='seconds') if updated else None,
        buckets=buckets, warnings=warnings)
