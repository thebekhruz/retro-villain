from datetime import date
from decimal import Decimal

from retro.modules.accountant.payroll import compute_pay, demo_attendance, draft_payroll
from retro.modules.accountant.roster import Employee


def person(source_row, rate='250000'):
    return Employee(source_row, source_row, f'Сотрудник {source_row}', 'повар', 'Кухня',
                    Decimal(rate) if rate is not None else None, None)


def test_late_still_earns_full_daily_rate_and_missing_earns_zero():
    assert compute_pay(Decimal('270000'), 'late', exception=False) == Decimal('270000')
    assert compute_pay(Decimal('270000'), 'missing', exception=False) == Decimal('0')
    assert compute_pay(None, 'on_time', exception=False) is None
    assert compute_pay(Decimal('270000'), 'unlinked', exception=False) is None
    assert compute_pay(Decimal('270000'), 'unlinked', exception=True) == Decimal('270000')
    assert compute_pay(Decimal('270000'), 'unavailable', exception=False) is None


def test_demo_status_is_stable_and_unlinked_is_visibly_separate():
    day = date(2026, 9, 16)
    employees = [person(row) for row in range(5, 79)]
    first = demo_attendance(day, employees)
    assert first == demo_attendance(day, employees)
    assert {'on_time', 'late', 'missing', 'unlinked'} <= {item.status for item in first}
    assert all(item.occurred_at is None for item in first if item.status in ('missing', 'unlinked'))


def test_daily_draft_marks_unknown_rate_instead_of_zero():
    day = date(2026, 9, 16)
    employees = [person(19), person(20, None)]
    rows = draft_payroll(day, employees, exceptions={19})
    assert rows[0].status == 'unlinked'
    assert rows[0].payable == Decimal('250000')
    assert rows[1].payable is None
