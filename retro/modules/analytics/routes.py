"""API модуля «Аналитика» (T-524): `/api/analytics/…`.

Доступ — роль `analytics` (аналитик) и полный доступ; остальным путь закрывает
security_middleware (`panel_for_path`). Только чтение.
"""
import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request

from retro.modules.cashier.service import today_tashkent

from .service import GROUPS, MAX_DAYS, WINDOW, YEAR_MODES, build_report, history_report, spans

router = APIRouter(prefix='/api/analytics', tags=['analytics'])


@router.get('/revenue/weeks')
async def revenue_weeks(request: Request, start: date | None = None, end: date | None = None,
                        days: int | None = None, year: str = 'weekday'):
    """Выручка Retro за выбранный период, столько же дней раньше и год назад.

    Период — `start`–`end`; без `start` — `days` дней (по умолчанию 7) по `end`
    (по умолчанию вчера). `end` в будущем недоступен; `end` = сегодня можно —
    тогда последний день помечен как идущий и это сказано в предупреждениях.

    `year` — режим окна «год назад» (T-648): `weekday` (по умолчанию) — 364 дня
    назад, совпадают дни недели; `date` — те же календарные даты годом раньше,
    для праздников. По умолчанию день недели: обычная неделя ресторана зависит
    от него сильнее, чем от числа. Окно «раньше» от режима не зависит."""
    if year not in YEAR_MODES:
        raise HTTPException(422, 'Режим «год назад» — weekday (те же дни недели) или date (те же даты).')
    today = today_tashkent()
    end = end or today - timedelta(days=1)
    if start is None:
        if days is not None and not 1 <= days <= MAX_DAYS:
            raise HTTPException(422, f'Период — от 1 до {MAX_DAYS} дней.')
        start = end - timedelta(days=(days or WINDOW) - 1)
    if end > today:
        raise HTTPException(422, 'Будущие даты недоступны.')
    if start > end:
        raise HTTPException(422, 'Дата начала должна быть не позже даты конца.')
    if (end - start).days >= MAX_DAYS:
        raise HTTPException(422, f'Период — не длиннее {MAX_DAYS} дней.')
    sales = request.app.state.sales
    # Сегодня — свежее двух минут, пропущенные дни — в очередь фоновой загрузки;
    # ошибка освежения не роняет ответ, а попадает в предупреждения. Год назад —
    # отдельным отрезком: промежуток между окнами не нужен и не грузится.
    refresh_error = None
    states = {}
    # Сначала свежие дни: фон берёт очередь по порядку, а смотреть хочется на
    # последнюю неделю, а не на неделю раньше, которая стоит в отрезке первой.
    for first, last in spans(start, end, year):
        known = await asyncio.to_thread(sales.store.states, first, last)
        if sales.sync is not None:
            sales.sync.want([first + timedelta(days=offset) for offset in range((last - first).days, -1, -1)
                             if first + timedelta(days=offset) not in known])
        refresh_error = await sales.refresh_today(first, last) or refresh_error
        states.update(await asyncio.to_thread(sales.store.states, first, last))
    return build_report(start, end, states, today=today, history_start=sales.settings.sales_history_start,
                        configured=sales.sync is not None, refresh_error=refresh_error, year=year)


@router.get('/revenue/history')
async def revenue_history(request: Request, group: str = 'week'):
    """Выручка Retro за всю историю продаж в базе панели — по дням, неделям или месяцам.

    Начало — первый сохранённый день или `SALES_HISTORY_START`, что раньше; конец —
    сегодня (идущий день помечен). Пропущенные дни встают в очередь фоновой
    загрузки, как и в окне выше; в iiko отдельно модуль не ходит."""
    if group not in GROUPS:
        raise HTTPException(422, 'Детализация — day, week или month.')
    today = today_tashkent()
    sales = request.app.state.sales
    history_start = sales.settings.sales_history_start
    coverage = await asyncio.to_thread(sales.store.coverage)
    first = min(history_start, date.fromisoformat(coverage['first_day'])) if coverage['first_day'] else history_start
    first = min(first, today)
    refresh_error = await sales.refresh_today(first, today)
    states = await asyncio.to_thread(sales.store.states, first, today)
    return history_report(first, today, states, group=group, history_start=history_start,
                          configured=sales.sync is not None, refresh_error=refresh_error)
