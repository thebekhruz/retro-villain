"""Accountant-owned attendance and daily cash endpoints."""

import asyncio
from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.cashier.expenses import cash_to_finance

from .attendance import export_entrances
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


async def cashier_handover(request: Request, day: date) -> Decimal | None:
    """Use manual handovers when enabled; otherwise retain the iiko fallback."""
    finance = request.app.state.accountant_finance
    if request.app.state.settings.manual_handover_only:
        return finance.handover_for_day(day)
    state = request.app.state
    snapshot = state.cache.latest_for_day(day)
    if snapshot is None and state.settings.configured:
        if state.iiko_lock.locked():
            raise HTTPException(429, 'Другой отчёт ещё загружается. Повторите через несколько секунд.')
        try:
            async with state.iiko_lock:
                snapshot = await asyncio.wait_for(state.iiko.load(day), timeout=90)
                state.cache.put(snapshot)
        except TimeoutError:
            raise HTTPException(504, 'iiko отвечает дольше обычного. Повторите запрос.') from None
        except DataError as error:
            raise HTTPException(503, str(error)) from None
    if snapshot is None:
        return None
    expense_total = sum((item.amount for item in state.expenses.list(day)), Decimal(0))
    receipt_total = sum((item.amount for item in state.expenses.list_receipts(day)), Decimal(0))
    return cash_to_finance(snapshot, expense_total, receipt_total)


async def required_handover(request: Request, day: date) -> Decimal:
    amount = await cashier_handover(request, day)
    if amount is None:
        raise HTTPException(409, 'Нет данных кассира за этот день. Обновите отчёт и повторите.')
    request.app.state.accountant_finance.record_handover(day, amount)
    return amount


@router.get('/day')
async def day_view(request: Request, date: date | None = None):
    day = selected_day(date)
    roster = request.app.state.accountant_roster.list()
    finance = request.app.state.accountant_finance
    rows = draft_payroll(day, roster, finance.exceptions_for_day(day))
    cashier_error = None
    try:
        cashier_amount = await cashier_handover(request, day)
    except HTTPException as error:
        if error.status_code not in (429, 503, 504):
            raise
        cashier_amount = None
        cashier_error = error.detail
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
    needed = summary['salary_debt'] if summary['payroll_confirmed'] else draft_total
    shortfall = max(Decimal(0), needed - (summary['cash_balance'] or Decimal(0)))
    scenarios = [dict(group=item['name'], saving=item['shift_cost'],
                      covers_shortfall=Decimal(item['shift_cost']) >= shortfall and shortfall > 0)
                 for item in group_items]
    reserves = finance.reserves(day)
    reserves['monthly']['total'] = str(request.app.state.accountant_roster.monthly_total())
    return dict(demo=True, date=day.isoformat(), source='Симуляция; ресторанный Hikvision не подключён',
                employees=[row.json() for row in rows], roster_count=len(roster), manual_handover=request.app.state.settings.manual_handover_only,
                actual_hikvision_unlinked=sum(employee.hikvision_id is None for employee in roster),
                missing_rates=sum(employee.rate is None for employee in roster),
                groups=group_items,
                payroll=dict(draft_total=str(draft_total), unknown_count=sum(row.payable is None for row in rows),
                             late_count=sum(row.status == 'late' for row in rows),
                             missing_count=sum(row.status == 'missing' for row in rows),
                             unlinked_count=sum(row.status == 'unlinked' for row in rows)),
                ledger=money_json(summary),
                reserves=reserves,
                expected_cashier=str(cashier_amount) if cashier_amount is not None else None,
                cashier_error=cashier_error,
                scenarios=dict(shortfall=str(shortfall), groups=scenarios,
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


class EmployeeCreateInput(BaseModel):
    name: str
    role: str
    rate: str | None = None
    group: str


@router.patch('/employees/{employee_id}')
def update_employee(request: Request, employee_id: int, body: EmployeeUpdateInput):
    try:
        employee = request.app.state.accountant_roster.update(
            employee_id, name=body.name, role=body.role, rate=body.rate,
            group_name=body.group, reason=body.reason)
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


@router.delete('/employees/{employee_id}', status_code=204)
def delete_employee(request: Request, employee_id: int):
    try:
        request.app.state.accountant_roster.delete(employee_id)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.post('/exceptions', status_code=201)
def add_exception(request: Request, body: ExceptionInput):
    day = selected_day(body.date)
    roster = request.app.state.accountant_roster.list()
    employee = next((item for item in roster if item.id == body.employee_id), None)
    if employee is None:
        raise HTTPException(404, 'Сотрудник не найден.')
    if employee.rate is None:
        raise HTTPException(422, 'Сначала укажите дневную ставку.')
    if request.app.state.accountant_finance.summary(day)['payroll_confirmed']:
        raise HTTPException(409, 'Начисления за день уже подтверждены.')
    simulated = next(row for row in draft_payroll(day, roster, set()) if row.employee_id == body.employee_id)
    if simulated.status != 'unlinked':
        raise HTTPException(422, 'Исключение доступно только для сотрудника без демопривязки.')
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
    rows = draft_payroll(day, request.app.state.accountant_roster.list(), finance.exceptions_for_day(day))
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
        request.app.state.accountant_finance.record_handover(day, amount)
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
    request.app.state.accountant_finance.delete_handover(day)


class ReserveInput(OpeningInput):
    account: str
    kind: str


@router.post('/reserves', status_code=201)
async def add_reserve(request: Request, body: ReserveInput):
    day = selected_day(body.date)
    cashier_amount = await required_handover(request, day) if body.kind == 'transfer' else None
    try:
        entry_id = request.app.state.accountant_finance.reserve_entry(
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
        entry_id = request.app.state.accountant_finance.pay_salary(
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


@router.post('/incomes', status_code=201)
async def add_finance_income(request: Request, body: FinanceExpenseInput):
    day = selected_day(body.date)
    try:
        amount = amount_value(body.amount, allow_zero=True)
        note = required_text(body.note, 'назначение')
        if body.item_code == 'income_cashier':
            entry_id = request.app.state.accountant_finance.record_handover(day, amount)
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
            entry_id = request.app.state.accountant_finance.add_expense(
                day, body.item_code, body.note, body.amount, cashier_amount=cashier_amount)
        else:
            entry_id = request.app.state.accountant_finance.record_debt(
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
        entry_id = request.app.state.accountant_finance.pay_debt(
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
    rows = draft_payroll(day, request.app.state.accountant_roster.list(),
                         finance.exceptions_for_day(day))
    selected = [row for row in rows if row.status == 'late'] if scope == 'late' else rows
    data = export_employees(day, selected, scope)
    name = f'Retro-{"late" if scope == "late" else "employees"}-{day.isoformat()}-DEMO.xlsx'
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
        entry_id = request.app.state.accountant_finance.give_procurement(
            day, body.recipient, body.purpose, body.amount, cashier_amount=cashier_amount)
    except LedgerError as error:
        finance_error(error)
    return dict(demo=True, id=entry_id)


@router.get('/entrances/export')
def download_entrances(date: date):
    if date > today_tashkent():
        raise HTTPException(422, 'Выберите сегодняшний или прошедший день.')
    # Empty until the restaurant's Hikvision ISAPI source is connected.
    data = export_entrances(date)
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="Retro-entrances-{date.isoformat()}.xlsx"'})
