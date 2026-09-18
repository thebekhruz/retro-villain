import asyncio
from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from .export import export_report
from .service import DataError, demo_snapshot, today_tashkent

router = APIRouter(prefix='/api/cashier', tags=['cashier'])


class ExpenseInput(BaseModel):
    date: date
    description: str
    amount: str


class ReceiptInput(ExpenseInput):
    pass


class UsdBalanceInput(BaseModel):
    date: date
    amount: str


def selected_day(value):
    day = value or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    return day


@router.get('/expenses')
def list_expenses(request: Request, date: date):
    day = selected_day(date)
    expenses = request.app.state.expenses.list(day)
    return dict(date=day.isoformat(), expenses=[item.json() for item in expenses],
                total=str(sum((item.amount for item in expenses), 0)))


@router.get('/usd-rate')
async def usd_rate(request: Request, date: date):
    day = selected_day(date)
    try:
        return (await request.app.state.usd_rates.get(day)).json()
    except DataError as error:
        raise HTTPException(503, str(error)) from None


@router.get('/usd-balance')
def usd_balance(request: Request, date: date):
    return request.app.state.usd_rates.balance(selected_day(date))


@router.post('/usd-balance', status_code=201)
def save_usd_balance(request: Request, body: UsdBalanceInput):
    try:
        return request.app.state.usd_rates.save_balance(selected_day(body.date), body.amount)
    except DataError as error:
        raise HTTPException(422, str(error)) from None


@router.post('/expenses', status_code=201)
def add_expense(request: Request, body: ExpenseInput):
    day = selected_day(body.date)
    try:
        return request.app.state.expenses.add(day, body.description, body.amount).json()
    except DataError as error:
        raise HTTPException(422, str(error)) from None


@router.delete('/expenses/{expense_id}', status_code=204)
def delete_expense(request: Request, expense_id: int, date: date):
    day = selected_day(date)
    if not request.app.state.expenses.delete(expense_id, day):
        raise HTTPException(404, 'Расход не найден для выбранного дня.')
    return Response(status_code=204)


@router.get('/receipts')
def list_receipts(request: Request, date: date):
    day = selected_day(date)
    receipts = request.app.state.expenses.list_receipts(day)
    return dict(date=day.isoformat(), receipts=[item.json() for item in receipts],
                total=str(sum((item.amount for item in receipts), 0)))


@router.post('/receipts', status_code=201)
def add_receipt(request: Request, body: ReceiptInput):
    day = selected_day(body.date)
    try:
        return request.app.state.expenses.add_receipt(day, body.description, body.amount).json()
    except DataError as error:
        raise HTTPException(422, str(error)) from None


@router.delete('/receipts/{receipt_id}', status_code=204)
def delete_receipt(request: Request, receipt_id: int, date: date):
    day = selected_day(date)
    if not request.app.state.expenses.delete_receipt(receipt_id, day):
        raise HTTPException(404, 'Поступление не найдено для выбранного дня.')
    return Response(status_code=204)


@router.get('/day')
async def day_report(request: Request, date: date | None = None, demo: bool = False):
    day = selected_day(date)
    state = request.app.state
    try:
        if demo:
            result = demo_snapshot(day)
        else:
            if state.iiko_lock.locked():
                raise HTTPException(429, 'Другой отчёт ещё загружается. Повторите через несколько секунд.')
            async with state.iiko_lock:
                result = await asyncio.wait_for(state.iiko.load(day), timeout=90)
        state.cache.put(result)
        return result.json()
    except TimeoutError:
        raise HTTPException(504, 'iiko отвечает дольше обычного. Повторите запрос.') from None
    except DataError as error:
        raise HTTPException(503, str(error)) from None


@router.get('/export')
def download_report(request: Request, date: date,
                    snapshot_id: str = Query(min_length=32, max_length=32, pattern='^[a-f0-9]+$')):
    day = selected_day(date)
    try:
        snapshot = request.app.state.cache.get(snapshot_id, day)
    except DataError as error:
        raise HTTPException(409, str(error)) from None
    expenses = [] if snapshot.demo else request.app.state.expenses.list(day)
    receipts = [] if snapshot.demo else request.app.state.expenses.list_receipts(day)
    data = export_report(snapshot, expenses, receipts)
    prefix = 'DEMO-' if snapshot.demo else ''
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{prefix}Retro-{day.isoformat()}.xlsx"'})
