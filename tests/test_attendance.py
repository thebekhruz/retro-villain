from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import openpyxl
from fastapi.testclient import TestClient

from retro.app import create_app
from retro.config import Settings
from retro.modules.cashier.attendance import Entrance, export_entrances, is_late


def test_ten_am_boundary_uses_tashkent_time():
    tz = ZoneInfo('Asia/Tashkent')
    assert not is_late(datetime(2026, 9, 16, 10, 0, 0, tzinfo=tz))
    assert is_late(datetime(2026, 9, 16, 10, 0, 1, tzinfo=tz))
    assert not is_late(datetime(2026, 9, 16, 4, 59, tzinfo=ZoneInfo('UTC')))


def test_placeholder_export_is_dated_and_contains_no_people():
    with TestClient(create_app(Settings()), client=('127.0.0.1', 50000)) as client:
        response = client.get('/api/cashier/entrances/export?date=2026-09-15')
        assert response.status_code == 200
        assert 'Retro-entrances-2026-09-15.xlsx' in response.headers['content-disposition']
        sheet = openpyxl.load_workbook(BytesIO(response.content)).active
        assert sheet['B3'].value.date() == date(2026, 9, 15)
        assert sheet['B4'].value == 0
        assert sheet['D4'].value == 0
        assert [sheet.cell(6, col).value for col in range(1, 5)] == ['№', 'Человек', 'Время входа', 'Статус']
        assert 'не подключены' in sheet['A7'].value
        assert client.get('/api/cashier/entrances/export?date=2099-01-01').status_code == 422


def test_cashier_page_previews_hikvision_people_as_explicit_demo_cards():
    with TestClient(create_app(Settings()), client=('127.0.0.1', 50000)) as client:
        page = client.get('/').text
    assert 'Демонстрационные карточки' in page
    assert 'Вовремя' in page
    assert 'Опоздал' in page
    assert 'Нет входа' in page
    assert 'Тестовые люди и время' in page


def test_future_rows_mark_late_and_keep_names_as_text():
    tz = ZoneInfo('Asia/Tashkent')
    entries = (Entrance('Азиза', datetime(2026, 9, 15, 10, 0, tzinfo=tz)),
               Entrance('=HYPERLINK("evil")', datetime(2026, 9, 15, 10, 1, tzinfo=tz)))
    sheet = openpyxl.load_workbook(BytesIO(export_entrances(date(2026, 9, 15), entries))).active
    assert sheet['B4'].value == 2
    assert sheet['D4'].value == 1
    assert sheet['D7'].value == 'Вовремя'
    assert sheet['D8'].value == 'Опоздал'
    assert sheet['B8'].data_type == 's'
    assert sheet['B8'].value.startswith('=HYPERLINK')
    assert sheet['A8'].fill.fgColor.rgb.endswith('FCE6E9')
