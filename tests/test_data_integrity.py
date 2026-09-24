"""Regressions from the 2026-09-24 audit; no live services or production writes."""
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from retro.config import Settings
from retro.integrations.claude import compact_analysis_input
from retro.integrations.hikvision import HikvisionError, parse_events_page
from retro.integrations.iiko import reconcile_director_costs
from retro.modules.accountant.ledger import FinanceStore, LedgerError
from retro.modules.accountant.payroll import PayrollRow
from retro.modules.accountant.roster import RosterStore
from retro.modules.cashier.service import DataError, today_tashkent
from retro.modules.director.models import SalesRow, build_snapshot

DAY = date(2026, 9, 14)


def salary(amount='100'):
    return PayrollRow(1, 'Тест', 'повар', 'Кухня', 'on_time', None,
                      D(amount) if amount else None, D(amount) if amount else None, False)


@pytest.fixture
def store(tmp_path):
    return FinanceStore(tmp_path / 'ledger.sqlite3')


def test_unknown_payroll_cannot_close_day_and_can_be_fixed(store):
    with pytest.raises(LedgerError, match='неполное'):
        store.confirm_payroll(DAY, [salary(None)], 'Тест')
    assert not store.summary(DAY)['payroll_confirmed']
    assert store.confirm_payroll(DAY, [salary()], 'Тест')
    assert store.summary(DAY)['salary_debt'] == 100


def test_empty_payroll_cannot_close_day(store):
    with pytest.raises(LedgerError):
        store.confirm_payroll(DAY, [], 'Тест')


def test_zero_accrual_is_frozen_too(store):
    store.confirm_payroll(DAY, [replace(salary(), status='missing', payable=D(0))], 'Тест')
    assert store.accruals(DAY)[0]['amount'] == '0'


def test_handover_correction_rolls_back_future_deficit(store):
    store.record_handover(DAY, D(100))
    tomorrow = DAY + timedelta(days=1)
    store.record_handover(tomorrow, D(10))
    store.add_expense(tomorrow, 'admin_other', 'Расход', 80, cashier_amount=D(10))
    with pytest.raises(LedgerError, match='отрицательным'):
        store.record_handover(DAY, D(10))
    assert store.handover_for_day(DAY) == 100
    assert store.daily_summary(tomorrow, D(10))['cash_balance'] == 30


def test_opening_cannot_disappear_when_first_handover_deleted(store):
    store.record_handover(DAY, D(100))
    store.set_cash_opening(DAY, 1000, 'Остаток')
    store.record_handover(DAY + timedelta(days=1), D(10))
    with pytest.raises(LedgerError):
        store.delete_handover(DAY)
    assert store.daily_summary(DAY + timedelta(days=1), D(10))['cash_balance'] == 1110
    # A legacy corrupt database must expose a gap, never silently lose opening.
    with sqlite3.connect(store.path) as conn:
        conn.execute('DELETE FROM accountant_handover_days WHERE day=?', (DAY.isoformat(),))
    result = store.daily_summary(DAY + timedelta(days=1), D(10))
    assert result['cash_balance'] is None
    assert result['cash_flow']['missing_day'] == DAY.isoformat()


def test_second_income_adds_and_duplicate_daily_creation_rejects(store):
    store.record_handover(DAY, D(100), create_only=True)
    store.record_handover(DAY, D(50), add=True)
    assert store.handover_for_day(DAY) == 150
    with pytest.raises(LedgerError):
        store.record_handover(DAY, D(40), create_only=True)


def test_reading_preview_does_not_persist_or_change_handover(store):
    assert store.daily_summary(DAY, D(100))['cash_balance'] == 100
    assert store.handover_for_day(DAY) is None
    assert store.audit_entries() == []
    store.record_handover(DAY, D(50))
    before = store.audit_entries()
    store.daily_summary(DAY, D(100))
    assert store.handover_for_day(DAY) == 50
    assert store.audit_entries() == before


def test_manual_display_and_spending_use_same_carry(store):
    store.record_handover(DAY, D(100))
    tomorrow = DAY + timedelta(days=1)
    store.record_handover(tomorrow, D(10))
    assert store.daily_summary(tomorrow, D(10), carry_history=False)['cash_balance'] == 110
    store.add_expense(tomorrow, 'admin_other', 'Расход', 80, cashier_amount=D(10))
    assert store.daily_summary(tomorrow, D(10), carry_history=False)['cash_balance'] == 30


def test_deleting_expense_uses_handover_balance(store):
    store.record_handover(DAY, D(100))
    one = store.add_expense(DAY, 'admin_other', 'Расход', 20, cashier_amount=D(100))
    store.add_expense(DAY, 'admin_other', 'Расход', 30, cashier_amount=D(100))
    store.delete_operation('movement', one, DAY)
    assert store.daily_summary(DAY, D(100))['cash_balance'] == 70


def test_shoh_dependent_balance_prevents_edit_and_delete(store):
    store.record_handover(DAY, D(100))
    store.reserve_entry(DAY, 'shoh', 'opening', 0, 'Начало')
    movement = store.add_expense(DAY, 'proc_shoh', 'Продукты', 100, cashier_amount=D(100))
    store.reserve_entry(DAY, 'shoh', 'withdrawal', 100, 'Накладная')
    with pytest.raises(LedgerError, match='подотчёта'):
        store.update_movement(movement, DAY, 'proc_shoh', 'Продукты', 10)
    with pytest.raises(LedgerError, match='подотчёта'):
        store.delete_operation('movement', movement, DAY)
    assert store.reserves(DAY)['shoh']['balance'] == '0'


@pytest.mark.parametrize('code', ['income_cashier', 'income_other', 'income_opening'])
def test_expense_rejects_income_codes_in_all_paths(store, code):
    store.record_handover(DAY, D(100))
    movement = store.add_expense(DAY, 'admin_other', 'Расход', 10, cashier_amount=D(100))
    with pytest.raises(LedgerError):
        store.add_expense(DAY, code, 'Ошибка', 10, cashier_amount=D(100))
    with pytest.raises(LedgerError):
        store.update_movement(movement, DAY, code, 'Ошибка', 10)
    with pytest.raises(LedgerError):
        store.record_debt(DAY, code, 'Ошибка', 20, 0)


def test_wages_require_accrual_reference_and_reduce_debt(store):
    store.record_handover(DAY, D(100))
    store.confirm_payroll(DAY, [salary()], 'Тест')
    with pytest.raises(LedgerError, match='начисление'):
        store.add_expense(DAY, 'salary_staff', 'Зарплата', 100, cashier_amount=D(100))
    store.pay_salary(store.accruals(DAY)[0]['id'], DAY, 100, cashier_amount=D(100))
    result = store.daily_summary(DAY, D(100))
    assert result['salary_debt'] == result['cash_balance'] == 0
    assert result['salary_paid_on_day'] == 100


def sale(**changes):
    row = SalesRow(DAY, 'Kassa-FiscalBox1', 'Ресторан', 'Демо', 'Плов', 'Еда',
                   D(1), D(100), D(40), 'Тест', 'order')
    return replace(row, **changes)


def test_exact_quantity_and_revenue_are_conserved_after_split():
    parts = [sale(quantity=D('.333'), revenue=D('33.33')) for _ in range(3)]
    result = reconcile_director_costs(parts, [sale(payment_type='')])
    assert sum(row.quantity for row in result) == 1
    assert sum(row.revenue for row in result) == 100
    assert sum(row.cost for row in result) == 40


def test_signed_refunds_and_zero_days_are_valid():
    rows = reconcile_director_costs([sale(quantity=D(-1), revenue=D(-100), cost=D(-40))],
                                   [sale(quantity=D(-1), revenue=D(-100), cost=D(-40))])
    report = build_snapshot(rows, {}, DAY, DAY + timedelta(days=9))
    assert report.cash_total == -100
    assert report.item_metrics['all']['Плов'].breakdown['sales'].revenue == -100
    assert build_snapshot([], {}, DAY, DAY + timedelta(days=9)).cash_total == 0


def test_director_totals_show_exclusions_and_share_classifier():
    report = build_snapshot([sale(), sale(category='Контейнеры', revenue=D(10))], {},
                            DAY, DAY + timedelta(days=9), excluded_groups={'Контейнеры'})
    assert report.cash_total == 110
    assert report.json()['menu_revenue'] == '100'
    assert report.json()['excluded_revenue'] == {'Контейнеры': '10'}
    with pytest.raises(DataError, match='касса'):
        build_snapshot([sale(register='unknown', item='Бехруз')], {}, DAY, DAY + timedelta(days=9))


def test_ai_uses_actual_direction_amounts_and_preserves_free_consumption():
    report = build_snapshot([sale(revenue=D(70)),
                            sale(register='GL-Kassa-Oksbrich', section='Зал', revenue=D(30)),
                            sale(revenue=D(0), non_cash_payment_type='Дегустация')], {},
                           DAY, DAY + timedelta(days=9))
    candidates = compact_analysis_input(report.json())['review_candidates']
    assert {row['direction']: row['revenue'] for row in candidates} == {'retro': '70', 'oxbridge': '30'}
    assert next(row for row in candidates if row['direction'] == 'retro')['breakdown']['tasting']['cost'] == '40'


@pytest.mark.parametrize('field,value', [('time', 'broken'), ('employeeNoString', ''), ('serialNo', None)])
def test_malformed_successful_entry_is_not_silently_marked_missing(field, value):
    event = dict(major=5, minor=75, serialNo='1', employeeNoString='7', time='2026-09-14T09:00:00+05:00')
    event[field] = value
    with pytest.raises(HikvisionError):
        parse_events_page(json.dumps({'AcsEvent': {'InfoList': [event]}}))


def test_roster_keeps_past_rate_and_deleted_employee(tmp_path):
    roster = RosterStore(tmp_path / 'roster.sqlite3')
    person = roster.add(name='Тест', role='повар', group_name='Кухня', rate='100')
    yesterday = today_tashkent() - timedelta(days=1)
    roster.update(person.id, rate='200', group_name='Кухня', reason='Новая ставка')
    assert roster.list(yesterday)[0].rate == 100
    assert roster.list()[0].rate == 200
    roster.delete(person.id)
    assert roster.list() == []
    assert roster.list(yesterday)[0].name == 'Тест'


def test_monthly_import_keeps_numeric_zero_and_is_atomic(tmp_path):
    from openpyxl import Workbook
    from scripts.import_monthly_payroll import FIELDS, import_rows, read_rows
    workbook = Workbook()
    workbook.active.append(FIELDS)
    workbook.active.append(['1', 'Первый', 'повар', 100, '', 0, 0, 0, 0])
    path = tmp_path / 'monthly.xlsx'
    workbook.save(path)
    roster = RosterStore(tmp_path / 'roster.sqlite3')
    assert import_rows(roster, read_rows(path)) == (1, 0)
    roster.add_monthly(name='Дубль', role='повар', salary='100')
    roster.add_monthly(name='Дубль', role='повар', salary='100')
    first = dict(read_rows(path)[0], salary=200)
    ambiguous = dict(first, external_key='', name='Дубль')
    with pytest.raises(ValueError, match='несколько'):
        import_rows(roster, [first, ambiguous])
    assert next(person for person in roster.list_monthly() if person.external_key == '1').salary == 100


def test_founder_independent_report_detects_mismatch():
    from contextlib import asynccontextmanager
    from retro.integrations.iiko import IikoClient
    client = IikoClient(Settings(login='test', password='test', store_id=1))

    @asynccontextmanager
    async def no_http():
        yield None

    async def olap(_client, start, end, groups, fields, extra_filters=()):
        values = [DAY.isoformat(), 'Kassa-FiscalBox1', 'Ресторан', 'Плов']
        if 'PayTypes' in groups:
            values += ['Демо', 100]
        else:
            values += [110]
        return [{f'field{i}': {'value': value} for i, value in enumerate(values)}]

    client._client = no_http
    client._olap_range = olap
    result = asyncio.run(client.load_founder_analytics(DAY, DAY, 'day', ('retro',)))
    assert result['totals']['selected'] == '110'
    assert result['payment_total'] == '100'
    assert result['discrepancy'] == '-10'
    assert not result['reconciled']


def test_opposite_daily_discrepancies_cannot_cancel():
    from retro.modules.founder.models import RevenueRow, PaymentRow, build_analytics
    tomorrow = DAY + timedelta(days=1)
    revenue = [RevenueRow(day, 'Kassa-FiscalBox1', 'Ресторан', 'Плов', D(100)) for day in (DAY, tomorrow)]
    payments = [PaymentRow(day, 'Kassa-FiscalBox1', 'Ресторан', 'Плов', 'Демо', amount)
                for day, amount in [(DAY, D(90)), (tomorrow, D(110))]]
    report = build_analytics(revenue, payments, DAY, tomorrow, 'month', ('retro',))
    assert report['discrepancy'] == '0'
    assert not report['reconciled']
    assert len(report['daily_discrepancies']) == 2


def test_raw_ai_details_cannot_expose_duplicate_additive_cost():
    from contextlib import asynccontextmanager
    from retro.integrations.iiko import IikoClient
    client = IikoClient(Settings(login='test', password='test', store_id=1))

    @asynccontextmanager
    async def no_http():
        yield None

    async def olap(_client, start, end, groups, fields):
        return [{f'field{i}': {'value': value} for i, value in enumerate(values)}
                for values in [('Плов', 'Демо', 1, 100, 40, 40, 1),
                               ('Плов', 'UzCard', 1, 100, 40, 40, 1)]]

    client._client = no_http
    client._olap_range = olap
    result = asyncio.run(client.load_sales_details(DAY, DAY, ['DishName', 'PayTypes'], limit=1))
    assert result['rows'][0]['product_cost_total'] is None
    assert not result['cost_additive']
    assert result['truncated']
    assert len(result['warnings']) == 2


def test_export_rejects_manual_entries_changed_after_screen(tmp_path):
    from fastapi.testclient import TestClient
    from retro.app import create_app
    from retro.modules.cashier.service import demo_snapshot
    app = create_app(Settings(data_dir=tmp_path), expense_db_path=tmp_path / 'cashier.sqlite3',
                     accountant_db_path=tmp_path / 'accountant.sqlite3',
                     director_db_path=tmp_path / 'director.sqlite3', founder_db_path=tmp_path / 'founder.sqlite3')
    snapshot = replace(demo_snapshot(DAY), demo=False)
    app.state.cache.put(snapshot)
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        revision = client.get('/api/cashier/expenses', params={'date': DAY.isoformat()}).json()['revision']
        client.post('/api/cashier/expenses', json=dict(date=DAY.isoformat(), description='Бумага', amount='10'))
        response = client.get('/api/cashier/export', params=dict(
            date=DAY.isoformat(), snapshot_id=snapshot.id, expense_revision=revision))
        assert response.status_code == 409


def test_financial_retries_replay_once_and_reject_changed_payload(tmp_path):
    from fastapi.testclient import TestClient
    from retro.app import create_app
    from uuid import uuid4
    app = create_app(Settings(data_dir=tmp_path), expense_db_path=tmp_path/'cashier.sqlite3',
                     accountant_db_path=tmp_path/'accountant.sqlite3',
                     director_db_path=tmp_path/'director.sqlite3', founder_db_path=tmp_path/'founder.sqlite3')
    with TestClient(app, client=('127.0.0.1', 50000)) as client:
        for path, payload in [
            ('/api/cashier/expenses', dict(date=DAY.isoformat(),description='Бумага',amount='10')),
            ('/api/accountant/incomes', dict(date=DAY.isoformat(),item_code='income_cashier',note='Передача',amount='100')),
        ]:
            headers = {'Idempotency-Key': str(uuid4())}
            first = client.post(path, json=payload, headers=headers)
            assert first.status_code == 201
            assert client.post(path, json=payload, headers=headers).json() == first.json()
            assert client.post(path, json=dict(payload, amount='200'), headers=headers).status_code == 409
        assert app.state.expenses.total(DAY) == 10
        assert app.state.accountant_finance.handover_for_day(DAY) == 100
        # A reservation interrupted between write and response must never execute again.
        import hashlib
        key = str(uuid4())
        body = json.dumps(dict(date=DAY.isoformat(),description='Бумага',amount='10')).encode()
        app.state.financial_requests.reserve('local', key, hashlib.sha256(b'/api/cashier/expenses\0' + body).hexdigest())
        assert client.post('/api/cashier/expenses', content=body,
                           headers={'Idempotency-Key':key,'Content-Type':'application/json'}).status_code == 409
        assert app.state.expenses.total(DAY) == 10


def test_cashier_prepayment_subtracts_whole_register_not_only_restaurant():
    from contextlib import asynccontextmanager
    from retro.integrations.iiko import IikoClient
    client = IikoClient(Settings(login='test', password='test', store_id=1))

    def row(*values):
        return {f'field{i}': {'value': value} for i, value in enumerate(values)}

    @asynccontextmanager
    async def no_http():
        yield None

    async def olap(_client, day, groups, fields, filters=()):
        if groups == ['CashRegisterName', 'RestaurantSection']:
            return [{**row('Kassa-FiscalBox1', None, 160),
                     'children': [row(None, 'Ресторан', 100), row(None, 'Бехруз (Свадьба)', 60)]}]
        if groups == ['OpenDate.Typed']:
            return [row(day.isoformat(), 1, 100)]
        narrow = any(f['field'] == 'RestaurantSection' for f in filters)
        return [row('Демо', 60 if narrow else 90), row('UzCard', 40 if narrow else 70)]

    async def shifts(*args):
        return {'shifts': [dict(cashRegNumber=1, openDate=DAY.isoformat()+'T10:00:00',
                               payOrders=200, salesCash=120, salesCard=80, salesCredit=0)]}

    client._client = no_http
    client._olap = olap
    client._post = shifts
    result = asyncio.run(client.load(DAY))
    assert result.revenue == 100
    # Old subtraction reported 100 total / 60 cash; 60 of that was banquet SALES.
    assert result.new_prepayment == 40
    assert result.cash_prepayment == 30
    assert result.register_received_total == 200


def test_detail_pages_require_same_source_revision():
    from contextlib import asynccontextmanager
    from retro.integrations.iiko import IikoClient
    client = IikoClient(Settings(login='test', password='test', store_id=1))
    raw = [{f'field{i}': {'value': value} for i, value in enumerate((name, 1, 100, 40, 40, 1))}
           for name in ('Плов', 'Суп', 'Чай')]

    @asynccontextmanager
    async def no_http():
        yield None

    async def olap(*args):
        return raw

    client._client = no_http
    client._olap_range = olap
    async def exercise():
        first = await client.load_sales_details(DAY, DAY, ['DishName'], limit=1)
        second = await client.load_sales_details(DAY, DAY, ['DishName'], limit=1,
                                               offset=first['next_offset'], revision=first['revision'])
        assert first['rows'][0]['dimensions']['DishName'] == 'Плов'
        assert second['rows'][0]['dimensions']['DishName'] == 'Суп'
        raw[0]['field2']['value'] = 200
        with pytest.raises(DataError, match='изменился'):
            await client.load_sales_details(DAY, DAY, ['DishName'], limit=1,
                                           offset=second['next_offset'], revision=first['revision'])
    asyncio.run(exercise())
