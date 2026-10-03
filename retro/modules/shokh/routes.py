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
    # Бухгалтер уже оплатил этим поставщикам переводом: наличными им не платить.
    # На деньги у Шоха на руках перечисления не влияют.
    transfers = request.app.state.accountant_finance.supplier_transfers(day)
    return dict(
        demo=False, date=day.isoformat(),
        **_pocket(request, day),
        purchases=request.app.state.shokh_sync.decorate(today_rows),
        spent_today=str(spent(today_rows)),
        transfers=transfers,
        transfers_total=str(sum((Decimal(row['amount']) for row in transfers), Decimal(0))),
        trips=[dict(trip, minutes=trip_minutes(trip)) for trip in trips])


def _history(request: Request) -> dict:
    """История покупок для «Часто покупаете», обычной цены и подстановки
    поставщика и склада по точке. Сливает её с товарами iiko экран
    (ShokhLogic.withHistory) — одно правило и после загрузки справочников, и
    после каждой покупки, когда экран берёт свежую историю через /history."""
    store = request.app.state.shokh
    defaults = store.point_defaults()
    return dict(history=store.item_history(today=today_tashkent()), point_defaults=defaults)


@router.get('/history')
def history(request: Request):
    return _history(request)


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
    known = await asyncio.to_thread(_history, request)
    return dict(result, points=points, **known,
                units=sorted({r['unit'] for r in result['items']}),
                # Для товара не из справочника iiko единицу выбирают сами.
                custom_units=list(UNITS))


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
                purchases=request.app.state.shokh_sync.decorate(rows), spent=str(spent(rows)))


@router.post('/trip/{trip_id}/close')
def close_trip(request: Request, trip_id: int):
    """× во время закупа: есть покупки — закуп завершается, нет — отменяется.

    Пустую поездку удаляем: иначе следующий «Новый закуп» продолжил бы её, и
    таймер начался бы с часа, когда Шох просто открыл и закрыл экран."""
    store = request.app.state.shokh
    trip = store.trip(trip_id)
    if trip is None:
        raise HTTPException(404, 'Закуп не найден.')
    if trip['finished_at'] is None and store.cancel_trip(trip_id):
        return dict(cancelled=True)
    return dict(finish_trip(request, trip_id), cancelled=False)


@router.post('/purchase', status_code=201)
async def add_purchase(request: Request,
                       point: str = Form(...), item: str = Form(...), unit: str = Form(...),
                       quantity: str = Form(...), price: str = Form(...),
                       trip_id: int | None = Form(None),
                       operation_id: str | None = Form(None),
                       product_id: str | None = Form(None), supplier_id: str | None = Form(None),
                       storage_id: str | None = Form(None), unit_id: str | None = Form(None),
                       off_catalog: bool = Form(False),
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
        if off_catalog:
            # Товара нет в справочнике iiko: покупку записываем у себя без
            # накладной. Бухгалтер видит её с пометкой «Нет в iiko», заводит
            # товар и проводит накладную руками, потом принимает покупку.
            row = await asyncio.to_thread(_off_catalog_purchase, request, day, point=point, item=item,
                unit=unit, quantity=quantity, price=price, trip_id=trip_id, key=operation_id,
                supplier_id=supplier_id, storage_id=storage_id, photo=content, photo_type=content_type)
            return dict(purchase=row, **await asyncio.to_thread(_pocket, request, day))
        if request.app.state.settings.configured:
            row = await request.app.state.shokh_sync.purchase(
                key=operation_id, day=day, at=_now(), product_id=product_id,
                supplier_id=supplier_id, storage_id=storage_id, unit_id=unit_id,
                point=point, quantity=quantity, price=price, trip_id=trip_id,
                photo=content, photo_type=content_type)
            result = dict(purchase=row,
                          **await asyncio.to_thread(_pocket, request, day))
            return JSONResponse(result, status_code=201 if row['iiko']['status'] == 'synced' else 202)
        row = request.app.state.shokh.add_purchase(
            day, _now(), point=point, item=item, unit=unit, quantity=quantity, price=price,
            photo=content, photo_type=content_type, trip_id=trip_id)
    except ShokhError as error:
        _fail(error)
    except DataError as error:
        raise HTTPException(503, str(error)) from None
    return dict(purchase=row, **_pocket(request, day))


def _off_catalog_purchase(request: Request, day: date, *, point, item, unit, quantity, price,
                          trip_id, key, supplier_id, storage_id, photo, photo_type):
    from uuid import UUID
    try:
        key = str(UUID(key))
    except (ValueError, TypeError, AttributeError):
        raise ShokhError('Не указан корректный ключ покупки. Обновите страницу.') from None
    # Поставщик и склад, выбранные на шаге точки: бухгалтеру для накладной и
    # подстановке по точке в следующий раз. Названия — из справочника в памяти,
    # за ними в iiko не ходим.
    cached = getattr(request.app.state.shokh_iiko, '_catalog', None) or {}
    def name(rows, id_):
        return next((row['name'] for row in rows or () if row['id'] == id_), None)
    refs = {key_: value for key_, value in dict(
        supplier_id=supplier_id or None, storage_id=storage_id or None,
        supplier=name(cached.get('suppliers'), supplier_id),
        storage=name(cached.get('storages'), storage_id)).items() if value}
    row = request.app.state.shokh.add_purchase(
        day, _now(), point=point, item=item, unit=unit, quantity=quantity, price=price,
        photo=photo, photo_type=photo_type, trip_id=trip_id, off_catalog=True,
        client_key=key, refs=refs or None)
    return request.app.state.shokh_sync.decorate([row])[0]


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
    if operation:
        return dict(purchase=request.app.state.shokh_sync.result(operation['purchase_id']))
    # Покупка не из справочника iiko хранит ключ у себя, без операции iiko.
    found = request.app.state.shokh.purchase_by_key(key)
    return dict(purchase=request.app.state.shokh_sync.decorate([found])[0] if found else None)
