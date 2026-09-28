"""API закупа: журнал покупок и приходные накладные iiko.

Наличные ушли из кассы один раз, когда бухгалтер выдал подотчёт. Поэтому
«на руках у Шоха» = выдано бухгалтером − записанные покупки, а бухгалтерский
подотчёт уменьшается отдельно, когда бухгалтер принимает накладную.
"""
import asyncio

from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, JSONResponse

from retro.modules.cashier.service import TZ, today_tashkent, DataError
from .gamification import (level_for, purchase_xp, quests, spent, streak, total_xp,
                           trip_bonus, trip_minutes, week_marks, FAST_TRIP_MINUTES)
from .store import MAX_PHOTO_BYTES, ShokhError, UNITS, pocket_position, FIRST_DAY

router = APIRouter(prefix='/api/shokh')


def _day(value: date | None) -> date:
    day = value or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    return day


def _now() -> datetime:
    return datetime.now(TZ)


def _fail(error: ShokhError):
    raise HTTPException(422, str(error)) from None


def _pocket(request: Request, day: date) -> dict:
    """Деньги на руках у Шоха. Формула — в store.pocket_position, одна на всех."""
    return pocket_position(request.app.state.shokh, request.app.state.accountant_finance, day)


@router.get('/home')
def home(request: Request, date_: date | None = Query(None, alias='date')):
    day = _day(date_)
    store = request.app.state.shokh
    today_rows = store.purchases(day)
    trips = store.trips(day)
    history = store.purchases_between(FIRST_DAY, day)
    days = {row['day'] for row in history}
    xp = total_xp(history, store.trips_between(FIRST_DAY, day))
    return dict(
        demo=False, date=day.isoformat(),
        **_pocket(request, day),
        purchases=request.app.state.shokh_sync.decorate(today_rows),
        spent_today=str(spent(today_rows)),
        level=level_for(xp),
        streak=streak({date.fromisoformat(value) for value in days}, day),
        week=week_marks({date.fromisoformat(value) for value in days}, day),
        quests=quests(today_rows, trips),
        trips=[dict(trip, minutes=trip_minutes(trip), bonus=trip_bonus(trip)) for trip in trips],
        fast_trip_minutes=FAST_TRIP_MINUTES)


@router.get('/catalog')
async def catalog(request: Request):
    store = request.app.state.shokh
    points, frequent = await asyncio.gather(asyncio.to_thread(store.points),
        asyncio.to_thread(store.frequent_items, today=today_tashkent()))
    if not request.app.state.settings.configured:
        return dict(source='local', can_create=False, points=points, units=list(UNITS),
                    suppliers=[], storages=[], items=frequent)
    try:
        result = dict(await request.app.state.shokh_iiko.catalog())
    except DataError as error:
        raise HTTPException(503, str(error)) from None
    result.pop('settings', None)
    history = {(r['item'], r['unit']): r for r in frequent}
    result['items'] = [dict(r, times=history.get((r['item'], r['unit']), {}).get('times', 0),
        usual_price=history.get((r['item'], r['unit']), {}).get('usual_price')) for r in result['items']]
    return dict(result, points=points, units=sorted({r['unit'] for r in result['items']}))


@router.post('/trip', status_code=201)
def start_trip(request: Request, date_: date | None = Query(None, alias='date')):
    day = _day(date_)
    store = request.app.state.shokh
    trip_id = store.open_trip(day, _now())
    # Незакрытый закуп дня продолжается, а не начинается заново. Время начала
    # отдаём клиенту: иначе таймер на экране шёл с нуля, а итог закупа считал
    # от настоящего начала — «00:06, успеваете», а потом «52:34, дольше плана».
    return dict(trip_id=trip_id, date=day.isoformat(),
                started_at=store.trip(trip_id)['started_at'])


@router.post('/trip/{trip_id}/finish')
def finish_trip(request: Request, trip_id: int):
    store = request.app.state.shokh
    if store.trip(trip_id) is None:
        raise HTTPException(404, 'Закуп не найден.')
    store.finish_trip(trip_id, _now())
    trip = store.trip(trip_id)
    rows = [row for row in store.purchases(date.fromisoformat(trip['day']))
            if row['trip_id'] == trip_id]
    return dict(trip=dict(trip, minutes=trip_minutes(trip), bonus=trip_bonus(trip)),
                purchases=request.app.state.shokh_sync.decorate(rows), spent=str(spent(rows)),
                xp=sum(purchase_xp(row)['total'] for row in rows) + trip_bonus(trip))


@router.post('/purchase', status_code=201)
async def add_purchase(request: Request,
                       point: str = Form(...), item: str = Form(...), unit: str = Form(...),
                       quantity: str = Form(...), price: str = Form(...),
                       trip_id: int | None = Form(None),
                       operation_id: str | None = Form(None),
                       product_id: str | None = Form(None), supplier_id: str | None = Form(None),
                       storage_id: str | None = Form(None), unit_id: str | None = Form(None),
                       date_: date | None = Form(None, alias='date'),
                       photo: UploadFile | None = File(None)):
    day = _day(date_)
    content, content_type = None, None
    if photo is not None and photo.filename:
        content = await photo.read(MAX_PHOTO_BYTES + 1)
        # Читаем с запасом в один байт, чтобы поймать превышение, а не обрезать.
        if len(content) > MAX_PHOTO_BYTES:
            raise HTTPException(413, 'Фото больше 6 МБ — переснимите поменьше.')
        content_type = photo.content_type
    try:
        if request.app.state.settings.configured:
            row = await request.app.state.shokh_sync.purchase(
                key=operation_id, day=day, at=_now(), product_id=product_id,
                supplier_id=supplier_id, storage_id=storage_id, unit_id=unit_id,
                point=point, quantity=quantity, price=price, trip_id=trip_id,
                photo=content, photo_type=content_type)
            result = dict(purchase=row, xp=purchase_xp(row),
                          **await asyncio.to_thread(_pocket, request, day))
            return JSONResponse(result, status_code=201 if row['iiko']['status'] == 'synced' else 202)
        row = request.app.state.shokh.add_purchase(
            day, _now(), point=point, item=item, unit=unit, quantity=quantity, price=price,
            photo=content, photo_type=content_type, trip_id=trip_id)
    except ShokhError as error:
        _fail(error)
    except DataError as error:
        raise HTTPException(503, str(error)) from None
    return dict(purchase=row, xp=purchase_xp(row), **_pocket(request, day))


@router.get('/photo/{purchase_id}')
def photo(request: Request, purchase_id: int):
    found = request.app.state.shokh.photo(purchase_id)
    if found is None:
        raise HTTPException(404, 'Фото не приложено.')
    content, media_type = found
    # Фото — часть финансового документа: в общий кеш его не отдаём.
    return Response(content, media_type=media_type,
                    headers={'Cache-Control': 'private, no-store'})


@router.get('/purchase/{purchase_id}/sync')
async def check_sync(request: Request, purchase_id: int):
    if await asyncio.to_thread(request.app.state.shokh.purchase, purchase_id) is None:
        raise HTTPException(404, 'Покупка не найдена.')
    try:
        return dict(purchase=await request.app.state.shokh_sync.reconcile(purchase_id))
    except ShokhError as error:
        _fail(error)


@router.post('/purchase/{purchase_id}/retry')
async def retry_sync(request: Request, purchase_id: int):
    operation = await asyncio.to_thread(request.app.state.shokh_sync.operation, purchase_id=purchase_id)
    if operation is None:
        raise HTTPException(404, 'Покупка iiko не найдена.')
    return dict(purchase=await request.app.state.shokh_sync.send(purchase_id, retry=True))


@router.get('/operation/{operation_id}')
def operation_status(request: Request, operation_id: str):
    from uuid import UUID
    try:
        key = str(UUID(operation_id))
    except ValueError:
        raise HTTPException(422, 'Некорректный ключ покупки.') from None
    operation = request.app.state.shokh_sync.operation(key=key)
    return dict(purchase=request.app.state.shokh_sync.result(operation['purchase_id']) if operation else None)
