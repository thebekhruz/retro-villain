"""USD goes to the safe; its daily Retro equivalent leaves the UZS handover."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import httpx
import openpyxl

from retro.integrations.cbu import UsdRate
from retro.accounting_period import ACCOUNTING_START
from retro.modules.cashier.service import DataError
from retro.modules.cashier.till import add_usd_deposit, till_totals
from retro.modules.founder import cabinet
from test_cashier_design_parity import (DAY, RECEIVED, accountant_day, c, hand_over,
                                        make_app, snapshot_for)


def snapshot(c):
    value = replace(snapshot_for(cash=Decimal(5_000_000)), cash_prepayment=Decimal(0),
                    prepayments=())
    c.app.state.cache.put(value)
    return value


def summary(c, value):
    response = c.get('/api/cashier/summary', params={'date': DAY.isoformat(), 'snapshot_id': value.id})
    assert response.status_code == 200, response.text
    return response.json()


def test_usd_300_at_11700_matches_handover_safe_founder_and_export(c):
    value = snapshot(c)
    finance = c.app.state.accountant_finance
    finance.reserve_entry(ACCOUNTING_START, 'usd', 'opening', '200', 'Сейф')
    result = c.post('/api/cashier/usd-deposits', json={'date': DAY.isoformat(), 'amount': '300'})
    assert result.status_code == 201, result.text
    assert result.json()['safe_balance'] == '500'
    data = summary(c, value)
    assert data['usd_total'] == '300'  # Not the whole 500 USD safe balance.
    assert data['usd_rate'] == '11700'
    assert data['usd_deduction'] == '3510000'
    assert data['handover'] == '1490000'
    assert data['cash_out'] == '0'  # Converting a receipt is not a new expense.
    assert hand_over(c, value, '5000000').status_code == 409
    assert hand_over(c, value, '1490000').status_code == 201
    recorded = finance.handover_for_day(RECEIVED)
    assert recorded == Decimal('1490000')
    assert summary(c, value)['handover'] == '1490000'  # Reload never deducts twice.

    class Iiko:
        async def load(self, day):
            return value

    c.app.state.iiko = Iiko()
    c.app.state.settings = replace(c.app.state.settings, login='test', password='test', store_id=1)
    request = SimpleNamespace(app=c.app, state=SimpleNamespace(request_id='test'))
    cashier, error = asyncio.run(cabinet.cashier_day(request, DAY))
    assert error is None and Decimal(cashier['expected_handover']) == Decimal('1490000')
    exported = c.get('/api/cashier/export', params={'date': DAY.isoformat(), 'snapshot_id': value.id})
    assert exported.status_code == 200
    book = openpyxl.load_workbook(BytesIO(exported.content))
    assert book['отчет']['B29'].value == book['отчет']['D39'].value == 1490000
    assert [book['отчет'][cell].value for cell in ('B32', 'B33', 'B34')] == [300, 11700, 3510000]
    assert book['Расходы']['B6'].value == 1490000
    assert Decimal(accountant_day(c)['cashier_handover']['amount']) == Decimal('1490000')


def test_multiple_deposits_delete_and_other_day_use_only_that_days_rate(c):
    value = snapshot(c)
    initial = c.get('/api/cashier/usd-deposits', params={'date': DAY.isoformat()}).json()
    earlier = DAY - timedelta(days=1)
    c.app.state.usd_rates._save(UsdRate(earlier, earlier, Decimal('13000'), Decimal('12800')))
    other = c.post('/api/cashier/usd-deposits', json={'date': earlier.isoformat(), 'amount': '500'})
    assert other.status_code == 201
    assert summary(c, value)['handover'] == '5000000'
    deposits = []
    for amount in ('200', '100.5'):
        result = c.post('/api/cashier/usd-deposits', json={'date': DAY.isoformat(), 'amount': amount})
        assert result.status_code == 201, result.text
        deposits.append(result.json()['deposit']['id'])
    assert Decimal(summary(c, value)['usd_deduction']) == Decimal('3515850')
    stale_export = c.get('/api/cashier/export', params={
        'date': DAY.isoformat(), 'snapshot_id': value.id, 'usd_revision': initial['revision']})
    assert stale_export.status_code == 409
    assert c.delete(f'/api/cashier/usd-deposits/{deposits[1]}', params={'date': DAY.isoformat()}).status_code == 204
    assert Decimal(summary(c, value)['handover']) == Decimal('2660000')
    assert c.delete(f'/api/cashier/usd-deposits/{deposits[0]}', params={'date': DAY.isoformat()}).status_code == 204
    assert summary(c, value)['handover'] == '5000000'


def test_old_deposit_without_rate_blocks_handover_and_export_is_unknown(c):
    value = snapshot(c)
    add_usd_deposit(c.app.state.accountant_finance, DAY, '300')
    with c.app.state.usd_rates._open() as connection:
        connection.execute('DELETE FROM cashier_usd_rates WHERE day = ?', (DAY.isoformat(),))
    async def unavailable(day):
        raise DataError('No rate')
    c.app.state.usd_rates.get = unavailable
    data = summary(c, value)
    assert data['handover'] is None and data['usd_deduction'] is None
    assert 'Нет курса' in data['usd_issue']
    assert hand_over(c, value, '5000000').status_code == 409
    failed = c.post('/api/cashier/usd-deposits', json={'date': DAY.isoformat(), 'amount': '100'})
    assert failed.status_code == 503
    assert till_totals(c.app.state, DAY).usd_total == 300
    exported = c.get('/api/cashier/export', params={'date': DAY.isoformat(), 'snapshot_id': value.id})
    sheet = openpyxl.load_workbook(BytesIO(exported.content))['отчет']
    assert sheet['B29'].value == sheet['B34'].value == 'требует проверки'


def test_existing_deposit_loads_historical_rate_and_survives_restart(make_app):
    from test_cashier_design_parity import client_for
    app = make_app()
    app.state.usd_rates.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[{
        'Ccy': 'USD', 'Nominal': '1', 'Rate': '11897.90', 'Date': DAY.strftime('%d.%m.%Y')}]))
    with client_for(app) as client:
        value = snapshot(client)
        add_usd_deposit(app.state.accountant_finance, DAY, '300')
        assert summary(client, value)['handover'] == '1490000'
    again = make_app()
    again.state.usd_rates.transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with client_for(again) as client:
        value = snapshot(client)
        assert summary(client, value)['handover'] == '1490000'
