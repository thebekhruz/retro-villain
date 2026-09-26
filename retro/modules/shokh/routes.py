"""API закупа. Пишет только журнал покупок: касса тут не задействована.

Наличные ушли из кассы один раз, когда бухгалтер выдал подотчёт. Поэтому
«на руках у Шоха» = выдано бухгалтером − записанные покупки, а бухгалтерский
подотчёт уменьшается отдельно, когда бухгалтер принимает накладную.
"""
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from retro.modules.cashier.service import TZ, today_tashkent
from .trips import spent, trip_minutes
from .store import MAX_PHOTO_BYTES, ShokhError, UNITS, pocket_position

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
    return dict(
        demo=False, date=day.isoformat(),
        **_pocket(request, day),
        purchases=today_rows,
        spent_today=str(spent(today_rows)),
        trips=[dict(trip, minutes=trip_minutes(trip)) for trip in trips])


@router.get('/catalog')
def catalog(request: Request):
    store = request.app.state.shokh
    return dict(points=store.points(), units=list(UNITS),
                items=store.frequent_items(today=today_tashkent()))


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
    return dict(trip=dict(trip, minutes=trip_minutes(trip)),
                purchases=rows, spent=str(spent(rows)))


@router.post('/purchase', status_code=201)
async def add_purchase(request: Request,
                       point: str = Form(...), item: str = Form(...), unit: str = Form(...),
                       quantity: str = Form(...), price: str = Form(...),
                       trip_id: int | None = Form(None),
                       date_: date | None = Form(None, alias='date'),
                       photo: UploadFile | None = File(None)):
    day = _day(date_)
    content, content_type = None, None
    if photo is not None and photo.filename:
        content = await photo.read()
        # Читаем с запасом в один байт, чтобы поймать превышение, а не обрезать.
        if len(content) > MAX_PHOTO_BYTES:
            raise HTTPException(413, 'Фото больше 6 МБ — переснимите поменьше.')
        content_type = photo.content_type
    try:
        row = request.app.state.shokh.add_purchase(
            day, _now(), point=point, item=item, unit=unit, quantity=quantity, price=price,
            photo=content, photo_type=content_type, trip_id=trip_id)
    except ShokhError as error:
        _fail(error)
    return dict(purchase=row, **_pocket(request, day))


@router.get('/photo/{purchase_id}')
def photo(request: Request, purchase_id: int):
    found = request.app.state.shokh.photo(purchase_id)
    if found is None:
        raise HTTPException(404, 'Фото не приложено.')
    content, media_type = found
    # Фото — часть финансового документа: в общий кеш его не отдаём.
    return Response(content, media_type=media_type,
                    headers={'Cache-Control': 'private, no-store'})
