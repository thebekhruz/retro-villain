"""API закупа. Пишет только журнал покупок: касса тут не задействована.

Наличные ушли из кассы один раз, когда бухгалтер выдал подотчёт. Поэтому
«на руках у Шоха» = выдано бухгалтером − записанные покупки, а бухгалтерский
подотчёт уменьшается отдельно, когда бухгалтер принимает накладную.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from retro.modules.cashier.service import TZ, today_tashkent
from .gamification import (level_for, purchase_xp, quests, spent, streak, total_xp,
                           trip_bonus, trip_minutes, week_marks, FAST_TRIP_MINUTES)
from .store import MAX_PHOTO_BYTES, ShokhError, UNITS

router = APIRouter(prefix='/api/shokh')

# Раньше этой даты закупа в системе не было; служит нижней границей выборок.
FIRST_DAY = date(2020, 1, 1)


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
    """Сколько наличных у Шоха на руках.

    Бухгалтерский подотчёт (`reserves.shoh.balance`) = выдано − принятые
    накладные. Покупки, которые Шох записал, но бухгалтер ещё не принял, из
    подотчёта не вычтены, хотя денег на руках уже нет. Поэтому:

        на руках = подотчёт по бухгалтерии − непринятые покупки

    Так обходимся теми данными, что уже отдаёт карточка «Баланс Шох», и два
    экрана не спорят о сумме.
    """
    store = request.app.state.shokh
    accounting = request.app.state.accountant_finance.reserves(day)['shoh']['balance']
    # Непринятым может быть и вчерашнее, поэтому смотрим всю историю до дня.
    history = store.purchases_between(FIRST_DAY, day)
    pending = sum((Decimal(row['total']) for row in history if row['accepted_at'] is None),
                  Decimal(0))
    return dict(accounting_balance=accounting,
                pocket=None if accounting is None else str(Decimal(accounting) - pending),
                pending=str(pending))


@router.get('/home')
def home(request: Request, date_: date | None = Query(None, alias='date')):
    day = _day(date_)
    store = request.app.state.shokh
    today_rows = store.purchases(day)
    trips = store.trips(day)
    history = store.purchases_between(day - timedelta(days=60), day)
    days = {row['day'] for row in history}
    xp = total_xp(history, [trip for trip in trips])
    return dict(
        demo=False, date=day.isoformat(),
        **_pocket(request, day),
        purchases=today_rows,
        spent_today=str(spent(today_rows)),
        level=level_for(xp),
        streak=streak({date.fromisoformat(value) for value in days}, day),
        week=week_marks({date.fromisoformat(value) for value in days}, day),
        quests=quests(today_rows, trips),
        trips=[dict(trip, minutes=trip_minutes(trip), bonus=trip_bonus(trip)) for trip in trips],
        fast_trip_minutes=FAST_TRIP_MINUTES)


@router.get('/catalog')
def catalog(request: Request):
    store = request.app.state.shokh
    return dict(points=store.points(), units=list(UNITS),
                items=store.frequent_items(today=today_tashkent()))


@router.post('/trip', status_code=201)
def start_trip(request: Request, date_: date | None = Query(None, alias='date')):
    day = _day(date_)
    return dict(trip_id=request.app.state.shokh.open_trip(day, _now()), date=day.isoformat())


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
                purchases=rows, spent=str(spent(rows)),
                xp=sum(purchase_xp(row)['total'] for row in rows) + trip_bonus(trip))


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
