"""Выгрузки бухгалтера в Excel: «Финансы дня» и ведомость месяца.

Берут ровно те данные, что видит экран, чтобы файл и страница не расходились:
«Финансы дня» — ответ `/day` за день выплат P и `/staff` за вчерашнюю смену
S = P − 1 (её и выдают в день P), ведомость — ответ `/payroll/month`.
"""

from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .employee_export import STATUS

GREEN = '143E35'
INK = '1C302B'
MUTED = '7D8881'
PALE = 'F5F7F1'
PINK = 'FCE6E9'
LINE = Side(style='thin', color='E3E7DF')
MONEY = '#,##0'
ABSENT = {'missing', 'manual_absent'}
MONTHLY_ITEM = 'salary_monthly'
# Тип строки журнала → подпись. Приходы идут в свою колонку, остальное — расход.
KIND = {'auto_cashier': 'Касса', 'other_receipt': 'Приход', 'other_expense': 'Расход',
        'procurement_advance': 'Выдано Шоху', 'salary_payment': 'Зарплата',
        'reserve_transfer': 'В сейф'}
INFLOW = {'auto_cashier', 'other_receipt'}
HANDOVER_SOURCE = {'cashier': 'кассир', 'accountant': 'бухгалтер', 'auto': 'по отчёту iiko'}


def _money(value) -> Decimal | None:
    if value in (None, ''):
        return None
    return Decimal(str(value))


def _sum(values) -> Decimal:
    return sum((value for value in values if isinstance(value, Decimal)), Decimal(0))


def _title(sheet, text: str, width: int):
    sheet.sheet_view.showGridLines = False
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=width)
    cell = sheet.cell(1, 1, text)
    cell.data_type = 's'
    cell.fill = PatternFill('solid', fgColor=GREEN)
    cell.font = Font(name='Calibri', size=15, bold=True, color='FFFFFF')
    cell.alignment = Alignment(vertical='center', indent=1)
    sheet.row_dimensions[1].height = 30


def _note(sheet, row: int, text: str, width: int):
    sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=width)
    cell = sheet.cell(row, 1, text)
    cell.data_type = 's'
    cell.font = Font(name='Calibri', size=10, italic=True, color=MUTED)
    cell.alignment = Alignment(vertical='center', wrap_text=True)


def _section(sheet, row: int, text: str):
    cell = sheet.cell(row, 1, text)
    cell.data_type = 's'
    cell.font = Font(name='Calibri', size=11, bold=True, color=GREEN)


def _head(sheet, row: int, labels: list[str]):
    for index, label in enumerate(labels, 1):
        cell = sheet.cell(row, index, label)
        cell.data_type = 's'
        cell.fill = PatternFill('solid', fgColor=PALE)
        cell.font = Font(name='Calibri', size=10, bold=True, color=MUTED)
        cell.border = Border(bottom=LINE)
        cell.alignment = Alignment(vertical='center', horizontal='left' if index == 1 else 'center')
    sheet.row_dimensions[row].height = 22


def _row(sheet, row: int, values: list, *, bold=False, money_from=2):
    for index, value in enumerate(values, 1):
        cell = sheet.cell(row, index, value)
        if isinstance(value, str):
            # Имена и подписи вводят люди: «=…» не должно стать формулой Excel.
            cell.data_type = 's'
        cell.font = Font(name='Calibri', size=11, bold=bold, color=INK)
        cell.border = Border(bottom=LINE)
        if isinstance(value, Decimal) and index >= money_from:
            cell.number_format = MONEY


def _widths(sheet, widths: list[int]):
    for index, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width


def _time(value: str | None) -> str:
    """«08:40» из ISO-времени, уже записанного по Ташкенту."""
    return value[11:16] if value and len(value) >= 16 else '—'


def _dm(value: str) -> str:
    return f'{value[8:10]}.{value[5:7]}'


def _status(value: str) -> str:
    return STATUS.get(value, value)


# ── Финансы дня ─────────────────────────────────────────────────────────────

def day_workbook(data: dict, staff: dict | None = None) -> bytes:
    """`data` — ответ `/day` за день выплат, `staff` — `/staff` за вчерашнюю смену."""
    payday = date.fromisoformat(data['date'])
    workbook = Workbook()
    _shift_sheet(workbook.active, payday, data['ledger'], staff or {})
    _journal_sheet(workbook.create_sheet('Операции'), payday, data)
    _total_sheet(workbook.create_sheet('Итог'), payday, data)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _shift_sheet(sheet, payday: date, ledger: dict, staff: dict):
    sheet.title = 'Смена'
    shift_day = payday - timedelta(days=1)
    width = 7
    _title(sheet, f'RETRO MILLIY · Финансы дня {payday.strftime("%d.%m.%Y")}', width)
    _widths(sheet, [34, 18, 10, 18, 16, 16, 16])
    accruals = ledger.get('accruals') or []
    own = [item for item in accruals if item['work_day'] == shift_day.isoformat()]
    people = {row['employee_id']: row for row in staff.get('employees') or []}
    # Как на экране: есть начисления за S — смена подтверждена, строки из
    # ведомости; нет — черновик из проходов и ставок за S.
    confirmed = bool(own)
    _note(sheet, 2, f'Смена {shift_day.strftime("%d.%m.%Y")} — выдаётся {payday.strftime("%d.%m.%Y")}. ' + (
        'Смена подтверждена: «Начислено» — по ведомости, «Выдано» — по этот день включительно.'
        if confirmed else
        'Смена не подтверждена: суммы предварительные, по проходам и ставкам; выдавать можно после подтверждения.'),
        width)
    sheet.row_dimensions[2].height = 30
    _head(sheet, 4, ['Сотрудник', 'Должность', 'Вход', 'Статус', 'Начислено', 'Выдано', 'Осталось'])
    lines = []
    if confirmed:
        for accrual in own:
            person = people.get(accrual['employee_id'], {})
            lines.append((accrual['name'], person.get('role') or accrual.get('group') or '',
                          person.get('first_entry'), accrual['status'], _money(accrual['amount']),
                          _money(accrual['paid']), _money(accrual['debt'])))
    else:
        for person in staff.get('employees') or []:
            payable = _money(person.get('payable'))
            lines.append((person['name'], person.get('role') or '', person.get('first_entry'),
                          person['status'], payable, Decimal(0) if payable is not None else None, payable))
    row = 5
    for name, role, entry, status, accrued, paid, debt in lines:
        _row(sheet, row, [name, role, _time(entry), _status(status), accrued, paid, debt], money_from=5)
        if status == 'late':
            sheet.cell(row, 4).fill = PatternFill('solid', fgColor=PINK)
        row += 1
    if not lines:
        _note(sheet, row, 'За смену записей нет.', width)
        row += 1
    _row(sheet, row, ['Итого за смену', None, None, None, _sum(line[4] for line in lines),
                      _sum(line[5] for line in lines), _sum(line[6] for line in lines)],
         bold=True, money_from=5)
    row += 1
    unknown = sum(line[4] is None for line in lines)
    if unknown:
        _note(sheet, row, f'Без суммы (нет ставки или данных Hikvision): {unknown} — в итог не вошли.', width)
        row += 1
    other = sorted((item for item in accruals
                    if item['work_day'] != shift_day.isoformat() and Decimal(item['debt']) > 0),
                   key=lambda item: (item['work_day'], item['name']))
    if other:
        row += 1
        _section(sheet, row, 'Другие смены — ещё не выдано')
        row += 1
        _head(sheet, row, ['Сотрудник', 'Смена', None, 'Статус', 'Начислено', 'Выдано', 'Осталось'])
        row += 1
        for item in other:
            _row(sheet, row, [item['name'], _dm(item['work_day']), None, _status(item['status']),
                              _money(item['amount']), _money(item['paid']), _money(item['debt'])],
                 money_from=5)
            row += 1
    sheet.freeze_panes = 'A5'


def _journal_sheet(sheet, payday: date, data: dict):
    ledger = data['ledger']
    width = 5
    _title(sheet, f'Операции за {payday.strftime("%d.%m.%Y")}', width)
    _widths(sheet, [16, 56, 14, 16, 16])
    _head(sheet, 3, ['Тип', 'Наименование', 'Время', 'Приход', 'Расход'])
    row = 4
    inflow = outflow = Decimal(0)
    for item in ledger.get('movements') or []:
        # «Остаток на начало дня» — расчёт, а не операция: он в «Итоге».
        if item['type'] == 'opening':
            continue
        amount = _money(item['amount'])
        label = KIND.get(item['type'], item['type'])
        if item['type'] == 'other_expense' and item.get('item_code') == MONTHLY_ITEM:
            label = 'Оклад'
        incoming = item['type'] in INFLOW
        _row(sheet, row, [label, item['description'], _time(item.get('created_at')),
                          amount if incoming else None, None if incoming else amount], money_from=4)
        if incoming:
            inflow += amount
        else:
            outflow += amount
        row += 1
    if row == 4:
        _note(sheet, row, 'Операций за день нет.', width)
        row += 1
    _row(sheet, row, ['Итого', None, None, inflow, outflow], bold=True, money_from=4)
    row += 2
    debts = ledger.get('manual_debts') or []
    if debts:
        _section(sheet, row, 'Долги по расходам')
        row += 1
        _head(sheet, row, ['С', 'Наименование', 'Всего', 'Оплачено', 'Осталось'])
        row += 1
        for item in debts:
            _row(sheet, row, [_dm(item['day']), item['description'], _money(item['total']),
                              _money(item['paid']), _money(item['debt'])], money_from=3)
            row += 1
        row += 1
    gives = (data.get('cashier_shokh_gives') or {}).get('gives') or []
    if gives:
        # Кассир выдал Шоху прямо из кассы: эти деньги не доходят до бухгалтера,
        # поэтому в приход/расход выше их нет — они уже вычтены из передачи кассира.
        _section(sheet, row, 'Выдано Шоху из кассы — не из остатка бухгалтера')
        row += 1
        _head(sheet, row, ['Кто', 'Наименование', 'Время', None, 'Сумма'])
        row += 1
        for item in gives:
            _row(sheet, row, ['Кассир', 'Выдано Шоху на закуп', _time(item.get('created_at')),
                              None, _money(item['amount'])], money_from=4)
            row += 1
        _row(sheet, row, ['Итого', None, None, None,
                          _sum(_money(item['amount']) for item in gives)], bold=True, money_from=4)
        row += 2
    transfers = data.get('supplier_transfers') or []
    if transfers:
        _section(sheet, row, 'Перечисления поставщикам — не из кассы, остаток не меняют')
        row += 1
        _head(sheet, row, ['Точка', 'Поставщик · за что', 'Время', None, 'Сумма'])
        row += 1
        for item in transfers:
            _row(sheet, row, [item['point'], f'{item["supplier"]} · {item["item"]}',
                              _time(item.get('created_at')), None, _money(item['amount'])], money_from=4)
            row += 1
    sheet.freeze_panes = 'A4'


def _total_sheet(sheet, payday: date, data: dict):
    ledger = data['ledger']
    flow = ledger.get('cash_flow') or {}
    movements = ledger.get('movements') or []

    def spent(check):
        return sum((Decimal(item['amount']) for item in movements if check(item)), Decimal(0))

    # Как карточка «Деньги на расходы»: оклады и выдачи Шоху отдельно от прочих.
    monthly = spent(lambda item: item['type'] == 'other_expense' and item.get('item_code') == MONTHLY_ITEM)
    shoh = spent(lambda item: item['type'] == 'procurement_advance')
    other = (_money(flow.get('other_outflows')) or Decimal(0)) - monthly - shoh
    _title(sheet, f'Деньги на расходы · {payday.strftime("%d.%m.%Y")}', 2)
    _widths(sheet, [46, 20])
    lines = [('На начало дня', _money(flow.get('opening_balance'))),
             ('+ От кассира', _money(flow.get('received_from_cashier'))),
             ('+ Прочие поступления', _money(flow.get('other_receipts'))),
             ('− Зарплаты сменным', _money(flow.get('salary_paid'))),
             ('− Оклады частями', monthly),
             ('− Выдано Шоху на закуп', shoh),
             ('− Прочие расходы и сейф', other),
             ('= Остаток на конец дня', _money(flow.get('closing_balance')))]
    row = 3
    for label, value in lines:
        _row(sheet, row, [label, value], bold=label.startswith('='))
        row += 1
    if flow.get('missing_day'):
        missing = date.fromisoformat(flow['missing_day']).strftime('%d.%m.%Y')
        _note(sheet, row, f'Остаток не посчитан: нет данных кассира за {missing}.', 2)
        row += 1
    row += 1
    # Справочно: ничего из этого не меняет остаток бухгалтера выше.
    handover = data.get('cashier_handover') or {}
    if handover.get('amount') is not None:
        who = HANDOVER_SOURCE.get(handover.get('source'), handover.get('source') or '—')
        received = _time(handover.get('handed_at'))
        _row(sheet, row, [f'Передача кассира · получено {received} · {who}',
                          _money(handover['amount'])])
        row += 1
    elif handover:
        _row(sheet, row, ['Передача кассира · ещё не записана', None])
        row += 1
    if handover.get('expected') is not None:
        _row(sheet, row, ['Передача по расчёту кассы (iiko)', _money(handover['expected'])])
        row += 1
    gives = data.get('cashier_shokh_gives') or {}
    if gives.get('gives'):
        _row(sheet, row, ['Шоху из кассы · не из остатка', _money(gives.get('total'))])
        row += 1
    shoh = ((data.get('reserves') or {}).get('shoh') or {}).get('balance')
    if shoh is not None:
        _row(sheet, row, ['Подотчёт Шоха на конец дня', _money(shoh)])
        row += 1
    for label, value in (('Долг сотрудникам по сменам', _money(ledger.get('salary_debt'))),
                         ('Долги по расходам', _money(ledger.get('manual_debt_total'))),
                         ('Перечислено поставщикам (не из кассы)',
                          _money(data.get('supplier_transfers_total')))):
        _row(sheet, row, [label, value])
        row += 1


# ── Ведомость месяца ────────────────────────────────────────────────────────

def payroll_workbook(data: dict) -> bytes:
    days = data['days']
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Ведомость'
    width = 2 + len(days) + 3
    _title(sheet, f'RETRO MILLIY · Зарплаты {data["month"]}', width)
    _widths(sheet, [34, 14] + [11] * len(days) + [16, 16, 16])
    _head(sheet, 3, ['Сотрудник', 'Ставка / оклад'] + [day[-2:] for day in days]
          + ['Начислено', 'Выдано', 'Осталось'])
    row = 4
    _section(sheet, row, 'Помесячные · оклад частями, в ячейке — выдано за день')
    row += 1
    cells = data.get('monthly_cells') or {}
    roster = data.get('monthly') or []
    known = {str(person['id']) for person in roster}
    people = [(person['name'], _money(person['salary']), cells.get(str(person['id']), {}))
              for person in roster]
    # Выплаты удалённым из реестра не пропадают из месяца: деньги ушли.
    people += [(f'Сотрудник удалён · №{key}', None, own)
               for key, own in sorted(cells.items(), key=lambda item: int(item[0]) if item[0].isdigit() else 0)
               if key not in known]
    per_day = [Decimal(0)] * len(days)
    paid_total = rest_total = Decimal(0)
    for name, salary, own in people:
        values = [_money(own.get(day)) for day in days]
        paid = _sum(values)
        rest = None if salary is None else salary - paid
        _row(sheet, row, [name, salary] + values + [salary, paid, rest])
        if rest is not None and rest < 0:
            sheet.cell(row, 2 + len(days) + 3).fill = PatternFill('solid', fgColor=PINK)
        for index, value in enumerate(values):
            if value is not None:
                per_day[index] += value
        paid_total += paid
        rest_total += max(Decimal(0), rest or Decimal(0))
        row += 1
    # Оклад, записанный общим расходом без сотрудника, — тоже выдан: без этой
    # строки итог месяца не сошёлся бы с журналом.
    unlinked = (_money(data.get('monthly_paid')) or Decimal(0)) - _sum(
        _money(value) for own in cells.values() for value in own.values())
    if unlinked > 0:
        _row(sheet, row, ['Оклады без сотрудника (общий расход)', None] + [None] * len(days)
             + [None, unlinked, None])
        paid_total += unlinked
        row += 1
    _row(sheet, row, ['Итого оклады', _money(data.get('monthly_total'))] + per_day
         + [_money(data.get('monthly_total')), paid_total, rest_total], bold=True)
    row += 2
    _section(sheet, row, 'Сменные · в ячейке — выдано за смену этого дня (выдают на следующий день)')
    row += 1
    shift_per_day = [Decimal(0)] * len(days)
    for person in sorted(data.get('shift') or [], key=lambda item: (item['employee_id'], item['name'])):
        values = []
        for index, day in enumerate(days):
            cell = person['cells'].get(day)
            if cell is None:
                values.append(None)
            elif cell['status'] in ABSENT and Decimal(cell['amount']) == 0:
                values.append('н/я')
            else:
                paid = _money(cell['paid'])
                values.append(paid)
                shift_per_day[index] += paid
        _row(sheet, row, [person['name'], _money(person['rate'])] + values
             + [_money(person['accrued']), _money(person['paid']), _money(person['debt'])])
        for index, day in enumerate(days):
            cell = person['cells'].get(day)
            if cell is not None and Decimal(cell['debt']) > 0:
                sheet.cell(row, 3 + index).fill = PatternFill('solid', fgColor=PINK)
        row += 1
    shift = data.get('shift') or []
    _row(sheet, row, ['Итого сменные', None] + shift_per_day
         + [_sum(_money(p['accrued']) for p in shift), _sum(_money(p['paid']) for p in shift),
            _sum(_money(p['debt']) for p in shift)], bold=True)
    row += 2
    # Из кассы по дням выдачи: сменные — по дню выплаты, оклады — по дню записи.
    paid_per_day = data.get('paid_per_day') or {}
    _row(sheet, row, ['Выдано из кассы за день', None]
         + [(_money(paid_per_day.get(day)) or Decimal(0)) + per_day[index] for index, day in enumerate(days)],
         bold=True)
    row += 1
    _note(sheet, row, 'Розовая ячейка — смена выдана не полностью; «н/я» — не был; '
          'розовый остаток оклада — переплата.', width)
    sheet.freeze_panes = 'C4'
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
