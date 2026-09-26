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
from retro.modules.founder.cabinet import dividend_summary

from .attendance import Entrance, export_entrances
from .employee_export import export_employees
from .expense_catalog import catalog_json
from .ledger import LedgerError, amount_value, required_text
from .payroll import draft_payroll

router = APIRouter(prefix='/api/accountant', tags=['accountant'])


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
    saved = {row['employee_id']: row for row in request.app.state.accountant_finance.accruals(day)
             if row['work_day'] == day.isoformat()}
    rows = [replace(row, rate=Decimal(saved[row.employee_id]['rate']),
                    payable=Decimal(saved[row.employee_id]['amount']))
            if row.employee_id in saved else row for row in rows]
    return snapshot, rows


async def cashier_handover(request: Request, day: date) -> Decimal | None:
    """Use manual handovers when enabled; otherwise retain the iiko fallback."""
    finance = request.app.state.accountant_finance
    recorded = await asyncio.to_thread(finance.handover_for_day, day)
    if recorded is not None or request.app.state.settings.manual_handover_only:
        return recorded
    state = request.app.state
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
        return None
    expenses = await asyncio.to_thread(state.expenses.list, day)
    receipts = await asyncio.to_thread(state.expenses.list_receipts, day)
    expense_total = sum((item.amount for item in expenses), Decimal(0))
    receipt_total = sum((item.amount for item in receipts), Decimal(0))
    return cash_to_finance(snapshot, expense_total, receipt_total)


async def required_handover(request: Request, day: date) -> Decimal:
    amount = await cashier_handover(request, day)
    if amount is None:
        raise HTTPException(409, 'Нет данных кассира за этот день. Обновите отчёт и повторите.')
    await asyncio.to_thread(request.app.state.accountant_finance.record_handover, day, amount)
    return amount


@router.get('/day')
async def day_view(request: Request, date: date | None = None):
    day = selected_day(date)
    cashier_error = None
    try:
        cashier_amount = await cashier_handover(request, day)
    except HTTPException as error:
        if error.status_code not in (429, 503, 504):
            raise
        cashier_amount = None
        cashier_error = error.detail
    return await asyncio.to_thread(_day_data, request, day, cashier_amount, cashier_error)


@router.get('/staff')
def staff_view(request: Request, date: date | None = None):
    return _day_data(request, selected_day(date), None, None, staff_only=True)


def _day_data(request, day, cashier_amount, cashier_error, *, staff_only=False):
    roster = request.app.state.accountant_roster.list(day)
    finance = request.app.state.accountant_finance
    attendance, rows = attendance_payroll(request, day, roster, finance.exceptions_for_day(day), frozen_pay=not staff_only)
    if staff_only:
        return dict(demo=False, date=day.isoformat(), source='Hikvision ISAPI',
                    attendance=attendance.health, employees=[row.json() for row in rows],
                    roster_count=len(roster), missing_rates=sum(employee.rate is None for employee in roster),
                    monthly_employees=[row.json() for row in request.app.state.accountant_roster.list_monthly()],
                    groups=[dict(name=name) for name in dict.fromkeys(e.group_name for e in roster)],
                    payroll={f'{status}_count': sum(row.status == status for row in rows)
                             for status in ('late', 'missing', 'unlinked', 'unavailable')})
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
    needed = summary['salary_debt'] + (Decimal(0) if summary['payroll_confirmed'] else draft_total)
    shortfall = (max(Decimal(0), needed - summary['cash_balance'])
                 if summary['cash_balance'] is not None and not any(row.payable is None for row in rows) else None)
    scenarios = [dict(group=item['name'], saving=item['shift_cost'],
                      covers_shortfall=shortfall is not None and Decimal(item['shift_cost']) >= shortfall and shortfall > 0)
                 for item in group_items]
    reserves = finance.reserves(day)
    reserves['monthly']['total'] = str(request.app.state.accountant_roster.monthly_total())
    return dict(demo=False, date=day.isoformat(), source='Hikvision ISAPI',
                attendance=attendance.health,
                employees=[row.json() for row in rows], roster_count=len(roster), manual_handover=request.app.state.settings.manual_handover_only,
                monthly_employees=[row.json() for row in request.app.state.accountant_roster.list_monthly()],
                actual_hikvision_unlinked=sum(employee.hikvision_id is None for employee in roster),
                missing_rates=sum(employee.rate is None for employee in roster),
                groups=group_items,
                payroll=dict(draft_total=str(draft_total), unknown_count=sum(row.payable is None for row in rows),
                             late_count=sum(row.status == 'late' for row in rows),
                             missing_count=sum(row.status == 'missing' for row in rows),
                             unlinked_count=sum(row.status == 'unlinked' for row in rows),
                             unavailable_count=sum(row.status == 'unavailable' for row in rows)),
                ledger=money_json(summary),
                reserves=reserves,
                expected_cashier=str(cashier_amount) if cashier_amount is not None else None,
                cashier_error=cashier_error,
                # Цель учредителя на неделю и сколько уже отложено в сейф.
                dividends_week=dividend_summary(request.app.state, day),
                scenarios=dict(shortfall=str(shortfall) if shortfall is not None else None, groups=scenarios,
                               note='Только оценка будущей смены; уже начисленный долг не уменьшается.'))


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


class EmployeeCreateInput(BaseModel):
    name: str
    role: str
    rate: str | None = None
    group: str


class MonthlyEmployeeInput(BaseModel):
    name: str
    role: str
    salary: str
    schedule: str = ''
    card: str = '0'
    cash: str = '0'
    advances: str = '0'
    remaining: str = '0'


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
    roster = request.app.state.accountant_roster
    # Оклады остаются ручным реестром: разбивки выплат по дням в данных нет,
    # поэтому отдаём их как есть, вместе с собственным предупреждением модели.
    return dict(demo=False, month=month, first=first.isoformat(), last=last.isoformat(),
                days=[(first + timedelta(days=offset)).isoformat()
                      for offset in range((last - first).days + 1)],
                monthly=[row.json() for row in roster.list_monthly()],
                monthly_total=str(roster.monthly_total()), **data)


@router.patch('/employees/{employee_id}')
def update_employee(request: Request, employee_id: int, body: EmployeeUpdateInput):
    try:
        employee = request.app.state.accountant_roster.update(
            employee_id, name=body.name, role=body.role, rate=body.rate,
            group_name=body.group, reason=body.reason)
        if body.manual_attendance is not None and body.manual_attendance != employee.manual_attendance:
            employee = request.app.state.accountant_roster.set_manual_attendance(
                employee_id, body.manual_attendance)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(demo=True, employee=employee.json())


@router.post('/employees', status_code=201)
def create_employee(request: Request, body: EmployeeCreateInput):
    try:
        employee = request.app.state.accountant_roster.add(
            name=body.name, role=body.role, rate=body.rate, group_name=body.group)
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
            card=body.card, cash=body.cash, advances=body.advances, remaining=body.remaining)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(demo=True, employee=employee.json())


@router.patch('/monthly-employees/{employee_id}')
def update_monthly_employee(request: Request, employee_id: int, body: MonthlyEmployeeInput):
    try:
        employee = request.app.state.accountant_roster.update_monthly(
            employee_id, name=body.name, role=body.role, salary=body.salary,
            schedule=body.schedule, card=body.card, cash=body.cash,
            advances=body.advances, remaining=body.remaining)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return dict(demo=True, employee=employee.json())


@router.delete('/monthly-employees/{employee_id}', status_code=204)
def delete_monthly_employee(request: Request, employee_id: int):
    try:
        request.app.state.accountant_roster.delete_monthly(employee_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.delete('/employees/{employee_id}', status_code=204)
def delete_employee(request: Request, employee_id: int):
    try:
        request.app.state.accountant_roster.delete(employee_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.post('/exceptions', status_code=201)
def add_exception(request: Request, body: ExceptionInput):
    day = selected_day(body.date)
    roster = request.app.state.accountant_roster.list(day)
    employee = next((item for item in roster if item.id == body.employee_id), None)
    if employee is None:
        raise HTTPException(404, 'Сотрудник не найден.')
    if employee.rate is None:
        raise HTTPException(422, 'Сначала укажите дневную ставку.')
    if request.app.state.accountant_finance.summary(day)['payroll_confirmed']:
        raise HTTPException(409, 'Начисления за день уже подтверждены.')
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


@router.post('/payroll/confirm')
def confirm_payroll(request: Request, body: ConfirmInput):
    day = selected_day(body.date)
    finance = request.app.state.accountant_finance
    roster = request.app.state.accountant_roster.list(day)
    _, rows = attendance_payroll(request, day, roster, finance.exceptions_for_day(day))
    if any(row.status == 'unavailable' for row in rows):
        raise HTTPException(409, 'Данные Hikvision за этот день неполные. Начисление не подтверждено.')
    try:
        confirmed = finance.confirm_payroll(day, rows, body.approver)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, date=day.isoformat(), already_confirmed=not confirmed,
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
    data = export_employees(day, selected, scope, attendance.health)
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
                purchases=request.app.state.shokh.purchases(day))


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
    try:
        request.app.state.accountant_finance.reserve_entry(
            day, 'shoh', 'withdrawal', purchase['total'],
            f"Закуп: {purchase['item']} · {purchase['point']}")
    except LedgerError as error:
        finance_error(error)
    request.app.state.shokh.accept(purchase_id, datetime.now(TZ))
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
