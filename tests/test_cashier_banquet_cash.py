"""T-588: наличные банкета на кассе Ретро входят в «К передаче бухгалтеру».

Числа — 01.11.2026 из iiko: заказ 72022 в секции «Бехруз (Свадьба)» доплатили
3 000 000 наличными на кассе Ретро. Кассир сдала 14 201 500, панель показала
11 201 500: банкетные наличные выпадали и из «Демо», и из предоплат наличными."""
import asyncio
import hashlib
import json
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import httpx
import openpyxl
import pytest
from PIL import Image

from retro.config import Settings
from retro.integrations.iiko import IikoClient
from retro.modules.cashier.archive import CashierArchive, decode_snapshot
from retro.modules.cashier.expenses import Expense, cash_to_finance
from retro.modules.cashier.export import export_report
from retro.modules.cashier.service import BANQUET_SECTION, RETRO_REGISTER, TZ
from retro.modules.cashier.telegram_report import DayReport, handover_rows, money, render
from retro.modules.cashier.till import TillTotals, till_summary
from retro.modules.founder.cabinet import till_report

DAY = date(2026, 11, 1)
RETRO_PAYMENTS = {'Демо': 11599500, 'UzCard': 6706000, 'Наличные (Инкасса QR)': 2800000, 'Xumo': 3706000,
                  'Я Rahmat': 3852500, 'Яндекс Еда': 1550000, 'Click/Payme Безналичный перевод': 200000}
# 4 050 000 предоплат безналом и 398 000 расходов кассира этого дня.
NEW_PREPAYMENTS = 4050000
TOTALS = TillTotals(expenses=Decimal(398000), shokh=Decimal(0), receipts=Decimal(0))
PLAIN_STEPS = ['Наличные', '+ Предоплаты наличными', '+ Поступления', '− Расходы', '− На закуп', '− Доллары в сейф']


def row(*values):
    return {f'field{index}': {'value': value} for index, value in enumerate(values)}


def load_day(banquet_cash):
    """`load` на подменённом iiko: тот же день, banquet_cash наличными в секции банкета."""
    retro_total = sum(RETRO_PAYMENTS.values())
    register = {**RETRO_PAYMENTS, 'Демо': RETRO_PAYMENTS['Демо'] + banquet_cash}
    paid = sum(register.values()) + NEW_PREPAYMENTS
    cash = register['Демо'] + register['Наличные (Инкасса QR)']

    def handler(request):
        if request.url.path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        if request.url.path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': [dict(
                id='shift-1', openDate='2026-11-01T10:00:00', cashRegNumber=1, payOrders=paid,
                salesCash=cash, salesCard=paid - cash, salesCredit=0)]})
        body = json.loads(request.content)
        group = body['groupFields'][0]
        if request.url.path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': group})
        without_banquet = any(item.get('field') == 'RestaurantSection' for item in body['filters'])
        if group == 'CashRegisterName':
            rows = [{'field0': {'value': RETRO_REGISTER}, 'field2': {'value': 78164000},
                     'children': [{'field1': {'value': 'Ресторан'}, 'field2': {'value': 32164000}},
                                  {'field1': {'value': BANQUET_SECTION}, 'field2': {'value': 46000000}}]}]
        elif group == 'OpenDate.Typed':
            rows = [row(DAY.isoformat(), 72, retro_total)]
        elif group == 'PayTypes':
            source = RETRO_PAYMENTS if without_banquet else register
            rows = [row(name, amount) for name, amount in source.items()]
        else:
            # Группы касс для передачи не нужны: без них день считается как раньше.
            rows = []
        return httpx.Response(200, json={'result': {'rows': rows}})

    client = IikoClient(Settings(login='test', password='test', store_id=123),
                        transport=httpx.MockTransport(handler), poll_delay=0)
    return asyncio.run(client.load(DAY))


def demo_cash(snapshot):
    return next(payment.amount for payment in snapshot.payments if payment.name == 'Демо')


def picture(snapshot):
    summary = till_summary(None, DAY, snapshot, totals=TOTALS)
    return render(DayReport(DAY, datetime(2026, 11, 1, 23, 30, tzinfo=TZ), snapshot, summary,
                            (Expense(1, DAY, 'Расходы дня', Decimal(398000)),), (), (), None))


def test_01_11_banquet_cash_goes_to_handover_and_retro_revenue_stays():
    snapshot = load_day(3000000)
    assert snapshot.banquet_cash == Decimal(3000000)
    # Выручка, «Демо» и приход — те же, что и до правки.
    assert snapshot.revenue == Decimal(30414000)
    assert demo_cash(snapshot) == Decimal(11599500)
    assert snapshot.cash_prepayment == 0
    assert cash_to_finance(snapshot, Decimal(398000)) == Decimal(14201500)
    summary = till_summary(None, DAY, snapshot, totals=TOTALS)
    assert summary['handover'] == '14201500'
    assert summary['banquet_cash'] == '3000000'
    assert summary['demo_cash'] == '11599500'
    assert summary['total_inflow'] == '37464000'


def test_day_without_banquet_cash_counts_as_before():
    snapshot = load_day(0)
    assert snapshot.banquet_cash == 0
    assert cash_to_finance(snapshot, Decimal(398000)) == Decimal(11201500)
    summary = till_summary(None, DAY, snapshot, totals=TOTALS)
    assert summary['handover'] == '11201500'
    assert summary['total_inflow'] == '34464000'
    # Ключа нет, поэтому отпечаток обычного дня прежний: повторное «Передать» не
    # отправит в группу «Исправленный отчёт» после выката.
    assert 'banquet_cash' not in summary
    assert [step[0] for step in handover_rows(summary)] == PLAIN_STEPS


def test_unknown_prepayments_keep_handover_unknown_even_with_banquet_cash():
    snapshot = replace(load_day(3000000), cash_prepayment=None, new_prepayment=None)
    assert cash_to_finance(snapshot, Decimal(398000)) is None


def test_picture_has_banquet_row_only_when_there_is_banquet_cash():
    summary = till_summary(None, DAY, load_day(3000000), totals=TOTALS)
    rows = handover_rows(summary)
    assert [step[0] for step in rows] == [PLAIN_STEPS[0], '+ Банкет наличными', *PLAIN_STEPS[1:]]
    assert rows[1][:3] == ('+ Банкет наличными', money('3000000'), False)
    with_row = Image.open(BytesIO(picture(load_day(3000000))))
    without_row = Image.open(BytesIO(picture(load_day(0))))
    assert with_row.height > without_row.height


def test_founder_brief_gets_the_same_banquet_step():
    with_banquet = till_report(None, DAY, load_day(3000000), TOTALS)
    assert (with_banquet['banquet_cash'], with_banquet['handover']) == ('3000000', '14201500')
    assert till_report(None, DAY, load_day(0), TOTALS)['banquet_cash'] is None


def test_archive_keeps_banquet_cash_and_payload_without_it_reads_as_zero(tmp_path):
    snapshot = load_day(3000000)
    archive = CashierArchive(tmp_path / 'cashier.sqlite3', Settings(store_id=123))
    archive.save(snapshot)
    assert archive.get(DAY).banquet_cash == Decimal(3000000)
    payload = snapshot.json()
    assert payload['banquet_cash'] == '3000000'
    del payload['banquet_cash']
    assert decode_snapshot(json.dumps(payload)).banquet_cash == 0


def test_snapshots_of_the_old_formula_are_read_again(tmp_path):
    """Снимок без поля занизил бы передачу. Смена версии архива перечитывает дни из iiko."""
    settings = Settings(store_id=123)
    archive = CashierArchive(tmp_path / 'cashier.sqlite3', settings)
    old_source = hashlib.sha256(json.dumps(
        [settings.base_url, settings.store_id, RETRO_REGISTER, 'cashier-2026-10-08-v1']).encode()).hexdigest()
    with closing(archive.db.connect()) as connection, connection:
        connection.execute('INSERT INTO cashier_day_snapshots(source,day,fetched_at,payload) VALUES (?,?,?,?)',
                           (old_source, DAY.isoformat(), '2026-11-02T06:00:00+05:00', '{}'))
    assert archive.get(DAY) is None


def workbook(snapshot):
    content = export_report(snapshot, [Expense(1, DAY, 'Расходы дня', Decimal(398000))])
    return openpyxl.load_workbook(BytesIO(content))


def test_excel_steps_add_up_to_handover_with_banquet_cash():
    book = workbook(load_day(3000000))
    report, manual = book['отчет'], book['Расходы']
    assert (report['A35'].value, report['B35'].value) == ('Банкет наличными', 3000000)
    assert 'банкет наличными' in report['A31'].value
    assert report['B29'].value == 14201500
    assert 'банкет' in manual['A4'].value
    assert manual['B4'].value + manual['D4'].value + manual['D5'].value - manual['B5'].value == manual['B6'].value == 14201500


def test_excel_without_banquet_cash_keeps_its_rows():
    book = workbook(load_day(0))
    report, manual = book['отчет'], book['Расходы']
    assert report['A35'].value is None and report['B35'].value is None
    assert 'банкет' not in report['A31'].value
    assert report['B29'].value == 11201500
    assert (manual['A4'].value, manual['B4'].value) == ('Демо · по данным iiko', 11599500)


def test_cashier_screen_keeps_banquet_row_hidden_until_there_is_a_sum():
    static = Path(__file__).resolve().parents[1] / 'retro' / 'static'
    html = (static / 'index.html').read_text(encoding='utf-8')
    assert '<div class="handover-rule" id="handover-banquet-row" hidden><span>+ Банкет наличными</span>' in html
    script = (static / 'app.js').read_text(encoding='utf-8')
    assert "summaryValue('banquet_cash')" in script
    assert "$('handover-banquet-row').hidden = !banquet" in script
    assert '+ Банкет наличными' in (static / 'i18n-uz.js').read_text(encoding='utf-8')
