"""Revenue follows receipt of money, not redemption or the booked event date."""
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from fastapi.testclient import TestClient

from legacy_app import create_app
from retro.config import Settings
from retro.modules.cashier.pay_groups import build_payment_groups, redeemed_orders
from retro.modules.cashier.service import DataError, Prepayment, build_snapshot


def row(*values):
    return {f'field{i}': {'value': value} for i, value in enumerate(values)}


def report(day, paid=0, credited=0):
    base = build_snapshot(day, [row(day.isoformat(), 1, paid)] if paid else [],
                          [row('Демо', paid)] if paid else [])
    redemptions = [('42', 'Наличные', 'Демо', Decimal(credited))] if credited else []
    groups = build_payment_groups([('Наличные', 'Демо', Decimal(paid))], redemptions)
    return replace(base, payment_groups=groups, redeemed_orders=redeemed_orders(redemptions),
                   full_total=Decimal(paid + credited), full_receipt_count=int(bool(paid or credited)))


def advance(day, amount=2_000_000, id='advance'):
    return Prepayment(id, f'{day.isoformat()}T14:00:00+05:00', Decimal(amount), order_number='42')


@pytest.mark.parametrize('received_day,lag,extra', [
    (date(2026, 10, 9), 2, 1_000_000),
    (date(2026, 9, 30), 3, 0),
    (date(2026, 10, 1), 0, 1_000_000),
])
def test_day_api_and_export_count_advance_only_on_receipt(tmp_path, received_day, lag, extra):
    closed_day = received_day + timedelta(days=lag)

    class Iiko:
        async def load(self, day):
            return report(day, extra if day == closed_day else 0, 2_000_000 if day == closed_day else 0)

        async def load_prepayments(self, day):
            # Multiple instalments of one order are both received, counted once.
            return (advance(day, 500_000, 'a'), advance(day, 1_500_000, 'b')) if day == received_day else ()

    app = create_app(Settings(data_dir=tmp_path))
    app.state.iiko = Iiko()
    totals = []
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        for day in sorted({received_day, closed_day}):
            expected = (2_000_000 if day == received_day else 0) + (extra if day == closed_day else 0)
            response = client.get('/api/cashier/day', params={'date': day.isoformat()})
            assert response.status_code == 200, response.text
            data = response.json()
            assert Decimal(data['daily_revenue']) == expected
            assert Decimal(data['received_prepayments_total']) == (2_000_000 if day == received_day else 0)
            assert Decimal(data['redeemed_total']) == (2_000_000 if day == closed_day else 0)
            # The till still uses actual cash; displaying revenue never adds the advance twice.
            assert Decimal(data['summary']['demo_cash']) == (extra if day == closed_day else 0)
            exported = client.get('/api/cashier/export', params={
                'date': day.isoformat(), 'snapshot_id': data['snapshot_id']})
            assert exported.status_code == 200, exported.text
            sheet = openpyxl.load_workbook(BytesIO(exported.content))['отчет']
            assert sheet['A2'].value == 'ВЫРУЧКА ЗА ДЕНЬ'
            assert sheet['B2'].value == expected
            assert sheet['B22'].value == int(data['received_prepayments_total'])
            assert sheet['B23'].value == int(data['revenue'])
            totals.append(expected)
        assert sum(totals) == 2_000_000 + extra


def test_unknown_registry_does_not_replace_actual_receipts_with_shift_estimate(tmp_path):
    day = date(2026, 10, 9)

    class Iiko:
        async def load(self, day):
            return replace(report(day, 1_000_000, 2_000_000), new_prepayment=Decimal(9_000_000))

        async def load_prepayments(self, day):
            raise DataError('unavailable')

    app = create_app(Settings(data_dir=tmp_path))
    app.state.iiko = Iiko()
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        data = client.get('/api/cashier/day', params={'date': day.isoformat()}).json()
        assert data['daily_revenue'] is None
        assert data['received_prepayments_total'] is None
        assert data['revenue'] == '1000000'
        exported = client.get('/api/cashier/export', params={
            'date': day.isoformat(), 'snapshot_id': data['snapshot_id']})
        sheet = openpyxl.load_workbook(BytesIO(exported.content))['отчет']
        assert sheet['B2'].value == 'требует проверки'
        assert sheet['B22'].value == 'требует проверки'
        assert sheet['B23'].value == 1_000_000


def test_revenue_does_not_require_optional_full_bill_groups():
    day = date(2026, 10, 9)
    snapshot = replace(report(day, 1_000_000), payment_groups=None, redeemed_orders=None,
                       prepayments=(advance(day),))
    assert snapshot.json()['daily_revenue'] == '3000000'
    assert replace(snapshot, prepayments=()).json()['daily_revenue'] == '1000000'
