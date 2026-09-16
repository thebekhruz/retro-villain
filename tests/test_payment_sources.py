from datetime import date
from io import BytesIO

import openpyxl
import pytest

from retro.modules.cashier.service import build_snapshot, DataError
from retro.modules.cashier.export import export_report

NAMES = ['Демо', 'UzCard', 'Наличные (Инкасса QR)', 'Xumo', 'Я Rahmat',
         'Яндекс Еда', 'Click/Payme Безналичный перевод', 'Uzum']


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
    assert [s.cell(r,3).value for r in range(3,11)] == NAMES
    assert [s.cell(r,4).value for r in range(3,11)] == [37329000,7118500,4346000,2632500,3939000,1135000,1784000,0]
    assert s['D11'].value == '=SUM(D3:D10)'
    assert s['B25'].value == 204
    assert w['Касса']['C8'].value == pytest.approx(37329000/58284000)
    assert s['B23'].value is None


def test_no_sales_retains_all_known_sources_with_zero_amounts():
    result=build_snapshot(date(2026,9,15), [], [])
    assert [p.name for p in result.payments] == NAMES
    assert all(p.amount==0 for p in result.payments)


@pytest.mark.parametrize('name', ['UzCard, Демо','Неизвестный платёж','(без оплаты)'])
def test_unknown_nonzero_source_is_not_silently_dropped_or_guessed(name):
    with pytest.raises(DataError):
        build_snapshot(date(2026,9,15),[row('2026-09-15',1,10)],[row(name,10)])
