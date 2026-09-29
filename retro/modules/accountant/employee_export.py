"""Выгрузка «Сотрудники» (1a) в Excel: то же, что на экране за выбранный день.

Сменные — по группам в порядке реестра, с итогом группы и общим итогом к
начислению; статусы подписаны как на экране. Ниже — раздел «На окладе»:
оклад, выдано с 1-го числа по выбранный день, осталось (минус — переплата).
«Опоздавшие» — плоский список опоздавших без окладников.
"""

from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from retro.modules.cashier.service import TZ

from .payroll import PayrollRow

GREEN = '16483F'
INK = '21342E'
PINK = 'FCE6E9'
PALE = 'EAF0E9'
GROUP_FILL = 'DDE7DA'
RED = 'A0344B'
# Подписи — как на экране «Сотрудники» (employees.js, statuses).
STATUS = {'on_time': 'Вовремя', 'late': 'Опоздал',
          'missing': 'Не пришёл', 'unlinked': 'Нет привязки',
          'unavailable': 'Нет данных', 'manual_present': 'Был · вручную',
          'manual_absent': 'Не был · вручную'}
NO_HIK = '⊘ Hik'
MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа',
          'сентября', 'октября', 'ноября', 'декабря')
COLUMNS = 9
WIDTHS = {'A': 6, 'B': 36, 'C': 22, 'D': 22, 'E': 18, 'F': 13, 'G': 9, 'H': 17, 'I': 19}
MONEY = '#,##0.##'


def _money(value) -> float | None:
    return float(value) if value is not None else None


def _style_row(sheet, row: int, *, fill: str = 'FFFFFF', bold: bool = False, color: str = INK,
               height: float = 25) -> None:
    for col in range(1, COLUMNS + 1):
        cell = sheet.cell(row, col)
        cell.fill = PatternFill('solid', fgColor=fill)
        cell.font = Font(name='Calibri', size=10, bold=bold, color=color)
        cell.alignment = Alignment(vertical='center', indent=1, wrap_text=col in (2, 3))
        cell.border = Border(bottom=Side(style='hair', color='DDE5DB'))
    sheet.row_dimensions[row].height = height


def _header(sheet, row: int, labels: tuple[str, ...]) -> None:
    for col, label in enumerate(labels, 1):
        cell = sheet.cell(row, col, label)
        cell.fill = PatternFill('solid', fgColor=GREEN)
        cell.font = Font(name='Calibri', size=10, bold=True, color='FFFFFF')
        cell.alignment = Alignment(vertical='center', horizontal='center', wrap_text=True)
    sheet.row_dimensions[row].height = 29


def export_employees(day: date, rows: list[PayrollRow], scope: str,
                     health: dict | None = None, *, manual: set[int] | frozenset = frozenset(),
                     group_order: list[str] | None = None,
                     monthly: list[dict] | None = None) -> bytes:
    """rows — строки смены за день (payable уже как на экране); manual — id
    сменных «Нет в Hikvision»; monthly — окладники: name, role, salary, paid,
    no_hikvision (только для scope='all')."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Опоздавшие' if scope == 'late' else 'Все сотрудники'
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = 'C7'
    for column, width in WIDTHS.items():
        sheet.column_dimensions[column].width = width
    sheet.merge_cells('A1:I2')
    title = sheet['A1']
    title.value = 'RETRO MILLIY / ' + ('ОПОЗДАВШИЕ' if scope == 'late' else 'ВСЕ СОТРУДНИКИ')
    title.fill = PatternFill('solid', fgColor=GREEN)
    title.font = Font(name='Calibri', size=18, bold=True, color='FFFFFF')
    title.alignment = Alignment(vertical='center', indent=1)
    sheet.row_dimensions[1].height = 25
    sheet.row_dimensions[2].height = 25
    accrued = sum((row.payable for row in rows if row.payable is not None), Decimal(0))
    sheet['A3'] = 'Дата'
    sheet['B3'] = datetime.combine(day, time.min)
    sheet['B3'].number_format = 'dd.mm.yyyy'
    sheet['A4'] = 'Сотрудников'
    sheet['B4'] = len(rows)
    sheet['C4'] = 'Опоздали'
    sheet['D4'] = sum(row.status == 'late' for row in rows)
    sheet['E4'] = 'К начислению, сум'
    sheet['F4'] = float(accrued)
    sheet['F4'].number_format = MONEY
    sheet['C3'] = 'Без ставки'
    sheet['D3'] = sum(row.rate is None for row in rows)
    for row_index in (3, 4):
        for col in range(1, COLUMNS + 1):
            cell = sheet.cell(row_index, col)
            cell.fill = PatternFill('solid', fgColor=PALE)
            cell.font = Font(name='Calibri', size=11, bold=col in (1, 3, 5), color=INK)
            cell.alignment = Alignment(vertical='center', indent=1)
        sheet.row_dimensions[row_index].height = 24
    sheet.merge_cells('A5:I5')
    source_status = (health or {}).get('status')
    sheet['A5'] = ('Первый подтверждённый вход по Hikvision ISAPI. «⊘ Hik» — не проходит турникет, '
                   'присутствие отмечается вручную.'
                   if source_status in {'ok', 'stale'} else
                   'Данные Hikvision неполные; «Нет данных» не означает отсутствие сотрудника.')
    sheet['A5'].font = Font(name='Calibri', size=10, italic=True, color='6D7F73')
    sheet['A5'].alignment = Alignment(vertical='center', indent=1)
    sheet.row_dimensions[5].height = 27
    _header(sheet, 6, ('№', 'Сотрудник', 'Должность', 'Группа', 'Статус', 'Первый вход',
                       'Hikvision', 'Ставка, сум', 'Начисление, сум'))

    line = 7
    number = 0

    def person_row(person: PayrollRow) -> None:
        nonlocal line, number
        number += 1
        at = person.occurred_at.astimezone(TZ).replace(tzinfo=None) if person.occurred_at else None
        late = person.status == 'late'
        values = (number, person.name, person.role, person.group_name, STATUS[person.status], at,
                  NO_HIK if person.employee_id in manual else None,
                  _money(person.rate), _money(person.payable))
        _style_row(sheet, line, fill=PINK if late else ('FFFFFF' if number % 2 else 'F5F7F2'),
                   color=RED if late else INK, height=27)
        for col, value in enumerate(values, 1):
            cell = sheet.cell(line, col, value)
            if col in (2, 3, 4, 5, 7):
                cell.data_type = 's'
        sheet.cell(line, 5).font = Font(name='Calibri', size=10, bold=late, color=RED if late else INK)
        sheet.cell(line, 6).number_format = 'hh:mm'
        for col in (8, 9):
            sheet.cell(line, col).number_format = MONEY
        line += 1

    if not rows:
        sheet.merge_cells(f'A{line}:I{line}')
        sheet[f'A{line}'] = 'За выбранный день записей нет'
        sheet[f'A{line}'].alignment = Alignment(horizontal='center', vertical='center')
        sheet.row_dimensions[line].height = 38
        line += 1
    elif scope == 'late':
        for person in rows:
            person_row(person)
        sheet.auto_filter.ref = f'A6:I{line - 1}'
    else:
        order = list(dict.fromkeys((group_order or []) + [row.group_name for row in rows]))
        for group in order:
            members = sorted((row for row in rows if row.group_name == group),
                             key=lambda row: row.name.casefold())
            if not members:
                continue
            subtotal = sum((row.payable for row in members if row.payable is not None), Decimal(0))
            _style_row(sheet, line, fill=GROUP_FILL, bold=True, height=24)
            sheet.cell(line, 2, group)
            sheet.cell(line, 3, f'{len(members)} чел.')
            sheet.cell(line, 8, 'Итого группы')
            sheet.cell(line, 9, float(subtotal)).number_format = MONEY
            line += 1
            for person in members:
                person_row(person)
        _style_row(sheet, line, fill=PALE, bold=True, height=27)
        sheet.cell(line, 2, 'Итого к начислению за день')
        sheet.cell(line, 9, float(accrued)).number_format = MONEY
        line += 1

    if scope != 'late' and monthly:
        line += 1
        month = f'{day.day} {MONTHS[day.month - 1]}'
        _style_row(sheet, line, fill=GROUP_FILL, bold=True, height=24)
        sheet.cell(line, 2, 'На окладе')
        sheet.cell(line, 3, f'{len(monthly)} чел.')
        sheet.cell(line, 8, 'Фонд окладов')
        sheet.cell(line, 9, float(sum(Decimal(str(row['salary'])) for row in monthly))).number_format = MONEY
        line += 1
        _header(sheet, line, ('№', 'Сотрудник', 'Должность', 'График', 'Оклад, сум',
                              '', 'Hikvision', f'Выдано с 1-го по {month}, сум', 'Осталось, сум'))
        line += 1
        for index, person in enumerate(monthly, 1):
            salary = Decimal(str(person['salary']))
            paid = Decimal(str(person.get('paid') or 0))
            rest = salary - paid
            _style_row(sheet, line, fill='FFFFFF' if index % 2 else 'F5F7F2', height=27)
            values = (index, person['name'], person.get('role') or '', person.get('schedule') or '',
                      float(salary), 'переплата' if rest < 0 else None,
                      NO_HIK if person.get('no_hikvision') else None, float(paid), float(rest))
            for col, value in enumerate(values, 1):
                cell = sheet.cell(line, col, value)
                if col in (2, 3, 4, 6, 7):
                    cell.data_type = 's'
            for col in (5, 8, 9):
                sheet.cell(line, col).number_format = MONEY
            if rest < 0:
                for col in (6, 9):
                    sheet.cell(line, col).font = Font(name='Calibri', size=10, bold=True, color=RED)
            line += 1
        total_salary = sum(Decimal(str(row['salary'])) for row in monthly)
        total_paid = sum(Decimal(str(row.get('paid') or 0)) for row in monthly)
        _style_row(sheet, line, fill=PALE, bold=True, height=27)
        sheet.cell(line, 2, 'Итого на окладе')
        sheet.cell(line, 5, float(total_salary)).number_format = MONEY
        sheet.cell(line, 8, float(total_paid)).number_format = MONEY
        sheet.cell(line, 9, float(total_salary - total_paid)).number_format = MONEY
        line += 1

    sheet.print_area = f'A1:I{max(7, line - 1)}'
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
