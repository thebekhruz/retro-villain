import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from uuid import UUID

from retro.report_cache import load_iiko
from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.founder.bookings import build_booking_analytics
from retro.modules.founder.models import DIRECTIONS, GRANULARITIES
from retro.modules.founder.tools import FounderChatTools
from retro.integrations.broadcasts import BroadcastConflict
from retro.modules.director.models import completed_period
from retro.modules.founder import cabinet, overview
from retro.modules.founder.dividends import DividendTargetError
from retro.modules.founder.export import month_workbook


router = APIRouter(prefix='/api/founder', tags=['founder'])


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class BroadcastInput(BaseModel):
    operation_id: UUID
    text: str = Field(min_length=1, max_length=4096)
    recipient_ids: list[str] = Field(min_length=1, max_length=2000)


def _chat_owner(request):
    owner = getattr(request.state, 'dashboard_user', None)
    if not owner:
        raise HTTPException(401, 'Для чата требуется вход.')
    return owner


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
        refresh: bool = False,
):
    start, end = _validated_period(start, end, granularity)
    selected = tuple(item.strip() for item in directions.split(',') if item.strip())
    if not selected or len(set(selected)) != len(selected) or any(item not in DIRECTIONS for item in selected):
        raise HTTPException(422, 'Выберите известные направления без повторов.')
    try:
        data = await load_iiko(request.app.state, 'load_founder_analytics',
                               start, end, granularity, selected, refresh=refresh,
                               request=request, timeout=180)
        if not isinstance(data.get('pnl'), dict) or 'net_profit' not in data['pnl']:
            return data
        cashier, accountant = await asyncio.gather(
            asyncio.to_thread(request.app.state.expenses.total_between, start, end),
            asyncio.to_thread(
                request.app.state.accountant_finance.expense_totals_between, start, end),
        )
        manual_total = cashier + accountant['total']
        result = dict(data)
        result['dashboard_expenses'] = {
            'cashier': str(cashier),
            'accountant_other': str(accountant['other']),
            'accountant_salary': str(accountant['salary']),
            'accountant': str(accountant['total']),
            'total': str(manual_total),
        }
        result['net_profit_after_dashboard_expenses'] = str(
            Decimal(data['pnl']['net_profit']) - manual_total)
        result['scope_note'] = data.get('scope_note', '') + (
            ' Чистая прибыль после расходов дэшборда равна чистой прибыли iiko '
            'минус расходы кассы и бухгалтерии за выбранные даты.')
        return result
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


@router.get('/broadcast/audience')
async def broadcast_audience(request: Request):
    try:
        return await asyncio.wait_for(request.app.state.broadcasts.audience(), timeout=20)
    except TimeoutError as error:
        log_safe_failure('founder-broadcast', error, operation='audience',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'API рассылок отвечает слишком долго.') from None
    except DataError as error:
        log_safe_failure('founder-broadcast', error, operation='audience',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


@router.post('/broadcast', status_code=202)
async def start_broadcast(request: Request, body: BroadcastInput):
    text = body.text.strip()
    if not text:
        raise HTTPException(422, 'Введите текст рассылки.')
    try:
        return await asyncio.wait_for(
            request.app.state.broadcasts.start(
                str(body.operation_id), text, body.recipient_ids), timeout=20)
    except BroadcastConflict as error:
        raise HTTPException(409, str(error)) from None
    except TimeoutError as error:
        log_safe_failure('founder-broadcast', error, operation='start',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'API рассылок отвечает слишком долго.') from None
    except DataError as error:
        log_safe_failure('founder-broadcast', error, operation='start',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


@router.get('/broadcast/{operation_id}')
async def broadcast_status(request: Request, operation_id: UUID):
    try:
        return await asyncio.wait_for(
            request.app.state.broadcasts.status(str(operation_id)), timeout=20)
    except TimeoutError as error:
        log_safe_failure('founder-broadcast', error, operation='status',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'API рассылок отвечает слишком долго.') from None
    except DataError as error:
        log_safe_failure('founder-broadcast', error, operation='status',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


@router.get('/chat')
def chat_history(request: Request):
    owner = _chat_owner(request)
    return {
        'configured': request.app.state.settings.claude_configured,
        'messages': request.app.state.founder_chat_store.list(owner),
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
    chat_tools = FounderChatTools(request.app)
    try:
        answer = await asyncio.wait_for(request.app.state.claude.chat(
            messages,
            tools=chat_tools.definitions,
            tool_handler=chat_tools.execute,
            current_date=today_tashkent().isoformat(),
        ), timeout=180)
    except TimeoutError as error:
        log_safe_failure('founder-chat', error, operation='answer',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'Claude отвечает слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('founder-chat', error, operation='answer',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None
    created_at = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(request.app.state.founder_chat_store.append_exchange,
                            owner, question, answer, created_at)
    return {'message': {'role': 'assistant', 'content': answer, 'created_at': created_at}}


@router.delete('/chat', status_code=204)
def clear_chat(request: Request):
    request.app.state.founder_chat_store.clear(_chat_owner(request))


# ── Кабинет учредителя (ui_solid1 7a/7b) ────────────────────────────────────

class DividendTargetInput(BaseModel):
    week: str = Field(min_length=8, max_length=8)
    amount: str = Field(min_length=1, max_length=20)


def _week_monday(week):
    try:
        return overview.parse_week(week)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.get('/dividends/weekly')
def dividend_target(request: Request, week: str | None = None):
    today = today_tashkent()
    if week is None:
        day = today
    else:
        monday = _week_monday(week)
        if monday > today:
            raise HTTPException(422, 'Будущая неделя ещё не началась.')
        # Для прошлой недели считаем по её воскресенью: неделя уже закрыта.
        day = min(today, monday + timedelta(days=6))
    return cabinet.dividend_summary(request.app.state, day)


@router.post('/dividends/weekly')
def set_dividend_target(request: Request, body: DividendTargetInput):
    monday = _week_monday(body.week)
    today = today_tashkent()
    if monday + timedelta(days=6) < today:
        raise HTTPException(422, 'Прошедшую неделю изменить нельзя: бухгалтер уже закрыл её.')
    if monday > today + timedelta(days=7):
        raise HTTPException(422, 'Цель ставится на текущую или следующую неделю.')
    try:
        request.app.state.dividend_targets.set(body.week, body.amount,
                                               getattr(request.state, 'dashboard_user', None))
    except DividendTargetError as error:
        raise HTTPException(422, str(error)) from None
    return cabinet.dividend_summary(request.app.state, max(today, monday))


@router.get('/day')
async def founder_day(request: Request, date: date | None = None):
    return await cabinet.founder_day(request, cabinet.selected_day(date))


@router.get('/week')
async def founder_week(request: Request, date: date | None = None):
    return await cabinet.founder_week(request, cabinet.selected_day(date))


@router.get('/forecast')
async def founder_forecast(request: Request, date: date | None = None):
    return await cabinet.founder_forecast(request, cabinet.selected_day(date))


@router.get('/chef-account')
async def chef_account(request: Request, date: date | None = None):
    return await cabinet.founder_chef(request, cabinet.selected_day(date))


@router.get('/spending')
def spending(request: Request, date: date | None = None):
    return cabinet.founder_spending(request.app.state, cabinet.selected_day(date))


@router.get('/dishes')
async def dishes(request: Request, days: int = Query(7, ge=1, le=30)):
    """Блюда за последние закрытые дни — тот же отчёт iiko, что у директора."""
    start, end = completed_period(today_tashkent(), days)
    snapshot, error = await cabinet.iiko_or_error(
        request, 'load_director_report', today_tashkent(), start=start, end=end,
        operation='dishes', timeout=150)
    if snapshot is None:
        raise HTTPException(503, error)
    return snapshot.json()


@router.get('/export/month')
async def export_month(request: Request, month: str | None = None):
    today = today_tashkent()
    try:
        first = date.fromisoformat((month or today.isoformat()[:7]) + '-01')
    except ValueError:
        raise HTTPException(422, 'Укажите месяц в виде ГГГГ-ММ.') from None
    if first > today:
        raise HTTPException(422, 'Будущий месяц ещё не начался.')
    last = min(today, cabinet.month_bounds(first)[1])
    rows, error = await cabinet.iiko_or_error(request, 'load_daily_orders', first, last,
                                              operation='export_month', timeout=120)
    orders = overview.register_days(rows) if rows is not None else None
    data = await asyncio.to_thread(month_workbook, request.app.state, first, last, orders, error)
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition':
                             f'attachment; filename="Retro-accountant-{first.isoformat()[:7]}.xlsx"'})
