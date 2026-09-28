from datetime import date
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest

from retro.modules.cashier.service import build_snapshot, DataError
from retro.modules.cashier.export import export_report
from retro.modules.cashier.expenses import cash_to_finance

NAMES = ['Демо', 'UzCard', 'Наличные (Инкасса QR)', 'Xumo', 'Я Rahmat',
         'Яндекс Еда', 'Click/Payme Безналичный перевод', 'Единый QR', 'Uzum']


def row(*values):
    return {f'field{i}': {'value': v} for i, v in enumerate(values)}


def actual_day():
    # Verified PayTypes response. Former Combo query put 2,109,000 in 'UzCard, Демо'.
    return build_snapshot(date(2026,9,15), [row('2026-09-15',204,58284000)], [
        row('(без оплаты)',0), row('Click/Payme Безналичный перевод',1784000),
        row('UzCard',7118500), row('Xumo',2632500), row('Демо',37329000),
        row('Наличные (Инкасса QR)',4346000), row('Я Rahmat',3939000), row('Яндех Еда',1135000)])


def test_display_sources_match_reference_without_reclassifying_demo():
    result = actual_day()
    assert [p.name for p in result.payments] == NAMES
    assert result.payments[0].amount == 37329000
    assert result.payments[1].amount == 7118500
    assert result.payments[2].amount == 4346000
    assert sum(p.amount for p in result.payments) == 58284000


def test_first_sheet_is_populated_even_without_environment_mapping():
    w = openpyxl.load_workbook(BytesIO(export_report(actual_day())))
    s=w['отчет']
    assert [s.cell(r,3).value for r in range(3,12)] == NAMES
    assert [s.cell(r,4).value for r in range(3,12)] == [37329000,7118500,4346000,2632500,3939000,1135000,1784000,0,0]
    assert s['D12'].value == '=SUM(D3:D11)'
    assert s['B25'].value == 204
    assert w['Касса']['C8'].value == pytest.approx(37329000/58284000)
    assert s['B23'].value is None


def test_no_sales_retains_all_known_sources_with_zero_amounts():
    result=build_snapshot(date(2026,9,15), [], [])
    assert [p.name for p in result.payments] == NAMES
    assert all(p.amount==0 for p in result.payments)


def test_unified_qr_is_visible_as_non_cash_payment():
    snapshot = build_snapshot(date(2026, 9, 17), [row('2026-09-17', 2, 137000)],
                              [row('Демо', 100000), row('Единый QR', 37000)])
    assert snapshot.json()['payment_total'] == '137000'
    assert next(payment.amount for payment in snapshot.payments
                if payment.name == 'Единый QR') == Decimal(37000)
    assert cash_to_finance(snapshot, Decimal(0)) == Decimal(100000)


def test_export_shows_unified_qr_and_sums_all_nine_payment_sources():
    snapshot = build_snapshot(date(2026, 9, 17), [row('2026-09-17', 2, 137000)],
                              [row('Демо', 100000), row('Единый QR', 37000)])
    workbook = openpyxl.load_workbook(BytesIO(export_report(snapshot)))
    summary = workbook['отчет']
    assert [summary.cell(r, 3).value for r in range(3, 12)] == NAMES
    assert summary['D10'].value == 37000
    assert summary['C12'].value == 'ИТОГО ОПЛАТЫ:'
    assert summary['D12'].value == '=SUM(D3:D11)'
    assert summary['C13'].value == 'РАСХОДЫ КАССЫ'
    assert summary['C11'].style_id == summary['C10'].style_id
    assert summary['D11'].style_id == summary['D10'].style_id
    detail = workbook['Касса']
    assert detail['A15'].value == 'Единый QR'
    assert detail['B15'].value == 37000
    assert detail['B17'].value == '=SUM(B8:B16)'


@pytest.mark.parametrize('name', ['UzCard, Демо','Неизвестный платёж','(без оплаты)'])
def test_unknown_nonzero_source_is_not_silently_dropped_or_guessed(name):
    with pytest.raises(DataError):
        build_snapshot(date(2026,9,15),[row('2026-09-15',1,10)],[row(name,10)])
