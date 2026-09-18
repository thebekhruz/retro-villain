import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from retro.modules.cashier.service import DataError, today_tashkent

router = APIRouter(prefix='/api/director', tags=['director'])


@router.get('/today')
async def today(request: Request):
    try:
        async with request.app.state.iiko_lock:
            snapshot = await request.app.state.iiko.load_director_report(today_tashkent())
        return snapshot.json()
    except DataError as error:
        raise HTTPException(503, str(error)) from None


@router.get('/reports')
def reports(request: Request):
    return {'reports': request.app.state.director_store.list()}


@router.post('/reports', status_code=201)
async def create_report(request: Request):
    if request.app.state.director_lock.locked():
        raise HTTPException(429, 'Другой анализ уже формируется.')
    try:
        async with request.app.state.director_lock:
            return await asyncio.wait_for(request.app.state.director_service.generate(today_tashkent()), timeout=180)
    except TimeoutError:
        raise HTTPException(504, 'Анализ формируется слишком долго. Повторите позже.') from None
    except DataError as error:
        raise HTTPException(503, str(error)) from None


@router.get('/reports/{report_id}')
def report(request: Request, report_id: str):
    result = request.app.state.director_store.get(report_id)
    if result is None:
        raise HTTPException(404, 'Отчёт не найден.')
    return result


@router.get('/reports/{report_id}/pdf')
def pdf(request: Request, report_id: str):
    value = request.app.state.director_store.get_pdf(report_id)
    if value is None:
        raise HTTPException(404, 'Отчёт не найден.')
    return Response(value, media_type='application/pdf', headers={'Content-Disposition': 'attachment; filename="Retro-director-report.pdf"'})
