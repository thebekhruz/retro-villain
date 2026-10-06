"""Отчёт бухгалтера за месяц в Excel для учредителя.

Деньги берутся из журнала бухгалтера, выручка и чеки — из iiko по кассам.
Если iiko недоступен, файл всё равно собирается: колонки iiko остаются
пустыми, а в шапке написано почему, — пустая ячейка не выдаёт себя за ноль.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from retro.accounting_period import accounting_range_start
from retro.modules.accountant.reserves import is_monthly_salary
from retro.modules.accountant.handover_dates import receipt_day

from . import overview

MONEY = '#,##0'
HEAD = PatternFill('solid', fgColor='143E35')


def text(cell, value):
    cell.value = value
    cell.data_type = 's'  # подписи из журнала не должны становиться формулами Excel


def closing_balance(state, day: date, handover):
    finance = state.accountant_finance
    manual = state.settings.manual_handover_only
    anchor = finance.cash_opening(day)
    carry_start = date.fromisoformat(anchor['day']) if anchor and manual else None
    summary = finance.daily_summary(
        day, handover, carry_history=not manual or (carry_start is not None and day >= carry_start),
        carry_start=carry_start)
    return summary['cash_balance']


def salary_split(rows):
    """{день: (сменные, оклады)} — выплаты сменным по начислениям и строки
    «Зарплаты» журнала: оклады отдельно, остальное — сменные."""
    result = {}
    for row in rows:
        if overview.flow_kind(row) != 'salary':
            continue
        shift, monthly = result.get(row['day'], (Decimal(0), Decimal(0)))
        if row['type'] != 'salary_payment' and is_monthly_salary(row.get('item_code')):
            monthly += Decimal(row['amount'])
        else:
            shift += Decimal(row['amount'])
        result[row['day']] = (shift, monthly)
    return result


def month_workbook(state, first: date, last: date, orders, orders_error, cashier=None, cashier_error=None):
    """Лист «По дням» — колонки Функционала (7b): Retro, Oxbridge, Демо, передал
    кассир, расчёт кассира, сменные, оклады, закуп, прочие, дивиденды, остаток;
    за ними чеки и прочие поступления. `cashier` — {день: касса кассира} из
    iiko (Демо и расчёт передачи), None — iiko не ответил."""
    first = accounting_range_start(first, last)
    flows_rows = state.accountant_finance.cash_flows_between(first, last)
    flows = overview.daily_flows(flows_rows)
    salaries = salary_split(flows_rows)
    cashier = cashier or {}
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = 'По дням'
    title = f'Retro Milliy · отчёт бухгалтера · {first.strftime("%m.%Y")}'
    text(sheet['A1'], title)
    sheet['A1'].font = Font(bold=True, size=13)
    note = ('Выручка и чеки — iiko по кассам, без банкетного зала. Деньги — журнал бухгалтера.'
            if orders is not None else f'Колонки iiko пустые: {orders_error}')
    if cashier_error:
        note += f' Демо и расчёт кассира пустые за дни без ответа iiko: {cashier_error}'
    note += ' Приход бухгалтера — за предыдущую смену. Сверка смен и дата закрытия кассы месяца — на листе «Передачи смен».'
    text(sheet['A2'], note)
    heads = ['Дата', 'Retro · выручка', 'Oxbridge · выручка', 'Демо · Retro', 'Приход за прошлую смену',
             'Расчёт кассира', 'Сменные', 'Оклады', 'Закуп · Шох и напрямую', 'Прочие расходы',
             'Дивиденды', 'Остаток на конец дня', 'Чеки Retro', 'Чеки Oxbridge', 'Прочие поступления']
    counts = (13, 14)
    for column, head in enumerate(heads, start=1):
        cell = sheet.cell(row=4, column=column)
        text(cell, head)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = HEAD
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    row_index = 5
    day = first
    while day <= last:
        values = flows.get(day.isoformat(), {})
        registers = (orders or {}).get(day.isoformat(), {})
        handover = values.get('handover')
        balance = closing_balance(state, day, handover)
        till = cashier.get(day.isoformat())
        shift, monthly = salaries.get(day.isoformat(), (Decimal(0), Decimal(0)))
        cells = [
            datetime.combine(day, datetime.min.time()),
            registers.get('retro', {}).get('revenue') if orders is not None else None,
            registers.get('school', {}).get('revenue') if orders is not None else None,
            Decimal(till['demo']) if till else None,
            handover,
            Decimal(till['expected']) if till and till.get('expected') is not None else None,
            shift, monthly,
            values.get('procurement', Decimal(0)), values.get('other', Decimal(0)),
            values.get('dividends', Decimal(0)), balance,
            registers.get('retro', {}).get('orders') if orders is not None else None,
            registers.get('school', {}).get('orders') if orders is not None else None,
            values.get('receipt', Decimal(0)),
        ]
        for column, value in enumerate(cells, start=1):
            cell = sheet.cell(row=row_index, column=column, value=value)
            if column == 1:
                cell.number_format = 'dd.mm.yyyy'
            elif column not in counts:
                cell.number_format = MONEY
        row_index += 1
        day += timedelta(days=1)
    for column, width in zip('ABCDEFGHIJKLMNO', (12, 16, 16, 14, 16, 16, 14, 14, 16, 14, 14, 16, 11, 11, 14)):
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = 'B5'

    transfers = workbook.create_sheet('Передачи смен')
    month_end = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    text(transfers['A1'], f'Касса смен за {first.strftime("%m.%Y")} · закрытие {receipt_day(month_end).strftime("%d.%m.%Y")}')
    transfers['A1'].font = Font(bold=True, size=13)
    text(transfers['A2'], 'Выручка относится к дате смены, приход бухгалтера — к следующему дню. Суммы приходов также показаны по дате получения на листе «По дням».')
    transfer_heads = ('Дата смены', 'Дата прихода', 'Расчёт кассира', 'Приход', 'Подтверждено бухгалтером')
    for column, head in enumerate(transfer_heads, start=1):
        cell = transfers.cell(4, column)
        text(cell, head)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = HEAD
    shift_day = first
    row_index = 5
    while shift_day <= last:
        received = receipt_day(shift_day)
        saved = state.accountant_finance.handover_state(received) or {}
        till = cashier.get(shift_day.isoformat()) or {}
        values = (datetime.combine(shift_day, datetime.min.time()),
                  datetime.combine(received, datetime.min.time()),
                  Decimal(till['expected']) if till.get('expected') is not None else None,
                  Decimal(saved['amount']) if saved.get('amount') is not None else None,
                  saved.get('confirmed_at'))
        for column, value in enumerate(values, start=1):
            cell = transfers.cell(row_index, column, value)
            cell.number_format = 'dd.mm.yyyy' if column <= 2 else MONEY if column <= 4 else 'General'
        row_index += 1
        shift_day += timedelta(days=1)
    for column, width in zip('ABCDE', (16, 16, 22, 22, 32)):
        transfers.column_dimensions[column].width = width
    transfers.freeze_panes = 'C5'

    categories = workbook.create_sheet('Расходы')
    text(categories['A1'], f'Куда ушли деньги · {first.strftime("%m.%Y")}')
    categories['A1'].font = Font(bold=True, size=13)
    # Как в кабинете: перечисления — своей категорией, выдачи Шоху из кассы —
    # один раз в «Закуп · наличные Шоху». В «По дням» их нет: это не деньги
    # бухгалтера (передача кассира уже меньше на эту сумму).
    from retro.modules.cashier.till import shokh_gives, shokh_total
    spending = overview.expense_categories(
        flows_rows, state.accountant_finance.supplier_transfers(first, last),
        shokh_from_till=shokh_total(shokh_gives(state.accountant_finance, first, last)))
    for column, head in enumerate(('Категория', 'Сумма', 'Доля, %'), start=1):
        cell = categories.cell(row=3, column=column)
        text(cell, head)
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = HEAD
    for index, item in enumerate(spending['categories'], start=4):
        text(categories.cell(row=index, column=1), item['label'])
        categories.cell(row=index, column=2, value=Decimal(item['amount'])).number_format = MONEY
        share = item['share_percent']
        categories.cell(row=index, column=3, value=Decimal(share) if share else None)
    total_row = 4 + len(spending['categories'])
    text(categories.cell(row=total_row, column=1), 'Итого')
    categories.cell(row=total_row, column=1).font = Font(bold=True)
    categories.cell(row=total_row, column=2, value=Decimal(spending['total'])).number_format = MONEY
    categories.column_dimensions['A'].width = 32
    categories.column_dimensions['B'].width = 16
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
