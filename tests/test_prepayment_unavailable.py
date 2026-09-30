"""Возврат аванса гонит смену в минус — день от этого валидным быть не перестаёт.

Прод 30 сентября: через кассу провели возврат аванса на 25 500 000, открытая
смена №1 ушла в минус (`payOrders` −19 941 500, `salesCash` −21 809 000 при
`salesCard` +1 867 500), и разностная оценка предоплат перестала сходиться.
До правки это валило весь снимок: кассир час висел на данных 13:17 с плашкой
«Не удалось обновить iiko», хотя выручка, чеки и способы оплаты были верны.

Числа смены и оплат в тестах — снятые с боевого iiko 30.09.2026."""

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import httpx
import openpyxl
import pytest

from retro.config import Settings
from retro.integrations.iiko import IikoClient, cash_prepay_from_shifts
from retro.modules.cashier.archive import CashierArchive
from retro.modules.cashier.expenses import cash_to_finance
from retro.modules.cashier.export import UNKNOWN, export_report
from retro.modules.cashier.service import (
    DataError, PrepaymentUnavailable, TZ, demo_snapshot,
)
from retro.modules.cashier.till import TillTotals, expected_handover, till_summary

DAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 13, 56, tzinfo=TZ)
# Открытая смена №1 в момент инцидента: тождество сходится, минусует только знак.
NEGATIVE_SHIFT = {'id': 'shift-1', 'openDate': '2026-09-30T11:20:45', 'cashRegNumber': 1,
                  'payOrders': -19941500, 'salesCash': -21809000,
                  'salesCard': 1867500, 'salesCredit': 0}
HEALTHY_SHIFT = {'id': 'shift-1', 'openDate': '2026-09-30T11:20:45', 'cashRegNumber': 1,
                 'payOrders': 5167500, 'salesCash': 3691000,
                 'salesCard': 1476500, 'salesCredit': 0}
# Оплаты того же момента: сумма — 5 167 500, как продажи по регистру.
PAYMENTS = [('Демо', 3669000), ('UzCard', 687500), ('Наличные (Инкасса QR)', 22000),
            ('Click/Payme Безналичный перевод', 372000), ('Яндех Еда', 417000)]
SALES = sum(amount for _, amount in PAYMENTS)
RECEIPTS = 9


def row(**fields):
    return {f'field{index}': {'value': value} for index, value in fields.items()}


def cashier_handler(shifts):
    """Живой iiko с подставленным списком смен."""
    def handler(request):
        path = request.url.path
        if path == '/api/auth/login':
            return httpx.Response(200, json={'token': 'test-only'})
        if path == '/api/cash/shift/list_period':
            return httpx.Response(200, json={'shifts': shifts})
        body = json.loads(request.content)
        groups = body['groupFields']
        if path == '/api/olap/init':
            return httpx.Response(200, json={'fetchId': groups[0]})
        if groups == ['CashRegisterName', 'RestaurantSection']:
            rows = [{**row(**{'0': 'Kassa-FiscalBox1', '2': SALES}),
                     'children': [row(**{'1': 'Ресторан', '2': SALES})]}]
        elif groups == ['OpenDate.Typed']:
            rows = [row(**{'0': DAY.isoformat(), '1': RECEIPTS, '2': SALES})]
        elif groups == ['PayTypes']:
            rows = [row(**{'0': name, '1': amount}) for name, amount in PAYMENTS]
        else:
            raise AssertionError(groups)
        return httpx.Response(200, json={'result': {'rows': rows}})
    return handler


def client_for(shifts):
    return IikoClient(Settings(login='test', password='test', store_id=123),
                      transport=httpx.MockTransport(cashier_handler(shifts)), poll_delay=0)


def unknown_snapshot(**overrides):
    base = replace(demo_snapshot(DAY), demo=False, fetched_at=NOW,
                   cash_prepayment=None, new_prepayment=None,
                   register_received_total=None,
                   prepayment_issue='Кассовая смена ушла в минус (возврат аванса) — '
                                    'предоплаты за день не посчитать.')
    return replace(base, **overrides) if overrides else base


def test_negative_shift_is_not_a_broken_day():
    with pytest.raises(PrepaymentUnavailable):
        cash_prepay_from_shifts(DAY, SALES, dict(PAYMENTS), [NEGATIVE_SHIFT])


def test_structural_damage_stays_fatal():
    """Битую структуру ответа мы по-прежнему не принимаем за валидный день."""
    for shifts in ('не список', [42]):
        with pytest.raises(DataError) as failure:
            cash_prepay_from_shifts(DAY, SALES, dict(PAYMENTS), shifts)
        assert not isinstance(failure.value, PrepaymentUnavailable)


def test_day_survives_the_refund_with_prepayments_marked_unknown():
    result = asyncio.run(client_for([NEGATIVE_SHIFT]).load(DAY))

    # Главное: загрузка дня прошла — плашка «Не удалось обновить iiko» не появится.
    assert result.revenue == SALES
    assert result.receipt_count == RECEIPTS
    assert {p.name: p.amount for p in result.payments}['Демо'] == 3669000
    # Неизвестно — это None, а не ноль.
    assert result.cash_prepayment is None and result.new_prepayment is None
    assert result.register_received_total is None
    assert result.prepayments_known is False
    assert 'минус' in result.prepayment_issue
    assert result.json()['cash_prepayment'] is None
    assert result.json()['prepayment_issue'] == result.prepayment_issue


def test_healthy_shift_still_counts_prepayments():
    """Негативный контроль: обычный день считается как раньше, причины нет."""
    result = asyncio.run(client_for([HEALTHY_SHIFT]).load(DAY))

    assert result.prepayment_issue is None and result.prepayments_known
    assert result.new_prepayment == 0
    assert result.register_received_total == SALES


def test_handover_refuses_to_guess_instead_of_substituting_zero():
    snapshot = unknown_snapshot()
    totals = TillTotals(Decimal(350000), Decimal(0), Decimal(0))

    assert cash_to_finance(snapshot, Decimal(350000)) is None
    assert expected_handover(snapshot, totals) is None
    summary = till_summary(SimpleNamespace(), DAY, replace(snapshot, demo=True))
    assert summary['handover'] is None
    assert summary['cash_prepayment'] is None
    # Приход без предоплат тоже неизвестен: продажи не выдаём за весь приход.
    assert summary['total_inflow'] is None
    assert summary['prepayment_issue'] == snapshot.prepayment_issue
    # Выручка при этом на месте — экран не пустеет.
    assert summary['sales'] == str(snapshot.revenue)


def test_export_prints_the_reason_not_a_number():
    workbook = openpyxl.load_workbook(BytesIO(export_report(unknown_snapshot())))
    sheet = workbook['отчет']

    assert sheet['B20'].value == UNKNOWN      # оценка новых предоплат
    assert sheet['B21'].value == UNKNOWN      # приход кассы
    assert sheet['B27'].value == UNKNOWN      # предоплаты наличными
    assert sheet['B29'].value == UNKNOWN      # к передаче в финансовый отдел
    assert sheet['D39'].value == UNKNOWN
    assert workbook['Расходы']['D6'].value == UNKNOWN
    assert 'минус' in sheet['A31'].value
    # Выручка и чеки — обычными числами.
    assert sheet['B2'].value == unknown_snapshot().revenue
    assert sheet['B25'].value == unknown_snapshot().receipt_count


def test_archive_keeps_unknown_prepayments_across_restart(tmp_path):
    archive = CashierArchive(tmp_path / 'cashier.sqlite3', Settings(store_id=uuid4().int % 10**9))
    saved = unknown_snapshot(day=DAY - timedelta(days=1))
    archive.save(saved)

    restored = archive.get(DAY - timedelta(days=1))
    assert restored.cash_prepayment is None and restored.new_prepayment is None
    assert restored.prepayment_issue == saved.prepayment_issue
    assert restored.revenue == saved.revenue
