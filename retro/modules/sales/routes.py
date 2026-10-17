"""Продажи за период и часть дня: директор (`/api/director/sales`) и учредитель (`/api/founder/sales`).

Права — как у соседних эндпоинтов: путь решает панель (`retro.app.panel_for_path`),
полный доступ есть у admin. Граница учёта 13.10 сюда не относится: это живая
аналитика iiko, как отчёт директора за 3/7/30 дней (T-486).
"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, today_tashkent

from .service import make_window

director_router = APIRouter(prefix='/api/director/sales', tags=['sales'])
founder_router = APIRouter(prefix='/api/founder/sales', tags=['sales'])
TIME = r'^\d{1,2}(:\d{2})?$'


async def sales(request: Request, start: date | None = None, end: date | None = None,
                from_hour: str | None = Query(None, pattern=TIME), to_hour: str | None = Query(None, pattern=TIME),
                window: Literal['lunch'] | None = None, basis: Literal['close', 'open'] = 'close',
                direction: Literal['all', 'retro', 'school', 'banquet'] = 'all',
                top: int = Query(10, ge=0, le=50), sort: Literal['quantity', 'revenue'] = 'quantity',
                all_items: bool = False):
    service = request.app.state.sales
    try:
        selected = make_window(from_hour, to_hour, lunch=service.lunch if window == 'lunch' else None,
                               basis=basis)
        return await service.summary(start, end, window=selected, direction=direction, top=top, sort=sort,
                                     include_non_dishes=all_items)
    except (DataError, ValueError) as error:
        raise HTTPException(422, str(error)) from None


async def sales_status(request: Request):
    return await request.app.state.sales.status()


async def sales_reconcile(request: Request, date: date | None = None):
    day = date or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    return await request.app.state.sales.reconcile(day)


async def sales_sync(request: Request, date: date | None = None):
    """Перечитать день из iiko сейчас — после правки задним числом."""
    day = date or today_tashkent()
    sync = request.app.state.sales.sync
    if sync is None:
        raise HTTPException(503, 'Подключение iiko не настроено. Продажи загрузить неоткуда.')
    if day > today_tashkent() or day < sync.history_start:
        raise HTTPException(422, f'Продажи хранятся с {sync.history_start:%d.%m.%Y} по сегодня.')
    if day in sync.jobs:
        raise HTTPException(429, 'Этот день уже загружается. Подождите.')
    try:
        return await sync.start_day(day, reason='manual')
    except DataError as error:
        log_safe_failure('sales-route', error, operation='sync', request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


for router in (director_router, founder_router):
    router.add_api_route('', sales, methods=['GET'])
    router.add_api_route('/status', sales_status, methods=['GET'])
    router.add_api_route('/reconcile', sales_reconcile, methods=['GET'])
director_router.add_api_route('/sync', sales_sync, methods=['POST'])
