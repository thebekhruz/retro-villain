"""Read-only audit of calculation defects; writes only to temporary databases.

Run from the repository root with PYTHONPATH=. python scripts/reproduce_data_audit.py.
No live credentials, live APIs, or application databases are used.
Assertions describe observed defects on c31e697, not desired future behavior.
"""
import json
import asyncio
import tempfile
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from retro.integrations.claude import compact_analysis_input
from retro.integrations.iiko import founder_rows_from_olap, reconcile_director_costs, cash_prepay_from_shifts
from retro.modules.accountant.ledger import FinanceStore, LedgerError
from retro.modules.accountant.payroll import PayrollRow, draft_payroll, AttendanceRow
from retro.modules.accountant.roster import RosterStore
from retro.modules.cashier.service import DataError, RETRO_REGISTER, Snapshot, Payment, TZ
from retro.modules.cashier.expenses import cash_to_finance
from retro.modules.director.models import SalesRow, build_snapshot, direction
from retro.modules.founder.models import RevenueRow, PaymentRow, build_analytics, classify_direction
from retro.config import HikvisionConfig
from retro.integrations.hikvision import HikvisionPerson, parse_events_page
from retro.integrations.hikvision_poller import HikvisionPoller
from retro.modules.accountant.hikvision import AttendanceStore, AttendanceService
from scripts.import_monthly_payroll import read_rows, import_rows
from openpyxl import Workbook

DAY = date(2026, 9, 14)
END = DAY + timedelta(days=9)
findings = []


def record(name, **values):
    findings.append(dict(case=name, **values))


def sale(**kwargs):
    return replace(SalesRow(DAY, RETRO_REGISTER, 'Ресторан', 'Демо', 'Тестовое блюдо',
                           'Меню', D(1), D(100), D(30), 'Тестовый официант', 'test-order'), **kwargs)


def ten_days(row=None):
    row = row or sale()
    return [replace(row, day=DAY+timedelta(days=i), order_id=str(i)) for i in range(10)]


def payroll(rate=None):
    return PayrollRow(1, 'Тест', 'официант', 'Обслуживание зала', 'on_time',
                      datetime(2026, 9, 14, 9, tzinfo=TZ), rate, rate, False)


def main():
    # Parent says 1,000; the retained leaf says 100. No subtotal validation.
    vals = [str(DAY), RETRO_REGISTER, 'Ресторан', 'Тестовое блюдо', 'Демо']
    leaf = {f'field{i}': {'value': v} for i, v in enumerate(vals+[100])}
    parent = {'field0': {'value': str(DAY)}, 'field5': {'value': 1000}, 'children': [leaf]}
    payments = founder_rows_from_olap([parent], payments=True)
    revenue = [RevenueRow(r.day, r.register, r.section, r.item, r.amount) for r in payments]
    out = build_analytics(revenue, payments, DAY, DAY, 'day', ('retro',))
    assert out['reconciled'] and out['totals']['selected'] == '100'
    record('self_reconciliation_and_ignored_parent', parent=1000, selected=100,
           reconciled=out['reconciled'], discrepancy=out['discrepancy'])

    for name, rows in [('empty_trading_day', ten_days()[:-1]),
                       ('negative_return', ten_days()+[sale(revenue=D(-50), quantity=D(-1), cost=D(-15))])]:
        try:
            build_snapshot(rows, {}, DAY, END)
        except DataError as error:
            record(name, rejected=str(error))
        else:
            raise AssertionError(name)

    src = sale(revenue=D(99), quantity=D(1))
    parts = [replace(src, quantity=D('.333'), revenue=D(33)) for _ in range(3)]
    normalized = reconcile_director_costs(parts, [src])
    assert sum(r.quantity for r in normalized) == D('.999')
    assert sum(r.cost for r in normalized) == src.cost
    record('quantity_rounding_not_reconciled', original=src.quantity,
           allocated=sum(r.quantity for r in normalized), cost_preserved=True)

    # Valid amount and an unknown register bypass validation when a dish contains the tag.
    tagged = sale(register='UNKNOWN', item='Салат Бехруз')
    assert direction(tagged) == 'banquet'
    try:
        classify_direction(tagged.register, tagged.section, tagged.item)
    except DataError:
        record('different_direction_validation', director='banquet', founder='rejected')

    # A shift-only excess has no transaction classification but is labelled a prepayment.
    shift = dict(cashRegNumber=1, openDate=str(DAY)+'T10:00:00',
                 payOrders=150, salesCash=150, salesCard=0, salesCredit=0)
    prepay, cash = cash_prepay_from_shifts(DAY, D(100), {'Демо': D(100)}, [shift])
    assert prepay == cash == D(50)
    record('unclassified_shift_excess_is_prepayment', shift_receipts=150,
           scoped_sales=100, labelled_new_prepayment=prepay)

    # Fiscal cash is deducted when deriving prepayments, then omitted from handover.
    snap = Snapshot('test', DAY, D(150), 1,
                    (Payment('Демо', D(100)), Payment('Наличные (Инкасса QR)', D(50))),
                    datetime.now(TZ))
    record('handover_excludes_fiscal_cash', two_cash_types=150,
           handover=cash_to_finance(snap, D(0)))

    with tempfile.TemporaryDirectory(prefix='retro-audit-') as folder:
        root = Path(folder)
        def store(name): return FinanceStore(root / (name+'.sqlite3'))

        f = store('payroll')
        assert f.confirm_payroll(DAY, [payroll()], 'Аудит')
        assert not f.confirm_payroll(DAY, [payroll(D(100))], 'Аудит')
        assert f.summary(DAY)['accrued_on_day'] == 0
        record('missing_rate_locks_payroll', payroll_confirmed=True, accrual=0,
               after_rate_fixed=0)

        f = store('handover')
        f.record_handover(DAY, D(100))
        f.add_expense(DAY, 'admin_other', 'Тест', '80', cashier_amount=D(100))
        f.record_handover(DAY, D(10))
        out = f.daily_summary(DAY, D(10))
        assert out['cash_balance'] == -70
        record('handover_edit_bypasses_balance_check', closing=out['cash_balance'])

        f = store('manual_without_anchor')
        f.record_handover(DAY, D(100))
        f.record_handover(DAY+timedelta(days=1), D(10))
        before = f.daily_summary(DAY+timedelta(days=1), D(10), carry_history=False)
        f.add_expense(DAY+timedelta(days=1), 'admin_other', 'Тест', '80', cashier_amount=D(10))
        after = f.daily_summary(DAY+timedelta(days=1), D(10), carry_history=False)
        assert before['cash_balance'] == 10 and after['cash_balance'] == -70
        record('manual_display_and_validation_differ', displayed_before=before['cash_balance'],
               allowed_payment=80, displayed_after=after['cash_balance'])

        f = store('first_handover_deleted')
        f.record_handover(DAY, D(100))
        f.set_cash_opening(DAY, '1000', 'Тест')
        f.record_handover(DAY+timedelta(days=1), D(10))
        f.delete_handover(DAY)
        out = f.daily_summary(DAY+timedelta(days=1), D(10))
        assert out['cash_balance'] == 10 and out['cash_flow']['missing_day'] is None
        record('deleted_anchor_handover_hides_opening', confirmed_opening=1000,
               displayed_closing=out['cash_balance'], missing_day=out['cash_flow']['missing_day'])

        f = store('delete_expense')
        f.record_handover(DAY, D(100))
        first = f.add_expense(DAY, 'admin_other', 'A', '20', cashier_amount=D(100))
        f.add_expense(DAY, 'admin_other', 'B', '30', cashier_amount=D(100))
        try:
            f.delete_operation('movement', first, DAY)
        except LedgerError as error:
            record('delete_expense_uses_legacy_balance', expected_after=70, rejected=str(error))
        else:
            raise AssertionError('delete should expose legacy-balance bug')

        f = store('shoh')
        f.record_handover(DAY, D(100))
        f.reserve_entry(DAY, 'shoh', 'opening', '0', 'Тест')
        movement = f.add_expense(DAY, 'proc_shoh', 'Закуп', '100', cashier_amount=D(100))
        f.reserve_entry(DAY, 'shoh', 'withdrawal', '100', 'Накладная')
        f.update_movement(movement, DAY, 'proc_shoh', 'Исправлено', '10')
        out = f.reserves(DAY)
        assert D(out['shoh']['balance']) == -90
        record('procurement_edit_makes_subledger_negative', shoh_balance=out['shoh']['balance'])

        f = store('label')
        out = f.daily_summary(DAY, D(100))
        record('handover_label_previous_day', accounting_day=DAY,
               journal_label=out['movements'][0]['description'])

        f = store('repeated_income')
        f.record_handover(DAY, D(100))
        f.record_handover(DAY, D(50))
        record('second_cashier_income_replaces_first', entered=[100, 50],
               stored=f.handover_for_day(DAY))

        f = store('generic_salary')
        f.record_handover(DAY, D(100))
        f.confirm_payroll(DAY, [payroll(D(100))], 'Аудит')
        f.add_expense(DAY, 'salary_staff', 'Тест', '100', cashier_amount=D(100))
        summary = f.daily_summary(DAY, D(100))
        assert summary['salary_recorded_on_day'] == summary['salary_debt'] == 100
        record('generic_salary_does_not_settle_accrual', displayed_paid=summary['salary_recorded_on_day'],
               salary_debt=summary['salary_debt'], cash_balance=summary['cash_balance'])

        f = store('income_as_expense')
        f.record_handover(DAY, D(100))
        f.add_expense(DAY, 'income_other', 'Тест', '10', cashier_amount=D(100))
        record('expense_api_accepts_income_category', label='Прочие поступления',
               closing=f.daily_summary(DAY, D(100))['cash_balance'])

        roster = RosterStore(root/'roster.sqlite3')
        employee = roster.add(name='Тест', role='официант', rate='100', group_name='Обслуживание зала')
        entries = [AttendanceRow(employee.id, 'on_time', datetime(2026,9,14,9,tzinfo=TZ))]
        before = draft_payroll(DAY, roster.list(), set(), entries)[0].payable
        roster.update(employee.id, rate='200', group_name=employee.group_name, reason='Новая ставка')
        after = draft_payroll(DAY, roster.list(), set(), entries)[0].payable
        assert before == 100 and after == 200
        record('historical_draft_uses_current_rate', original=before, after_rate_change=after)
        monthly = roster.add_monthly(name='Тест', role='менеджер', salary='100', card='100', cash='100', remaining='100')
        record('monthly_values_not_reconciled', monthly=monthly.json())

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['external_key','name','role','salary','schedule','card','cash','advances','remaining'])
        sheet.append(['audit-xlsx','Новый тест','менеджер',100,'',0,0,0,100])
        workbook.save(root/'monthly.xlsx')
        imported = read_rows(root/'monthly.xlsx')
        assert imported[0]['card'] == ''
        try:
            import_rows(roster, imported)
        except ValueError as error:
            record('xlsx_numeric_zero_becomes_empty', input_card=0, parsed_card=imported[0]['card'],
                   rejected=str(error))
        else:
            raise AssertionError('XLSX numeric-zero import unexpectedly succeeded')

        attendance = AttendanceStore(root/'attendance.sqlite3')
        roster.link_hikvision_people((HikvisionPerson('audit-person', 'Тест'),))
        config = HikvisionConfig('https://example.invalid', 'unused', 'unused', source='audit')
        now = datetime(2026, 9, 15, 12, tzinfo=TZ)

        class MalformedEventClient:
            async def fetch_events(self, start, end):
                return parse_events_page(json.dumps({'AcsEvent': {
                    'responseStatusStrg':'OK', 'numOfMatches':1,
                    'InfoList':[{'major':5, 'minor':75, 'serialNo':1,
                                 'employeeNoString':'audit-person', 'time':'INVALID'}]}}), 'audit').items

        result = asyncio.run(HikvisionPoller(config, MalformedEventClient(), roster, attendance,
                                             now=lambda:now).run_once())
        snapshot = AttendanceService(attendance, source='audit', enabled=True, poll_seconds=30).snapshot(
            DAY, roster.list(), now=now)
        assert result.success and snapshot.complete and snapshot.rows[0].status == 'missing'
        record('malformed_event_marked_as_complete_absence', sync_success=result.success,
               complete=snapshot.complete, employee_status=snapshot.rows[0].status)

    # All-direction amounts are assigned to one direction in the compressed AI input.
    metric = dict(quantity='1', revenue='100', cost='10', gross_profit='90', margin_percent='90',
                  breakdown={'sales': dict(quantity='1',revenue='100',cost='10',gross_profit='90')})
    compact = compact_analysis_input(dict(item_metrics={'all':{'Одинаковое имя':metric},
                                    'retro':{'Одинаковое имя':dict(metric,revenue='70')},
                                    'oxbridge':{'Одинаковое имя':dict(metric,revenue='30')}}))
    candidate = compact['review_candidates'][0]
    assert candidate['direction']=='oxbridge' and candidate['revenue']=='100' and 'breakdown' not in candidate
    record('ai_loses_direction_and_breakdown', candidate=candidate, school_revenue='30',
           breakdown_sent=False)
    print(json.dumps(findings, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
