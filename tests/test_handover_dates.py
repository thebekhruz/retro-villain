"""Receipt date regressions, including restart-safe legacy migration on both DBs."""
from contextlib import closing
from datetime import date
from decimal import Decimal

import pytest

from test_cashier_design_parity import make_app, c, snapshot_for, hand_over
from retro.modules.accountant.handover_dates import cashier_day, receipt_day


@pytest.mark.parametrize('shift,received', [
    ('2026-10-04', '2026-10-05'), ('2026-10-31', '2026-11-01'), ('2026-09-30', '2026-10-01'),
    ('2025-12-31', '2026-01-01'), ('2024-02-29', '2024-03-01'),
])
def test_shift_cash_arrives_only_the_next_day(c, shift, received, monkeypatch):
    monkeypatch.setattr("retro.modules.cashier.routes.today_tashkent", lambda: date(2026, 11, 2))
    monkeypatch.setattr("retro.modules.accountant.routes.today_tashkent", lambda: date(2026, 11, 2))
    shift, received = date.fromisoformat(shift), date.fromisoformat(received)
    snap = snapshot_for(shift)
    c.app.state.cache.put(snap)
    finance = c.app.state.accountant_finance
    finance.record_handover(shift, Decimal('100'))  # previous shift's receipt
    finance.set_cash_opening(shift, '0', 'Пересчёт')
    assert hand_over(c, snap, '1200000', shift).status_code == 201
    old = c.get('/api/accountant/day', params={'date': shift.isoformat()}).json()
    new = c.get('/api/accountant/day', params={'date': received.isoformat()}).json()
    assert old['expected_cashier'] == '100'
    assert old['ledger']['cash_balance'] == '100'
    assert new['cashier_date'] == shift.isoformat()
    assert new['expected_cashier'] == '1200000'
    assert new['ledger']['cash_flow']['opening_balance'] == '100'
    assert new['ledger']['cash_balance'] == '1200100'
    assert cashier_day(received) == shift and receipt_day(shift) == received
    assert finance.cash_flows_between(shift, shift)[0]['amount'] == '100'
    received_flow = finance.cash_flows_between(received, received)
    assert [r['amount'] for r in received_flow if r['type'] == 'handover'] == ['1200000']
    expense = dict(date=shift.isoformat(), item_code='admin_other', note='Такси', amount='200')
    assert c.post('/api/accountant/expenses', json=expense).status_code == 422
    expense['date'] = received.isoformat()
    assert c.post('/api/accountant/expenses', json=expense).status_code == 201
    confirmed = c.post('/api/accountant/handover/confirm', json={'date': received.isoformat(), 'amount': '1200000'})
    assert confirmed.status_code == 200
    assert confirmed.json()['handover']['shortfall'] == '0'
    cashier = c.get('/api/cashier/handover', params={'date': shift.isoformat()}).json()
    assert cashier['handover']['confirmed_at']


def legacy(finance):
    with closing(finance._open()) as conn, conn:
        conn.execute('DELETE FROM accountant_data_migrations')


def test_migration_preserves_confirmations_and_manual_receipt_dates(c):
    finance = c.app.state.accountant_finance
    finance.record_handover(date(2026, 9, 29), Decimal('20'), source='cashier')
    finance.record_handover(date(2026, 9, 30), Decimal('30'), source='cashier')
    finance.confirm_handover(date(2026, 9, 30), '29', Decimal('30'), 'Бухгалтер')
    finance.record_handover(date(2026, 9, 28), Decimal('10'))  # already a receipt day
    before = finance.handover_state(date(2026, 9, 30))
    legacy(finance)
    finance._initialize()
    from retro.modules.cashier.till import cashier_active
    assert cashier_active(c.app.state, date(2026, 9, 29))
    assert not cashier_active(c.app.state, date(2026, 9, 28))
    assert finance.handover_for_day(date(2026, 9, 29)) is None
    assert finance.handover_for_day(date(2026, 9, 28)) == Decimal('10')
    assert finance.handover_for_day(date(2026, 9, 30)) == Decimal('20')
    after = finance.handover_state(date(2026, 10, 1))
    for key in ('amount', 'confirmed_at', 'confirmed_by', 'expected_amount', 'handed_at', 'source'):
        assert before[key] == after[key]
    finance._initialize()  # a second worker/restart must not shift anything again
    assert finance.handover_state(date(2026, 10, 1)) == after
    assert len(finance.audit_entries(entity_type='handover', entity_id='2026-10-01')) == 1


def test_migration_conflict_rolls_back_all_moves(c):
    finance = c.app.state.accountant_finance
    finance.record_handover(date(2026, 9, 28), Decimal('10'), source='cashier')
    finance.record_handover(date(2026, 9, 29), Decimal('20'))  # manual receipt: never overwrite
    finance.record_handover(date(2026, 9, 30), Decimal('30'), source='cashier')
    legacy(finance)
    with pytest.raises(ValueError, match='нужна сверка'):
        finance._initialize()
    assert finance.handover_for_day(date(2026, 10, 1)) is None
    assert finance.handover_for_day(date(2026, 9, 30)) == Decimal('30')
    assert finance.handover_for_day(date(2026, 9, 28)) == Decimal('10')
    assert finance.handover_for_day(date(2026, 9, 29)) == Decimal('20')
    assert not finance.audit_entries(entity_type='handover', entity_id='2026-10-01')


def test_month_report_includes_last_shifts_next_month_receipt(c):
    from io import BytesIO
    import openpyxl
    from retro.modules.founder.export import month_workbook

    finance = c.app.state.accountant_finance
    finance.record_handover(date(2026, 11, 1), Decimal('21896000'), source='cashier')
    finance.confirm_handover(date(2026, 11, 1), '21896000', Decimal('21895200'), 'Бухгалтер')
    data = month_workbook(c.app.state, date(2026, 10, 1), date(2026, 10, 31), {}, None,
                          {'2026-10-31': {'demo': '21545200', 'expected': '21895200'}})
    book = openpyxl.load_workbook(BytesIO(data))
    transfers = book['Передачи смен']
    assert 'закрытие 01.11.2026' in transfers['A1'].value
    assert transfers['A35'].value.date() == date(2026, 10, 31)
    assert transfers['B35'].value.date() == date(2026, 11, 1)
    assert transfers['C35'].value == 21895200
    assert transfers['D35'].value == 21896000
    assert transfers['E35'].value
    # Receipt belongs to November's cash book, not October 31 closing cash.
    assert book['По дням']['E35'].value is None
