"""API модуля «Аналитика» (T-524): `/api/analytics/…`.

Доступ — роль `analytics` (аналитик) и полный доступ; остальным путь закрывает
security_middleware (`panel_for_path`). Только чтение.
"""
import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request

from retro.modules.cashier.service import today_tashkent

from .service import MAX_DAYS, WINDOW, build_report, spans

router = APIRouter(prefix='/api/analytics', tags=['analytics'])


@router.get('/revenue/weeks')
async def revenue_weeks(request: Request, start: date | None = None, end: date | None = None,
                        days: int | None = None):
    """Выручка Retro за выбранный период, столько же дней раньше и те же дни год назад.

    Период — `start`–`end`; без `start` — `days` дней (по умолчанию 7) по `end`
    (по умолчанию вчера). `end` в будущем недоступен; `end` = сегодня можно —
    тогда последний день помечен как идущий и это сказано в предупреждениях."""
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
    for first, last in spans(start, end):
        known = await asyncio.to_thread(sales.store.states, first, last)
        if sales.sync is not None:
            sales.sync.want([first + timedelta(days=offset) for offset in range((last - first).days, -1, -1)
                             if first + timedelta(days=offset) not in known])
        refresh_error = await sales.refresh_today(first, last) or refresh_error
        states.update(await asyncio.to_thread(sales.store.states, first, last))
    return build_report(start, end, states, today=today, history_start=sales.settings.sales_history_start,
                        configured=sales.sync is not None, refresh_error=refresh_error)
