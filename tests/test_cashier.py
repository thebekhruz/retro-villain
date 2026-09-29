from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest

from retro.modules.cashier.service import (build_snapshot, DataError, Payment,
                                           demo_snapshot, today_tashkent)
from retro.modules.cashier.export import export_report


DAY = date(2026, 9, 10)


def row(*values):
    return {f'field{i}': {'value': value} for i, value in enumerate(values)}


def snapshot():
    return build_snapshot(DAY, [row('2026-09-10', 3, 450000)],
                          [row('Наличные (Инкасса QR)', 200000), row('UzCard', 150000),
                           row('Демо', 100000)])


def test_receipts_are_independent_from_payment_groups():
    result = snapshot()
    assert result.receipt_count == 3
    assert result.revenue == Decimal('450000')
    assert result.average_receipt == Decimal('150000.00')
    assert result.payments[2].name == 'Наличные (Инкасса QR)'


def test_mismatched_payments_cannot_be_exported_as_success():
    with pytest.raises(DataError):
        build_snapshot(DAY, [row('2026-09-10', 3, 450000)], [row('Карта', 449000)])


@pytest.mark.parametrize('bad', [None, True, '450000', float('nan')])
def test_invalid_amounts_are_not_silently_zeroed(bad):
    with pytest.raises(DataError):
        build_snapshot(DAY, [row('2026-09-10', 3, bad)], [])


def test_empty_day_is_distinct_from_missing_fields():
    result = build_snapshot(DAY, [], [])
    assert result.receipt_count == 0
    assert result.revenue == 0
    assert result.average_receipt is None
    with pytest.raises(DataError):
        build_snapshot(DAY, [{'field0': {'value': '2026-09-10'}}], [])


def test_other_day_and_fractional_receipt_count_rejected():
    for total in [row('2026-09-11', 3, 10), row('2026-09-10', 1.5, 10)]:
        with pytest.raises(DataError):
            build_snapshot(DAY, [total], [row('Карта', 10)])


def test_today_uses_tashkent_not_server_timezone():
    assert today_tashkent(datetime(2026, 9, 10, 20, tzinfo=timezone.utc)) == date(2026, 9, 11)


def test_export_keeps_template_and_removes_historical_operations():
    w = openpyxl.load_workbook(BytesIO(export_report(snapshot())))
    assert w.sheetnames == ['отчет', 'Касса', 'Расходы']
    s = w['отчет']
    assert s['B2'].value == 450000
    assert s['B25'].value == 3
    assert s['D4'].value == 150000
    assert s['A1'].value.date() == DAY
    assert s['B28'].value == 0
    assert s['B29'].value == 100000
    assert 'A1:D1' in str(s.merged_cells)
    for cell in ['D2', 'D13', 'D22', 'D40', 'B23']:
        assert s[cell].value is None
    text = ' '.join(str(c.value) for sheet in w for cells in sheet for c in cells)
    assert '13312000' not in text
    assert 'Любовь зп' not in text
    assert 'Наличные (Инкасса QR)' in text


def test_iiko_formula_names_are_rejected_before_export():
    with pytest.raises(DataError):
        build_snapshot(DAY, [row('2026-09-10', 1, 10)], [row('=1+2', 10)])


def test_handover_formula_lives_only_on_the_server():
    """Функционал §1: формула передачи одна — на сервере. Экран получает
    готовые числа (till_summary), своей копии формулы у него нет, поэтому
    сервер и экран не могут разойтись."""
    import subprocess
    from dataclasses import replace
    from types import SimpleNamespace

    from retro.modules.cashier.expenses import cash_to_finance
    from retro.modules.cashier.till import till_summary

    day = date(2026, 9, 16)
    snapshot = replace(demo_snapshot(day), demo=False,
                       payments=(Payment('Демо', Decimal('4000000')),
                                 Payment('Карта', Decimal('12000000')),
                                 Payment('Наличные (Инкасса QR)', Decimal('2000000'))),
                       cash_prepayment=Decimal('500000'))
    expenses = SimpleNamespace(list=lambda _day: [SimpleNamespace(amount=Decimal('300000'))],
                               list_receipts=lambda _day: [SimpleNamespace(amount=Decimal('100000'))])

    class NoGives:
        def _open(self):
            raise AssertionError('not used')
    import retro.modules.cashier.till as till
    original = till.shokh_gives
    till.shokh_gives = lambda *_args: []
    try:
        summary = till_summary(SimpleNamespace(expenses=expenses, accountant_finance=NoGives()), day, snapshot)
    finally:
        till.shokh_gives = original
    assert Decimal(summary['handover']) == cash_to_finance(snapshot, Decimal('300000'), Decimal('100000'))
    assert Decimal(summary['handover']) == Decimal('4000000') + Decimal('500000') + Decimal('100000') - Decimal('300000')

    script = ("const logic=require('./retro/static/cashier-logic.js');"
              "console.log(typeof logic.handover, typeof logic.totalInflow);")
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    assert result.stdout.split() == ['undefined', 'undefined']
