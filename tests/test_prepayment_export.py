from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import BytesIO

import openpyxl

from retro.modules.cashier.export import export_report
from retro.modules.cashier.service import Prepayment, build_snapshot


DAY = date(2026, 10, 6)


def row(*values):
    return {f'field{i}': {'value': value} for i, value in enumerate(values)}


def snapshot(entries, issue=None):
    base = build_snapshot(DAY, [row(DAY.isoformat(), 1, 100000)],
                          [row('Демо', 100000)])
    return replace(base, new_prepayment=Decimal('1000000'),
                   cash_prepayment=Decimal('200000'),
                   prepayments=entries, prepayments_issue=issue)


def entry(index, amount='200000', **changes):
    values = dict(id=str(index), received_at=f'2026-10-06T11:{index:02d}:00',
                  amount=Decimal(amount), payment_method='Наличными',
                  comment=f'Гость {index}', order_number=None)
    return Prepayment(**(values | changes))


def workbook(entries, issue=None):
    return openpyxl.load_workbook(BytesIO(export_report(snapshot(entries, issue))))


def test_exports_each_prepayment_without_adding_it_to_cash_twice():
    book = workbook((entry(1, order_number='42'),
                     entry(2, '800000', payment_method='UzCard')))
    summary, detail = book['отчет'], book['Предоплаты']
    assert summary['A3'].value == 'ПРЕДОПЛАТЫ ЗА ДЕНЬ'
    assert all(label in summary['A4'].value for label in ('11:01', '42', 'Гость 1', 'Наличными'))
    assert 'UzCard' in summary['A5'].value
    assert [summary[f'B{row}'].value for row in (4, 5)] == [200000, 800000]
    assert summary['B20'].value == 1000000
    assert summary['B21'].value == 1100000
    assert summary['B29'].value == 300000
    assert detail['E7'].value == 1000000
    assert [detail[f'C{row}'].value for row in (5, 6)] == ['Гость 1', 'Гость 2']


def test_all_sixteen_template_rows_are_visible_and_overflow_is_preserved():
    exactly_sixteen = workbook(tuple(entry(index, '10') for index in range(16)))
    summary = exactly_sixteen['отчет']
    assert summary['B19'].value == 10
    assert all(not summary.row_dimensions[row].hidden for row in range(4, 20))

    overflow = workbook(tuple(entry(index, '10') for index in range(17)))
    summary, detail = overflow['отчет'], overflow['Предоплаты']
    assert summary['A19'].value == 'Ещё 2 — на листе «Предоплаты»'
    assert summary['B19'].value is None
    assert not summary.row_dimensions[19].hidden
    assert [detail.cell(row, 3).value for row in range(5, 22)] == [f'Гость {i}' for i in range(17)]
    assert detail['E22'].value == 170


def test_missing_details_are_not_exported_as_no_prepayments():
    unknown = workbook(None, 'Обновите данные iiko')['отчет']
    empty = workbook(())['отчет']
    assert unknown['A4'].value == 'Детализация предоплат недоступна'
    assert unknown['A5'].value == 'Обновите данные iiko'
    assert unknown['B4'].value is None
    assert empty['A4'].value == 'Предоплат за день нет'
    assert empty['B4'].value is None


def test_iiko_text_cannot_become_a_formula_and_unknown_method_is_explicit():
    item = entry(1, comment='=HYPERLINK("https://example.invalid")',
                 order_number='=1+1', payment_method=None,
                 received_at='2026-10-06T06:57:00+00:00')
    book = workbook((item,))
    summary, detail = book['отчет'], book['Предоплаты']
    assert summary['A4'].value.startswith('11:57')
    assert detail['A5'].value == '11:57'
    assert detail['B5'].value == '=1+1'
    assert detail['B5'].data_type == 's'
    assert detail['C5'].value == item.comment
    assert detail['C5'].data_type == 's'
    assert detail['D5'].value == 'Не указан'
