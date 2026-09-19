import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.founder.bookings import build_booking_analytics
from retro.modules.founder.models import DIRECTIONS, GRANULARITIES


router = APIRouter(prefix='/api/founder', tags=['founder'])


def _validated_period(start, end, granularity):
    today = today_tashkent()
    if start is None and end is None:
        end = today - timedelta(days=1)
        start = end - timedelta(days=29)
    elif start is None or end is None:
        raise HTTPException(422, 'Укажите обе даты периода.')
    if start > end:
        raise HTTPException(422, 'Дата начала должна быть не позже даты конца.')
    if end > today:
        raise HTTPException(422, 'Будущие даты недоступны.')
    if (end - start).days >= 366:
        raise HTTPException(422, 'Период не может быть длиннее 366 дней.')
    if granularity not in GRANULARITIES:
        raise HTTPException(422, 'Неизвестная детализация.')
    return start, end


@router.get('/analytics')
async def analytics(
        request: Request,
        start: date | None = None,
        end: date | None = None,
        granularity: str = Query('day'),
        directions: str = Query(','.join(DIRECTIONS)),
):
    start, end = _validated_period(start, end, granularity)
    selected = tuple(item.strip() for item in directions.split(',') if item.strip())
    if not selected or len(set(selected)) != len(selected) or any(item not in DIRECTIONS for item in selected):
        raise HTTPException(422, 'Выберите известные направления без повторов.')
    try:
        async with request.app.state.iiko_lock:
            return await asyncio.wait_for(
                request.app.state.iiko.load_founder_analytics(
                    start, end, granularity, selected), timeout=90)
    except TimeoutError as error:
        log_safe_failure('founder-route', error, operation='analytics',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'iiko формирует аналитику слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('founder-route', error, operation='analytics',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


@router.get('/bookings')
async def bookings(
        request: Request,
        start: date | None = None,
        end: date | None = None,
        granularity: str = Query('day'),
):
    start, end = _validated_period(start, end, granularity)
    try:
        raw = await asyncio.wait_for(request.app.state.bookings.load(start, end), timeout=20)
        return build_booking_analytics(raw, start, end, granularity)
    except TimeoutError as error:
        log_safe_failure('founder-route', error, operation='bookings',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'API бронирований отвечает слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('founder-route', error, operation='bookings',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None
