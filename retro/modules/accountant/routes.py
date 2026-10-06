"""Accountant-owned attendance and daily cash endpoints."""

import asyncio
from datetime import date, datetime, timedelta
from dataclasses import replace
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from retro.report_cache import load_iiko
from retro.logging_config import log_safe_failure
from retro.modules.cashier.service import DataError, TZ, today_tashkent
from retro.modules.cashier.expenses import cash_to_finance
from retro.modules.cashier.till import (expected_from_saved, expected_handover, handover_check, shokh_gives,
                                        shokh_total, till_totals)
from retro.modules.founder.cabinet import dividend_summary
from retro.modules.shokh.store import pocket_position

from .handover_dates import cashier_day
from .attendance import Entrance, export_entrances
from .employee_export import export_employees
from .expense_catalog import catalog_json
from .ledger import LedgerError, amount_value, required_text
from .payroll import blocker_reason, draft_payroll
from .roster import HikvisionIdTaken

router = APIRouter(prefix='/api/accountant', tags=['accountant'])


def changed_by(request: Request) -> str | None:
    """Кто меняет реестр — для истории сотрудника (логин панели)."""
    return getattr(request.state, 'dashboard_user', None) or getattr(request.state, 'dashboard_role', None)


def selected_day(day: date | None) -> date:
    value = day or today_tashkent() - timedelta(days=1)
    if value > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    return value


def finance_error(error: LedgerError):
    code = 409 if 'уже' in str(error).casefold() else 422
    raise HTTPException(code, str(error)) from None


def money_json(summary: dict) -> dict:
    return {key: str(value) if isinstance(value, Decimal) else value
            for key, value in summary.items()}


def attendance_payroll(request: Request, day: date, roster, exceptions, *, frozen_pay=False):
    snapshot = request.app.state.attendance.snapshot(day, roster)
    rows = draft_payroll(day, roster, exceptions, snapshot.rows)
    if not frozen_pay:
        return snapshot, rows
    saved = request.app.state.accountant_finance.day_accruals(day)
    rows = [replace(row, rate=Decimal(saved[row.employee_id]['rate']),
                    payable=Decimal(saved[row.employee_id]['amount']))
            if row.employee_id in saved else row for row in rows]
    return snapshot, rows


async def cashier_handover(request: Request, day: date) -> Decimal | None:
    amount, _ = await cashier_handover_detail(request, day)
    return amount


async def cashier_handover_detail(request: Request, day: date) -> tuple[Decimal | None, str | None]:
    """Передача кассира и причина, по которой её нет.

    Use manual handovers when enabled; otherwise retain the iiko fallback.
    Вторым значением — почему суммы нет, когда день у нас есть, а посчитать её
    нельзя (неизвестные предоплаты). Иначе None."""
    finance = request.app.state.accountant_finance
    recorded = await asyncio.to_thread(finance.handover_for_day, day)
    if recorded is not None or request.app.state.settings.manual_handover_only:
        return recorded, None
    state = request.app.state
    day = cashier_day(day)
    snapshot = state.cache.latest_for_day(day)
    if state.settings.configured:
        try:
            snapshot = await load_iiko(state, 'load', day, request=request)
            state.cache.put(snapshot)
        except TimeoutError as error:
            log_safe_failure('accountant-route', error, operation='cashier_handover',
                             request_id=request.state.request_id)
            raise HTTPException(504, 'iiko отвечает дольше обычного. Повторите запрос.') from None
        except DataError as error:
            log_safe_failure('accountant-route', error, operation='cashier_handover',
                             request_id=request.state.request_id)
            raise HTTPException(503, str(error)) from None
    if snapshot is None:
        return None, None
    # Расходы кассы вместе с выдачами Шоху из кассы: одна формула для всех экранов.
    totals = await asyncio.to_thread(till_totals, state, day)
    amount = cash_to_finance(snapshot, totals.cash_out, totals.receipts)
    return amount, snapshot.prepayment_issue if amount is None else None


async def required_handover(request: Request, day: date) -> Decimal:
    amount, issue = await cashier_handover_detail(request, day)
    if amount is None:
        # Режим проверки: вместо отказа считаем приход нулевым. Строка прихода за
        # день появится с суммой 0 — её перезапишет обычная запись бухгалтера,
        # когда настоящая касса приедет.
        if not request.app.state.settings.check_mode:
            # Неверную сумму не пишем: неизвестные предоплаты — это не ноль.
            raise HTTPException(409, f'{issue} Передайте кассу вручную.' if issue else
                                'Нет данных кассира за этот день. Обновите отчёт и повторите.')
        amount = Decimal(0)
    # Уже записанный приход той же суммой не перезаписывается (время «получено»
    # остаётся); новый — расчёт iiko, его кассир может заменить своей передачей.
    await asyncio.to_thread(request.app.state.accountant_finance.record_handover, day, amount,
                            source='auto')
    return amount


async def current_cashier_expected(request: Request, day: date, *, force: bool = False):
    """Текущий расчёт кассы дня (iiko + расходы кассира и выдачи Шоху) — то,
    с чем сверяется полученное бухгалтером. Сначала снимок, который уже есть
    на сервере; если его нет, а сверять есть что (приход записан бухгалтером
    или подтверждён; `force` — само подтверждение), — спрашиваем iiko. iiko
    не ответил — сверки нет, как и без расчёта вообще.
    Возвращает (сумма или None, время снимка или None)."""
    state = request.app.state
    day = cashier_day(day)
    expected, fetched_at = await asyncio.to_thread(expected_from_saved, state, day)
    if expected is not None or not state.settings.configured:
        return expected, fetched_at
    if not force:
        recorded = await asyncio.to_thread(handover_check, state, day)
        if not recorded or not recorded.get('checked'):
            return None, None
    try:
        snapshot = await load_iiko(state, 'load', day, request=request)
    except (TimeoutError, DataError) as error:
        log_safe_failure('accountant-route', error, operation='current_cashier_expected',
                         request_id=request.state.request_id)
        return None, None
    if snapshot is None or snapshot.demo:
        return None, None
    state.cache.put(snapshot)
    totals = await asyncio.to_thread(till_totals, state, day)
    return expected_handover(snapshot, totals), snapshot.fetched_at


@router.get('/day')
async def day_view(request: Request, date: date | None = None):
    day = selected_day(date)
    cashier_error = None
    try:
        cashier_amount, cashier_error = await cashier_handover_detail(request, day)
    except HTTPException as error:
        if error.status_code not in (429, 503, 504):
            raise
        cashier_amount = None
        cashier_error = error.detail
    current = await current_cashier_expected(request, day)
    return await asyncio.to_thread(_day_data, request, day, cashier_amount, cashier_error, current=current)


@router.get('/staff')
def staff_view(request: Request, date: date | None = None):
    return _day_data(request, selected_day(date), None, None, staff_only=True)


def shift_rows_json(rows, accrued: dict[int, int], closed: bool, roster=()) -> list[dict]:
    """Строки смены с тем, что нужно экрану для выдачи по людям.

    accrued — начислено ли уже (тогда сумма в строке — начисленная),
    accrual_id — по нему выдают деньги, blocker — почему начислить пока
    нельзя: missing_rate / unavailable (None — можно или уже начислено).
    manual_attendance / hikvision_registered — из реестра: по ним «Сотрудники»
    показывают переключатель «Нет в Hikvision · отмечать вручную».
    """
    people = {employee.id: employee for employee in roster}
    result = []
    for row in rows:
        accrual_id = accrued.get(row.employee_id)
        item = dict(row.json(), accrued=accrual_id is not None, accrual_id=accrual_id,
                    blocker=None if accrual_id is not None or closed else blocker_reason(row))
        employee = people.get(row.employee_id)
        if employee is not None:
            item.update(manual_attendance=employee.manual_attendance,
                        hikvision_registered=employee.hikvision_id is not None,
                        hikvision_id=employee.hikvision_id)
        result.append(item)
    return result


def payroll_state(rows, accrued: dict[int, int], confirmed: bool) -> dict:
    waiting = [row for row in rows if row.employee_id not in accrued]
    return dict(payroll_confirmed=confirmed,
                payroll_partial=bool(accrued) and not confirmed,
                accrued_count=len(accrued),
                blocked_count=0 if confirmed else sum(blocker_reason(row) is not None for row in waiting),
                pending_total=str(Decimal(0) if confirmed else sum(
                    (row.payable for row in waiting if row.payable is not None), Decimal(0))))


def _day_data(request, day, cashier_amount, cashier_error, *, staff_only=False, current=None):
    roster = request.app.state.accountant_roster.list(day)
    finance = request.app.state.accountant_finance
    # Начисленным сотрудникам сумма всегда из начисления: смену подтверждают
    # по людям, и начисленное не должно «плыть» вслед за ставкой или отметкой.
    attendance, rows = attendance_payroll(request, day, roster, finance.exceptions_for_day(day), frozen_pay=True)
    accrued = finance.accrued_employees(day)
    confirmed = finance.is_payroll_confirmed(day)
    state = payroll_state(rows, accrued, confirmed)
    if staff_only:
        return dict(demo=False, date=day.isoformat(), source='Hikvision ISAPI',
                    attendance=attendance.health, employees=shift_rows_json(rows, accrued, confirmed, roster),
                    **state,
                    roster_count=len(roster), missing_rates=sum(employee.rate is None for employee in roster),
                    monthly_employees=[row.json() for row in request.app.state.accountant_roster.list_monthly()],
                    groups=[dict(name=name) for name in dict.fromkeys(e.group_name for e in roster)],
                    payroll={f'{status}_count': sum(row.status == status for row in rows)
                             for status in ('late', 'missing', 'unlinked', 'unavailable',
                                            'manual_present', 'manual_absent')})
    anchor = finance.cash_opening()
    carry_start = date.fromisoformat(anchor['day']) if anchor and request.app.state.settings.manual_handover_only else None
    summary = finance.daily_summary(
        day, cashier_amount,
        carry_history=not request.app.state.settings.manual_handover_only or
        (carry_start is not None and day >= carry_start),
        carry_start=carry_start)
    draft_total = sum((row.payable for row in rows if row.payable is not None), Decimal(0))
    groups = {}
    for employee, row in zip(roster, rows, strict=True):
        item = groups.setdefault(employee.group_name, dict(name=employee.group_name, count=0,
                                                             missing_rates=0, shift_cost=Decimal(0),
                                                             draft_total=Decimal(0)))
        item['count'] += 1
        item['missing_rates'] += employee.rate is None
        item['shift_cost'] += employee.rate or Decimal(0)
        item['draft_total'] += row.payable or Decimal(0)
    group_items = [dict(item, shift_cost=str(item['shift_cost']),
                        draft_total=str(item['draft_total'])) for item in groups.values()]
    # Долг уже начисленных — в salary_debt; к нему добавляются только ещё не начисленные.
    needed = summary['salary_debt'] + Decimal(state['pending_total'])
    shortfall = (max(Decimal(0), needed - summary['cash_balance'])
                 if summary['cash_balance'] is not None and not any(row.payable is None for row in rows) else None)
    scenarios = [dict(group=item['name'], saving=item['shift_cost'],
                      covers_shortfall=shortfall is not None and Decimal(item['shift_cost']) >= shortfall and shortfall > 0)
                 for item in group_items]
    reserves = finance.reserves(day)
    reserves['monthly']['total'] = str(request.app.state.accountant_roster.monthly_total())
    transfers = finance.supplier_transfers(day)
    return dict(demo=False, date=day.isoformat(), source='Hikvision ISAPI',
                attendance=attendance.health,
                employees=shift_rows_json(rows, accrued, confirmed, roster), roster_count=len(roster), manual_handover=request.app.state.settings.manual_handover_only,
                monthly_employees=[row.json() for row in request.app.state.accountant_roster.list_monthly()],
                actual_hikvision_unlinked=sum(employee.hikvision_id is None for employee in roster),
                missing_rates=sum(employee.rate is None for employee in roster),
                groups=group_items,
                payroll=dict(draft_total=str(draft_total), unknown_count=sum(row.payable is None for row in rows),
                             late_count=sum(row.status == 'late' for row in rows),
                             missing_count=sum(row.status == 'missing' for row in rows),
                             unlinked_count=sum(row.status == 'unlinked' for row in rows),
                             unavailable_count=sum(row.status == 'unavailable' for row in rows),
                             manual_present_count=sum(row.status == 'manual_present' for row in rows),
                             manual_absent_count=sum(row.status == 'manual_absent' for row in rows),
                             **state),
                monthly_payments=monthly_payments_json(finance, request.app.state.accountant_roster, day),
                # Перечисления поставщикам — безнал: в ledger (наличные) их нет.
                supplier_transfers=transfers,
                supplier_transfers_total=str(sum((Decimal(row['amount']) for row in transfers), Decimal(0))),
                procurement_points=request.app.state.shokh.points(),
                ledger=money_json(summary),
                reserves=reserves,
                expected_cashier=str(cashier_amount) if cashier_amount is not None else None,
                cashier_error=cashier_error,
                # «От кассира · ожидается / получено HH:MM»: запись передачи и расчёт
                # по последнему снимку iiko на сервере (подсказка, в остаток не входит).
                cashier_handover=cashier_handover_json(request, day, current),
                cashier_date=cashier_day(day).isoformat(),
                # Выдачи Шоху из кассы: уже вычтены из передачи кассира. Подотчёт Шоха
                # их считает (reserves.shoh), а деньги бухгалтера — нет.
                cashier_shokh_gives=cashier_gives_json(finance, day),
                # «На руках у Шоха» — одна формула для 2a, 3a и 7b (shokh.store).
                shoh_pocket=pocket_position(request.app.state.shokh, finance, day),
                # Цель учредителя на неделю и сколько уже отложено в сейф.
                dividends_week=dividend_summary(request.app.state, day),
                scenarios=dict(shortfall=str(shortfall) if shortfall is not None else None, groups=scenarios,
                               note='Только оценка будущей смены; уже начисленный долг не уменьшается.'))


def cashier_handover_json(request, day: date, current=None) -> dict:
    state = request.app.state
    expected, fetched_at = current if current is not None else expected_from_saved(state, cashier_day(day))
    recorded = handover_check(state, cashier_day(day), expected) or {}
    return dict(amount=recorded.get('amount'), handed_at=recorded.get('handed_at'),
                source=recorded.get('source'),
                # Подтверждение бухгалтера: получено (amount), расчёт на момент
                # подтверждения (expected_amount), текущий расчёт, с которым
                # сверено полученное (calculation), и недостача к нему — ошибка
                # «получено меньше расчёта». Ручная запись прихода сверяется так же.
                confirmed_at=recorded.get('confirmed_at'), confirmed_by=recorded.get('confirmed_by'),
                expected_amount=recorded.get('expected_amount'), shortfall=recorded.get('shortfall'),
                checked=recorded.get('checked', False), calculation=recorded.get('calculation'),
                cashier_active=recorded.get('cashier_active'),
                expected_changed=recorded.get('expected_changed', False),
                expected=str(expected) if expected is not None else None,
                expected_at=fetched_at.isoformat() if fetched_at is not None else None)


def cashier_gives_json(finance, day: date) -> dict:
    gives = shokh_gives(finance, day)
    return dict(gives=gives, total=str(shokh_total(gives)))


def monthly_payments_json(finance, roster, day: date) -> dict:
    """Выплаты окладов: сколько каждому выдано с начала месяца и что — сегодня."""
    # Удалённые (архив) — тоже по имени: выплаты прошлых дней остаются чьими-то.
    names = {person.id: person.name for person in roster.list_monthly() + roster.list_monthly(archived=True)}
    rows = finance.monthly_payments(day.replace(day=1), day)
    paid = {}
    for row in rows:
        paid[row['employee_id']] = paid.get(row['employee_id'], Decimal(0)) + Decimal(row['amount'])
    return dict(month=day.strftime('%Y-%m'),
                paid_by_employee={str(key): str(value) for key, value in paid.items()},
                today=[dict(row, name=names.get(row['employee_id'], 'Сотрудник удалён'))
                       for row in rows if row['day'] == day.isoformat()])


class ExceptionInput(BaseModel):
    date: date
    employee_id: int
    reason: str
    approver: str


class EmployeeUpdateInput(BaseModel):
    name: str | None = None
    role: str | None = None
    rate: str | None
    group: str | None = None
    reason: str
    manual_attendance: bool | None = None
    # Номер сотрудника на устройстве Hikvision; пусто/null — снять привязку.
    # Поле не прислали — привязку не трогаем.
    hikvision_id: str | None = None


class EmployeeCreateInput(BaseModel):
    name: str
    role: str
    rate: str | None = None
    group: str
    manual_attendance: bool = False


class MonthlyEmployeeInput(BaseModel):
    name: str
    role: str
    salary: str
    schedule: str = ''
    card: str = '0'
    cash: str = '0'
    advances: str = '0'
    remaining: str = '0'
    # «⊘ Hik» у окладника; None — не менять.
    no_hikvision: bool | None = None
    reason: str = ''


@router.get('/payroll/month')
def payroll_month(request: Request, month: str):
    """Ведомость месяца: сотрудник × день, одним запросом вместо тридцати."""
    try:
        first = date.fromisoformat(month + '-01')
    except ValueError:
        raise HTTPException(422, 'Укажите месяц в виде ГГГГ-ММ.') from None
    if first > today_tashkent().replace(day=1):
        raise HTTPException(422, 'Выберите текущий или прошедший месяц.')
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    data = request.app.state.accountant_finance.payroll_month(first, last)
    # Проверка «Остаток ушёл в минус»: дни месяца (до сегодня) с минусом на
    # конец дня — тем же расчётом, что и остаток в «Финансах дня».
    anchor = request.app.state.accountant_finance.cash_opening()
    carry_start = (date.fromisoformat(anchor['day'])
                   if anchor and request.app.state.settings.manual_handover_only else None)
    data['negative_cash'] = request.app.state.accountant_finance.negative_cash_days(
        first, min(last, today_tashkent()), carry_start)
    roster = request.app.state.accountant_roster
    # Выплаты окладов раскладываются по людям и дням из журнала расходов:
    # monthly_cells — сумма за день, monthly_cell_ops — сами записи, чтобы
    # ячейку можно было исправить (PUT) или очистить (DELETE) по id движения.
    # monthly_paid из payroll_month — весь оклад месяца, включая записи без
    # сотрудника.
    monthly_cells, monthly_cell_ops = {}, {}
    for row in request.app.state.accountant_finance.monthly_payments(first, last):
        cells = monthly_cells.setdefault(str(row['employee_id']), {})
        cells[row['day']] = str(Decimal(cells.get(row['day'], '0')) + Decimal(row['amount']))
        monthly_cell_ops.setdefault(str(row['employee_id']), {}).setdefault(row['day'], []).append(
            dict(id=row['id'], amount=row['amount']))
    return dict(demo=False, month=month, first=first.isoformat(), last=last.isoformat(),
                monthly_cells=monthly_cells, monthly_cell_ops=monthly_cell_ops,
                days=[(first + timedelta(days=offset)).isoformat()
                      for offset in range((last - first).days + 1)],
                monthly=[row.json() for row in roster.list_monthly()],
                # Окладники в архиве, у кого есть выплаты этого месяца: ведомость
                # показывает их по имени, а не «Сотрудник удалён · №».
                monthly_archived=[row.json() for row in roster.list_monthly(archived=True)
                                  if str(row.id) in monthly_cells],
                monthly_total=str(roster.monthly_total()), **data)


@router.patch('/employees/{employee_id}')
def update_employee(request: Request, employee_id: int, body: EmployeeUpdateInput):
    roster = request.app.state.accountant_roster
    try:
        if 'hikvision_id' in body.model_fields_set:
            # Сначала привязка: занятый номер (409) не должен оставить
            # наполовину сохранённую карточку.
            old, new = roster.set_hikvision_id(employee_id, body.hikvision_id, by=changed_by(request))
            if old != new:
                store = request.app.state.attendance_store
                if old:
                    store.forget_link(employee_id, old)
                if new:
                    # Входы, пришедшие до привязки, сразу становятся его первыми входами.
                    store.reconcile_links({new: employee_id})
        employee = request.app.state.accountant_roster.update(
            employee_id, name=body.name, role=body.role, rate=body.rate,
            group_name=body.group, reason=body.reason, by=changed_by(request))
        if body.manual_attendance is not None and body.manual_attendance != employee.manual_attendance:
            employee = request.app.state.accountant_roster.set_manual_attendance(
                employee_id, body.manual_attendance, by=changed_by(request))
    except HikvisionIdTaken as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(demo=True, employee=employee.json())


@router.post('/employees', status_code=201)
def create_employee(request: Request, body: EmployeeCreateInput):
    try:
        employee = request.app.state.accountant_roster.add(
            name=body.name, role=body.role, rate=body.rate, group_name=body.group,
            by=changed_by(request))
        # «Нет в Hikvision · отмечать вручную» можно выбрать сразу при добавлении.
        if body.manual_attendance:
            employee = request.app.state.accountant_roster.set_manual_attendance(
                employee.id, True, by=changed_by(request))
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(demo=True, employee=employee.json())


@router.post('/hikvision/sync-people')
async def sync_hikvision_people(request: Request):
    poller = request.app.state.hikvision_poller
    if poller is None:
        raise HTTPException(503, 'Hikvision не настроен.')
    try:
        report = await poller.sync_all_people()
        poll = await poller.run_once()
    except Exception as error:
        log_safe_failure('accountant-route', error, operation='sync-hikvision-people',
                         request_id=request.state.request_id)
        raise HTTPException(503, 'Не удалось синхронизировать сотрудников Hikvision.') from None
    if not poll.success:
        raise HTTPException(503, 'Сотрудники привязаны, но проходы Hikvision пока недоступны.')
    return dict(source='Hikvision ISAPI', **report, events=poll.events)


@router.post('/monthly-employees', status_code=201)
def create_monthly_employee(request: Request, body: MonthlyEmployeeInput):
    try:
        employee = request.app.state.accountant_roster.add_monthly(
            name=body.name, role=body.role, salary=body.salary, schedule=body.schedule,
            card=body.card, cash=body.cash, advances=body.advances, remaining=body.remaining,
            no_hikvision=body.no_hikvision, by=changed_by(request))
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(demo=True, employee=employee.json())


@router.patch('/monthly-employees/{employee_id}')
def update_monthly_employee(request: Request, employee_id: int, body: MonthlyEmployeeInput):
    try:
        employee = request.app.state.accountant_roster.update_monthly(
            employee_id, name=body.name, role=body.role, salary=body.salary,
            schedule=body.schedule, card=body.card, cash=body.cash,
            advances=body.advances, remaining=body.remaining, no_hikvision=body.no_hikvision,
            by=changed_by(request), reason=body.reason)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(demo=True, employee=employee.json())


@router.delete('/monthly-employees/{employee_id}', status_code=204)
def delete_monthly_employee(request: Request, employee_id: int):
    try:
        request.app.state.accountant_roster.delete_monthly(employee_id, by=changed_by(request))
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.delete('/employees/{employee_id}', status_code=204)
def delete_employee(request: Request, employee_id: int):
    try:
        request.app.state.accountant_roster.delete(employee_id, by=changed_by(request))
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.get('/employees/{employee_id}/history')
def employee_history(request: Request, employee_id: int):
    """История сменного: ставка, группа, «без Hikvision», добавление и архив."""
    return dict(history=request.app.state.accountant_roster.history(employee_id))


@router.get('/monthly-employees/{employee_id}/history')
def monthly_employee_history(request: Request, employee_id: int):
    return dict(history=request.app.state.accountant_roster.history(employee_id, kind='monthly'))


@router.post('/exceptions', status_code=201)
def add_exception(request: Request, body: ExceptionInput):
    day = selected_day(body.date)
    roster = request.app.state.accountant_roster.list(day)
    employee = next((item for item in roster if item.id == body.employee_id), None)
    if employee is None:
        raise HTTPException(404, 'Сотрудник не найден.')
    if employee.rate is None:
        raise HTTPException(422, 'Сначала укажите дневную ставку.')
    # Закрыт ли день именно этого сотрудника, проверяет grant_exception — в
    # одной транзакции с записью.
    _, rows = attendance_payroll(request, day, roster, set())
    employee_row = next(row for row in rows if row.employee_id == body.employee_id)
    if employee_row.status != 'unlinked':
        raise HTTPException(422, 'Исключение доступно только для сотрудника без привязки Hikvision.')
    try:
        request.app.state.accountant_finance.grant_exception(body.employee_id, day,
                                                              body.reason, body.approver)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, employee_id=body.employee_id, day=day.isoformat())


class ConfirmInput(BaseModel):
    date: date
    approver: str
    # Кого начислить сейчас; без списка — всех, кого можно.
    employee_ids: list[int] | None = None


@router.post('/payroll/confirm')
def confirm_payroll(request: Request, body: ConfirmInput):
    """Начислить смену по людям: кого можно — сейчас, остальные ждут."""
    day = selected_day(body.date)
    finance = request.app.state.accountant_finance
    roster = request.app.state.accountant_roster.list(day)
    snapshot, rows = attendance_payroll(request, day, roster, finance.exceptions_for_day(day))
    try:
        # Отметки, по которым посчитаны строки, сверяются внутри транзакции.
        result = finance.confirm_payroll(day, rows, body.approver, marks=snapshot.marks,
                                         employee_ids=body.employee_ids)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, date=day.isoformat(), already_confirmed=result.already_confirmed,
                confirmed=result.confirmed, partial=not result.confirmed and bool(
                    result.accrued or result.accrued_before),
                accrued=result.accrued, blockers=result.blockers,
                total=str(finance.summary(day)['accrued_on_day']))


class OpeningInput(BaseModel):
    date: date
    amount: str
    note: str


@router.post('/handover', status_code=201)
def add_handover(request: Request, body: OpeningInput):
    day = selected_day(body.date)
    try:
        amount = amount_value(body.amount, allow_zero=True)
        note = required_text(body.note, 'примечание к приходу')
        request.app.state.accountant_finance.record_handover(day, amount, create_only=True)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, date=day.isoformat(), amount=str(amount), note=note)


class HandoverConfirmInput(BaseModel):
    date: date
    amount: str


@router.post('/handover/confirm')
async def confirm_handover(request: Request, body: HandoverConfirmInput):
    """«Получено» от кассира: бухгалтер подтверждает, сколько наличных пришло.

    Сверяется с ТЕКУЩИМ расчётом кассы; он же запоминается как расчёт на
    момент подтверждения. Ответ несёт недостачу — её и показывает сообщение."""
    day = selected_day(body.date)
    state = request.app.state
    expected, _ = await current_cashier_expected(request, day, force=True)
    approver = getattr(request.state, 'dashboard_user', None) or 'бухгалтер'
    try:
        result = await asyncio.to_thread(state.accountant_finance.confirm_handover, day, body.amount,
                                         expected, approver)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=False, date=day.isoformat(), handover=result)


@router.put('/handover/{handover_date}')
def update_handover(request: Request, handover_date: date, body: OpeningInput):
    if handover_date != body.date:
        raise HTTPException(422, 'Дата в адресе и форме должна совпадать.')
    day = selected_day(handover_date)
    try:
        amount = amount_value(body.amount, allow_zero=True)
        required_text(body.note, 'примечание к приходу')
        if request.app.state.accountant_finance.handover_for_day(day) is None:
            raise LedgerError('Приход за этот день ещё не записан.')
        request.app.state.accountant_finance.record_handover(day, amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, date=day.isoformat(), amount=str(amount))


@router.delete('/handover/{handover_date}', status_code=204)
def delete_handover(request: Request, handover_date: date):
    day = selected_day(handover_date)
    try:
        request.app.state.accountant_finance.delete_handover(day)
    except LedgerError as error:
        finance_error(error)


class ShokhAcceptInput(BaseModel):
    date: date


class ReserveInput(OpeningInput):
    account: str
    kind: str


@router.post('/reserves', status_code=201)
async def add_reserve(request: Request, body: ReserveInput):
    day = selected_day(body.date)
    cashier_amount = await required_handover(request, day) if body.kind == 'transfer' else None
    try:
        entry_id = await asyncio.to_thread(request.app.state.accountant_finance.reserve_entry,
            day, body.account, body.kind, body.amount, body.note, cashier_amount=cashier_amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=entry_id)


@router.post('/monthly-plan', status_code=201)
def add_monthly_plan(request: Request, body: OpeningInput):
    day = selected_day(body.date)
    try:
        request.app.state.accountant_finance.set_monthly_plan(day, body.amount, body.note)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True)


@router.post('/cash-opening', status_code=201)
def add_cash_opening(request: Request, body: OpeningInput):
    day = selected_day(body.date)
    try:
        request.app.state.accountant_finance.set_cash_opening(day, body.amount, body.note)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True)


@router.post('/opening', status_code=410)
def add_opening(request: Request, body: OpeningInput):
    raise HTTPException(410, 'Начальный остаток больше не вводится вручную в дневном отчёте.')


class TransferInput(BaseModel):
    cashier_date: date
    received_date: date
    amount: str


@router.post('/transfers', status_code=410)
def add_transfer(request: Request, body: TransferInput):
    raise HTTPException(410, 'Сумма к передаче теперь берётся из кассового отчёта автоматически.')


class SalaryPaymentInput(BaseModel):
    accrual_id: int
    date: date
    amount: str


class ManualAttendanceInput(BaseModel):
    date: date
    employee_id: int
    present: bool


@router.post('/manual-attendance')
def mark_manual_attendance(request: Request, body: ManualAttendanceInput):
    """«Был / не был» по одному дню: без Hikvision или когда день вне выгрузки."""
    day = selected_day(body.date)
    employee = next((item for item in request.app.state.accountant_roster.list(day)
                     if item.id == body.employee_id), None)
    if employee is None:
        raise HTTPException(404, 'Сотрудник не найден.')
    if not request.app.state.attendance.manual_markable(day, employee):
        raise HTTPException(422, 'Сотрудник отмечается через Hikvision.')
    approver = getattr(request.state, 'dashboard_user', None) or 'бухгалтер'
    try:
        # Проверка «смена не подтверждена», запись и аудит — одна транзакция.
        request.app.state.accountant_finance.mark_manual_attendance(
            body.employee_id, day, body.present, approver)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=False, employee_id=body.employee_id, day=day.isoformat(), present=body.present)


class MonthlyPaymentInput(BaseModel):
    date: date
    employee_id: int
    amount: str


@router.post('/monthly-payments', status_code=201)
async def add_monthly_payment(request: Request, body: MonthlyPaymentInput):
    day = selected_day(body.date)
    employee = next((item for item in request.app.state.accountant_roster.list_monthly()
                     if item.id == body.employee_id), None)
    if employee is None:
        raise HTTPException(404, 'Сотрудник на окладе не найден.')
    cashier_amount = await required_handover(request, day)
    try:
        entry_id = await asyncio.to_thread(request.app.state.accountant_finance.pay_monthly,
            employee.id, employee.name, day, body.amount, cashier_amount=cashier_amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=False, id=entry_id)


class SupplierTransferInput(BaseModel):
    date: date
    supplier: str
    item: str = ''
    point: str
    amount: str


@router.post('/supplier-transfers', status_code=201)
def add_supplier_transfer(request: Request, body: SupplierTransferInput):
    """Перечисление поставщику со счёта: касса и подотчёт Шоха не меняются."""
    day = selected_day(body.date)
    point = body.point.strip() if isinstance(body.point, str) else ''
    if point not in request.app.state.shokh.points():
        raise HTTPException(422, 'Выберите точку закупа из списка.')
    finance = request.app.state.accountant_finance
    try:
        transfer_id = finance.add_supplier_transfer(day, body.supplier, body.item, point, body.amount)
    except LedgerError as error:
        finance_error(error)
    transfer = next(row for row in finance.supplier_transfers(day) if row['id'] == transfer_id)
    return dict(demo=False, id=transfer_id, transfer=transfer)


@router.delete('/supplier-transfers/{transfer_id}', status_code=204)
def delete_supplier_transfer(request: Request, transfer_id: int, date: date):
    day = selected_day(date)
    try:
        request.app.state.accountant_finance.delete_supplier_transfer(transfer_id, day)
    except LedgerError as error:
        if 'не найдено' in str(error):
            raise HTTPException(404, str(error)) from None
        finance_error(error)


@router.get('/day/export')
async def download_day(request: Request, date: date | None = None):
    """«Финансы дня» в Excel так, как их видит экран.

    Выбранная дата — день выплат P: лист «Смена» — вчерашняя смена S = P − 1,
    которую выдают сегодня, «Операции» и «Итог» — деньги самого дня P.
    """
    return await _day_export(request, selected_day(date), None)


class DayCheckInput(BaseModel):
    lvl: str
    text: str
    sub: str | None = None


class DayExportInput(BaseModel):
    date: date
    checks: list[DayCheckInput] = []


@router.post('/day/export')
async def download_day_with_checks(request: Request, body: DayExportInput):
    """То же, что GET, плюс лист «Проверки» — ровно те, что показал экран."""
    checks = [item.model_dump() for item in body.checks[:200]]
    return await _day_export(request, selected_day(body.date), checks)


async def _day_export(request: Request, day: date, checks):
    from .sheets_export import day_workbook
    data = await day_view(request, day)
    staff = await asyncio.to_thread(_day_data, request, day - timedelta(days=1), None, None,
                                    staff_only=True)
    purchases = await asyncio.to_thread(request.app.state.shokh.purchases, day)
    body = await asyncio.to_thread(day_workbook, data, staff, purchases=purchases, checks=checks)
    return Response(body, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="Retro-finance-{day.isoformat()}.xlsx"'})


@router.get('/payroll/month/export')
def download_payroll_month(request: Request, month: str):
    from .sheets_export import payroll_workbook
    data = payroll_month(request, month)
    body = payroll_workbook(data)
    return Response(body, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="Retro-payroll-{month}.xlsx"'})


@router.post('/salary-payments', status_code=201)
async def add_salary_payment(request: Request, body: SalaryPaymentInput):
    day = selected_day(body.date)
    cashier_amount = await required_handover(request, day)
    try:
        entry_id = await asyncio.to_thread(request.app.state.accountant_finance.pay_salary,
            body.accrual_id, day, body.amount, cashier_amount=cashier_amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=entry_id)


class FinanceExpenseInput(BaseModel):
    date: date
    item_code: str
    note: str = ''
    amount: str
    paid_amount: str | None = None


class OperationUpdateInput(BaseModel):
    date: date
    item_code: str = ''
    note: str = ''
    amount: str


@router.put('/operations/{operation_type}/{operation_id}')
def update_finance_operation(request: Request, operation_type: str, operation_id: int,
                             body: OperationUpdateInput):
    day = selected_day(body.date)
    try:
        if operation_type == 'movement':
            request.app.state.accountant_finance.update_movement(
                operation_id, day, body.item_code, body.note, body.amount)
        elif operation_type == 'salary_payment':
            request.app.state.accountant_finance.update_salary_payment(operation_id, day, body.amount)
        else:
            raise LedgerError('Эту операцию нельзя изменить здесь.')
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=operation_id)


@router.delete('/operations/{operation_type}/{operation_id}', status_code=204)
def delete_finance_operation(request: Request, operation_type: str, operation_id: int,
                             date: date):
    day = selected_day(date)
    try:
        request.app.state.accountant_finance.delete_operation(operation_type, operation_id, day)
    except LedgerError as error:
        finance_error(error)


@router.post('/incomes', status_code=201)
def add_finance_income(request: Request, body: FinanceExpenseInput):
    day = selected_day(body.date)
    try:
        amount = amount_value(body.amount, allow_zero=True)
        note = required_text(body.note, 'назначение')
        if body.item_code == 'income_cashier':
            entry_id = request.app.state.accountant_finance.record_handover(day, amount, add=True)
        else:
            entry_id = request.app.state.accountant_finance.add_income(day, body.item_code, note, amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=entry_id)


@router.get('/expenses/catalog')
def expense_catalog():
    return catalog_json()


@router.post('/expenses', status_code=201)
async def add_finance_expense(request: Request, body: FinanceExpenseInput):
    day = selected_day(body.date)
    try:
        total = amount_value(body.amount)
        paid = amount_value(body.amount if body.paid_amount is None else body.paid_amount,
                            allow_zero=True)
        if paid > total:
            raise LedgerError('Оплаченная сумма не может превышать весь расход.')
        cashier_amount = await required_handover(request, day) if paid > 0 else None
        if paid == total:
            entry_id = await asyncio.to_thread(request.app.state.accountant_finance.add_expense,
                day, body.item_code, body.note, body.amount, cashier_amount=cashier_amount)
        else:
            entry_id = await asyncio.to_thread(request.app.state.accountant_finance.record_debt,
                day, body.item_code, body.note, body.amount, body.paid_amount,
                cashier_amount=cashier_amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=entry_id)


class DebtPaymentInput(BaseModel):
    date: date
    debt_id: int
    amount: str


@router.post('/debts/pay', status_code=201)
async def pay_finance_debt(request: Request, body: DebtPaymentInput):
    day = selected_day(body.date)
    cashier_amount = await required_handover(request, day)
    try:
        entry_id = await asyncio.to_thread(request.app.state.accountant_finance.pay_debt,
            body.debt_id, day, body.amount, cashier_amount=cashier_amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=entry_id)


@router.get('/employees/export')
def download_employees(request: Request, date: date, scope: str):
    day = selected_day(date)
    if scope not in ('late', 'all'):
        raise HTTPException(422, 'Выберите опоздавших или всех сотрудников.')
    finance = request.app.state.accountant_finance
    roster = request.app.state.accountant_roster.list(day)
    attendance, rows = attendance_payroll(request, day, roster, finance.exceptions_for_day(day), frozen_pay=True)
    selected = [row for row in rows if row.status == 'late'] if scope == 'late' else rows
    # Как на экране 1a: группы в порядке реестра, «⊘ Hik», окладники с выдачей
    # с 1-го числа по выбранный день.
    paid = monthly_payments_json(finance, request.app.state.accountant_roster, day)['paid_by_employee']
    monthly = [dict(person.json(), paid=paid.get(str(person.id), '0'))
               for person in sorted(request.app.state.accountant_roster.list_monthly(), key=lambda p: p.id)]
    data = export_employees(day, selected, scope, attendance.health,
                            manual={employee.id for employee in roster if employee.manual_attendance},
                            group_order=list(dict.fromkeys(employee.group_name for employee in roster)),
                            monthly=monthly)
    name = f'Retro-{"late" if scope == "late" else "employees"}-{day.isoformat()}.xlsx'
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


class ProcurementInput(BaseModel):
    date: date
    recipient: str
    purpose: str
    amount: str


@router.post('/procurement', status_code=201)
async def add_procurement(request: Request, body: ProcurementInput):
    day = selected_day(body.date)
    cashier_amount = await required_handover(request, day)
    try:
        entry_id = await asyncio.to_thread(request.app.state.accountant_finance.give_procurement,
            day, body.recipient, body.purpose, body.amount, cashier_amount=cashier_amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=entry_id)


@router.get('/shokh/purchases')
def shokh_purchases(request: Request, date: date | None = None):
    """Покупки Шоха за день — бухгалтеру для проверки и приёмки."""
    day = selected_day(date)
    return dict(demo=False, date=day.isoformat(),
                purchases=request.app.state.shokh_sync.decorate(request.app.state.shokh.purchases(day)))


@router.post('/shokh/purchases/{purchase_id}/accept')
def accept_shokh_purchase(request: Request, purchase_id: int, body: ShokhAcceptInput):
    """Принять покупку: подотчёт уменьшается, касса второй раз не списывается.

    Наличные ушли из кассы, когда выдавали подотчёт. Приёмка лишь переносит
    сумму из «на руках у Шоха» в расход по накладной.
    """
    day = selected_day(body.date)
    purchase = request.app.state.shokh.purchase(purchase_id)
    if purchase is None:
        raise HTTPException(404, 'Покупка не найдена.')
    if purchase['accepted_at'] is not None:
        raise HTTPException(409, 'Покупка уже принята.')
    operation = request.app.state.shokh_sync.operation(purchase_id=purchase_id)
    if operation and operation['status'] != 'synced':
        raise HTTPException(409, 'Сначала подтвердите проведение накладной в iiko.')
    from retro.modules.shokh.store import ShokhError
    try:
        accepted = request.app.state.shokh.accept_with_finance(purchase_id, day, datetime.now(TZ),
                                                              request.app.state.accountant_finance)
    except (LedgerError, ShokhError) as error:
        raise HTTPException(422, str(error)) from None
    if not accepted:
        raise HTTPException(409, 'Покупка уже принята.')
    return dict(demo=False, purchase=request.app.state.shokh.purchase(purchase_id))


@router.get('/entrances/export')
def download_entrances(request: Request, date: date):
    day = date
    if date > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    roster = request.app.state.accountant_roster.list(day)
    attendance, rows = attendance_payroll(request, date, roster, set())
    entries = tuple(Entrance(row.name, row.occurred_at) for row in rows
                    if row.status in ('on_time', 'late') and row.occurred_at is not None)
    data = export_entrances(date, entries, attendance.health)
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="Retro-entrances-{date.isoformat()}.xlsx"'})


@router.get('/shokh/photo/{purchase_id}')
def shokh_photo(request: Request, purchase_id: int):
    from retro.modules.shokh.routes import photo
    return photo(request, purchase_id)
