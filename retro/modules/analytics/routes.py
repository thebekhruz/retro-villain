"""API модуля «Аналитика» (T-524): `/api/analytics/…`.

Доступ — директор, учредитель и полный доступ (`retro.app.SHARED_PANELS`);
остальным путь закрывает security_middleware. Только чтение.
"""
import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Request

from retro.modules.cashier.service import today_tashkent

from .service import build_report, span

router = APIRouter(prefix='/api/analytics', tags=['analytics'])


@router.get('/revenue/weeks')
async def revenue_weeks(request: Request, end: date | None = None):
    """Выручка за 7 дней по `end` (по умолчанию — вчера) и две семидневки до них.

    `end` в будущем недоступен; `end` = сегодня можно — тогда последний день
    помечен как идущий и это сказано в предупреждениях."""
    today = today_tashkent()
    end = end or today - timedelta(days=1)
    if end > today:
        raise HTTPException(422, 'Будущие даты недоступны.')
    sales = request.app.state.sales
    first, last = span(end)
    # Сегодня — свежее двух минут, пропущенные дни — в очередь фоновой загрузки;
    # ошибка освежения не роняет ответ, а попадает в предупреждения.
    refresh_error = await sales.refresh_today(first, last)
    states = await asyncio.to_thread(sales.store.states, first, last)
    return build_report(end, states, today=today, history_start=sales.settings.sales_history_start,
                        configured=sales.sync is not None, refresh_error=refresh_error)
