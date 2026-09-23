import asyncio
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from uuid import UUID

from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.founder.bookings import build_booking_analytics
from retro.modules.founder.models import DIRECTIONS, GRANULARITIES
from retro.modules.founder.tools import FounderChatTools
from retro.integrations.broadcasts import BroadcastConflict


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
):
    start, end = _validated_period(start, end, granularity)
    selected = tuple(item.strip() for item in directions.split(',') if item.strip())
    if not selected or len(set(selected)) != len(selected) or any(item not in DIRECTIONS for item in selected):
        raise HTTPException(422, 'Выберите известные направления без повторов.')
    try:
        async with request.app.state.iiko_lock:
            return await asyncio.wait_for(
                request.app.state.iiko.load_founder_analytics(
                    start, end, granularity, selected), timeout=180)
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
    history = request.app.state.founder_chat_store.list(owner, limit=24)
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
    request.app.state.founder_chat_store.append_exchange(
        owner, question, answer, created_at)
    return {'message': {'role': 'assistant', 'content': answer, 'created_at': created_at}}


@router.delete('/chat', status_code=204)
def clear_chat(request: Request):
    request.app.state.founder_chat_store.clear(_chat_owner(request))
