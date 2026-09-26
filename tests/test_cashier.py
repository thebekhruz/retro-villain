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


def test_handover_formula_matches_the_one_shown_to_the_cashier():
    """Сервер и экран считают передачу одинаково — иначе кассир сверяет не то.

    Формулу на экране держит retro/static/cashier-logic.js; здесь прогоняем ту
    же функцию в node и сравниваем с cash_to_finance на одних данных.
    """
    import json
    import subprocess
    from dataclasses import replace

    from retro.modules.cashier.expenses import cash_to_finance

    day = date(2026, 9, 16)
    snapshot = replace(demo_snapshot(day), demo=False,
                       payments=(Payment('Демо', Decimal('4000000')),
                                 Payment('Карта', Decimal('12000000')),
                                 Payment('Наличные (Инкасса QR)', Decimal('2000000'))),
                       cash_prepayment=Decimal('500000'))
    expenses, receipts = Decimal('300000'), Decimal('100000')
    expected = cash_to_finance(snapshot, expenses, receipts)

    script = (
        "const logic=require('./retro/static/cashier-logic.js');"
        f"console.log(JSON.stringify(logic.handover({json.dumps(snapshot.json())},"
        f"'{expenses}','{receipts}')));"
    )
    result = subprocess.run(['node', '-e', script], check=True, capture_output=True, text=True)
    assert Decimal(str(json.loads(result.stdout))) == expected
