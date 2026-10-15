"""API KPI официантов: менеджер смотрит, директор настраивает и закрывает период.

Доступ по панелям (app.py security_middleware): /api/manager/kpi — менеджер,
/api/director/kpi — директор; администратору открыты оба. Деньги премий
появляются у бухгалтера только через «Передать бухгалтеру»: это обычные
доп. выплаты (salary_extra_payout), без новой сущности.
"""
import asyncio
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from retro.logging_config import log_safe_failure
from retro.modules.accountant.ledger import LedgerError
from retro.modules.cashier.service import DataError, today_tashkent

from .export import kpi_workbook
from .rules import KpiError, normalize_scheme, normalize_targets
from .service import fill_margins, period_view, snapshot_for_close

manager_router = APIRouter(prefix='/api/manager/kpi', tags=['kpi'])
director_router = APIRouter(prefix='/api/director/kpi', tags=['kpi'])
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
PAYOUT_NOTE = 'KPI официантов {start}–{end}'


class SettingsInput(BaseModel):
    scheme: dict
    targets: list
    kind: str | None = None
    start: date | None = None
    end: date | None = None


class LinkInput(BaseModel):
    waiter: str
    employee_id: int | None = None
    active: bool = True


class CloseInput(BaseModel):
    withheld: dict[str, int] = {}


class PayoutsInput(BaseModel):
    paid_day: date | None = None
    confirm: bool = False


def _user(request: Request) -> str | None:
    return getattr(request.state, 'dashboard_user', None)


def _period_or_404(request: Request, period_id: int | None):
    store = request.app.state.kpi_store
    period = store.get(period_id) if period_id else store.ensure_current(today_tashkent(), by=_user(request))
    if period is None:
        raise HTTPException(404, 'Период KPI не найден.')
    return period


def _photo_url(employee):
    return f'/api/manager/employees/{employee["id"]}/photo?v={quote(employee["photo_updated_at"], safe="")}'


async def _view(request: Request, period_id, refresh, *, photos):
    period = _period_or_404(request, period_id)
    try:
        return await period_view(request.app.state, period, refresh=refresh,
                                 photo_url=_photo_url if photos else None)
    except TimeoutError:
        raise HTTPException(504, 'iiko формирует отчёт слишком долго. Повторите позже.') from None
    except DataError as error:
        log_safe_failure('kpi-route', error, operation='period_view', request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None
    except KpiError as error:
        raise HTTPException(422, str(error)) from None


@manager_router.get('')
async def manager_period(request: Request, period: int | None = None, refresh: bool = False):
    return await _view(request, period, refresh, photos=True)


@manager_router.get('/periods')
def manager_periods(request: Request):
    return dict(periods=_period_list(request))


def _period_list(request: Request):
    return [dict(id=p['id'], kind=p['kind'], start=p['start'], end=p['end'], status=p['status'],
                 closed_at=p['closed_at'], fund=(p['snapshot'] or {}).get('result', {}).get('fund'),
                 paid=bool(p['payouts']))
            for p in request.app.state.kpi_store.list()]


@director_router.get('')
async def director_period(request: Request, period: int | None = None, refresh: bool = False):
    return await _view(request, period, refresh, photos=False)


@director_router.get('/periods')
def director_periods(request: Request):
    return dict(periods=_period_list(request))


@director_router.get('/settings')
def settings(request: Request, period: int | None = None):
    current = _period_or_404(request, period)
    store = request.app.state.kpi_store
    cards = request.app.state.accountant_roster.manager_cards()
    return dict(period=dict(id=current['id'], kind=current['kind'], start=current['start'], end=current['end'],
                            status=current['status']),
                settings=current['settings'], links=store.links(),
                employees=[dict(id=card['id'], name=card['name'], role=card['role'], group=card['group'])
                           for card in cards if card['group'] in ('Обслуживание зала', 'Бар', 'Встреча гостей')])


@director_router.put('/settings')
async def save_settings(request: Request, body: SettingsInput, period: int | None = None):
    current = _period_or_404(request, period)
    try:
        scheme = normalize_scheme(body.scheme)
        targets = normalize_targets(body.targets)
        if body.kind and body.kind not in ('week', 'month'):
            raise KpiError('Период — неделя или месяц.')
        start = body.start or date.fromisoformat(current['start'])
        targets = await fill_margins(request.app.state, targets, start)
        saved = request.app.state.kpi_store.update_settings(
            current['id'], dict(scheme=scheme, targets=targets), by=_user(request),
            kind=body.kind, start=body.start, end=body.end)
    except KpiError as error:
        raise HTTPException(422, str(error)) from None
    except DataError as error:
        raise HTTPException(503, str(error)) from None
    return dict(period=dict(id=saved['id'], kind=saved['kind'], start=saved['start'], end=saved['end'],
                            status=saved['status']), settings=saved['settings'])


@director_router.get('/items')
def search_items(request: Request, q: str = ''):
    """Поиск карточек iiko по сохранённому меню панели (menu_store), до 20 строк."""
    query = (q or '').strip()
    if len(query) < 2:
        return dict(items=[])
    store = request.app.state.menu_store
    rows = store.items(scope='menu', query=query, limit=20)
    return dict(items=[dict(id=row['product_id'], name=row['name'], group=row.get('group_path') or '', price=row.get('price')) for row in rows])


@director_router.put('/links')
def save_link(request: Request, body: LinkInput):
    try:
        request.app.state.kpi_store.set_link(body.waiter, body.employee_id, active=body.active)
    except KpiError as error:
        raise HTTPException(422, str(error)) from None
    return dict(links=request.app.state.kpi_store.links())


@director_router.post('/close')
async def close_period(request: Request, body: CloseInput, period: int | None = None):
    current = _period_or_404(request, period)
    if current['status'] != 'open':
        raise HTTPException(409, 'Период уже закрыт.')
    view = await _view(request, current['id'], True, photos=False)
    snapshot = snapshot_for_close(view)
    snapshot['withheld'] = {k: int(v) for k, v in body.withheld.items()}
    try:
        closed = request.app.state.kpi_store.close(current['id'], snapshot, by=_user(request))
    except KpiError as error:
        raise HTTPException(409, str(error)) from None
    following = request.app.state.kpi_store.current(today_tashkent())
    return dict(period=dict(id=closed['id'], status=closed['status'], closed_at=closed['closed_at']),
                totals=view['totals'],
                next=dict(id=following['id'], start=following['start'], end=following['end']) if following else None)


@director_router.get('/export')
async def export(request: Request, period: int | None = None):
    current = _period_or_404(request, period)
    view = await _view(request, current['id'], False, photos=False)
    withheld = (current.get('snapshot') or {}).get('withheld') or {}
    body = await asyncio.to_thread(kpi_workbook, view, withheld=withheld)
    name = f"kpi-{current['start']}-{current['end']}.xlsx"
    return Response(body, media_type=XLSX, headers={'Content-Disposition': f'attachment; filename="{name}"'})


@director_router.post('/payouts')
async def payouts(request: Request, body: PayoutsInput, period: int | None = None):
    """Премии закрытого периода → доп. выплаты у бухгалтера по привязанным сотрудникам."""
    current = _period_or_404(request, period)
    if current['status'] != 'closed':
        raise HTTPException(409, 'Сначала закройте период.')
    if current.get('payouts'):
        raise HTTPException(409, 'Премии этого периода уже переданы бухгалтеру.')
    snapshot = current['snapshot']
    withheld = snapshot.get('withheld') or {}
    paid_day = body.paid_day or today_tashkent()
    finance = request.app.state.accountant_finance
    results = []
    for row in snapshot['result']['rows']:
        amount = max(int(row['premium']) - int(withheld.get(row['waiter'], 0)), 0)
        if amount <= 0:
            continue
        if not row.get('employee_id'):
            results.append(dict(waiter=row['waiter'], amount=amount, status='unlinked',
                                message='Логин не привязан к сотруднику реестра.'))
            continue
        try:
            payout = await asyncio.to_thread(
                finance.add_extra_payout, employee_id=row['employee_id'], work_day=date.fromisoformat(current['end']),
                paid_day=paid_day, amount=str(amount), note=PAYOUT_NOTE.format(start=current['start'], end=current['end']),
                confirm=body.confirm, by=_user(request))
            results.append(dict(waiter=row['waiter'], amount=amount, status='created', payout_id=payout.get('id')))
        except LedgerError as error:
            results.append(dict(waiter=row['waiter'], amount=amount, status='error', message=str(error)))
    if any(item['status'] == 'created' for item in results):
        request.app.state.kpi_store.set_payouts(current['id'], dict(paid_day=paid_day.isoformat(), results=results))
    return dict(results=results, paid_day=paid_day.isoformat())
