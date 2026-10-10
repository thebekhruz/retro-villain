"""T-429: посещаемость смены в клетке «Зарплаты · день» (ТЗ 09.10, Б-04).

До выплаты клетка показывает то же, что «Сотрудники» за день смены: время
входа вовремя, опоздание, «не пришёл», «нет данных» и ручную отметку. Выплата
посещаемость не меняет: после снятия отметки в клетке снова исходный статус.
Контрольный пример ТЗ — Ихтиер, Жахонгир и Сельвина. На SQLite и на Postgres
(RETRO_TEST_POSTGRES_URL).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from test_accountant_design_parity import any_db, manual_person  # noqa: F401 — any_db — фикстура

from retro.integrations.hikvision import HikvisionEvent
from retro.modules.accountant import routes, salary_day
from retro.modules.accountant.hikvision import AttendanceService, AttendanceStore
from retro.modules.accountant.roster import Employee
from retro.modules.cashier.service import TZ

TODAY = date(2026, 10, 9)
SOURCE = 'retro-main-entry'
IHTIYOR, JAHONGIR, SELVINA = 'Баходиров Ихтиер', 'Каримов Жахонгир', 'Абдулганиева Сельвина'
UNLINKED, GUARD = 'Без привязки', 'Охрана вручную'


@pytest.fixture
def stand(any_db, monkeypatch):
    c = any_db
    monkeypatch.setattr(salary_day, 'today_tashkent', lambda: TODAY)
    monkeypatch.setattr(routes, 'today_tashkent', lambda: TODAY)
    roster, store = c.app.state.accountant_roster, c.app.state.attendance_store
    people = {}
    for name, role, group, number in ((IHTIYOR, 'Менеджер', 'Управление', '101'),
                                      (JAHONGIR, 'Менеджер', 'Управление', '102'),
                                      (SELVINA, 'Хостес', 'Встреча гостей', '103'),
                                      (UNLINKED, 'официант', 'Обслуживание зала', None)):
        person = roster.add(name=name, role=role, rate='360000', group_name=group)
        if number:
            roster.set_hikvision_id(person.id, number)
        people[name] = person.id
    people[GUARD] = manual_person(c, GUARD, 'охрана', '200000', 'Охрана').id

    def enter(name, number, at):
        store.ingest(HikvisionEvent(SOURCE, f'{number}-{at:%m%d%H%M}', number, at), people[name])

    enter(IHTIYOR, '101', datetime(2026, 10, 7, 9, 38, tzinfo=TZ))
    enter(IHTIYOR, '101', datetime(2026, 10, 8, 12, 0, tzinfo=TZ))   # не первый вход дня
    enter(IHTIYOR, '101', datetime(2026, 10, 8, 9, 31, tzinfo=TZ))
    enter(JAHONGIR, '102', datetime(2026, 10, 8, 10, 58, tzinfo=TZ))
    # Выгрузка Hikvision накрыла дни до 09.10 целиком: «нет прохода» = «не пришёл».
    store.record_success(SOURCE, at=datetime(2026, 10, 9, 0, 30, tzinfo=TZ),
                         cursor_at=datetime(2026, 10, 9, 0, 30, tzinfo=TZ),
                         covered_from=datetime(2026, 10, 1, tzinfo=TZ),
                         covered_through=datetime(2026, 10, 9, 0, 30, tzinfo=TZ))
    finance = c.app.state.accountant_finance
    for offset in range(8):
        finance.record_handover(date(2026, 10, 5) + timedelta(days=offset), Decimal(0))
    finance.set_cash_opening(date(2026, 10, 5), '5000000', 'Стенд')
    return c, people


def month_cells(c, people):
    response = c.get('/api/accountant/salary-day/month', params={'month': '2026-10'})
    assert response.status_code == 200, response.text
    by_id = {person['id']: person['cells'] for person in response.json()['people']}
    return {name: by_id[employee_id] for name, employee_id in people.items()}


def test_payout_cell_shows_the_shift_attendance_of_the_employees_page(stand):
    c, people = stand
    cells = month_cells(c, people)
    # Столбец выплаты 09.10 — смена 08.10, столбец 08.10 — смена 07.10.
    assert cells[IHTIYOR]['2026-10-09']['attendance'] == dict(status='on_time', time='09:31', source='on_time')
    assert cells[IHTIYOR]['2026-10-08']['attendance'] == dict(status='on_time', time='09:38', source='on_time')
    assert cells[JAHONGIR]['2026-10-09']['attendance'] == dict(status='late', time='10:58', source='late')
    for paid_day in ('2026-10-09', '2026-10-08'):
        assert cells[SELVINA][paid_day]['attendance'] == dict(status='absent', time=None, source='missing')
        # Без привязки к Hikvision человек не отсутствующий — данных о нём нет.
        assert cells[UNLINKED][paid_day]['attendance'] == dict(status='unknown', time=None, source='unlinked')
        # Ручное присутствие — без придуманного времени входа.
        assert cells[GUARD][paid_day]['attendance'] == dict(status='manual', time=None, source='manual_present')
    # Смена 09.10 идёт сегодня — её не размечаем; суммы клеток те же, что и были.
    assert all('attendance' not in person['2026-10-10'] for person in cells.values())
    assert cells[IHTIYOR]['2026-10-09']['amount'] == '0'
    # Источник истины — «Сотрудники» за день смены: тот же статус у каждого.
    for shift in (date(2026, 10, 7), date(2026, 10, 8)):
        staff = c.get('/api/accountant/staff', params={'date': shift.isoformat()}).json()['employees']
        paid_day = (shift + timedelta(days=1)).isoformat()
        for row in staff:
            name = next(name for name, employee_id in people.items() if employee_id == row['employee_id'])
            cell = cells[name][paid_day]['attendance']
            assert (cell['source'], cell['status']) == (row['status'], salary_day.CELL_ATTENDANCE[row['status']])


def test_payment_does_not_change_attendance_and_clearing_returns_the_status(stand):
    c, people = stand
    finance = c.app.state.accountant_finance
    paid_day = date(2026, 10, 9)
    finance.set_salary_day_cell(paid_day, people[IHTIYOR], '360000', '0')
    finance.set_salary_day_cell(paid_day, people[SELVINA], '360000', '0')
    cells = month_cells(c, people)
    assert cells[IHTIYOR]['2026-10-09']['amount'] == '360000'
    assert cells[IHTIYOR]['2026-10-09']['attendance'] == dict(status='on_time', time='09:31', source='on_time')
    # Выдача не превращает «не пришёл» в «был»: в подсказке после выплаты — правда о смене.
    assert cells[SELVINA]['2026-10-09']['attendance'] == dict(status='absent', time=None, source='missing')
    finance.set_salary_day_cell(paid_day, people[IHTIYOR], '0', '360000')
    finance.set_salary_day_cell(paid_day, people[SELVINA], '0', '360000')
    cells = month_cells(c, people)
    assert cells[IHTIYOR]['2026-10-09']['amount'] == '0'
    assert cells[IHTIYOR]['2026-10-09']['attendance']['time'] == '09:31'
    assert cells[SELVINA]['2026-10-09']['attendance']['status'] == 'absent'


def test_absent_only_after_a_complete_day_or_a_manual_mark(tmp_path):
    store = AttendanceStore(tmp_path / 'attendance.sqlite3')
    service = AttendanceService(store, source='entry', enabled=True, poll_seconds=30)
    linked = Employee(1, 1, 'Привязан', 'хостес', 'Встреча гостей', Decimal(1), '7')
    unlinked = Employee(2, 2, 'Без привязки', 'официант', 'Обслуживание зала', Decimal(1), None)
    manual = Employee(3, 3, 'Вручную', 'охрана', 'Охрана', Decimal(1), None, manual_attendance=True)
    staff = [linked, unlinked, manual]
    # Выгрузка накрыла только 08.10: 07.10 устройство «молчит» — это не прогул.
    store.record_success('entry', at=datetime(2026, 10, 9, 1, tzinfo=TZ),
                         cursor_at=datetime(2026, 10, 9, 1, tzinfo=TZ),
                         covered_from=datetime(2026, 10, 8, tzinfo=TZ),
                         covered_through=datetime(2026, 10, 9, tzinfo=TZ))
    store.set_manual_mark(linked.id, date(2026, 10, 6), False, 'Бухгалтер')
    store.set_manual_mark(linked.id, date(2026, 10, 5), True, 'Бухгалтер')
    store.set_manual_mark(manual.id, date(2026, 10, 8), False, 'Бухгалтер')
    days = [date(2026, 10, 5) + timedelta(days=offset) for offset in range(4)]
    now = datetime(2026, 10, 9, 12, tzinfo=TZ)
    calls = []
    for method in ('first_entries_between', 'manual_marks_between'):
        original = getattr(store, method)
        setattr(store, method, lambda first, last, original=original, method=method:
                calls.append(method) or original(first, last))
    rows = service.rows_by_day(days, lambda day: staff, now=now)
    # Входы и отметки — одним запросом на весь период, не по дню и не по клетке.
    assert sorted(calls) == ['first_entries_between', 'manual_marks_between']
    status = {day: {employee_id: row.status for employee_id, row in rows[day].items()} for day in days}
    assert status[date(2026, 10, 5)] == {1: 'manual_present', 2: 'unlinked', 3: 'manual_present'}
    assert status[date(2026, 10, 6)] == {1: 'manual_absent', 2: 'unlinked', 3: 'manual_present'}
    assert status[date(2026, 10, 7)] == {1: 'unavailable', 2: 'unlinked', 3: 'manual_present'}
    assert status[date(2026, 10, 8)] == {1: 'missing', 2: 'unlinked', 3: 'manual_absent'}
    # Тот же расчёт, что у «Сотрудников» за день (без отметки выплаты).
    for day in days:
        assert {row.employee_id: row for row in service.snapshot(day, staff, now=now).rows} == rows[day]
    assert service.rows_by_day([], lambda day: staff, now=now) == {}
