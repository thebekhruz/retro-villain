import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from retro.modules.accountant.handover_dates import receipt_day
from retro.report_cache import load_iiko
from retro.logging_config import log_safe_failure
from retro.modules.accountant.ledger import LedgerError
from retro.modules.shokh.store import pocket_position

from .archive import archive_boundary
from .expenses import AutomaticExpense, Expense
from .export import export_report
from .service import DataError, TZ, demo_snapshot, today_tashkent
from .transfer_breakdown import PAYMENT_NAME
from .till import (HandoverChanged, NothingToHandOver, add_usd_deposit, delete_shokh_give,
                   delete_usd_deposit, give_shokh, hand_over, handover_check, shokh_gives, shokh_total,
                   till_summary, usd_balance, usd_day)

router = APIRouter(prefix='/api/cashier', tags=['cashier'])

# Завершившийся день в iiko сам не меняется; правки вносит человек, и для них
# есть кнопка обновления.
CLOSED_DAY_TTL = 15 * 60
# Передавать можно только по свежим цифрам: снимок живого дня старше этого —
# повод обновить отчёт, иначе в передачу не попадут последние продажи.
HANDOVER_SNAPSHOT_AGE = timedelta(minutes=5)


def entries_revision(items):
    return hashlib.sha256(json.dumps([item.json() for item in items],
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def gives_revision(gives):
    return hashlib.sha256(json.dumps(gives, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ExpenseInput(BaseModel):
    date: date
    description: str
    amount: str


class ReceiptInput(ExpenseInput):
    pass


class UsdBalanceInput(BaseModel):
    date: date
    amount: str


class AmountInput(BaseModel):
    date: date
    amount: str


class HandoverInput(BaseModel):
    date: date
    # Снимок iiko, который кассир видит на экране: сумму считаем по нему же.
    snapshot_id: str = Field(min_length=32, max_length=32, pattern='^[a-f0-9]+$')
    # Сумма «К передаче» на экране. Разошлась с серверной — не пишем, а просим
    # проверить: другой телефон мог добавить расход.
    expected: str | None = None


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
                revision=entries_revision(expenses),
                total=str(sum((item.amount for item in expenses), 0)),
                expense_policy_configured=request.app.state.expenses.policy_configured())


@router.get('/usd-rate')
async def usd_rate(request: Request, date: date):
    day = selected_day(date)
    try:
        return (await request.app.state.usd_rates.get(day)).json()
    except DataError as error:
        log_safe_failure('cashier-route', error, operation='usd_rate',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


# ── Доллары сразу в сейф ─────────────────────────────────────────────────
# Раньше за день хранилась одна сумма; теперь — взносы с временем. Они же —
# приход резерва `usd` у бухгалтера («Доллары в сейфе»).

@router.get('/usd-balance')
def usd_balance_route(request: Request, date: date):
    """Прежний ответ: доллары за день одной суммой (сумма взносов)."""
    result = usd_balance(request.app.state.accountant_finance, selected_day(date))
    return dict(date=result['date'], amount=result['amount'])


@router.post('/usd-balance', status_code=410)
def save_usd_balance(request: Request, body: UsdBalanceInput):
    raise HTTPException(410, 'Доллары теперь кладутся в сейф взносами. Обновите страницу.')


@router.get('/usd-deposits')
def list_usd_deposits(request: Request, date: date):
    return usd_day(request.app.state.accountant_finance, selected_day(date))


@router.post('/usd-deposits', status_code=201)
def add_usd(request: Request, body: AmountInput):
    day = selected_day(body.date)
    finance = request.app.state.accountant_finance
    try:
        deposit = add_usd_deposit(finance, day, body.amount)
    except LedgerError as error:
        raise HTTPException(422, str(error)) from None
    return dict(deposit=deposit, **usd_day(finance, day))


@router.delete('/usd-deposits/{deposit_id}', status_code=204)
def remove_usd(request: Request, deposit_id: int, date: date):
    day = selected_day(date)
    try:
        found = delete_usd_deposit(request.app.state.accountant_finance, deposit_id, day)
    except LedgerError as error:
        raise HTTPException(409, str(error)) from None
    if not found:
        raise HTTPException(404, 'Взнос не найден для выбранного дня.')
    return Response(status_code=204)


# ── Выдать Шоху из кассы ─────────────────────────────────────────────────

def shokh_payload(state, day):
    gives = shokh_gives(state.accountant_finance, day)
    # «На руках у него» — та же формула, что на экране закупа и у учредителя.
    position = pocket_position(state.shokh, state.accountant_finance, day)
    return dict(date=day.isoformat(), gives=gives, total=str(shokh_total(gives)),
                pocket=position['pocket'], revision=gives_revision(gives))


@router.get('/shokh')
def shokh_day(request: Request, date: date):
    return shokh_payload(request.app.state, selected_day(date))


@router.post('/shokh', status_code=201)
def give_to_shokh(request: Request, body: AmountInput):
    day = selected_day(body.date)
    try:
        give = give_shokh(request.app.state.accountant_finance, day, body.amount)
    except LedgerError as error:
        raise HTTPException(422, str(error)) from None
    return dict(give=give, **shokh_payload(request.app.state, day))


@router.delete('/shokh/{give_id}', status_code=204)
def remove_shokh_give(request: Request, give_id: int, date: date):
    day = selected_day(date)
    try:
        found = delete_shokh_give(request.app.state.accountant_finance, give_id, day)
    except LedgerError as error:
        raise HTTPException(409, str(error)) from None
    if not found:
        raise HTTPException(404, 'Выдача не найдена для выбранного дня.')
    return Response(status_code=204)


# ── Передача бухгалтеру ──────────────────────────────────────────────────

def handover_snapshot(state, day, snapshot_id):
    try:
        snapshot = state.cache.get(snapshot_id, day)
    except DataError:
        raise HTTPException(409, 'Отчёт на экране устарел. Обновите его и передайте ещё раз.') from None
    if snapshot.demo:
        raise HTTPException(422, 'В демонстрационном режиме передача не записывается.')
    if snapshot.stale or snapshot.refreshing:
        raise HTTPException(409, 'Дождитесь обновления iiko, затем передайте.')
    now = datetime.now(TZ)
    if now < archive_boundary(day) and now - snapshot.fetched_at > HANDOVER_SNAPSHOT_AGE:
        raise HTTPException(409, 'Данные iiko старше 5 минут. Обновите отчёт и передайте ещё раз.')
    return snapshot


def screen_amount(value):
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(422, 'Некорректная сумма передачи.') from None
    if not amount.is_finite():
        raise HTTPException(422, 'Некорректная сумма передачи.')
    return amount


@router.get('/summary')
def day_summary(request: Request, date: date,
                snapshot_id: str = Query(min_length=32, max_length=32, pattern='^[a-f0-9]+$')):
    """«К передаче» и «Касса за день» после записи кассира — числа с сервера,
    по тому снимку iiko, что открыт на экране."""
    day = selected_day(date)
    try:
        snapshot = request.app.state.cache.get(snapshot_id, day)
    except DataError:
        raise HTTPException(409, 'Отчёт на экране устарел. Обновите его.') from None
    return till_summary(request.app.state, day, snapshot)


@router.get('/payment-breakdown')
async def payment_breakdown(request: Request, date: date,
                            snapshot_id: str = Query(min_length=32, max_length=32,
                                                     pattern='^[a-f0-9]+$')):
    day = selected_day(date)
    state = request.app.state
    try:
        snapshot = state.cache.get(snapshot_id, day)
    except DataError:
        raise HTTPException(409, 'Отчёт на экране устарел. Обновите его.') from None
    if snapshot.stale or snapshot.refreshing:
        raise HTTPException(409, 'Дождитесь обновления iiko, затем откройте детализацию.')
    if snapshot.demo:
        raise HTTPException(422, 'В демонстрационном режиме детализация iiko недоступна.')
    expected = sum((payment.amount for payment in snapshot.payments
                    if payment.name == PAYMENT_NAME), Decimal(0))
    try:
        result = await load_iiko(state, 'load_transfer_breakdown', day, request=request,
                                 ttl=CLOSED_DAY_TTL if day < today_tashkent() else 30)
        if Decimal(result['total']) != expected:
            # Refresh can replace the displayed day while a previous detail
            # report remains cached. Give that report one fresh read as well.
            result = await load_iiko(state, 'load_transfer_breakdown', day, request=request,
                                     refresh=True,
                                     ttl=CLOSED_DAY_TTL if day < today_tashkent() else 30)
    except (DataError, TimeoutError) as error:
        log_safe_failure('cashier-route', error, operation='payment_breakdown')
        raise HTTPException(503, 'Не удалось получить детализацию переводов из iiko. Повторите запрос.') from None
    if Decimal(result['total']) != expected:
        raise HTTPException(409, 'Данные iiko изменились. Обновите день.')
    return result


@router.get('/handover')
def handover_state(request: Request, date: date):
    day = selected_day(date)
    return dict(date=day.isoformat(),
                handover=handover_check(request.app.state, day))


@router.post('/handover', status_code=201)
def hand_over_to_accountant(request: Request, body: HandoverInput):
    """«Передать бухгалтеру»: сумма считается здесь, по формуле бухгалтера.

    Повторное нажатие после новых расходов или продаж переписывает сумму дня
    («передайте разницу позже»); запись самого бухгалтера кнопка не трогает."""
    day = selected_day(body.date)
    state = request.app.state
    snapshot = handover_snapshot(state, day, body.snapshot_id)
    try:
        handover = hand_over(state, day, snapshot, expected=screen_amount(body.expected))
    except NothingToHandOver as error:
        raise HTTPException(422, str(error)) from None
    except LedgerError as error:  # в том числе HandoverChanged
        raise HTTPException(409, str(error)) from None
    return dict(date=day.isoformat(), handover=handover)


@router.delete('/handover', status_code=204)
def cancel_handover(request: Request, date: date):
    day = selected_day(date)
    try:
        request.app.state.accountant_finance.delete_handover(receipt_day(day), only_source='cashier')
    except LedgerError as error:
        message = str(error)
        if 'используется в остатках' in message:
            message = ('Бухгалтер уже провёл операции с этими деньгами — отменить передачу нельзя. '
                       'Если сумма неверна, нажмите «Передать» ещё раз или скажите бухгалтеру.')
        raise HTTPException(409, message) from None
    return Response(status_code=204)


# ── Ручные расходы и поступления ─────────────────────────────────────────

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
    try:
        found = request.app.state.expenses.delete(expense_id, day)
    except AutomaticExpense as error:
        raise HTTPException(409, str(error)) from None
    if not found:
        raise HTTPException(404, 'Расход не найден для выбранного дня.')
    return Response(status_code=204)


@router.get('/receipts')
def list_receipts(request: Request, date: date):
    day = selected_day(date)
    receipts = request.app.state.expenses.list_receipts(day)
    return dict(date=day.isoformat(), receipts=[item.json() for item in receipts],
                revision=entries_revision(receipts),
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
async def day_report(request: Request, date: date | None = None, demo: bool = False,
                     refresh: bool = False, allow_stale: bool = False):
    day = selected_day(date)
    state = request.app.state
    try:
        if demo:
            result = demo_snapshot(day)
        else:
            # Закрытый день уже не меняется — держим его снимок дольше
            # тридцати секунд, иначе каждое открытие страницы идёт в iiko.
            result = await load_iiko(state, 'load', day, refresh=refresh, request=request,
                                     ttl=CLOSED_DAY_TTL if day < today_tashkent() else None,
                                     allow_stale=allow_stale)
            # Load the registry separately so old daily archives also receive it,
            # and an unavailable transaction report never hides sales/handover.
            try:
                entries = await load_iiko(state, 'load_prepayments', day,
                                          refresh=refresh, request=request, timeout=35,
                                          ttl=CLOSED_DAY_TTL if day < today_tashkent() else 30)
                result = replace(result, prepayments=entries, prepayments_issue=None)
            except (DataError, TimeoutError) as error:
                log_safe_failure('cashier-route', error, operation='prepayments')
                result = replace(result, prepayments=None,
                                 prepayments_issue='Не удалось получить список предоплат из iiko. Обновите день.')
            # Keep an already displayed/exportable registry immutable even when
            # the base daily archive still has the same snapshot id.
            registry_revision = json.dumps(
                [result.id, [entry.json() for entry in result.prepayments]
                 if result.prepayments is not None else None, result.prepayments_issue],
                ensure_ascii=False, sort_keys=True)
            result = replace(result, id=hashlib.sha256(registry_revision.encode()).hexdigest()[:32])
        state.cache.put(result)
        # Передача дня бухгалтеру: «Передано в 21:40» у кассира. В демо — никогда.
        handover = None if result.demo else await asyncio.to_thread(handover_check, state, day)
        return {**result.json(),
                'expense_policy_configured': await asyncio.to_thread(state.expenses.policy_configured),
                'handover': handover,
                'summary': await asyncio.to_thread(till_summary, state, day, result)}
    except TimeoutError as error:
        log_safe_failure('cashier-route', error, operation='day_report',
                         request_id=request.state.request_id)
        raise HTTPException(504, 'iiko отвечает дольше обычного. Повторите запрос.') from None
    except DataError as error:
        log_safe_failure('cashier-route', error, operation='day_report',
                         request_id=request.state.request_id)
        raise HTTPException(503, str(error)) from None


def till_expense_rows(day, gives):
    """Выдачи Шоху из кассы в выгрузке идут строками расходов: так итог
    расходов и «К передаче» в файле те же, что на экране."""
    return [Expense(None, day, 'Шоху на закуп' + (f' · {give["created_at"][11:16]}'
                                                   if give['created_at'] else ''),
                    Decimal(give['amount'])) for give in gives]


@router.get('/export')
def download_report(request: Request, date: date,
                    snapshot_id: str = Query(min_length=32, max_length=32, pattern='^[a-f0-9]+$'),
                    expense_revision: str | None = None, receipt_revision: str | None = None,
                    shokh_revision: str | None = None):
    day = selected_day(date)
    try:
        snapshot = request.app.state.cache.get(snapshot_id, day)
    except DataError as error:
        raise HTTPException(409, str(error)) from None
    if snapshot.stale or snapshot.refreshing:
        raise HTTPException(409, 'Дождитесь обновления iiko перед скачиванием отчёта.')
    expenses = [] if snapshot.demo else request.app.state.expenses.list(day)
    receipts = [] if snapshot.demo else request.app.state.expenses.list_receipts(day)
    gives = [] if snapshot.demo else shokh_gives(request.app.state.accountant_finance, day)
    if ((expense_revision is not None and expense_revision != entries_revision(expenses))
            or (receipt_revision is not None and receipt_revision != entries_revision(receipts))
            or (shokh_revision is not None and shokh_revision != gives_revision(gives))):
        raise HTTPException(409, 'Расходы или поступления изменились. Обновите день перед скачиванием.')
    data = export_report(snapshot, [*expenses, *till_expense_rows(day, gives)], receipts)
    prefix = 'DEMO-' if snapshot.demo else ''
    return Response(data, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{prefix}Retro-{day.isoformat()}.xlsx"'})


# ── Реестр предоплат (ТЗ «Выручка, оплаты и предоплаты», 08.10.2026, п. 3.2) ─────

class PrepaymentNoteInput(BaseModel):
    guest: str = Field('', max_length=120)
    phone: str = Field('', max_length=40)
    event_day: date | None = None
    method: str = Field('', max_length=60)
    refunded: bool = False


def demo_registry(day, period=False):
    """Пример реестра для демонстрационного режима: ничего не пишет и не читает iiko."""
    def row(order, received, amount, guest, phone, event, method, credited=None):
        return dict(order_number=order, received_day=received.isoformat(),
                    received_at=f'{received.isoformat()}T13:20:00+05:00', amount=amount, guest=guest,
                    phone=phone, event_day=event.isoformat() if event else None, method=method, refunded=False,
                    credited_day=credited.isoformat() if credited else None,
                    credited_amount=amount if credited else None, credited_methods=['Демо'] if credited else [],
                    updated_at=None, updated_by=None, status='credited' if credited else 'pending',
                    method_shown=method or ('Демо' if credited else ''))
    received = [row('102', day, '3000000', 'Гость (пример)', '', day + timedelta(days=3), 'Наличные')]
    credited = [row('101', day - timedelta(days=3), '2000000', 'Банкет (пример)', '', day, '', credited=day)]
    if period:
        return dict(start=(day - timedelta(days=14)).isoformat(), end=(day + timedelta(days=14)).isoformat(),
                    rows=credited + received, issue=None)
    return dict(date=day.isoformat(), received=received, credited=credited, received_total='3000000',
                credited_total='2000000', issue=None)


def group_advances(entries, day=None):
    """Авансы iiko → по заказу: [(заказ, день получения, первое время, сумма)].

    Один заказ часто получает аванс частями (наличные, потом картой) — в реестре
    это одна предоплата. Без номера заказа аванс держим отдельной строкой."""
    orders = {}
    for entry in entries or ():
        key = entry.order_number or 'iiko-' + entry.id[:12]
        received_at, amount = orders.get(key, (entry.received_at, Decimal(0)))
        orders[key] = (min(received_at, entry.received_at), amount + entry.amount)
    return [(order, day or date.fromisoformat(received_at[:10]), received_at, amount)
            for order, (received_at, amount) in orders.items()]


@router.get('/prepayments')
async def prepayment_registry(request: Request, date: date | None = None,
                              start: date | None = None, end: date | None = None, demo: bool = False):
    """Реестр: «Получено сегодня», «Зачтено сегодня» или все предоплаты за период."""
    state = request.app.state
    registry = state.prepayment_registry
    today = today_tashkent()
    if demo:
        return demo_registry(date or start or today, period=start is not None or end is not None)
    if start is not None or end is not None:
        if start is None or end is None or start > end:
            raise HTTPException(422, 'Укажите период: с какой и по какую дату.')
        if (end - start).days > 62:
            raise HTTPException(422, 'Период реестра — не больше двух месяцев.')
        if start > today:
            raise HTTPException(422, 'Период начинается в будущем: предоплат за него ещё нет.')
        # Из iiko — авансы до сегодня; реестр — и с датами событий впереди.
        fetch_end = min(end, today)
        issue = None
        try:
            entries = await load_iiko(state, 'load_prepayments_range', start, fetch_end, request=request,
                                      timeout=60, ttl=CLOSED_DAY_TTL if fetch_end < today else 30)
            await asyncio.to_thread(registry.record_received, group_advances(entries))
            rows = await asyncio.to_thread(registry.rows_between, start, end)
            waiting = tuple(row['order_number'] for row in rows
                            if row['status'] == 'pending' and row['order_number'].isdigit())
            if waiting:
                found = await load_iiko(state, 'prepayment_redemptions', waiting, start, today,
                                        request=request, timeout=60, ttl=60)
                by_day = {}
                for order, day_text, method, amount in found:
                    if amount:
                        totals = by_day.setdefault(day_text, {}).setdefault(str(order), [Decimal(0), []])
                        totals[0] += amount
                        if method not in totals[1]:
                            totals[1].append(method)
                for day_text, orders in by_day.items():
                    await asyncio.to_thread(registry.record_credited, datetime.fromisoformat(day_text).date(),
                                            [(order, total, methods) for order, (total, methods) in orders.items()])
        except (DataError, TimeoutError) as error:
            log_safe_failure('cashier-route', error, operation='prepayment_registry_period')
            issue = 'Не всё удалось получить из iiko: показано то, что уже есть в реестре.'
        rows = await asyncio.to_thread(registry.rows_between, start, end)
        return dict(start=start.isoformat(), end=end.isoformat(), rows=rows, issue=issue)

    day = selected_day(date)
    issue = None
    try:
        snapshot = await load_iiko(state, 'load', day, request=request, allow_stale=True,
                                   ttl=CLOSED_DAY_TTL if day < today else None)
        entries = await load_iiko(state, 'load_prepayments', day, request=request, timeout=35,
                                  ttl=CLOSED_DAY_TTL if day < today else 30)
    except (DataError, TimeoutError) as error:
        log_safe_failure('cashier-route', error, operation='prepayment_registry')
        raise HTTPException(503, 'Не удалось получить предоплаты из iiko. Обновите день.') from None
    received = group_advances(entries, day)
    credited = snapshot.redeemed_orders or ()
    await asyncio.to_thread(registry.record_received, received)
    await asyncio.to_thread(registry.record_credited, day,
                            [(order.order_number, order.amount, order.methods) for order in credited])
    # Когда получен аванс по зачтённому сегодня заказу — ищем в iiko на 90 дней назад
    # (банкет оплачивают заранее), один раз: дальше это лежит в реестре.
    missing = await asyncio.to_thread(registry.missing_received, [order.order_number for order in credited])
    if missing:
        try:
            found = await load_iiko(state, 'load_prepayments_range', day - timedelta(days=90),
                                    day - timedelta(days=1), tuple(missing), request=request,
                                    timeout=40, ttl=600)
            await asyncio.to_thread(registry.record_received, group_advances(found))
        except (DataError, TimeoutError) as error:
            log_safe_failure('cashier-route', error, operation='prepayment_received_lookup')
            issue = 'По части зачтённых предоплат iiko не сказал, когда они получены.'
    received_rows = await asyncio.to_thread(registry.rows, [item[0] for item in received])
    credited_rows = await asyncio.to_thread(registry.rows, [order.order_number for order in credited])
    if snapshot.redeemed_orders is None:
        issue = issue or 'iiko не отдал зачёт предоплат за этот день.'
    return dict(date=day.isoformat(), received=received_rows, credited=credited_rows,
                received_total=str(sum((item[3] for item in received), Decimal(0))),
                credited_total=str(sum((order.amount for order in credited), Decimal(0))),
                issue=issue)


@router.put('/prepayments/{order_number}')
def annotate_prepayment(request: Request, order_number: str, body: PrepaymentNoteInput):
    """Кассир вписывает, кто внёс предоплату, телефон, на какую дату, способ, возврат."""
    try:
        return request.app.state.prepayment_registry.annotate(
            order_number, guest=body.guest, phone=body.phone, event_day=body.event_day,
            method=body.method, refunded=body.refunded,
            by=getattr(request.state, 'dashboard_user', None))
    except DataError as error:
        raise HTTPException(422, str(error)) from None
