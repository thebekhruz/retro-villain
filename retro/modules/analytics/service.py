"""Модуль «Аналитика» (T-524): выручка Retro за 7 дней против недели раньше и года назад.

Источник — история чеков iiko в базе панели (`sales_days`, T-486). Берётся
только направление **Retro** (касса ресторана без банкетного зала): итог по
направлениям лежит в `reconciliation.lines` каждого дня, как его посчитал
`prepare_day`; школа (`school`) и банкет (`banquet`) в цифры не входят.
Отдельных запросов в iiko модуль не делает — только читает уже загруженные дни;
сегодняшний день, если он в окне, освежает тем же `refresh_today`, что продажи.

Окно — семь завершённых дней по вчера включительно (как «7 завершённых дней» в
аналитике учредителя и `completed_period` блюд): день в iiko закрывается ночью,
и сравнивать идущий день с полными нечестно. Сравнение идёт день к дню:
понедельник с понедельником неделей раньше и 52 недели (364 дня) назад — «год
назад» со сдвигом на целые недели, чтобы дни недели совпадали.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from retro.modules.cashier.service import TZ

WINDOW = 7
YEAR_SHIFT = 52 * WINDOW
DIRECTION = 'retro'
DIRECTION_NAME = 'Retro · без школы и банкетного зала'
# Ключ, подпись и сдвиг окна в днях относительно текущего.
PERIODS = (('current', 'Последние 7 дней', 0),
           ('previous', 'Неделей раньше', WINDOW),
           ('year', 'Год назад', YEAR_SHIFT))
WEEKDAYS = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')


def spans(end):
    """Непрерывные отрезки дней, которые нужны для окон по `end`: последние две
    недели одним куском и неделя год назад — отдельно (между ними ничего не грузим)."""
    return [(end - timedelta(days=2 * WINDOW - 1), end),
            (end - timedelta(days=YEAR_SHIFT + WINDOW - 1), end - timedelta(days=YEAR_SHIFT))]


def retro_revenue(state):
    """Выручка направления Retro за день из сохранённой сверки; None — в записи
    дня нет разбивки по направлениям (старый формат), день считаем незагруженным."""
    lines = (state.get('reconciliation') or {}).get('lines')
    if not isinstance(lines, dict):
        return None
    return Decimal(str((lines.get(DIRECTION) or {}).get('revenue', 0)))


def windows(end):
    return [dict(key=key, label=label, start=end - timedelta(days=shift + WINDOW - 1), end=end - timedelta(days=shift))
            for key, label, shift in PERIODS]


def _day(day, state, today):
    revenue = None if state is None else retro_revenue(state)
    if revenue is None:
        return dict(day=day.isoformat(), weekday=WEEKDAYS[day.weekday()], revenue=None, missing=True, partial=False)
    return dict(day=day.isoformat(), weekday=WEEKDAYS[day.weekday()], revenue=str(revenue),
                missing=False, partial=day >= today or not state['closed'])


def _period(window, states, today):
    days = [_day(window['start'] + timedelta(days=offset), states.get(window['start'] + timedelta(days=offset)), today)
            for offset in range(WINDOW)]
    covered = [item for item in days if not item['missing']]
    total = sum((Decimal(item['revenue']) for item in covered), Decimal(0)) if covered else None
    return dict(key=window['key'], label=window['label'], start=window['start'].isoformat(),
                end=window['end'].isoformat(), total=None if total is None else str(total),
                days=days, days_with_data=len(covered),
                complete=len(covered) == WINDOW and not any(item['partial'] for item in days))


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


def build_report(end, states, *, today, history_start, configured=True, refresh_error=None):
    """Ответ модуля из состояний дней (`SalesStore.states`) за весь диапазон."""
    periods = [_period(window, states, today) for window in windows(end)]
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
        if len(gone) == WINDOW:
            text = f'{period["label"]} ({label(gone[0])} – {label(gone[-1])}): данных нет'
        else:
            text = f'{period["label"]}: нет данных за ' + ', '.join(label(day) for day in gone)
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
        today=today.isoformat(), end=end.isoformat(), includes_today=end >= today,
        direction=DIRECTION, direction_name=DIRECTION_NAME,
        window_days=WINDOW, history_start=history_start.isoformat(), configured=configured,
        updated_at=updated.astimezone(TZ).isoformat(timespec='seconds') if updated else None,
        complete=all(period['complete'] for period in periods),
        periods=periods, comparison=comparison, warnings=warnings)
