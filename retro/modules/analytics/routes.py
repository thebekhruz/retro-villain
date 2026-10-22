"""API модуля «Аналитика» (T-524): `/api/analytics/…`.

Доступ — директор, учредитель и полный доступ (`retro.app.SHARED_PANELS`);
остальным путь закрывает security_middleware. Только чтение.
"""
import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request

from retro.modules.cashier.service import today_tashkent

from .service import build_report, spans

router = APIRouter(prefix='/api/analytics', tags=['analytics'])


@router.get('/revenue/weeks')
async def revenue_weeks(request: Request, end: date | None = None):
    """Выручка Retro за 7 дней по `end` (по умолчанию — вчера), неделей раньше и год назад.

    `end` в будущем недоступен; `end` = сегодня можно — тогда последний день
    помечен как идущий и это сказано в предупреждениях."""
    today = today_tashkent()
    end = end or today - timedelta(days=1)
    if end > today:
        raise HTTPException(422, 'Будущие даты недоступны.')
    sales = request.app.state.sales
    # Сегодня — свежее двух минут, пропущенные дни — в очередь фоновой загрузки;
    # ошибка освежения не роняет ответ, а попадает в предупреждения. Год назад —
    # отдельным отрезком: промежуток между окнами не нужен и не грузится.
    refresh_error = None
    states = {}
    # Сначала свежие дни: фон берёт очередь по порядку, а смотреть хочется на
    # последнюю неделю, а не на неделю раньше, которая стоит в отрезке первой.
    for first, last in spans(end):
        known = await asyncio.to_thread(sales.store.states, first, last)
        if sales.sync is not None:
            sales.sync.want([first + timedelta(days=offset) for offset in range((last - first).days, -1, -1)
                             if first + timedelta(days=offset) not in known])
        refresh_error = await sales.refresh_today(first, last) or refresh_error
        states.update(await asyncio.to_thread(sales.store.states, first, last))
    return build_report(end, states, today=today, history_start=sales.settings.sales_history_start,
                        configured=sales.sync is not None, refresh_error=refresh_error)
