import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from retro.report_cache import load_iiko
from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.accountant.payroll import ABSENT_STATUSES, PRESENT_STATUSES, draft_payroll
from retro.modules.accountant.roster import GROUPS, UNASSIGNED_GROUP, group_for
from retro.modules.director.models import resolve_period
from retro.modules.director.tools import DirectorChatTools

router = APIRouter(prefix='/api/director', tags=['director'])

# Завершившийся период в iiko сам не меняется: правки вносит человек, и для них
# есть кнопка обновления.
CLOSED_PERIOD_TTL = 15 * 60


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


def _chat_owner(request):
    owner = getattr(request.state, 'dashboard_user', None)
    if not owner:
        raise HTTPException(401, 'Для чата требуется вход.')
    return f'director:{owner}'


@router.get('/attendance')
def attendance(request: Request, date: date | None = None):
    """Return read-only Hikvision attendance without payroll amounts."""
    day = date or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    roster = request.app.state.accountant_roster.list(day)
    snapshot = request.app.state.attendance.snapshot(day, roster)
    rows = draft_payroll(day, roster, set(), snapshot.rows,
                         pay_unlinked=request.app.state.settings.check_mode)
    # Ручная отметка «был / не был» считается наравне с проходом Hikvision.
    arrived = [row for row in rows if row.status in PRESENT_STATUSES]
    late = [row for row in rows if row.status == 'late']
    return {
        'demo': False,
        'date': day.isoformat(),
        'source': 'Hikvision ISAPI',
        'attendance': snapshot.health,
        'roster_count': len(rows),
        'arrived_count': len(arrived),
        'late_count': len(late),
        'missing_count': sum(row.status in ABSENT_STATUSES for row in rows),
        'unavailable_count': sum(row.status == 'unavailable' for row in rows),
        'employees': [dict(employee_id=row.employee_id, name=row.name, role=row.role,
                           group=row.group_name, status=row.status,
                           first_entry=row.occurred_at.isoformat() if row.occurred_at else None,
                           demo=False) for row in rows],
    }


def period_or_422(start: date | None, end: date | None, days: int | None = None):
    """Проверенный период либо ранний 422 — до всякого обращения к iiko."""
    try:
        return resolve_period(today_tashkent(), start, end, days)
    except DataError as error:
        raise HTTPException(422, str(error)) from None


@router.get('/today')
async def today(request: Request, refresh: bool = False):
    try:
        snapshot = await load_iiko(request.app.state, 'load_director_report', today_tashkent(),
                                   refresh=refresh, request=request, timeout=150)
        return snapshot.json()
    except TimeoutError:
        raise HTTPException(504, 'iiko формирует отчёт слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('director-route', error, operation='today',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


@router.get('/report')
async def report_for_period(request: Request, start: date | None = None, end: date | None = None,
                            days: int | None = None, refresh: bool = False):
    start, end = period_or_422(start, end, days)
    try:
        # period_or_422 пропускает только завершившиеся дни, поэтому период
        # здесь всегда закрыт и сам по себе в iiko не меняется. Сырые отчёты за
        # месяц весят около 46 MB и в кэш по весу не помещаются — повтор держится
        # на готовом снимке, и пяти минут ему мало: архив и повторные открытия
        # заново выкачивали тот же сентябрь. Кнопка обновления обходит кэш.
        snapshot = await load_iiko(request.app.state, 'load_director_report', today_tashkent(),
                                   start=start, end=end, refresh=refresh, request=request,
                                   timeout=150, ttl=CLOSED_PERIOD_TTL)
        return snapshot.json()
    except TimeoutError:
        raise HTTPException(504, 'iiko формирует отчёт слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('director-route', error, operation='report',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


@router.get('/reports')
def reports(request: Request):
    return {'reports': request.app.state.director_store.list_metadata()}


@router.get('/chat')
def chat_history(request: Request):
    return {
        'configured': request.app.state.settings.claude_configured,
        'messages': request.app.state.founder_chat_store.list(_chat_owner(request)),
    }


@router.post('/chat')
async def chat(request: Request, body: ChatInput):
    owner = _chat_owner(request)
    question = body.message.strip()
    if not question:
        raise HTTPException(422, 'Напишите вопрос.')
    history = await asyncio.to_thread(request.app.state.founder_chat_store.list, owner, limit=24)
    messages = [{'role': item['role'], 'content': item['content']} for item in history]
    messages.append({'role': 'user', 'content': question})
    chat_tools = DirectorChatTools(request.app)
    try:
        answer = await asyncio.wait_for(request.app.state.claude.chat(
            messages, tools=chat_tools.definitions, tool_handler=chat_tools.execute,
            current_date=today_tashkent().isoformat(), audience='director'), timeout=180)
    except TimeoutError as error:
        log_safe_failure('director-chat', error, operation='answer',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'ИИ отвечает слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('director-chat', error, operation='answer',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None
    created_at = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(request.app.state.founder_chat_store.append_exchange,
                            owner, question, answer, created_at)
    return {'message': {'role': 'assistant', 'content': answer, 'created_at': created_at}}


@router.delete('/chat', status_code=204)
def clear_chat(request: Request):
    request.app.state.founder_chat_store.clear(_chat_owner(request))


@router.post('/reports', status_code=201)
async def create_report(request: Request, start: date | None = None, end: date | None = None,
                        days: int | None = None):
    if request.app.state.director_lock.locked():
        raise HTTPException(429, 'Другой анализ уже формируется.')
    start, end = period_or_422(start, end, days)
    try:
        async with request.app.state.director_lock:
            return await asyncio.wait_for(
                request.app.state.director_service.generate(today_tashkent(), start=start, end=end),
                timeout=180)
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


# ── Телефон директора (ui_solid1 6a) ────────────────────────────────────────
# Директор правит тот же реестр, что бухгалтер в «Сотрудниках»: отдельной
# копии нет, поэтому новая ставка сразу видна в «Финансах дня» и ведомости.

class TeamInput(BaseModel):
    type: Literal['shift', 'monthly']
    name: str = Field(min_length=1, max_length=160)
    role: str = Field(min_length=1, max_length=80)
    amount: str = Field(min_length=1, max_length=20)
    manual_attendance: bool = False


class TeamUpdateInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    role: str = Field(min_length=1, max_length=80)
    amount: str = Field(min_length=1, max_length=20)
    manual_attendance: bool | None = None


def _group(role, current=None):
    """Группа по должности. Незнакомая должность не сбрасывает группу,
    которую бухгалтер уже выбрал руками («Шашлычник» в «Кухне»)."""
    try:
        return group_for(role)
    except ValueError:
        return current or UNASSIGNED_GROUP


def _amount(value):
    return value.replace(' ', '').replace(' ', '')


def month_shift_totals(finance, day):
    """Смены сменного с 1-го числа по день D и выданное за них.

    Считаются начисленные смены, на которые человек пришёл (или отмечен «был»),
    выдано — выплаты по начислениям этих дней. Ведомость месяца читает те же
    начисления, поэтому числа сходятся с 2b."""
    data = finance.payroll_month(day.replace(day=1), day)
    shifts, paid = {}, {}
    for person in data['shift']:
        key = person['employee_id']
        shifts[key] = shifts.get(key, 0) + sum(cell['status'] in PRESENT_STATUSES
                                               for cell in person['cells'].values())
        paid[key] = paid.get(key, Decimal(0)) + Decimal(person['paid'])
    return shifts, paid


@router.get('/team')
def team(request: Request, date: date | None = None):
    day = date or today_tashkent()
    if day > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    state = request.app.state
    roster = state.accountant_roster.list(day)
    snapshot = state.attendance.snapshot(day, roster)
    rows = draft_payroll(day, roster, state.accountant_finance.exceptions_for_day(day), snapshot.rows,
                         pay_unlinked=state.settings.check_mode)
    by_id = {employee.id: employee for employee in roster}
    shifts_month, paid_month = month_shift_totals(state.accountant_finance, day)
    shift = [dict(row.json(), manual_attendance=by_id[row.employee_id].manual_attendance,
                  manual_since=(by_id[row.employee_id].manual_since.isoformat()
                                if by_id[row.employee_id].manual_since else None),
                  hikvision_registered=by_id[row.employee_id].hikvision_id is not None,
                  month_shifts=shifts_month.get(row.employee_id, 0),
                  month_paid=str(paid_month.get(row.employee_id, Decimal(0))))
             for row in rows]
    # Оклад: выдано за месяц — сумма частичных выплат с 1-го числа (3.5), тем
    # же расчётом, что у бухгалтера; остаток меньше нуля — переплата.
    from retro.modules.accountant.routes import monthly_payments_json

    monthly_paid = monthly_payments_json(state.accountant_finance, state.accountant_roster, day)['paid_by_employee']
    monthly = []
    for person in state.accountant_roster.list_monthly():
        paid = Decimal(monthly_paid.get(str(person.id), '0'))
        monthly.append(dict(person.json(), month_paid=str(paid), month_left=str(Decimal(str(person.salary)) - paid)))
    return dict(demo=False, date=day.isoformat(), attendance=snapshot.health,
                shift=shift, monthly=monthly, month=day.strftime('%Y-%m'),
                roles=sorted(set(GROUPS) | {'повар', 'кондитер'}),
                counts=dict(late=sum(row['status'] == 'late' for row in shift),
                            missing=sum(row['status'] in ABSENT_STATUSES for row in shift),
                            no_hikvision=sum(not row['hikvision_registered'] for row in shift)))


def _who(request: Request) -> str:
    """Кто меняет реестр — в историю сотрудника (1a)."""
    return getattr(request.state, 'dashboard_user', None) or 'директор'


@router.post('/team', status_code=201)
def add_team_member(request: Request, body: TeamInput):
    roster = request.app.state.accountant_roster
    try:
        if body.type == 'monthly':
            return dict(employee=roster.add_monthly(
                name=body.name, role=body.role, salary=_amount(body.amount),
                no_hikvision=body.manual_attendance, by=_who(request)).json())
        employee = roster.add(name=body.name, role=body.role, rate=_amount(body.amount),
                              group_name=_group(body.role), by=_who(request))
        if body.manual_attendance:
            employee = roster.set_manual_attendance(employee.id, True, by=_who(request))
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(employee=employee.json())


@router.patch('/team/shift/{employee_id}')
def update_shift_member(request: Request, employee_id: int, body: TeamUpdateInput):
    roster = request.app.state.accountant_roster
    current = next((person for person in roster.list() if person.id == employee_id), None)
    if current is None:
        raise HTTPException(404, 'Сотрудник не найден.')
    try:
        employee = roster.update(employee_id, name=body.name, role=body.role,
                                 rate=_amount(body.amount),
                                 group_name=_group(body.role, current.group_name),
                                 reason='Изменено директором', by=_who(request))
        if body.manual_attendance is not None and body.manual_attendance != employee.manual_attendance:
            employee = roster.set_manual_attendance(employee_id, body.manual_attendance, by=_who(request))
    except ValueError as error:
        raise HTTPException(404 if 'не найден' in str(error) else 422, str(error)) from None
    return dict(employee=employee.json())


@router.delete('/team/shift/{employee_id}', status_code=204)
def delete_shift_member(request: Request, employee_id: int):
    try:
        request.app.state.accountant_roster.delete(employee_id, by=_who(request))
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.patch('/team/monthly/{employee_id}')
def update_monthly_member(request: Request, employee_id: int, body: TeamUpdateInput):
    # Выплаты в реестре окладов ведёт бухгалтер — директор меняет только имя,
    # должность и оклад. Пишем одним UPDATE по трём полям, чтобы выплата,
    # записанная бухгалтером в ту же секунду, не затёрлась старыми цифрами.
    try:
        employee = request.app.state.accountant_roster.update_monthly_basics(
            employee_id, name=body.name, role=body.role, salary=_amount(body.amount), by=_who(request))
    except ValueError as error:
        raise HTTPException(404 if 'не найден' in str(error) else 422, str(error)) from None
    return dict(employee=employee.json())


@router.delete('/team/monthly/{employee_id}', status_code=204)
def delete_monthly_member(request: Request, employee_id: int):
    try:
        request.app.state.accountant_roster.delete_monthly(employee_id, by=_who(request))
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.get('/cash-today')
async def cash_today(request: Request):
    """Касса дня так, как её видит кассир: выручка по заведениям, чеки, «Демо»."""
    from retro.modules.founder.cabinet import cashier_day

    cashier, error = await cashier_day(request, today_tashkent())
    if cashier is None:
        raise HTTPException(503, error)
    return dict(date=today_tashkent().isoformat(), **cashier)


@router.get('/accounting/day')
async def accounting_day(request: Request, date: date | None = None):
    """Отчёт бухгалтера за день, только чтение: из него экран считает ошибки."""
    from retro.modules.accountant.routes import day_view

    return await day_view(request, date or today_tashkent())


@router.get('/accounting/staff')
def accounting_staff(request: Request, date: date | None = None):
    """Смена дня глазами бухгалтера, только чтение. Вместе с /accounting/day и
    покупками Шоха из неё считаются те же проверки, что в «Финансах дня» (2a)."""
    from retro.modules.accountant.routes import staff_view

    return staff_view(request, date or today_tashkent())


@router.get('/accounting/purchases')
def accounting_purchases(request: Request, date: date | None = None):
    """Покупки Шоха за день, только чтение: проверки фото и цены для 6a."""
    from retro.modules.accountant.routes import shokh_purchases

    return shokh_purchases(request, date or today_tashkent())
