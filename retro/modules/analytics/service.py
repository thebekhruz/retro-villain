"""Модуль «Аналитика» (T-524): выручка за 7 дней против двух предыдущих семидневок.

Источник — история чеков iiko в базе панели (`sales_days.revenue`, T-486):
тот же дневной итог, что в продажах директора и учредителя, все направления
вместе (Retro, Retro в школе, банкетный зал). Отдельных запросов в iiko модуль
не делает — только читает уже загруженные дни; сегодняшний день, если он в окне,
освежает тем же `refresh_today`, что продажи.

Окно — семь завершённых дней по вчера включительно (как «7 завершённых дней» в
аналитике учредителя и `completed_period` блюд): день в iiko закрывается ночью,
и сравнивать идущий день с полными нечестно. Сравнение идёт день к дню:
понедельник с понедельником неделей раньше и двумя неделями раньше.
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from retro.modules.cashier.service import TZ

WINDOW = 7
# Ключ, подпись и сдвиг окна в днях относительно текущего.
PERIODS = (('current', 'Последние 7 дней', 0),
           ('previous', 'Неделей раньше', WINDOW),
           ('before', 'Двумя неделями раньше', 2 * WINDOW))
WEEKDAYS = ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')
SPAN_DAYS = WINDOW * len(PERIODS)


def span(end):
    """Первый и последний день, которые нужны для трёх окон по `end`."""
    return end - timedelta(days=SPAN_DAYS - 1), end


def windows(end):
    return [dict(key=key, label=label, start=end - timedelta(days=shift + WINDOW - 1), end=end - timedelta(days=shift))
            for key, label, shift in PERIODS]


def _day(day, state, today):
    if state is None:
        return dict(day=day.isoformat(), weekday=WEEKDAYS[day.weekday()], revenue=None, missing=True, partial=False)
    return dict(day=day.isoformat(), weekday=WEEKDAYS[day.weekday()], revenue=str(state['revenue']),
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
    first, last = span(end)
    all_days = [first + timedelta(days=offset) for offset in range(SPAN_DAYS)]
    missing = [day for day in all_days if day not in states]
    warnings = []
    if not configured:
        warnings.append('iiko не подключён: показана только история продаж, уже сохранённая в базе панели.')
    if missing:
        warnings.append('Нет загруженных данных за ' + ', '.join(day.strftime('%d.%m') for day in missing[:10])
                        + (f' и ещё {len(missing) - 10}' if len(missing) > 10 else '')
                        + ' — эти дни в итоги не вошли.')
        if any(day < history_start for day in missing):
            warnings.append(f'История продаж в базе начинается с {history_start:%d.%m.%Y}.')
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
        window_days=WINDOW, history_start=history_start.isoformat(), configured=configured,
        updated_at=updated.astimezone(TZ).isoformat(timespec='seconds') if updated else None,
        complete=all(period['complete'] for period in periods),
        periods=periods, comparison=comparison, warnings=warnings)
