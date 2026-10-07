import asyncio
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from retro.modules.accountant import routes, salary_day
from retro.modules.accountant.ledger import FinanceStore, LedgerError
from retro.modules.accountant.roster import RosterStore
from retro.modules.accountant.salary_day import SalaryCellChanged

PAID = date(2026, 10, 7)
WORK = date(2026, 10, 6)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setattr(salary_day, 'today_tashkent', lambda: date(2026, 10, 10))
    monkeypatch.setattr(routes, 'today_tashkent', lambda: date(2026, 10, 10))
    path = tmp_path / 'finance.sqlite3'
    roster = RosterStore(path)
    finance = FinanceStore(path)
    person = roster.add(name='Сотрудник без Hikvision и ставки', role='официант', rate=None,
                        group_name='Обслуживание зала')
    for offset in range(9):
        finance.record_handover(date(2026, 10, 2)+timedelta(days=offset), Decimal(0))
    finance.set_cash_opening(date(2026, 10, 2), '1000000', 'Начало')
    return finance, roster, person


def rows(finance, table):
    with closing(finance._open()) as connection:
        return connection.execute(f'SELECT * FROM {table}').fetchall()


def test_manual_cell_has_one_previous_day_accrual_and_one_cash_payment(stores):
    finance, _, person = stores
    result = finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    assert result['work_day'] == WORK.isoformat()
    assert result['changed'] is True
    accrual = rows(finance, 'accountant_accruals')[0]
    assert accrual[1] == WORK.isoformat()
    assert accrual[5] == salary_day.MANUAL_STATUS
    assert accrual[6:] == ('0', '250000')
    assert len(rows(finance, 'accountant_salary_payments')) == 1
    assert rows(finance, 'accountant_payroll_days') == []
    summary = finance.daily_summary(PAID, None)
    assert summary['salary_paid_on_day'] == Decimal('250000')
    assert summary['salary_debt'] == Decimal(0)
    assert summary['cash_balance'] == Decimal('750000')
    assert finance.expense_totals_between(PAID, PAID)['salary'] == Decimal('250000')
    assert sum(x['type'] == 'salary_payment' for x in summary['movements']) == 1


def test_absolute_edit_retry_clear_and_audit_have_no_duplicates_or_phantom_debt(stores):
    finance, _, person = stores
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    assert finance.set_salary_day_cell(PAID, person.id, '250000', '0')['changed'] is False
    finance.set_salary_day_cell(PAID, person.id, '300000', '250000')
    finance.set_salary_day_cell(PAID, person.id, '200000', '300000')
    assert len(rows(finance, 'accountant_accruals')) == len(rows(finance, 'accountant_salary_payments')) == 1
    finance.set_salary_day_cell(PAID, person.id, '0', '200000')
    assert rows(finance, 'accountant_accruals') == rows(finance, 'accountant_salary_payments') == []
    assert finance.summary(PAID)['salary_debt'] == Decimal(0)
    assert finance.daily_summary(PAID, None)['cash_balance'] == Decimal('1000000')
    assert finance.set_salary_day_cell(PAID, person.id, '0', '0')['changed'] is False
    assert [a['action'] for a in finance.audit_entries(entity_type='salary_payment')] == [
        'create', 'update', 'update', 'delete']


def test_optimistic_conflict_and_insufficient_cash_roll_back_everything(stores):
    finance, _, person = stores
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    original = rows(finance, 'accountant_finance_audit')
    with pytest.raises(SalaryCellChanged):
        finance.set_salary_day_cell(PAID, person.id, '300000', '0')
    with pytest.raises(LedgerError, match='недостаточно'):
        finance.set_salary_day_cell(PAID, person.id, '1000001', '250000')
    assert rows(finance, 'accountant_salary_payments')[0][3] == '250000'
    assert rows(finance, 'accountant_accruals')[0][7] == '250000'
    assert rows(finance, 'accountant_finance_audit') == original


def test_historical_aggregate_import_remains_untouched_and_does_not_block_entry(stores):
    finance, _, person = stores
    with closing(finance._open()) as connection, connection:
        connection.execute('INSERT INTO accountant_movements '
                           '(day,kind,description,amount,item_code,reference,created_at) VALUES (?,?,?,?,?,?,?)',
                           (WORK.isoformat(), 'other_expense', 'Импорт персонал', '10000',
                            'salary_staff', 'historical-import', '2026-10-06'))
    before = rows(finance, 'accountant_movements')
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    assert rows(finance, 'accountant_movements') == before
    matrix = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    assert matrix['people'][0]['cells'][WORK.isoformat()]['amount'] == '0'


def test_legacy_accrual_is_taken_into_the_cell_but_other_shift_payment_is_not_overwritten(stores):
    finance, _, person = stores
    with closing(finance._open()) as connection, connection:
        accrual_id = connection.execute(
            'INSERT INTO accountant_accruals '
            '(work_day,employee_id,employee_name,group_name,attendance_status,rate,amount) '
            'VALUES (?,?,?,?,?,?,?)',
            (WORK.isoformat(), person.id, person.name, person.group_name, 'present', '100000', '100000')).lastrowid
    matrix = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    assert matrix['people'][0]['cells'][PAID.isoformat()]['editable'] is True
    # Начисление прежней ведомости без выплаты: клетка его забирает — одна пара, без второй записи.
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    accruals = rows(finance, 'accountant_accruals')
    assert [(a[0], a[5], a[7]) for a in accruals] == [(accrual_id, salary_day.MANUAL_STATUS, '250000')]
    assert [(p[1], p[2], p[3]) for p in rows(finance, 'accountant_salary_payments')] == [
        (accrual_id, PAID.isoformat(), '250000')]
    assert finance.summary(PAID)['salary_debt'] == Decimal(0)
    finance.set_salary_day_cell(PAID, person.id, '0', '250000')
    assert rows(finance, 'accountant_accruals') == rows(finance, 'accountant_salary_payments') == []
    # Выплата другой смены в этот день — не трогаем: правка задвоила бы деньги.
    with closing(finance._open()) as connection, connection:
        other = connection.execute(
            'INSERT INTO accountant_accruals '
            '(work_day,employee_id,employee_name,group_name,attendance_status,rate,amount) '
            'VALUES (?,?,?,?,?,?,?)',
            ('2026-10-05', person.id, person.name, person.group_name, 'present', '100000', '100000')).lastrowid
        connection.execute('INSERT INTO accountant_salary_payments (accrual_id,paid_day,amount,created_at) '
                           'VALUES (?,?,?,?)', (other, PAID.isoformat(), '100000', '2026-10-07'))
    with pytest.raises(SalaryCellChanged):
        finance.set_salary_day_cell(PAID, person.id, '250000', '100000')
    matrix = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    assert matrix['people'][0]['cells'][PAID.isoformat()]['editable'] is False


def test_aggregate_salary_on_payout_day_no_longer_locks_but_is_reported(stores):
    finance, _, person = stores
    with closing(finance._open()) as connection, connection:
        connection.execute('INSERT INTO accountant_movements '
                           '(day,kind,description,amount,item_code,reference,created_at) VALUES (?,?,?,?,?,?,?)',
                           (PAID.isoformat(), 'other_expense', 'Персонал общая сумма', '10000',
                            'salary_staff', 'aggregate-today', '2026-10-07'))
    matrix = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    assert matrix['people'][0]['cells'][PAID.isoformat()]['editable'] is True
    assert matrix['aggregate_days'] == [dict(day=PAID.isoformat(), amount='10000')]
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    assert len(rows(finance, 'accountant_salary_payments')) == 1


def test_employee_added_after_the_shift_takes_the_current_rate(stores):
    finance, roster, _ = stores
    from retro.modules.accountant import roster as roster_module
    roster_module_today = roster_module.today_tashkent
    try:
        roster_module.today_tashkent = lambda: date(2026, 10, 9)
        late = roster.add(name='Новенький', role='официант', rate='170000', group_name='Обслуживание зала')
    finally:
        roster_module.today_tashkent = roster_module_today
    data = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    cells = next(p for p in data['people'] if p['id'] == late.id)['cells']
    assert cells['2026-10-04']['editable'] is True and cells['2026-10-04']['rate'] == '170000'
    finance.set_salary_day_cell(date(2026, 10, 4), late.id, '170000', '0')
    assert rows(finance, 'accountant_accruals')[-1][3] == 'Новенький'


def test_named_payment_blocks_aggregate_salary_creation_and_recategorization(stores):
    finance, _, person = stores
    ordinary = finance.add_expense(PAID, 'ops_rent', 'Аренда', '1000')
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    before = rows(finance, 'accountant_movements')
    for code in ('salary_cashier', 'salary_staff', 'salary_technical', 'salary_carryover'):
        with pytest.raises(LedgerError, match='по сотрудникам'):
            finance.add_expense(PAID, code, 'Повтор зарплаты', '250000')
        with pytest.raises(LedgerError, match='по сотрудникам'):
            finance.update_movement(ordinary, PAID, code, 'Повтор зарплаты', '250000')
    assert rows(finance, 'accountant_movements') == before


def test_concurrent_cell_writers_cannot_overwrite_same_expected_amount(stores):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    finance, _, person = stores
    barrier = Barrier(2)

    def save(amount):
        barrier.wait(timeout=5)
        try:
            return finance.set_salary_day_cell(PAID, person.id, amount, '0')['amount']
        except SalaryCellChanged:
            return 'conflict'

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ['100000', '200000']))
    assert results.count('conflict') == 1
    assert len(rows(finance, 'accountant_salary_payments')) == 1
    assert len(rows(finance, 'accountant_accruals')) == 1


def test_first_of_next_month_pays_last_shift_of_previous_month(stores, monkeypatch):
    finance, _, person = stores
    monkeypatch.setattr(salary_day, 'today_tashkent', lambda: date(2026, 11, 2))
    for offset in range(22):
        finance.record_handover(date(2026, 10, 11)+timedelta(days=offset), Decimal(0))
    finance.set_salary_day_cell(date(2026, 11, 1), person.id, '150000', '0')
    assert rows(finance, 'accountant_accruals')[0][1] == '2026-10-31'
    matrix = finance.salary_day_month(date(2026, 11, 1), date(2026, 11, 30))
    assert matrix['people'][0]['cells']['2026-11-01'] == dict(
        amount='150000', work_day='2026-10-31', editable=True, rate=None)


def test_generic_salary_mutations_cannot_break_manual_pair(stores):
    finance, _, person = stores
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    payment = rows(finance, 'accountant_salary_payments')[0]
    for action in [lambda: finance.delete_operation('salary_payment', payment[0], PAID),
                   lambda: finance.update_salary_payment(payment[0], PAID, '200000'),
                   lambda: finance.pay_salary(payment[1], PAID, '1000')]:
        with pytest.raises(LedgerError, match='ячейке'):
            action()
    assert rows(finance, 'accountant_salary_payments')[0][3] == '250000'


def test_legacy_confirmation_does_not_lock_the_manual_cell(stores):
    from retro.modules.accountant.payroll import PayrollRow

    finance, _, person = stores
    row = PayrollRow(person.id, person.name, person.role, person.group_name,
                     'manual_present', None, Decimal('100000'), Decimal('100000'), False)
    finance.set_salary_day_cell(PAID, person.id, '100000', '0')
    with pytest.raises(LedgerError, match='ручная зарплата'):
        finance.confirm_payroll(WORK, [row], 'Бухгалтер')
    assert rows(finance, 'accountant_payroll_days') == []
    finance.set_salary_day_cell(PAID, person.id, '0', '100000')
    assert rows(finance, 'accountant_accruals') == []
    finance.confirm_payroll(WORK, [row], 'Бухгалтер')
    # Смена подтверждена в прежней ведомости — клетка всё равно работает и забирает начисление.
    matrix = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    assert matrix['people'][0]['cells'][PAID.isoformat()]['editable'] is True
    finance.set_salary_day_cell(PAID, person.id, '90000', '0')
    assert [(a[5], a[7]) for a in rows(finance, 'accountant_accruals')] == [(salary_day.MANUAL_STATUS, '90000')]


def test_before_start_future_archived_and_both_month_closed_dates_are_readonly(stores, monkeypatch):
    finance, roster, person = stores
    for invalid in [date(2026, 10, 1), date(2026, 10, 11)]:
        with pytest.raises(LedgerError):
            finance.set_salary_day_cell(invalid, person.id, '1000', '0')
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    roster.delete(person.id)
    matrix = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    assert matrix['people'][0]['archived'] is True
    assert matrix['people'][0]['cells'][PAID.isoformat()]['amount'] == '250000'
    assert matrix['people'][0]['cells'][PAID.isoformat()]['editable'] is False
    with pytest.raises(LedgerError, match='архиве'):
        finance.set_salary_day_cell(PAID, person.id, '0', '250000')
    active = roster.add(name='Второй', role='официант', rate=None, group_name='Обслуживание зала')
    with closing(finance._open()) as connection, connection:
        connection.execute('INSERT INTO accountant_month_closures '
                           '(month,last_day,closing_balance,closed_at,closed_by,snapshot) VALUES (?,?,?,?,?,?)',
                           ('2026-10', '2026-10-31', '750000', '2026-11-01', 'Бухгалтер', '{}'))
    monkeypatch.setattr(salary_day, 'today_tashkent', lambda: date(2026, 11, 2))
    for invalid in [PAID, date(2026, 11, 1)]:
        with pytest.raises(LedgerError, match='Месяц закрыт'):
            finance.set_salary_day_cell(invalid, active.id, '1000', '0')
    november = finance.salary_day_month(date(2026, 11, 1), date(2026, 11, 30))
    assert november['people'][0]['cells']['2026-11-01']['editable'] is False


def test_matrix_paid_day_alignment_missing_cells_and_submitted_reports(stores):
    finance, _, person = stores
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    with closing(finance._open()) as connection, connection:
        connection.execute('INSERT INTO accountant_day_reports VALUES (?,?,?,?)',
                           (PAID.isoformat(), '2026-10-07', 'Бухгалтер', '{}'))
    data = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))
    assert data['entry_start'] == '2026-10-02'
    cells = data['people'][0]['cells']
    assert len(cells) == 31
    assert cells['2026-10-01'] == dict(amount='0', work_day='2026-09-30', editable=False, rate=None)
    assert cells['2026-10-07'] == dict(amount='250000', work_day='2026-10-06', editable=True, rate=None)
    assert cells['2026-10-08'] == dict(amount='0', work_day='2026-10-07', editable=True, rate=None)
    assert cells['2026-10-11']['editable'] is False
    finance.set_salary_day_cell(PAID, person.id, '200000', '250000')


def test_route_reductions_do_not_request_iiko_and_increases_require_handover(stores, monkeypatch):
    finance, _, person = stores
    finance.set_salary_day_cell(PAID, person.id, '250000', '0')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(accountant_finance=finance)))
    calls = []

    async def handover(_request, day):
        calls.append(day)
        raise HTTPException(409, 'Нет данных кассира')

    # Avoid threads in this test; the actual store transaction remains intact.
    async def to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(routes, 'required_handover', handover)
    monkeypatch.setattr(routes.asyncio, 'to_thread', to_thread)
    body = routes.SalaryDayCellInput(date=PAID, employee_id=person.id,
                                     amount='200000', expected_amount='250000')
    assert asyncio.run(routes.salary_day_cell(request, body))['amount'] == '200000'
    assert calls == []
    body = routes.SalaryDayCellInput(date=PAID, employee_id=person.id,
                                     amount='300000', expected_amount='200000')
    with pytest.raises(HTTPException) as failure:
        asyncio.run(routes.salary_day_cell(request, body))
    assert failure.value.status_code == 409
    assert calls == [PAID]


def test_cell_rate_follows_the_shift_day_not_today(stores, monkeypatch):
    """Галочка «по ставке» за прошлый день платит ставку того дня, а не нынешнюю."""
    finance, roster, person = stores
    from retro.modules.accountant import roster as roster_module
    monkeypatch.setattr(roster_module, 'today_tashkent', lambda: date(2026, 10, 7))
    roster.update(person.id, rate='200000', reason='Ставка')
    monkeypatch.setattr(roster_module, 'today_tashkent', lambda: date(2026, 10, 8))
    roster.update(person.id, rate='250000', reason='Повышение')
    cells = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))['people'][0]['cells']
    assert cells['2026-10-08']['rate'] == '200000'
    assert cells['2026-10-09']['rate'] == '250000'


def test_past_days_since_accounting_start_are_editable_like_month_sheet(stores):
    """Прошедшие дни месяца отмечаются, как в «Зарплате · месяц»: с 02.10."""
    finance, _, person = stores
    result = finance.set_salary_day_cell(date(2026, 10, 3), person.id, '180000', '0')
    assert result['work_day'] == '2026-10-02'
    cells = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31))['people'][0]['cells']
    assert cells['2026-10-03']['amount'] == '180000' and cells['2026-10-03']['editable'] is True
    assert cells['2026-10-05']['editable'] is True
    assert cells['2026-10-01']['editable'] is False


def test_late_shift_marks_the_payout_cell_with_entry_time(stores):
    from datetime import datetime
    from types import SimpleNamespace as Entry
    from retro.modules.cashier.service import TZ
    finance, _, person = stores
    entries = {date(2026, 10, 5): {person.id: Entry(occurred_at=datetime(2026, 10, 5, 10, 25, tzinfo=TZ))},
               date(2026, 10, 6): {person.id: Entry(occurred_at=datetime(2026, 10, 6, 9, 55, tzinfo=TZ))},
               date(2026, 10, 10): {person.id: Entry(occurred_at=datetime(2026, 10, 10, 11, 0, tzinfo=TZ))}}
    cells = finance.salary_day_month(date(2026, 10, 1), date(2026, 10, 31),
                                     lambda day: entries.get(day, {}))['people'][0]['cells']
    assert cells['2026-10-06']['late'] == '10:25'      # смена 05.10 — вход после 10:00
    assert 'late' not in cells['2026-10-07']           # смена 06.10 — вовремя
    assert 'late' not in cells['2026-10-11']           # смена 10.10 — сегодня, ещё не смотрим
