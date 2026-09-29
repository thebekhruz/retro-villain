"""Explicitly simulated attendance and daily payroll for the local finance preview."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from retro.modules.cashier.service import TZ

from .roster import Employee


@dataclass(frozen=True)
class AttendanceRow:
    employee_id: int
    status: str
    occurred_at: datetime | None


@dataclass(frozen=True)
class PayrollRow:
    employee_id: int
    name: str
    role: str
    group_name: str
    status: str
    occurred_at: datetime | None
    rate: Decimal | None
    payable: Decimal | None
    exception: bool

    def json(self):
        return dict(employee_id=self.employee_id, name=self.name, role=self.role,
                    group=self.group_name, status=self.status,
                    first_entry=self.occurred_at.isoformat() if self.occurred_at else None,
                    rate=str(self.rate) if self.rate is not None else None,
                    payable=str(self.payable) if self.payable is not None else None,
                    exception=self.exception, demo=False)


# «Пришли» и «не пришли» везде считаются одинаково — как на странице
# сотрудников: ручная отметка бухгалтера наравне с проходом Hikvision.
# unlinked и unavailable — ни то ни другое: данных о дне нет.
PRESENT_STATUSES = frozenset({'on_time', 'late', 'manual_present'})
ABSENT_STATUSES = frozenset({'missing', 'manual_absent'})


def blocker_reason(row: 'PayrollRow') -> str | None:
    """Почему начисление сотруднику за день нельзя подтвердить (None — можно).

    missing_rate — нет дневной ставки; unlinked — нет привязки Hikvision и
    ручной отметки; unavailable — данные Hikvision за день неполные. Смену
    подтверждают по людям: такие строки ждут, остальные начисляются.
    """
    if row.payable is not None and row.rate is not None:
        return None
    if row.rate is None:
        return 'missing_rate'
    if row.status in ('unlinked', 'unavailable'):
        return row.status
    return 'unknown'


def compute_pay(rate: Decimal | None, status: str, *, exception: bool,
                pay_unlinked: bool = False) -> Decimal | None:
    """Сколько начислить за день. None — начислить нельзя, данных о дне нет.

    `pay_unlinked` — временный режим проверки (ACCOUNTANT_CHECK_MODE): человек
    без привязки к Hikvision считается пришедшим, как будто на него заведено
    исключение. Вне режима такая строка ждёт исключения или ручной отметки:
    иначе зарплата шла бы людям, чей день никто не подтверждал.
    """
    if rate is None:
        return None
    if status in ('missing', 'manual_absent'):
        return Decimal(0)
    if status in ('on_time', 'late', 'manual_present'):
        return rate
    if status == 'unlinked' and (exception or pay_unlinked):
        return rate
    if status == 'unavailable':
        return None
    return None


def demo_attendance(day: date, employees: list[Employee]) -> list[AttendanceRow]:
    """Reproducible examples, never a claim about actual device registration."""
    result = []
    for employee in employees:
        seed = day.toordinal() + employee.source_row
        if employee.source_row % 19 == 0:
            status, occurred_at = 'unlinked', None
        elif seed % 17 == 0:
            status, occurred_at = 'missing', None
        elif seed % 7 == 0:
            status = 'late'
            occurred_at = datetime.combine(day, time(10, 1), TZ) + timedelta(minutes=seed % 38)
        else:
            status = 'on_time'
            occurred_at = datetime.combine(day, time(9, 0), TZ) + timedelta(minutes=seed % 61)
        result.append(AttendanceRow(employee.id, status, occurred_at))
    return result


def draft_payroll(day: date, employees: list[Employee], exceptions: set[int],
                  attendance: list[AttendanceRow] | tuple[AttendanceRow, ...] | None = None,
                  *, pay_unlinked: bool = False) -> list[PayrollRow]:
    attendance = demo_attendance(day, employees) if attendance is None else attendance
    by_employee = {entry.employee_id: entry for entry in attendance}
    return [PayrollRow(employee.id, employee.name, employee.role, employee.group_name,
                       by_employee[employee.id].status, by_employee[employee.id].occurred_at, employee.rate,
                       compute_pay(employee.rate, by_employee[employee.id].status,
                                   exception=employee.id in exceptions, pay_unlinked=pay_unlinked),
                       employee.id in exceptions)
            for employee in employees]
