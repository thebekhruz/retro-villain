import asyncio
from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.accountant.payroll import draft_payroll

router = APIRouter(prefix='/api/director', tags=['director'])


@router.get('/attendance')
def attendance(request: Request, date: date | None = None):
    """Return read-only Hikvision attendance without payroll amounts."""
    day = date or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    roster = request.app.state.accountant_roster.list()
    snapshot = request.app.state.attendance.snapshot(day, roster)
    rows = draft_payroll(day, roster, set(), snapshot.rows)
    arrived = [row for row in rows if row.status in ('on_time', 'late')]
    late = [row for row in rows if row.status == 'late']
    return {
        'demo': False,
        'date': day.isoformat(),
        'source': 'Hikvision ISAPI',
        'attendance': snapshot.health,
        'roster_count': len(rows),
        'arrived_count': len(arrived),
        'late_count': len(late),
        'missing_count': sum(row.status == 'missing' for row in rows),
        'unavailable_count': sum(row.status == 'unavailable' for row in rows),
        'employees': [dict(employee_id=row.employee_id, name=row.name, role=row.role,
                           group=row.group_name, status=row.status,
                           first_entry=row.occurred_at.isoformat() if row.occurred_at else None,
                           demo=False) for row in rows],
    }


@router.get('/today')
async def today(request: Request):
    try:
        async with request.app.state.iiko_lock:
            snapshot = await request.app.state.iiko.load_director_report(today_tashkent())
        return snapshot.json()
    except DataError as error:
        log_safe_failure('director-route', error, operation='today',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


@router.get('/reports')
def reports(request: Request):
    return {'reports': request.app.state.director_store.list_metadata()}


@router.post('/reports', status_code=201)
async def create_report(request: Request):
    if request.app.state.director_lock.locked():
        raise HTTPException(429, 'Другой анализ уже формируется.')
    try:
        async with request.app.state.director_lock:
            return await asyncio.wait_for(request.app.state.director_service.generate(today_tashkent()), timeout=180)
    except TimeoutError as error:
        log_safe_failure('director-route', error, operation='create_report',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'Анализ формируется слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('director-route', error, operation='create_report',
                         request_id=request.state.request_id)
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
